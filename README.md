# Torn Deal Finder Pro v4

A read-only Torn market intelligence service for a Linux VPS. It scans Torn API v2 Item Market data, filters suspicious pricing, ranks realistic opportunities, stores history in SQLite, and sends interactive Telegram alerts.

## v4 highlights

- Interactive Telegram commands: `/status`, `/top`, `/history <item>`, `/watch <item>`, `/unwatch <item>`, `/watchlist`, `/budget`, `/minprofit`, `/minroi`, `/pause`, `/resume`, `/settings`, `/help`.
- Live reference pricing from multiple listings rather than a single displayed value.
- Historical reference sanity checks to reject sudden suspicious market jumps.
- Support-depth requirement: multiple listings must exist near the reference price.
- Optional API average-price anchor when Torn provides it.
- Listing turnover tracking in SQLite to estimate whether an item is actually moving.
- Confidence, turnover, capital-efficiency, ROI and profit combined into a deal score.
- Global ranking so Telegram receives the best opportunities first.
- Runtime settings persist in SQLite, so `/budget`, `/watch`, `/pause`, etc. survive restarts.
- Duplicate/cooldown protection and API rate-limit backoff.

The program **never buys, sells, travels, trains, uses items, or performs gameplay actions**.

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

## Telegram controls

```text
/status
/top
/history Xanax
/watch Can of Taurine Elite
/unwatch Bag of Candy Kisses
/watchlist
/budget 5000000
/minprofit 50000
/minroi 8
/pause
/resume
/settings
/help
```

`/budget` is the maximum capital allowed for one detected deal, not your total Torn cash.

## How v4 validates a deal

A low listing is not automatically considered a bargain. The scanner first builds a reference from a cluster of current listings, trims outliers, requires multiple supporting listings near that reference, compares the reference against recent stored history and Torn's API average when available, then calculates post-fee profit and ROI. Items with weak depth or suspicious reference jumps are rejected.

The turnover score is an estimate based on listings appearing/disappearing between scans. It is useful as a relative signal, but it is **not actual Torn sales volume**.

## API load

Torn's documentation states an API limit of up to 100 individual requests per minute and notes that identical API requests may be cached for up to 30 seconds. v4 therefore defaults to a moderate watchlist and a five-minute scan cycle rather than aggressive polling.

## Updating the VPS later

```bash
cd ~/torn-deal-finder
git pull
sudo systemctl restart torn-deal-finder
```

## Files that must stay private

Never commit your `config.json` if you put secrets in it, `.env`, `~/.torn-env`, or `torn_deals.sqlite3`. The included `.gitignore` excludes the common runtime files.
