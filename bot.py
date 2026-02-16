#!/usr/bin/env python3
"""
Rolimons Trade Ad Bot
─────────────────────
Automatically posts trade ads on Rolimons based on items you pick from your inventory.

Usage:
  python bot.py              → start bot (loop, posts every 15 min)
  python bot.py --once       → post once and exit
  python bot.py --dry-run    → preview only (nothing is posted)
  python bot.py --setup      → re-run the setup wizard

Requirements: pip install httpx pyyaml questionary rich
"""

import argparse
import asyncio
import logging
import sys
from collections import Counter
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path

import httpx
import questionary
from questionary import Choice
import yaml

from rich import box
from rich.console import Console
from rich.logging import RichHandler
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# ──────────────────────────────────────────────────────────
# CONSTANTS
# ──────────────────────────────────────────────────────────

VERSION = "1.0.0"

BASE_DIR    = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.yaml"
LOG_FILE    = BASE_DIR / "logs" / "bot.log"

ROBLOX_COLLECTIBLES_URL = "https://inventory.roblox.com/v1/users/{user_id}/assets/collectibles"
ROLIMONS_ITEMS_URL      = "https://www.rolimons.com/itemapi/itemdetails"
ROLIMONS_CREATE_AD_URL  = "https://api.rolimons.com/tradeads/v1/createad"

VALID_TAGS = {"any", "demand", "rares", "robux", "upgrade", "downgrade",
              "rap", "wishlist", "projecteds", "adds"}

console = Console(highlight=False)


# ──────────────────────────────────────────────────────────
# CUSTOM EXCEPTIONS
# ──────────────────────────────────────────────────────────

class BotError(Exception):
    pass

class CooldownError(BotError):
    pass

class AuthError(BotError):
    pass

class RateLimitError(BotError):
    pass


# ──────────────────────────────────────────────────────────
# LOGGING
# ──────────────────────────────────────────────────────────

def _setup_logging(level: str = "INFO") -> logging.Logger:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("trade_bot")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.handlers.clear()

    # Colored console output via rich
    ch = RichHandler(
        console=console,
        show_path=False,
        show_time=True,
        rich_tracebacks=True,
        markup=True,
        log_time_format="%H:%M:%S",
    )
    ch.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.addHandler(ch)

    # Plain file output
    fh = RotatingFileHandler(LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=3)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    fh.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.addHandler(fh)

    return logger


log = _setup_logging()


# ──────────────────────────────────────────────────────────
# HTTP HELPERS
# ──────────────────────────────────────────────────────────

def _build_headers(roli_cookie: str = None, is_post: bool = False) -> dict:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/118.0.0.0 Safari/537.36"
        ),
        "Content-Type": "application/json",
        "Connection": "keep-alive",
    }
    if is_post:
        headers.update({
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Origin": "https://www.rolimons.com",
            "Referer": "https://www.rolimons.com/tradeads",
        })
    if roli_cookie:
        headers["Cookie"] = f"_RoliVerification={roli_cookie}"
    return headers


# ──────────────────────────────────────────────────────────
# API CALLS
# ──────────────────────────────────────────────────────────

async def fetch_roblox_inventory(user_id: int) -> list[dict]:
    """Fetch all collectibles from Roblox (handles pagination)."""
    base_url = ROBLOX_COLLECTIBLES_URL.format(user_id=user_id)
    raw_items = []
    cursor = None

    async with httpx.AsyncClient(timeout=30.0) as client:
        while True:
            url = base_url if not cursor else f"{base_url}?cursor={cursor}"
            resp = await client.get(url, headers=_build_headers())

            if resp.status_code == 403:
                raise BotError(
                    "Inventory is private.\n"
                    "Go to Roblox Settings → Privacy → set inventory to 'Everyone'."
                )
            resp.raise_for_status()

            data = resp.json()
            raw_items.extend(data.get("data", []))
            cursor = data.get("nextPageCursor")
            if not cursor:
                break

    result = []
    for item in raw_items:
        asset_id = item.get("assetId")
        if not asset_id:
            continue
        result.append({
            "id":         asset_id,
            "name":       item.get("name", "Unknown"),
            "rap":        item.get("recentAveragePrice", 0),
            "is_on_hold": item.get("isOnHold", False),
        })
    return result


async def fetch_rolimons_items() -> dict[int, dict]:
    """Fetch all item details from Rolimons."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(ROLIMONS_ITEMS_URL, headers=_build_headers())
        if resp.status_code == 429:
            raise RateLimitError("Rolimons rate limit hit — try again in a minute")
        resp.raise_for_status()
        data = resp.json()
        if not data.get("success"):
            raise BotError("Rolimons API returned an error")

    items: dict[int, dict] = {}
    for id_str, codes in data.get("items", {}).items():
        item_id = int(id_str)
        items[item_id] = {
            "name":      codes[0] if len(codes) > 0 else "Unknown",
            "rap":       codes[2] if len(codes) > 2 else -1,
            "value":     codes[3] if len(codes) > 3 else -1,
            "projected": codes[7] == 1 if len(codes) > 7 else False,
            "rare":      codes[9] == 1 if len(codes) > 9 else False,
        }
    return items


async def post_trade_ad(
    user_id: int,
    offer_ids: list[int],
    request_tags: list[str],
    request_item_ids: list[int],
    roli_cookie: str,
) -> None:
    """Post a trade ad to Rolimons."""
    payload = {
        "player_id":        user_id,
        "offer_item_ids":   offer_ids,
        "request_item_ids": request_item_ids,
        "request_tags":     request_tags,
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            ROLIMONS_CREATE_AD_URL,
            headers=_build_headers(roli_cookie=roli_cookie, is_post=True),
            json=payload,
        )

    if resp.status_code == 201:
        return
    elif resp.status_code == 400:
        raise CooldownError("Cooldown not expired — wait 15 minutes between posts")
    elif resp.status_code == 422:
        raise AuthError("Invalid or expired _RoliVerification cookie")
    elif resp.status_code == 429:
        raise RateLimitError("Daily limit reached (max 60 ads per 24 hours)")
    else:
        raise BotError(f"Unexpected response: HTTP {resp.status_code}")


# ──────────────────────────────────────────────────────────
# CONFIG HANDLING
# ──────────────────────────────────────────────────────────

def load_config() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    with open(CONFIG_FILE, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def save_config(cfg: dict) -> None:
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    console.print(f"\n  [green]✓[/] Saved → [dim]{CONFIG_FILE}[/]")


def validate_config(cfg: dict) -> None:
    if not cfg.get("user_id"):
        raise ValueError("Missing 'user_id' in config.yaml")
    if not cfg.get("roli_verification"):
        raise ValueError("Missing 'roli_verification' in config.yaml")
    if not cfg.get("offer_item_ids"):
        raise ValueError("No items selected — run  python bot.py --setup  to pick your items")
    if not cfg.get("trade_ad", {}).get("request_tags"):
        raise ValueError("Missing 'request_tags' in trade_ad section")


# ──────────────────────────────────────────────────────────
# SETUP WIZARD HELPERS
# ──────────────────────────────────────────────────────────

def _ask(prompt: str, default: str = "", required: bool = True) -> str:
    hint = f" [{default}]" if default else ""
    while True:
        val = input(f"  {prompt}{hint}: ").strip()
        if not val and default:
            return default
        if val or not required:
            return val
        print("  (required — please enter a value)")


def _ask_int(prompt: str, default: int) -> int:
    while True:
        val = input(f"  {prompt} [{default}]: ").strip()
        if not val:
            return default
        if val.lstrip("-").isdigit():
            return int(val)
        print("  Please enter a whole number.")


def _ask_yn(prompt: str, default: bool = True) -> bool:
    hint = "[Y/n]" if default else "[y/N]"
    val = input(f"  {prompt} {hint}: ").strip().lower()
    if not val:
        return default
    return val.startswith("y")


# ──────────────────────────────────────────────────────────
# INVENTORY HELPERS
# ──────────────────────────────────────────────────────────

def _build_inventory_list(
    roblox_items: list[dict],
    rolimons_items: dict[int, dict],
) -> tuple[list[dict], list[dict]]:
    """
    Returns (tradeable, on_hold) — both sorted by RAP descending.
    """
    tradeable: list[dict] = []
    on_hold:   list[dict] = []

    for item in roblox_items:
        item_id         = item["id"]
        roli            = rolimons_items.get(item_id, {})
        rap             = item["rap"]
        value           = roli.get("value", -1)
        effective_value = value if value > 0 else rap

        entry = {
            "id":        item_id,
            "name":      roli.get("name") or item["name"],
            "rap":       rap,
            "value":     effective_value,
            "projected": roli.get("projected", False),
            "rare":      roli.get("rare", False),
            "new":       item_id not in rolimons_items,
        }

        if item["is_on_hold"]:
            on_hold.append(entry)
        else:
            tradeable.append(entry)

    tradeable.sort(key=lambda x: x["rap"], reverse=True)
    on_hold.sort(key=lambda x: x["rap"], reverse=True)
    return tradeable, on_hold


def _item_flags(item: dict) -> str:
    flags = ""
    if item["projected"]: flags += " 📈"
    if item["rare"]:      flags += " 💎"
    if item["new"]:       flags += " 🆕"
    return flags


def _item_label(item: dict, display_name: str) -> str:
    return (
        f"{display_name:<42}"
        f"  RAP: {item['rap']:>8,}"
        f"  Value: {item['value']:>8,}"
        f"{_item_flags(item)}"
    )


def _build_checkbox_choices(
    tradeable: list[dict],
    on_hold: list[dict],
    preselected_ids: list[int],
) -> list:
    all_items   = tradeable + on_hold
    id_counts   = Counter(item["id"] for item in all_items)
    id_seen:    dict[int, int] = {}
    preselected = list(preselected_ids)
    choices:    list = []

    for idx, item in enumerate(tradeable):
        item_id = item["id"]
        if id_counts[item_id] > 1:
            id_seen[item_id] = id_seen.get(item_id, 0) + 1
            display_name = f"{item['name']} [{id_seen[item_id]}/{id_counts[item_id]}]"
        else:
            display_name = item["name"]

        pre = False
        if item_id in preselected:
            preselected.remove(item_id)
            pre = True

        choices.append(Choice(
            title=_item_label(item, display_name),
            value=(idx, item_id),
            checked=pre,
        ))

    if on_hold:
        choices.append(questionary.Separator("─── On Hold  (cannot be traded right now) ───"))
        for item in on_hold:
            item_id = item["id"]
            if id_counts[item_id] > 1:
                id_seen[item_id] = id_seen.get(item_id, 0) + 1
                display_name = f"{item['name']} [{id_seen[item_id]}/{id_counts[item_id]}]"
            else:
                display_name = item["name"]

            choices.append(Choice(
                title=_item_label(item, display_name),
                value=None,
                disabled="on hold",
            ))

    return choices


# ──────────────────────────────────────────────────────────
# SETUP WIZARD
# ──────────────────────────────────────────────────────────

async def run_setup(existing: dict) -> dict:
    """Interactive setup wizard."""

    console.print(Panel(
        "[bold cyan]Rolimons Trade Ad Bot[/]  [dim]— Setup Wizard[/]",
        border_style="cyan",
        padding=(0, 2),
    ))
    console.print()
    console.print("  This wizard creates your [bold]config.yaml[/].")
    console.print("  Press [dim]Enter[/] to keep the value shown in [dim][brackets][/].")
    console.print()

    cfg: dict = {}

    # ── Step 1: Credentials ──────────────────────────────
    console.print("[bold cyan]  Step 1 of 3[/]  [dim]·  Basic Settings[/]")
    console.print()

    user_id = _ask_int("Your Roblox User ID", existing.get("user_id") or 0)
    if not user_id:
        console.print("  [red]User ID is required. Aborting.[/]")
        sys.exit(1)
    cfg["user_id"] = user_id

    console.print()
    console.print("  [dim]How to get your _RoliVerification cookie:[/]")
    console.print("  [dim]  1. Log in to  https://www.rolimons.com[/]")
    console.print("  [dim]  2. Press F12  →  Application tab  →  Cookies[/]")
    console.print("  [dim]  3. Find '_RoliVerification' and copy its Value[/]")
    console.print()

    roli = _ask("_RoliVerification cookie", default=existing.get("roli_verification", ""))
    cfg["roli_verification"] = roli

    # ── Step 2: Pick items ───────────────────────────────
    console.print()
    console.print("[bold cyan]  Step 2 of 3[/]  [dim]·  Pick items to offer[/]")
    console.print()

    tradeable: list[dict] = []
    on_hold:   list[dict] = []
    try:
        with console.status("[dim]Connecting to Roblox...[/]", spinner="dots"):
            roblox_items = await fetch_roblox_inventory(user_id)
        with console.status("[dim]Fetching Rolimons values...[/]", spinner="dots"):
            rolimons_items = await fetch_rolimons_items()
        tradeable, on_hold = _build_inventory_list(roblox_items, rolimons_items)

        parts = [f"[green]{len(tradeable)} tradeable[/]"]
        if on_hold:
            parts.append(f"[yellow]{len(on_hold)} on hold[/]")
        console.print(f"  Found {len(roblox_items)} items  ({', '.join(parts)})")

    except Exception as e:
        console.print(f"  [red]Could not load inventory:[/] {e}")
        console.print("  Check your User ID and make sure your inventory is public.")
        sys.exit(1)

    if not tradeable:
        console.print("  [red]No tradeable items found in your inventory.[/]")
        sys.exit(1)

    console.print()
    console.print("  [dim]Space[/] = select/deselect   [dim]Enter[/] = confirm")
    if on_hold:
        console.print("  [dim]On-hold items are shown at the bottom but cannot be selected.[/]")
    console.print()

    preselected = existing.get("offer_item_ids", [])
    choices     = _build_checkbox_choices(tradeable, on_hold, preselected)

    selected_tuples: list[tuple[int, int]] = await questionary.checkbox(
        "Select items to offer:",
        choices=choices,
        instruction=" (↑↓ navigate  ·  Space select  ·  Enter confirm)",
    ).ask_async()

    if not selected_tuples:
        console.print("\n  [yellow]No items selected — setup cancelled.[/]")
        sys.exit(0)

    offer_item_ids = [item_id for (_, item_id) in selected_tuples]

    # Summary table of selected items
    console.print()
    table = Table(
        box=box.SIMPLE,
        show_header=True,
        header_style="bold dim",
        padding=(0, 1),
    )
    table.add_column("Item",  style="cyan",    no_wrap=False)
    table.add_column("RAP",   justify="right", style="white")
    table.add_column("Value", justify="right", style="green")
    table.add_column("",      justify="left")

    id_counts_sel: Counter = Counter(item_id for (_, item_id) in selected_tuples)
    id_seen_sel:   dict[int, int] = {}
    total_value = 0

    for idx, item_id in selected_tuples:
        item = tradeable[idx]
        total_value += item["value"]

        if id_counts_sel[item_id] > 1:
            id_seen_sel[item_id] = id_seen_sel.get(item_id, 0) + 1
            name = f"{item['name']} [{id_seen_sel[item_id]}/{id_counts_sel[item_id]}]"
        else:
            name = item["name"]

        table.add_row(
            name,
            f"{item['rap']:,}",
            f"{item['value']:,}",
            _item_flags(item),
        )

    console.print(table)
    console.print(f"  [bold]{len(offer_item_ids)} item(s) selected[/]   "
                  f"Total value: [green bold]{total_value:,}[/]")

    cfg["offer_item_ids"] = offer_item_ids

    # ── Step 3: Request tags ─────────────────────────────
    console.print()
    console.print("[bold cyan]  Step 3 of 3[/]  [dim]·  What are you looking for?[/]")
    console.print()

    tag_choices = [
        Choice("upgrade    — items worth more than yours",  value="upgrade",   checked=True),
        Choice("downgrade  — items worth less (+ adds)",    value="downgrade", checked=False),
        Choice("demand     — high-demand items",            value="demand",    checked=True),
        Choice("projected  — projected / rising items",     value="projected", checked=False),
        Choice("rare       — rare limiteds",                value="rare",      checked=False),
        Choice("any        — accept anything",              value="any",       checked=False),
    ]
    prev_tags = set(existing.get("trade_ad", {}).get("request_tags", []))
    if prev_tags:
        for ch in tag_choices:
            ch.checked = ch.value in prev_tags

    request_tags: list[str] = await questionary.checkbox(
        "What are you looking for?",
        choices=tag_choices,
        instruction=" (Space select  ·  Enter confirm)",
    ).ask_async()

    if not request_tags:
        request_tags = ["upgrade", "demand"]
        console.print("  [dim]Nothing selected — using defaults: upgrade, demand[/]")

    console.print(f"  Tags: [cyan]{', '.join(request_tags)}[/]")

    console.print()
    req_ids_raw = _ask("Specific item IDs to request (optional, comma-separated)", required=False)
    request_item_ids: list[int] = []
    for part in req_ids_raw.split(","):
        part = part.strip()
        if part.isdigit():
            request_item_ids.append(int(part))

    cfg["trade_ad"] = {
        "request_tags":     request_tags,
        "request_item_ids": request_item_ids,
        "dry_run":          False,
    }

    # ── Automation ───────────────────────────────────────
    console.print()
    prev_interval = existing.get("automation", {}).get("interval_minutes", 15)
    interval = _ask_int("Post interval in minutes (minimum 15)", prev_interval)
    if interval < 15:
        interval = 15
        console.print("  [yellow]Minimum is 15 minutes — set to 15.[/]")

    cfg["automation"] = {"interval_minutes": interval, "run_once": False}
    cfg["logging"]    = {"level": "INFO"}

    # ── Final summary ─────────────────────────────────────
    console.print()
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="dim", justify="right")
    grid.add_column()
    grid.add_row("User ID",    str(cfg["user_id"]))
    grid.add_row("Offering",   f"{len(offer_item_ids)} item(s)  (total value: [green]{total_value:,}[/])")
    grid.add_row("Requesting", f"[cyan]{', '.join(request_tags)}[/]")
    grid.add_row("Interval",   f"every {interval} minutes")

    console.print(Panel(grid, title="[bold]Summary[/]", border_style="cyan", padding=(0, 2)))
    console.print()

    if _ask_yn("Save config and start the bot?", default=True):
        save_config(cfg)
        return cfg
    else:
        console.print("  [yellow]Setup cancelled — no changes saved.[/]")
        sys.exit(0)


# ──────────────────────────────────────────────────────────
# BOT LOGIC
# ──────────────────────────────────────────────────────────

async def run_once(cfg: dict, dry_run: bool = False) -> dict:
    user_id     = cfg["user_id"]
    roli_cookie = cfg["roli_verification"]
    offer_ids   = cfg.get("offer_item_ids", [])
    trade_cfg   = cfg.get("trade_ad", {})

    request_tags = trade_cfg.get("request_tags", ["upgrade", "demand"])
    request_ids  = trade_cfg.get("request_item_ids", [])
    is_dry_run   = dry_run or trade_cfg.get("dry_run", False)

    try:
        with console.status("[dim]Fetching Rolimons item values...[/]", spinner="dots"):
            rolimons_items = await fetch_rolimons_items()

        # For new limiteds not yet in Rolimons, fall back to Roblox RAP
        roblox_rap: dict[int, int] = {}
        new_ids = [i for i in offer_ids if i not in rolimons_items]
        if new_ids:
            with console.status(
                f"[dim]Fetching Roblox RAP for {len(new_ids)} new item(s)...[/]",
                spinner="dots",
            ):
                roblox_items = await fetch_roblox_inventory(user_id)
                roblox_rap = {item["id"]: item["rap"] for item in roblox_items}

        def _effective_value(item_id: int) -> int:
            roli = rolimons_items.get(item_id, {})
            v = max(roli.get("value", 0), roli.get("rap", 0))
            return v if v > 0 else roblox_rap.get(item_id, 0)

        total_value = sum(_effective_value(i) for i in offer_ids)

        log.info(
            f"Offering [bold]{len(offer_ids)}[/] item(s)  |  "
            f"total value: [green]{total_value:,}[/]"
        )
        log.info(f"Requesting: [cyan]{', '.join(request_tags)}[/]")

        if is_dry_run:
            log.info("[yellow][DRY RUN][/] Trade ad was [bold]NOT[/] posted (preview mode)")
            return {"success": True, "dry_run": True, "offer_count": len(offer_ids)}

        with console.status("[dim]Posting trade ad to Rolimons...[/]", spinner="dots"):
            await post_trade_ad(user_id, offer_ids, request_tags, request_ids, roli_cookie)

        url = f"https://www.rolimons.com/playertrades/{user_id}"
        log.info(f"[green]Trade ad posted![/]  {url}")
        return {"success": True, "url": url, "offer_count": len(offer_ids)}

    except CooldownError:
        log.warning("Cooldown not expired — Rolimons requires 15 minutes between posts")
        return {"success": False, "error": "cooldown"}

    except AuthError:
        log.error("Invalid or expired [bold]_RoliVerification[/] cookie")
        log.error("  Run  [bold]python bot.py --setup[/]  to update it")
        return {"success": False, "error": "auth"}

    except RateLimitError as e:
        log.error(f"Rate limit: {e}")
        return {"success": False, "error": "rate_limit"}

    except Exception as e:
        log.error(f"Unexpected error: {e}", exc_info=True)
        return {"success": False, "error": "unknown", "message": str(e)}


async def run_loop(cfg: dict, dry_run: bool = False) -> None:
    interval = cfg.get("automation", {}).get("interval_minutes", 15)
    if interval < 15:
        interval = 15
        log.warning("Interval below minimum — using 15 minutes")

    log.info(f"Bot running — posting every [bold]{interval}[/] minutes  [dim](Ctrl+C to stop)[/]")
    iteration = 0

    while True:
        iteration += 1
        console.rule(
            f"[dim]Run #{iteration}  ·  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}[/]",
            style="dim",
        )

        result = await run_once(cfg, dry_run=dry_run)

        if result.get("error") == "auth":
            log.error("Stopping bot — please fix your cookie and restart.")
            break

        wait = 60 if result.get("error") == "rate_limit" else interval
        next_run = datetime.now() + timedelta(minutes=wait)
        log.info(f"Next run: [bold]{next_run.strftime('%H:%M:%S')}[/]  [dim](in {wait} min)[/]")

        try:
            await asyncio.sleep(wait * 60)
        except asyncio.CancelledError:
            break

    log.info("Bot stopped.")


# ──────────────────────────────────────────────────────────
# ENTRY POINT
# ──────────────────────────────────────────────────────────

def _print_banner() -> None:
    console.print()
    console.print(Panel(
        Text.assemble(
            ("Rolimons Trade Ad Bot", "bold cyan"),
            ("  v" + VERSION, "dim"),
        ),
        border_style="cyan",
        padding=(0, 2),
        expand=False,
    ))
    console.print()


def _print_active_config(cfg: dict) -> None:
    t = cfg.get("trade_ad", {})
    a = cfg.get("automation", {})

    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="dim", justify="right")
    grid.add_column()

    grid.add_row("User ID",
                 str(cfg["user_id"]))
    grid.add_row("Profile",
                 f"[link]https://www.rolimons.com/playertrades/{cfg['user_id']}[/link]")
    grid.add_row("Offering",
                 f"[cyan]{len(cfg.get('offer_item_ids', []))}[/] item(s)")
    grid.add_row("Requesting",
                 f"[cyan]{', '.join(t.get('request_tags', []))}[/]")

    if t.get("dry_run"):
        mode = "[yellow]DRY RUN[/] (no ads will be posted)"
    elif a.get("run_once"):
        mode = "post once and exit"
    else:
        mode = f"loop every [bold]{a.get('interval_minutes', 15)}[/] min"
    grid.add_row("Mode", mode)

    console.print(Panel(grid, border_style="blue", padding=(0, 2)))
    console.print()


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Rolimons Trade Ad Bot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python bot.py              # start loop (posts every 15 min)\n"
            "  python bot.py --once       # post once and exit\n"
            "  python bot.py --dry-run    # preview only, nothing is posted\n"
            "  python bot.py --setup      # re-run the setup wizard\n"
        ),
    )
    parser.add_argument("--setup",   action="store_true", help="Run the setup wizard")
    parser.add_argument("--once",    action="store_true", help="Post once and exit")
    parser.add_argument("--dry-run", action="store_true", help="Preview mode — nothing is posted")
    args = parser.parse_args()

    _print_banner()

    cfg = load_config()

    if args.setup or not cfg:
        if not cfg and not args.setup:
            console.print("  [dim]No config.yaml found — starting setup wizard.[/]")
            console.print()
        cfg = await run_setup(cfg)

    try:
        validate_config(cfg)
    except ValueError as e:
        console.print(f"  [red bold]ERROR:[/] {e}")
        console.print()
        sys.exit(1)

    level = cfg.get("logging", {}).get("level", "INFO")
    log.setLevel(getattr(logging, level.upper(), logging.INFO))

    _print_active_config(cfg)

    run_once_mode = args.once or cfg.get("automation", {}).get("run_once", False)
    dry_run       = args.dry_run or cfg.get("trade_ad", {}).get("dry_run", False)

    if run_once_mode:
        result = await run_once(cfg, dry_run=dry_run)
        sys.exit(0 if result.get("success") else 1)
    else:
        await run_loop(cfg, dry_run=dry_run)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        console.print("\n  [dim]Bye![/]")
