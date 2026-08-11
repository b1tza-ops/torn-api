#!/usr/bin/env python3
import os
import time
import json
import math
import hashlib
from pathlib import Path
from statistics import median
from datetime import datetime

import requests

API_BASE = "https://api.torn.com/v2"
CONFIG_FILE = Path(os.getenv("TORN_CONFIG", "config.json"))
STATE_FILE = Path(os.getenv("TORN_STATE", "state.json"))
UA = "TornDealFinder/2.0 personal-read-only-market-watcher"

def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default

def save_json(path, obj):
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2)
    tmp.replace(path)

def fmt_money(x):
    return f"${int(round(x)):,}"

def api_get(path, api_key, params=None):
    headers = {
        "Authorization": f"ApiKey {api_key}",
        "Accept": "application/json",
        "User-Agent": UA,
    }
    r = requests.get(API_BASE + path, params=params or {}, headers=headers, timeout=20)
    if r.status_code == 429:
        raise RuntimeError("Rate limited by Torn API. Increase check_interval_seconds.")
    r.raise_for_status()
    data = r.json()
    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError(str(data["error"]))
    return data

def fetch_item_market(item_id, api_key):
    return api_get(f"/market/{int(item_id)}/itemmarket", api_key)

def find_listing_list(obj):
    if isinstance(obj, dict):
        for k in ("itemmarket", "item_market", "listings", "market"):
            v = obj.get(k)
            if isinstance(v, list):
                return v
            if isinstance(v, dict):
                for kk in ("listings", "items", "results"):
                    vv = v.get(kk)
                    if isinstance(vv, list):
                        return vv
        for v in obj.values():
            out = find_listing_list(v)
            if out:
                return out
    elif isinstance(obj, list):
        if obj and all(isinstance(x, dict) for x in obj):
            if any(("price" in x or "cost" in x) for x in obj):
                return obj
        for v in obj:
            out = find_listing_list(v)
            if out:
                return out
    return []

def get_num(d, keys, default=None):
    for k in keys:
        if k in d and isinstance(d[k], (int, float)):
            return d[k]
    return default

def normalize_listing(x):
    price = get_num(x, ("price", "cost", "unit_price"))
    qty = get_num(x, ("quantity", "qty", "amount"), 1)
    if price is None:
        return None
    ident = None
    for k in ("id", "listing_id", "ID"):
        if k in x:
            ident = str(x[k])
            break
    if ident is None:
        ident = hashlib.sha1(json.dumps(x, sort_keys=True).encode()).hexdigest()[:16]
    return {"id": ident, "price": int(price), "quantity": int(qty), "raw": x}

def robust_reference_price(listings, top_n=20, ignore_cheapest=1):
    prices = sorted(l["price"] for l in listings if l["price"] > 0)
    if len(prices) < 3:
        return None
    sample = prices[ignore_cheapest:ignore_cheapest + top_n] if len(prices) > ignore_cheapest else prices
    if not sample:
        sample = prices
    return median(sample)

def telegram_send(token, chat_id, text):
    if not token or not chat_id:
        print("\n" + text + "\n")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    r = requests.post(url, json={
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True
    }, timeout=20)
    r.raise_for_status()

def make_market_link(item_id):
    return f"https://www.torn.com/page.php?sid=ItemMarket#/market/view=search&itemID={item_id}"

def analyze(item, listings, cfg):
    ref = robust_reference_price(
        listings,
        top_n=int(item.get("reference_top_n", cfg.get("reference_top_n", 20))),
        ignore_cheapest=int(item.get("ignore_cheapest_for_reference", cfg.get("ignore_cheapest_for_reference", 1))),
    )
    if not ref:
        return [], None

    fee = float(item.get("sale_fee_percent", cfg.get("sale_fee_percent", 5))) / 100.0
    min_roi = float(item.get("min_roi_percent", cfg.get("min_roi_percent", 8)))
    min_profit = int(item.get("min_profit_per_item", cfg.get("min_profit_per_item", 25000)))
    min_discount = float(item.get("min_discount_percent", cfg.get("min_discount_percent", 8)))
    max_buy = item.get("max_buy_price")
    max_listing_cost = item.get("max_listing_cost", cfg.get("max_listing_cost"))

    results = []
    for l in sorted(listings, key=lambda a: a["price"]):
        p = l["price"]
        if p <= 0:
            continue
        if max_buy is not None and p > int(max_buy):
            continue
        listing_cost = p * max(1, l["quantity"])
        if max_listing_cost is not None and listing_cost > int(max_listing_cost):
            continue

        expected_net = ref * (1 - fee)
        profit = expected_net - p
        roi = profit / p * 100
        discount = (ref - p) / ref * 100

        if profit >= min_profit and roi >= min_roi and discount >= min_discount:
            results.append({
                **l,
                "reference": ref,
                "profit": profit,
                "roi": roi,
                "discount": discount,
                "listing_cost": listing_cost,
            })
    return results, ref

def alert_text(item, deal):
    qty = deal["quantity"]
    total_est = deal["profit"] * qty
    lines = [
        "🔥 TORN DEAL FOUND",
        f"{item['name']} (ID {item['id']})",
        f"Buy: {fmt_money(deal['price'])} each",
        f"Qty: {qty:,}",
        f"Reference: {fmt_money(deal['reference'])}",
        f"Discount: {deal['discount']:.1f}%",
        f"Est. net profit: {fmt_money(deal['profit'])} each",
        f"Est. ROI: {deal['roi']:.1f}%",
    ]
    if qty > 1:
        lines.append(f"Est. total profit: {fmt_money(total_est)}")
    lines.append(make_market_link(item["id"]))
    return "\n".join(lines)

def heartbeat(cfg, token, chat_id, state):
    hours = float(cfg.get("heartbeat_hours", 0))
    if hours <= 0:
        return
    now = time.time()
    last = state.get("last_heartbeat", 0)
    if now - last >= hours * 3600:
        telegram_send(token, chat_id, f"✅ Torn Deal Finder is running\n{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        state["last_heartbeat"] = now

def main():
    cfg = load_json(CONFIG_FILE, None)
    if not cfg:
        raise SystemExit("Missing config.json. Copy config.example.json to config.json and edit it.")

    api_key = os.getenv("TORN_API_KEY") or cfg.get("torn_api_key")
    if not api_key:
        raise SystemExit("Missing TORN_API_KEY environment variable.")

    tg = cfg.get("telegram", {})
    tg_token = os.getenv("TELEGRAM_BOT_TOKEN") or tg.get("bot_token")
    tg_chat = os.getenv("TELEGRAM_CHAT_ID") or tg.get("chat_id")

    state = load_json(STATE_FILE, {"seen": [], "last_heartbeat": 0})
    seen = set(state.get("seen", []))
    max_seen = int(cfg.get("remember_seen", 5000))
    interval = max(20, int(cfg.get("check_interval_seconds", 60)))

    print(f"Torn Deal Finder v2 started. Watching {len(cfg.get('items', []))} item(s).")

    while True:
        try:
            heartbeat(cfg, tg_token, tg_chat, state)
            for item in cfg.get("items", []):
                try:
                    payload = fetch_item_market(item["id"], api_key)
                    raw = find_listing_list(payload)
                    listings = [normalize_listing(x) for x in raw]
                    listings = [x for x in listings if x]

                    deals, ref = analyze(item, listings, cfg)
                    if ref:
                        print(f"[{datetime.now().strftime('%H:%M:%S')}] {item['name']}: ref {fmt_money(ref)}, {len(deals)} deal(s)")

                    max_alerts = int(item.get("max_alerts_per_cycle", 2))
                    sent = 0
                    for d in deals:
                        key = f"{item['id']}:{d['id']}:{d['price']}"
                        if key in seen:
                            continue
                        telegram_send(tg_token, tg_chat, alert_text(item, d))
                        seen.add(key)
                        sent += 1
                        if sent >= max_alerts:
                            break

                    time.sleep(float(cfg.get("per_item_delay_seconds", 1.5)))

                except Exception as e:
                    print(f"[ERROR] {item.get('name', item.get('id'))}: {e}")

            state["seen"] = list(seen)[-max_seen:]
            save_json(STATE_FILE, state)
            time.sleep(interval)

        except KeyboardInterrupt:
            state["seen"] = list(seen)[-max_seen:]
            save_json(STATE_FILE, state)
            print("Stopped.")
            break
        except Exception as e:
            print("[LOOP ERROR]", e)
            time.sleep(30)

if __name__ == "__main__":
    main()
