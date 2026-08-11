# Torn Deal Finder Pro v4.2

A read-only Torn market intelligence service for a Linux VPS with an interactive Telegram control panel and a manual selling assistant.

## Core scanner

- Scans Torn API v2 Item Market listings.
- Builds reference prices from market depth instead of trusting one listing.
- Uses stored history, support depth, API average anchors, turnover estimates, ROI and capital efficiency.
- Ranks opportunities and sends only the strongest alerts.
- Stores data and runtime settings in SQLite.
- Telegram commands for status, watchlists, thresholds, item browsing and market snapshots.

## Selling assistant

Torn currently does not expose player inventory through the API, so v4.2 includes a private inventory ledger that you update through Telegram. The bot then uses live Item Market data to recommend sale prices and estimate post-fee profit.

Commands:

```text
/own Xanax 10 700000
/addown Xanax 5 710000
/inventory
/sell Xanax
/undercut Xanax 1000
/sellplan
/sold Xanax 5 810000
/sales
```

`/own <item> <qty> [cost_each]` sets a tracked holding. `/addown` adds to it and recalculates weighted average cost. `/sell` compares the holding with current market listings and calculates a suggested price, estimated net proceeds and estimated profit. `/undercut` lets you specify the undercut amount. `/sellplan` ranks your tracked holdings for selling. `/sold` records a completed sale and reduces the tracked quantity.

The bot **never submits a listing or performs a gameplay action**. Final sale listings are confirmed manually in Torn.

## Other Telegram commands

```text
/status
/top
/history Xanax
/items Candy
/categories
/find xanax
/item Xanax
/watch Xanax
/unwatch Xanax
/watchlist
/budget 5000000
/minprofit 50000
/minroi 8
/pause
/resume
/settings
/help
```

## Install on Ubuntu/Debian VPS

```bash
sudo apt update
sudo apt install -y python3 python3-pip python3-venv git
cd ~
git clone https://github.com/b1tza-ops/torn-api.git torn-deal-finder
cd torn-deal-finder
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp config.example.json config.json
```

Create `~/.torn-env`:

```bash
export TORN_API_KEY="YOUR_TORN_KEY"
export TELEGRAM_BOT_TOKEN="YOUR_TELEGRAM_TOKEN"
export TELEGRAM_CHAT_ID="YOUR_CHAT_ID"
```

Then:

```bash
chmod 600 ~/.torn-env
source ~/.torn-env
cd ~/torn-deal-finder
source venv/bin/activate
python app.py
```

## Updating

```bash
cd ~/torn-deal-finder
git pull
```

If running with systemd:

```bash
sudo systemctl restart torn-deal-finder
```

If running manually, stop the old process and start `python app.py` again.

## Privacy

Never commit Torn API keys, Telegram tokens, `config.json` containing secrets, `.env`, `~/.torn-env`, or the SQLite database. The included `.gitignore` excludes common runtime files.
