# Torn Deal Finder Pro v3

Read-only Torn market intelligence service for a Linux VPS.

Features include Torn API v2 item discovery, multi-category Item Market scanning, robust reference pricing, SQLite history, ROI/profit/discount/confidence scoring, ranked deal alerts, duplicate suppression, Telegram notifications, summaries, heartbeats, retries and rate-limit backoff.

It never purchases, sells, trains, travels, uses items, or performs gameplay actions.

## VPS setup

```bash
sudo apt update
sudo apt install -y python3 python3-pip python3-venv git
git clone https://github.com/b1tza-ops/torn-api.git torn-deal-finder
cd torn-deal-finder
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp config.example.json config.json
```

Set secrets as environment variables:

```bash
export TORN_API_KEY="..."
export TELEGRAM_BOT_TOKEN="..."
export TELEGRAM_CHAT_ID="..."
```

Never commit those secrets.

Run with:

```bash
source venv/bin/activate
python app.py
```

The default scanner targets liquid categories including drugs, plushies, flowers, energy, alcohol, medical, temporary and candy items. Adjust `config.json` to change ROI/profit thresholds, bankroll limits, categories or explicit item IDs/names.
