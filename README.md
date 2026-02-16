# Rolimons Trade Ad Bot

Automatically posts trade ads on Rolimons based on items you pick from your inventory.

---

## Requirements

- **Python 3.10 or newer** — check with: `python --version`
- **pip** — comes installed with Python

---

## Quick Start (3 steps)

### 1. Install dependencies

Open a terminal / command prompt in this folder and run:

```
pip install httpx pyyaml questionary
```

### 2. Run the setup wizard

```
python bot.py
```

The first time you run the bot it will automatically launch the setup wizard.

### 3. Done!

After setup the bot starts running and posts a new trade ad every 15 minutes.

Press **Ctrl+C** to stop it.

---

## Setup Wizard

The wizard walks you through 3 steps:

**Step 1 — Basic settings**
- Your Roblox User ID
- Your `_RoliVerification` cookie from rolimons.com (see below)

**Step 2 — Pick items to offer**

Your full inventory is fetched and displayed as an interactive checklist:

```
  Select items to offer:
  > ○ Korblox Deathspeaker              RAP:   50,000  Value:   55,000  💎
    ○ Domino Crown                      RAP:  180,000  Value:  190,000  📈 💎
    ○ Blue Sparkle Time Fedora [1/2]    RAP:    8,500  Value:    9,000
    ○ Blue Sparkle Time Fedora [2/2]    RAP:    8,500  Value:    9,000
    ─── On Hold  (cannot be traded right now) ───
    - Headless Horseman                 RAP:   75,000  Value:   80,000     [on hold]
```

- **Space** — select / deselect an item
- **Enter** — confirm selection
- Items sorted by RAP (highest first)
- On-hold items are shown at the bottom but **cannot be selected**
- Flags: `📈` projected &nbsp; `💎` rare &nbsp; `🆕` new item (not yet in Rolimons database)
- If you own duplicates of the same item, they appear as `[1/2]`, `[2/2]`, etc.

**Step 3 — What you're looking for**

Pick request tags (what kind of items you want in return):

| Tag | Meaning |
|-----|---------|
| `upgrade` | items worth more than yours |
| `downgrade` | items worth less (+ adds) |
| `demand` | high-demand items |
| `projected` | projected / rising-value items |
| `rare` | rare limiteds |
| `any` | accept anything |

Optionally add specific item IDs you're looking for.

---

## Commands

| Command | What it does |
|---------|-------------|
| `python bot.py` | Start the bot (posts every 15 min, runs forever) |
| `python bot.py --once` | Post one trade ad and exit |
| `python bot.py --dry-run` | Preview only — shows what would be posted, nothing is sent |
| `python bot.py --setup` | Re-run the setup wizard to change your items or settings |
| `python bot.py --once --dry-run` | Preview a single run without posting |

---

## How to get your _RoliVerification cookie

1. Log in to **https://www.rolimons.com**
2. Press **F12** to open Developer Tools
3. Click the **Application** tab at the top
4. In the left panel, expand **Cookies** → click on `https://www.rolimons.com`
5. Find the row called `_RoliVerification` and **copy the Value**
6. Paste it in the setup wizard when asked

> **Keep this value secret** — it lets the bot post on your behalf.
> Cookies expire occasionally; if the bot shows an auth error, repeat these steps and run `python bot.py --setup`.

---

## Configuration

Your settings are saved in **`config.yaml`** after the wizard finishes.
You can edit it with any text editor, or just re-run `python bot.py --setup`.

The file `config.yaml.example` shows all available options with explanations.

---

## Rolimons limits

- Only one post every **15 minutes** (Rolimons enforces this)
- Maximum **60 trade ads per 24 hours**

The bot respects these limits automatically. If the daily limit is hit, it waits 1 hour before retrying.

---

## Logs

Detailed logs are saved to the `logs/` folder. If something goes wrong, check `logs/bot.log`.

---

## Troubleshooting

| Error | Fix |
|-------|-----|
| `Invalid or expired cookie` | Get a new `_RoliVerification` from DevTools, then run `python bot.py --setup` |
| `Inventory is private` | Roblox Settings → Privacy → set inventory to **Everyone** |
| `Cooldown not expired` | Rolimons enforces 15 min between posts — the bot will wait automatically |
| `Daily limit reached` | 55 ads/24h hit — bot waits 1 hour and retries |
| `ModuleNotFoundError` | Run `pip install httpx pyyaml questionary` |
