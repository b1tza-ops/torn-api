# Torn Deal Finder Pro v7

Clean rewrite of the Torn market assistant. The app is read-only with respect to Torn: it reads API data, tracks your confirmed/manual holdings locally, and gives you buy/sell guidance through Telegram. It does **not** submit purchases, listings, travel, training, item use, or other gameplay actions.

## Core workflow

1. Scanner checks a controlled watchlist of liquid Torn items.
2. A strong deal creates a Telegram alert with **I bought it**.
3. You confirm the actual quantity bought.
4. The bot stores the purchase and maintains weighted-average cost.
5. Portfolio tracks estimated net value, unrealized P/L and realized P/L.
6. The bot monitors held items for exit opportunities.
7. The Sell screen recommends a price and shows expected after-fee P/L.
8. After you sell manually in Torn, **Mark sold** records actual quantity and price.

## Telegram menu

`/menu` opens:

- **Deals** — recent detected opportunities
- **Portfolio** — owned positions, average cost, estimated P/L
- **Add existing** — add items you already owned before the bot
- **Sell** — pick an owned item and get a live sell recommendation
- **Market** — search an item and view live market/reference data
- **Watchlist** — add/remove watched items
- **Settings** — budget, minimum ROI/profit, target exit ROI and fee
- **Pause/Resume** — stops scanning while keeping Telegram controls available

### Adding existing inventory

Tap **Add existing**, type part of the Torn item name, select the matching item, then enter quantity and average purchase price. Enter `0` for unknown cost basis. The item immediately appears in Portfolio and Sell.

### Confirming a new purchase

Deal alerts contain **✅ I bought it**. Tap it and enter the quantity you actually purchased. Repeated purchases automatically update the weighted-average cost.

### Selling

Portfolio/Sell shows current cheapest listing, calculated reference price, suggested listing price, confidence, estimated net P/L and ROI. You perform the listing manually in Torn. Afterwards tap **Mark sold**, enter the quantity and actual sale price, and the bot updates your position and realized P/L.

## VPS install/update

```bash
sudo apt update
sudo apt install -y python3 python3-pip python3-venv git
cd ~/torn-deal-finder
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp config.example.json config.json
```

Set secrets in `~/.torn-env`:

```bash
export TORN_API_KEY="YOUR_TORN_KEY"
export TELEGRAM_BOT_TOKEN="YOUR_TELEGRAM_TOKEN"
export TELEGRAM_CHAT_ID="YOUR_CHAT_ID"
```

Run:

```bash
source ~/.torn-env
source venv/bin/activate
python app.py
```

For later updates:

```bash
cd ~/torn-deal-finder
git pull
```

Then restart the process/service.

## Persistence

Runtime data lives in `torn_deals.sqlite3`, including holdings, confirmed purchases, sales, price samples, deal history, runtime settings and alert cooldown state. Existing `holdings` data from earlier versions is retained where compatible.

## Safety / practical limits

The Torn API does not provide a reliable full personal inventory feed for this workflow, so inventory is built from items you manually add or purchases you confirm. Market reference prices are estimates, not guaranteed resale prices. The scanner uses a moderate watchlist and polling cadence to avoid aggressive API use.
