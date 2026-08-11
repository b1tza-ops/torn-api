# Torn Deal Finder v2

A 24/7 **read-only** market watcher for Torn.

## What is better in v2

- No manual resale price required.
- Reads current Item Market listings.
- Builds a reference price from current listings.
- Ignores the cheapest listing when estimating the normal price, helping prevent one bargain from dragging down the reference.
- Calculates discount, estimated post-fee profit and ROI.
- Telegram phone alerts.
- Duplicate alert prevention.
- Optional heartbeat to tell you the bot is alive.
- Budget guard (`max_listing_cost`).
- Designed for simple Python hosting services.

It never buys, sells, travels, trains, or performs gameplay actions.

## Files

- `app.py` — watcher
- `config.example.json` — settings
- `requirements.txt`
- `Procfile` — useful on hosts that support worker processes
- `start.sh` — simple Linux launch script

## Quick setup

### 1. Install

```bash
pip install -r requirements.txt
```

### 2. Config

```bash
cp config.example.json config.json
```

Edit `config.json`.

### 3. Secrets

Set these in your host's Environment Variables / Secrets section:

```text
TORN_API_KEY=your_torn_api_key
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
TELEGRAM_CHAT_ID=your_chat_id
```

Do **not** publish these in GitHub or screenshots.

### 4. Start command

```bash
python app.py
```

## How deal detection works

For each watched item, the program:

1. Fetches Item Market listings from Torn API v2.
2. Sorts current listing prices.
3. Ignores the cheapest listing by default.
4. Uses the median of the next group as a reference market price.
5. Estimates proceeds after your configured selling fee.
6. Calculates profit and ROI for each cheap listing.
7. Alerts only when all configured thresholds are met.

This is deliberately conservative. A reference price is an estimate, not a guarantee that an item will actually resell at that price.

## Useful config values

`min_roi_percent`
: Minimum estimated return on purchase price.

`min_profit_per_item`
: Minimum estimated dollar profit per unit.

`min_discount_percent`
: How far below the current reference price the listing must be.

`max_listing_cost`
: Stops alerts for listings that would tie up more capital than you want.

`heartbeat_hours`
: Telegram "still running" message interval. Set to 0 to disable.

## Adding items

Add another object under `items`:

```json
{
  "name": "Your item",
  "id": 123,
  "min_roi_percent": 10,
  "min_profit_per_item": 10000,
  "min_discount_percent": 10,
  "max_listing_cost": 3000000,
  "max_alerts_per_cycle": 2
}
```

Use the correct Torn item ID.

## Hosting

On hosts that ask for a start command:

```text
python app.py
```

If the platform supports a Procfile, this package includes:

```text
worker: python app.py
```

## Important

Torn's API is officially intended as a read-only interface for external tools. This program only reads API data and sends notifications.

Keep the polling interval sensible. API availability, response formats and limits can change, so if Torn changes API v2, the parser may need updating.
