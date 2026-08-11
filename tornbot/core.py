#!/usr/bin/env python3
"""Torn Deal Finder Pro v7.

Clean rewrite focused on one coherent workflow:
market scanner -> user confirms purchase -> portfolio -> sell recommendation -> user marks sold.
Torn access is read-only; no gameplay action is submitted automatically.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import signal
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Any

import requests

API_BASE = "https://api.torn.com/v2"
DB_PATH = Path(os.getenv("TORN_DB", "torn_deals.sqlite3"))
CONFIG_PATH = Path(os.getenv("TORN_CONFIG", "config.json"))
STOP = False


def _stop(*_):
    global STOP
    STOP = True


signal.signal(signal.SIGINT, _stop)
signal.signal(signal.SIGTERM, _stop)


def ts() -> int:
    return int(time.time())


def money(value: float | int | None) -> str:
    if value is None:
        return "n/a"
    return f"${int(round(float(value))):,}"


def parse_money(text: str) -> float:
    return float(text.replace(",", "").replace("$", "").strip())


@dataclass
class Settings:
    scan_interval: int = 300
    per_item_delay: float = 1.2
    market_limit: int = 100
    sale_fee_percent: float = 5.0
    min_roi_percent: float = 7.0
    min_total_profit: float = 15000.0
    max_capital_per_deal: float = 5_000_000.0
    target_exit_roi: float = 5.0
    max_items: int = 60


class Store:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self._schema()

    def _schema(self):
        self.db.execute("CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY,v TEXT)")
        self.db.execute("""CREATE TABLE IF NOT EXISTS holdings(
            item_id INTEGER PRIMARY KEY,item_name TEXT NOT NULL,qty INTEGER NOT NULL,
            cost_each REAL NOT NULL,updated_ts INTEGER NOT NULL)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS purchases(
            id INTEGER PRIMARY KEY AUTOINCREMENT,ts INTEGER,item_id INTEGER,item_name TEXT,
            qty INTEGER,cost_each REAL,source TEXT)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS sales(
            id INTEGER PRIMARY KEY AUTOINCREMENT,ts INTEGER,item_id INTEGER,item_name TEXT,
            qty INTEGER,sell_each REAL,cost_each REAL,realized_profit REAL)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS price_samples(
            ts INTEGER,item_id INTEGER,item_name TEXT,reference_price REAL,cheapest REAL,
            api_average REAL,confidence REAL,support INTEGER,PRIMARY KEY(ts,item_id))""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS deals(
            id INTEGER PRIMARY KEY AUTOINCREMENT,ts INTEGER,item_id INTEGER,item_name TEXT,
            listing_key TEXT UNIQUE,buy_price REAL,qty INTEGER,reference_price REAL,
            est_profit REAL,roi REAL,discount REAL,confidence REAL,score REAL)""")
        self.db.execute("CREATE TABLE IF NOT EXISTS alerts(k TEXT PRIMARY KEY,ts INTEGER)")
        self.db.commit()

    def get(self, key: str, default=None):
        row = self.db.execute("SELECT v FROM settings WHERE k=?", (key,)).fetchone()
        if not row:
            return default
        try:
            return json.loads(row[0])
        except Exception:
            return row[0]

    def set(self, key: str, value):
        self.db.execute("INSERT OR REPLACE INTO settings(k,v) VALUES(?,?)", (key, json.dumps(value)))
        self.db.commit()

    def holdings(self):
        return self.db.execute(
            "SELECT item_id,item_name,qty,cost_each FROM holdings WHERE qty>0 ORDER BY item_name"
        ).fetchall()

    def holding(self, item_id: int):
        return self.db.execute(
            "SELECT item_id,item_name,qty,cost_each FROM holdings WHERE item_id=?", (item_id,)
        ).fetchone()

    def set_existing(self, item: dict, qty: int, cost_each: float):
        self.db.execute(
            "INSERT OR REPLACE INTO holdings VALUES(?,?,?,?,?)",
            (item["id"], item["name"], qty, cost_each, ts()),
        )
        self.db.execute(
            "INSERT INTO purchases(ts,item_id,item_name,qty,cost_each,source) VALUES(?,?,?,?,?,?)",
            (ts(), item["id"], item["name"], qty, cost_each, "existing"),
        )
        self.db.commit()

    def add_purchase(self, item: dict, qty: int, cost_each: float, source="deal"):
        old = self.holding(item["id"])
        old_qty = int(old[2]) if old else 0
        old_cost = float(old[3]) if old else 0.0
        new_qty = old_qty + qty
        new_cost = ((old_qty * old_cost) + (qty * cost_each)) / new_qty
        self.db.execute(
            "INSERT OR REPLACE INTO holdings VALUES(?,?,?,?,?)",
            (item["id"], item["name"], new_qty, new_cost, ts()),
        )
        self.db.execute(
            "INSERT INTO purchases(ts,item_id,item_name,qty,cost_each,source) VALUES(?,?,?,?,?,?)",
            (ts(), item["id"], item["name"], qty, cost_each, source),
        )
        self.db.commit()
        return new_qty, new_cost

    def remove_holding(self, item_id: int):
        self.db.execute("DELETE FROM holdings WHERE item_id=?", (item_id,))
        self.db.commit()

    def record_sale(self, item: dict, qty: int, sell_each: float, fee_pct: float):
        row = self.holding(item["id"])
        if not row:
            raise ValueError("Item is not in portfolio")
        owned = int(row[2]); cost = float(row[3])
        qty = max(1, min(qty, owned))
        net_each = sell_each * (1 - fee_pct / 100)
        realized = (net_each - cost) * qty
        left = owned - qty
        if left:
            self.db.execute("UPDATE holdings SET qty=?,updated_ts=? WHERE item_id=?", (left, ts(), item["id"]))
        else:
            self.db.execute("DELETE FROM holdings WHERE item_id=?", (item["id"],))
        self.db.execute(
            "INSERT INTO sales(ts,item_id,item_name,qty,sell_each,cost_each,realized_profit) VALUES(?,?,?,?,?,?,?)",
            (ts(), item["id"], item["name"], qty, sell_each, cost, realized),
        )
        self.db.commit()
        return qty, realized, left

    def realized_profit(self):
        return float(self.db.execute("SELECT COALESCE(SUM(realized_profit),0) FROM sales").fetchone()[0] or 0)

    def recent_deals(self, limit=10):
        return self.db.execute(
            "SELECT item_id,item_name,buy_price,qty,est_profit,roi,score FROM deals ORDER BY ts DESC,score DESC LIMIT ?",
            (limit,),
        ).fetchall()


class TornClient:
    def __init__(self, key: str):
        self.key = key
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"ApiKey {key}",
            "Accept": "application/json",
            "User-Agent": "TornDealFinderPro/7.0",
        })

    def get(self, path: str, params=None):
        delay = 2
        last = None
        for attempt in range(4):
            try:
                r = self.session.get(API_BASE + path, params=params or {}, timeout=20)
                if r.status_code == 429:
                    time.sleep(max(delay, int(r.headers.get("Retry-After", delay))))
                    delay *= 2
                    continue
                r.raise_for_status()
                data = r.json()
                if isinstance(data, dict) and data.get("error"):
                    raise RuntimeError(str(data["error"]))
                return data
            except Exception as exc:
                last = exc
                if attempt == 3:
                    break
                time.sleep(delay); delay *= 2
        raise RuntimeError(f"Torn API request failed: {last}")

    @staticmethod
    def _find_list(obj, preferred=()):
        if isinstance(obj, dict):
            for key in preferred:
                v = obj.get(key)
                if isinstance(v, list):
                    return v
                if isinstance(v, dict):
                    for nested in ("items", "listings", "results"):
                        if isinstance(v.get(nested), list):
                            return v[nested]
            for v in obj.values():
                found = TornClient._find_list(v, preferred)
                if found:
                    return found
        elif isinstance(obj, list):
            if obj and all(isinstance(x, dict) for x in obj):
                return obj
            for v in obj:
                found = TornClient._find_list(v, preferred)
                if found:
                    return found
        return []

    def catalog(self):
        data = self.get("/torn/items")
        result = {}
        for x in self._find_list(data, ("items",)):
            iid = x.get("id") or x.get("ID")
            name = x.get("name") or x.get("item_name")
            typ = x.get("type") or x.get("category") or x.get("item_type") or "Other"
            if iid is not None and name:
                result[int(iid)] = {"id": int(iid), "name": str(name), "type": str(typ)}
        return result

    def market(self, item_id: int, limit=100):
        payload = self.get(f"/market/{item_id}/itemmarket", {"limit": limit})
        raw = self._find_list(payload, ("itemmarket", "item_market", "listings"))
        avg = None
        try:
            avg = payload.get("itemmarket", {}).get("item", {}).get("average_price")
        except Exception:
            pass
        listings = []
        for x in raw:
            price = x.get("price", x.get("cost", x.get("unit_price")))
            qty = x.get("quantity", x.get("qty", x.get("amount", 1)))
            if not isinstance(price, (int, float)) or price <= 0:
                continue
            lid = x.get("id") or x.get("listing_id") or hashlib.sha1(json.dumps(x, sort_keys=True).encode()).hexdigest()[:18]
            listings.append({"id": str(lid), "price": int(price), "quantity": max(1, int(qty or 1))})
        return sorted(listings, key=lambda z: z["price"]), (float(avg) if isinstance(avg, (int, float)) and avg > 0 else None)


class Pricing:
    @staticmethod
    def reference(listings, api_avg, historical, cfg):
        prices = sorted(x["price"] for x in listings)
        if len(prices) < int(cfg.get("min_listings_for_reference", 6)):
            return None, 0.0, 0
        skip = int(cfg.get("ignore_cheapest_for_reference", 2))
        top_n = int(cfg.get("reference_top_n", 30))
        sample = prices[skip:skip + top_n]
        if len(sample) < 4:
            sample = prices[:top_n]
        if len(sample) < 4:
            return None, 0.0, 0
        ref = float(median(sample))
        max_dev = float(cfg.get("reference_max_deviation_percent", 22))
        trimmed = [p for p in sample if abs(p - ref) / ref * 100 <= max_dev]
        if len(trimmed) >= 4:
            sample = trimmed
            ref = float(median(sample))
        support_band = float(cfg.get("support_band_percent", 7))
        support = sum(1 for p in prices if abs(p - ref) / ref * 100 <= support_band)
        mean = sum(sample) / len(sample)
        cv = math.sqrt(sum((p - mean) ** 2 for p in sample) / len(sample)) / mean if mean else 1
        stability = max(0.0, min(1.0, 1 - cv * 2.2))
        depth = min(1.0, support / max(5, int(cfg.get("min_support_listings", 5))))
        confidence = (0.55 * stability + 0.45 * depth) * 100
        anchors = [x for x in (api_avg, historical) if x]
        if anchors:
            anchor = float(median(anchors))
            diff = abs(ref - anchor) / anchor * 100
            confidence *= max(0.4, min(1.0, 1 - diff / 50))
        if historical:
            w = float(cfg.get("live_reference_weight", 0.7))
            ref = ref * w + historical * (1 - w)
        return ref, confidence, support


class Telegram:
    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = str(chat_id)
        self.base = f"https://api.telegram.org/bot{token}"

    def send(self, text, markup=None):
        payload = {"chat_id": self.chat_id, "text": text, "disable_web_page_preview": True}
        if markup:
            payload["reply_markup"] = markup
        r = requests.post(self.base + "/sendMessage", json=payload, timeout=15)
        r.raise_for_status()

    def edit(self, message_id, text, markup=None):
        payload = {"chat_id": self.chat_id, "message_id": message_id, "text": text, "disable_web_page_preview": True}
        if markup:
            payload["reply_markup"] = markup
        r = requests.post(self.base + "/editMessageText", json=payload, timeout=15)
        if not r.ok:
            self.send(text, markup)

    def ack(self, callback_id):
        if callback_id:
            requests.post(self.base + "/answerCallbackQuery", json={"callback_query_id": callback_id}, timeout=10)

    def updates(self, offset):
        r = requests.get(
            self.base + "/getUpdates",
            params={"offset": offset, "timeout": 0, "allowed_updates": json.dumps(["message", "callback_query"])},
            timeout=12,
        )
        r.raise_for_status()
        return r.json().get("result", [])


def B(text, data=None, url=None):
    return {"text": text, ("url" if url else "callback_data"): (url if url else data)}


def KB(rows):
    return {"inline_keyboard": rows}


def market_link(item_id):
    return f"https://www.torn.com/page.php?sid=ItemMarket#/market/view=search&itemID={item_id}"


class Bot:
    def __init__(self, cfg: dict, settings: Settings, torn: TornClient, tg: Telegram, store: Store):
        self.cfg = cfg
        self.s = settings
        self.torn = torn
        self.tg = tg
        self.store = store
        self.catalog = torn.catalog()
        self.offset = int(store.get("telegram_offset", 0) or 0)
        self.state: dict[str, Any] = {}
        self.market_cache: dict[int, tuple] = {}
        self.watch_items = self._build_watchlist()

    def _build_watchlist(self):
        scanner = self.cfg.get("scanner", {})
        categories = [str(x).lower() for x in scanner.get("categories", [])]
        explicit_names = {str(x).lower() for x in scanner.get("item_names", [])}
        explicit_ids = {int(x) for x in scanner.get("item_ids", [])}
        adds = set(self.store.get("watch_add", []))
        removes = set(self.store.get("watch_remove", []))
        rows = []
        for iid, item in self.catalog.items():
            typ = item["type"].lower(); name = item["name"].lower()
            chosen = iid in explicit_ids or name in explicit_names or iid in adds or any(c in typ or c in name for c in categories)
            if chosen and iid not in removes:
                rows.append(item)
        return rows[: int(scanner.get("max_items", self.s.max_items))]

    def _find_items(self, query: str):
        q = query.strip().lower()
        if not q:
            return []
        exact = [x for x in self.catalog.values() if x["name"].lower() == q]
        if exact:
            return exact
        return [x for x in self.catalog.values() if q in x["name"].lower()][:10]

    def _historical_ref(self, item_id):
        cut = ts() - int(float(self.cfg.get("historical_reference_hours", 24)) * 3600)
        rows = self.store.db.execute(
            "SELECT reference_price FROM price_samples WHERE item_id=? AND ts>=? ORDER BY ts DESC LIMIT 200",
            (item_id, cut),
        ).fetchall()
        vals = [float(r[0]) for r in rows if r[0]]
        return float(median(vals)) if len(vals) >= 3 else None

    def _market_data(self, item_id, refresh=False):
        cached = self.market_cache.get(item_id)
        if cached and not refresh and ts() - cached[0] < 60:
            return cached[1]
        listings, avg = self.torn.market(item_id, self.s.market_limit)
        hist = self._historical_ref(item_id)
        ref, confidence, support = Pricing.reference(listings, avg, hist, self.cfg) if listings else (None, 0.0, 0)
        result = (listings, avg, ref, confidence, support)
        self.market_cache[item_id] = (ts(), result)
        return result

    def main_menu(self):
        return KB([
            [B("🔥 Deals", "m:deals"), B("💼 Portfolio", "m:portfolio")],
            [B("➕ Add existing", "m:add"), B("💰 Sell", "m:sell")],
            [B("📊 Market", "m:market"), B("👀 Watchlist", "m:watch")],
            [B("⚙️ Settings", "m:settings"), B("⏯ Pause/Resume", "m:toggle")],
        ])

    def home(self, message_id=None):
        text = (
            "🤖 TORN DEAL FINDER PRO v7\n"
            f"Scanner: {'⏸ PAUSED' if self.store.get('paused', False) else '🟢 RUNNING'}\n"
            f"Watching: {len(self.watch_items)} items\n"
            f"Portfolio: {len(self.store.holdings())} item types\n\n"
            "Choose what you want to do:"
        )
        if message_id: self.tg.edit(message_id, text, self.main_menu())
        else: self.tg.send(text, self.main_menu())

    def portfolio(self, message_id=None):
        rows = self.store.holdings()
        if not rows:
            markup = KB([[B("➕ Add existing item", "m:add")], [B("⬅️ Menu", "m:home")]])
            text = "💼 Portfolio is empty.\nAdd what you already own or confirm future purchases from deal alerts."
            return self.tg.edit(message_id, text, markup) if message_id else self.tg.send(text, markup)
        fee = self._fee()
        invested = value = 0.0
        lines = ["💼 PORTFOLIO"]
        buttons = []
        for iid, name, qty, cost in rows[:15]:
            invested += qty * cost
            try:
                listings, _, ref, conf, _ = self._market_data(iid)
                px = ref or (listings[0]["price"] if listings else cost)
                net = px * (1 - fee / 100)
                pnl = (net - cost) * qty
                value += net * qty
                lines.append(f"• {name} ×{qty}: avg {money(cost)} | est P/L {money(pnl)}")
            except Exception:
                lines.append(f"• {name} ×{qty}: avg {money(cost)}")
            buttons.append([B(f"{name} ×{qty}", f"pos:{iid}")])
        lines += ["", f"Invested: {money(invested)}", f"Est. net value: {money(value)}", f"Unrealized P/L: {money(value-invested)}", f"Realized P/L: {money(self.store.realized_profit())}"]
        buttons += [[B("🔄 Refresh", "m:portfolio"), B("➕ Add existing", "m:add")], [B("⬅️ Menu", "m:home")]]
        markup = KB(buttons)
        text = "\n".join(lines)
        return self.tg.edit(message_id, text, markup) if message_id else self.tg.send(text, markup)

    def position(self, iid: int, message_id=None, refresh=False):
        item = self.catalog.get(iid); row = self.store.holding(iid)
        if not item or not row:
            return self.home(message_id)
        _, _, qty, cost = row
        listings, avg, ref, conf, support = self._market_data(iid, refresh=refresh)
        cheapest = listings[0]["price"] if listings else None
        undercut = int(self.cfg.get("selling", {}).get("undercut_amount", 1))
        suggested = max(1, int(cheapest - undercut)) if cheapest else int(ref or cost)
        if ref: suggested = min(suggested, int(ref))
        fee = self._fee(); net = suggested * (1 - fee / 100); pnl = (net - cost) * qty
        roi = ((net - cost) / cost * 100) if cost else 0.0
        status = "🟢 SELL CANDIDATE" if cost and roi >= self._target_exit_roi() and conf >= 50 else "🟡 HOLD / WATCH"
        text = (
            f"{status}\n{item['name']} ×{qty}\n"
            f"Average cost: {money(cost)}\n"
            f"Cheapest market: {money(cheapest)}\n"
            f"Suggested listing: {money(suggested)}\n"
            f"Reference: {money(ref)}\nConfidence: {conf:.0f}% | Support: {support}\n"
            f"Est. net P/L: {money(pnl)}\nEst. ROI: {roi:.1f}%"
        )
        markup = KB([
            [B("✅ Mark sold", f"sale:{iid}:{suggested}"), B("📊 Open market", url=market_link(iid))],
            [B("✏️ Edit holding", f"edit:{iid}"), B("🗑 Remove", f"removeask:{iid}")],
            [B("🔄 Refresh", f"refresh:{iid}"), B("⬅️ Portfolio", "m:portfolio")],
        ])
        return self.tg.edit(message_id, text, markup) if message_id else self.tg.send(text, markup)

    def deals(self, message_id=None):
        rows = self.store.recent_deals(10)
        text = "🔥 RECENT DEALS\n" + ("\n".join(f"• {n}: {money(b)} ×{q} | est {money(p)} | ROI {r:.1f}%" for _,n,b,q,p,r,_ in rows) if rows else "No deals recorded yet.")
        markup = KB([[B("⬅️ Menu", "m:home")]])
        return self.tg.edit(message_id, text, markup) if message_id else self.tg.send(text, markup)

    def sell_menu(self, message_id=None):
        rows = self.store.holdings()
        if not rows:
            markup = KB([[B("➕ Add existing", "m:add")], [B("⬅️ Menu", "m:home")]])
            text = "💰 Nothing to sell is tracked yet."
        else:
            markup = KB([[B(f"{name} ×{qty}", f"pos:{iid}")] for iid,name,qty,_ in rows[:20]] + [[B("⬅️ Menu", "m:home")]])
            text = "💰 SELECT AN ITEM TO REVIEW FOR SALE"
        return self.tg.edit(message_id, text, markup) if message_id else self.tg.send(text, markup)

    def watchlist(self, message_id=None):
        text = "👀 WATCHLIST\n" + "\n".join(f"• {x['name']} [{x['type']}]" for x in self.watch_items[:40])
        markup = KB([[B("➕ Add item", "watch:add"), B("➖ Remove item", "watch:remove")], [B("⬅️ Menu", "m:home")]])
        return self.tg.edit(message_id, text, markup) if message_id else self.tg.send(text, markup)

    def settings_menu(self, message_id=None):
        text = (
            "⚙️ SETTINGS\n"
            f"Max capital/deal: {money(self._budget())}\n"
            f"Min ROI: {self._min_roi():.1f}%\n"
            f"Min total profit: {money(self._min_profit())}\n"
            f"Target exit ROI: {self._target_exit_roi():.1f}%\n"
            f"Sale fee: {self._fee():.1f}%"
        )
        markup = KB([
            [B("💵 Budget", "set:budget"), B("📈 Min ROI", "set:minroi")],
            [B("💰 Min profit", "set:minprofit"), B("🎯 Exit ROI", "set:exitroi")],
            [B("🧾 Sale fee", "set:fee")], [B("⬅️ Menu", "m:home")],
        ])
        return self.tg.edit(message_id, text, markup) if message_id else self.tg.send(text, markup)

    def market_search(self, message_id=None):
        self.state = {"mode": "market_search"}
        text = "📊 MARKET SEARCH\nType an item name or part of the name. Example: Xanax"
        markup = KB([[B("⬅️ Menu", "m:home")]])
        return self.tg.edit(message_id, text, markup) if message_id else self.tg.send(text, markup)

    def market_snapshot(self, iid: int, message_id=None):
        item = self.catalog[iid]
        listings, avg, ref, conf, support = self._market_data(iid, refresh=True)
        cheapest = listings[0]["price"] if listings else None
        text = (
            f"📊 {item['name']} [{item['type']}]\n"
            f"Cheapest: {money(cheapest)}\nAPI average: {money(avg)}\nReference: {money(ref)}\n"
            f"Confidence: {conf:.0f}% | Support: {support}\nListings read: {len(listings)}"
        )
        markup = KB([[B("👀 Watch", f"watchone:{iid}"), B("🌐 Open market", url=market_link(iid))], [B("🔎 Search again", "m:market"), B("⬅️ Menu", "m:home")]])
        return self.tg.edit(message_id, text, markup) if message_id else self.tg.send(text, markup)

    def _fee(self): return float(self.store.get("fee", self.cfg.get("selling", {}).get("sale_fee_percent", self.cfg.get("sale_fee_percent", self.s.sale_fee_percent))))
    def _budget(self): return float(self.store.get("budget", self.cfg.get("bankroll", {}).get("max_capital_per_deal", self.s.max_capital_per_deal)))
    def _min_roi(self): return float(self.store.get("minroi", self.cfg.get("deal_rules", {}).get("min_roi_percent", self.s.min_roi_percent)))
    def _min_profit(self): return float(self.store.get("minprofit", self.cfg.get("deal_rules", {}).get("min_total_profit", self.s.min_total_profit)))
    def _target_exit_roi(self): return float(self.store.get("exitroi", self.cfg.get("selling", {}).get("target_profit_percent", self.s.target_exit_roi)))

    def _prompt_add_existing(self, message_id=None):
        self.state = {"mode": "add_search"}
        text = "➕ ADD EXISTING ITEM\nType the item name or part of it. I’ll show matching Torn items."
        markup = KB([[B("⬅️ Menu", "m:home")]])
        return self.tg.edit(message_id, text, markup) if message_id else self.tg.send(text, markup)

    def _handle_text(self, text: str):
        if text in ("/start", "/menu"):
            self.state = {}; return self.home()
        if text == "/pause": self.store.set("paused", True); return self.home()
        if text == "/resume": self.store.set("paused", False); return self.home()
        if text == "/cancel": self.state = {}; return self.home()
        mode = self.state.get("mode")
        if mode in ("add_search", "market_search", "watch_add_search", "watch_remove_search"):
            matches = self._find_items(text)
            if not matches:
                return self.tg.send("No matching item. Try another search, or /cancel.")
            prefix = {"add_search":"pickadd", "market_search":"pickmarket", "watch_add_search":"pickwatchadd", "watch_remove_search":"pickwatchremove"}[mode]
            self.tg.send("Select the item:", KB([[B(x["name"], f"{prefix}:{x['id']}")] for x in matches[:8]] + [[B("❌ Cancel", "m:home")]]))
            return
        if mode == "add_qty":
            qty = int(text.replace(",", ""));
            if qty <= 0: raise ValueError("Quantity must be positive")
            self.state["qty"] = qty; self.state["mode"] = "add_cost"
            return self.tg.send("What was your average purchase price per item? Enter 0 if unknown.")
        if mode == "add_cost":
            cost = parse_money(text); item = self.catalog[int(self.state["item"])]
            self.store.set_existing(item, int(self.state["qty"]), cost); self.state = {}
            self.tg.send(f"✅ Added\n{item['name']} ×{self.store.holding(item['id'])[2]}\nAverage cost: {money(cost)}")
            return self.portfolio()
        if mode == "edit_qty":
            qty = int(text.replace(",", "")); self.state["qty"] = qty; self.state["mode"] = "edit_cost"
            return self.tg.send("Enter the new average cost per item.")
        if mode == "edit_cost":
            item = self.catalog[int(self.state["item"])]; cost = parse_money(text)
            self.store.set_existing(item, int(self.state["qty"]), cost); self.state = {}
            return self.position(item["id"])
        if mode == "buy_qty":
            q = int(text.replace(",", "")); q = max(1, min(q, int(self.state["max"])))
            item = self.catalog[int(self.state["item"])]; price = float(self.state["price"])
            nq, nc = self.store.add_purchase(item, q, price, "confirmed_deal"); self.state = {}
            self.tg.send(f"✅ Purchase recorded\n{item['name']} +{q} @ {money(price)}\nNow hold: {nq}\nWeighted average: {money(nc)}")
            return self.portfolio()
        if mode == "sale_qty":
            q = int(text.replace(",", "")); self.state["qty"] = q; self.state["mode"] = "sale_price"
            return self.tg.send(f"Enter the actual sale price per item, or send the suggested price {money(self.state['suggested'])}.")
        if mode == "sale_price":
            item = self.catalog[int(self.state["item"])]; price = parse_money(text)
            q, realized, left = self.store.record_sale(item, int(self.state["qty"]), price, self._fee()); self.state = {}
            self.tg.send(f"✅ Sale recorded\n{item['name']} ×{q} @ {money(price)}\nRealized P/L: {money(realized)}\nRemaining: {left}")
            return self.portfolio()
        if mode == "setting":
            key = self.state["key"]; val = parse_money(text)
            self.store.set(key, val); self.state = {}
            return self.settings_menu()

    def _handle_callback(self, data: str, message_id: int):
        if data == "m:home": self.state = {}; return self.home(message_id)
        if data == "m:portfolio": return self.portfolio(message_id)
        if data == "m:deals": return self.deals(message_id)
        if data == "m:sell": return self.sell_menu(message_id)
        if data == "m:add": return self._prompt_add_existing(message_id)
        if data == "m:market": return self.market_search(message_id)
        if data == "m:watch": return self.watchlist(message_id)
        if data == "m:settings": return self.settings_menu(message_id)
        if data == "m:toggle":
            self.store.set("paused", not bool(self.store.get("paused", False))); return self.home(message_id)
        if data.startswith("pos:"): return self.position(int(data.split(":")[1]), message_id)
        if data.startswith("refresh:"): return self.position(int(data.split(":")[1]), message_id, True)
        if data.startswith("pickadd:"):
            iid = int(data.split(":")[1]); self.state = {"mode":"add_qty", "item":iid}
            return self.tg.edit(message_id, f"➕ {self.catalog[iid]['name']}\nHow many do you currently own?", KB([[B("❌ Cancel", "m:home")]]))
        if data.startswith("pickmarket:"): self.state = {}; return self.market_snapshot(int(data.split(":")[1]), message_id)
        if data.startswith("edit:"):
            iid = int(data.split(":")[1]); self.state = {"mode":"edit_qty", "item":iid}
            return self.tg.edit(message_id, f"✏️ Edit {self.catalog[iid]['name']}\nEnter the quantity you currently own.", KB([[B("❌ Cancel", "m:home")]]))
        if data.startswith("removeask:"):
            iid = int(data.split(":")[1])
            return self.tg.edit(message_id, f"Remove {self.catalog[iid]['name']} from the tracked portfolio?", KB([[B("✅ Remove", f"remove:{iid}"), B("❌ Keep", f"pos:{iid}")]]))
        if data.startswith("remove:"):
            self.store.remove_holding(int(data.split(":")[1])); return self.portfolio(message_id)
        if data.startswith("buy:"):
            _, iid, price, maxqty = data.split(":")
            self.state = {"mode":"buy_qty", "item":int(iid), "price":float(price), "max":int(maxqty)}
            return self.tg.edit(message_id, f"✅ Confirm purchase\n{self.catalog[int(iid)]['name']} at {money(float(price))}\nHow many did you actually buy? Max detected: {maxqty}", KB([[B("❌ Cancel", "m:home")]]))
        if data.startswith("sale:"):
            _, iid, suggested = data.split(":"); row = self.store.holding(int(iid))
            self.state = {"mode":"sale_qty", "item":int(iid), "suggested":float(suggested), "max":int(row[2])}
            return self.tg.edit(message_id, f"✅ Mark sale\n{self.catalog[int(iid)]['name']}\nHow many did you sell? You own {row[2]}.", KB([[B("❌ Cancel", f"pos:{iid}")]]))
        if data.startswith("set:"):
            k = data.split(":")[1]; labels={"budget":"maximum capital per deal", "minroi":"minimum ROI %", "minprofit":"minimum total profit", "exitroi":"target exit ROI %", "fee":"sale fee %"}
            self.state={"mode":"setting","key":k}; return self.tg.edit(message_id, f"Enter the new {labels[k]}.", KB([[B("❌ Cancel", "m:settings")]]))
        if data == "watch:add": self.state={"mode":"watch_add_search"}; return self.tg.edit(message_id,"Type an item name to add to the watchlist.",KB([[B("❌ Cancel","m:watch")]]))
        if data == "watch:remove": self.state={"mode":"watch_remove_search"}; return self.tg.edit(message_id,"Type an item name to remove from the watchlist.",KB([[B("❌ Cancel","m:watch")]]))
        if data.startswith("pickwatchadd:") or data.startswith("watchone:"):
            iid=int(data.split(":")[1]); adds=set(self.store.get("watch_add",[])); rem=set(self.store.get("watch_remove",[])); adds.add(iid); rem.discard(iid); self.store.set("watch_add",sorted(adds)); self.store.set("watch_remove",sorted(rem)); self.watch_items=self._build_watchlist(); self.state={}; return self.watchlist(message_id)
        if data.startswith("pickwatchremove:"):
            iid=int(data.split(":")[1]); adds=set(self.store.get("watch_add",[])); rem=set(self.store.get("watch_remove",[])); rem.add(iid); adds.discard(iid); self.store.set("watch_add",sorted(adds)); self.store.set("watch_remove",sorted(rem)); self.watch_items=self._build_watchlist(); self.state={}; return self.watchlist(message_id)

    def process_updates(self):
        try:
            updates = self.tg.updates(self.offset)
        except Exception:
            return
        for u in updates:
            self.offset = max(self.offset, int(u.get("update_id", 0)) + 1)
            cb = u.get("callback_query")
            msg = u.get("message") or (cb or {}).get("message") or {}
            chat_id = str((msg.get("chat") or {}).get("id", ""))
            if chat_id != self.tg.chat_id:
                continue
            try:
                if cb:
                    self.tg.ack(cb.get("id")); self._handle_callback(cb.get("data", ""), int(msg.get("message_id")))
                else:
                    text = (msg.get("text") or "").strip()
                    if text:
                        self._handle_text(text)
            except Exception as exc:
                self.tg.send(f"⚠️ Action error: {exc}\nUse /menu to recover.")
        self.store.set("telegram_offset", self.offset)

    def _scan_deals(self):
        candidates = []
        fee = self._fee()
        for item in list(self.watch_items):
            if STOP: break
            self.process_updates()
            try:
                listings, avg = self.torn.market(item["id"], self.s.market_limit)
                if not listings:
                    continue
                hist = self._historical_ref(item["id"])
                ref, conf, support = Pricing.reference(listings, avg, hist, self.cfg)
                if not ref:
                    continue
                self.market_cache[item["id"]] = (ts(), (listings, avg, ref, conf, support))
                self.store.db.execute(
                    "INSERT OR REPLACE INTO price_samples VALUES(?,?,?,?,?,?,?,?)",
                    (ts(), item["id"], item["name"], ref, listings[0]["price"], avg, conf, support),
                ); self.store.db.commit()
                if conf < float(self.cfg.get("deal_rules", {}).get("min_confidence_percent", 45)):
                    continue
                for l in listings[:10]:
                    q = min(l["quantity"], int(self.cfg.get("deal_rules", {}).get("max_quantity_to_value", 1000)))
                    capital = l["price"] * q
                    if capital > self._budget(): continue
                    profit_each = ref * (1 - fee / 100) - l["price"]
                    total = profit_each * q
                    roi = profit_each / l["price"] * 100
                    discount = (ref - l["price"]) / ref * 100
                    if roi < self._min_roi() or total < self._min_profit() or discount < float(self.cfg.get("deal_rules", {}).get("min_discount_percent", 7)):
                        continue
                    score = roi * .45 + conf * .30 + min(100, math.log10(max(total,10))*14) * .25
                    candidates.append((score,item,l,ref,conf,total,roi,discount))
            except Exception as exc:
                print(f"[SCAN ERROR] {item['name']}: {exc}", flush=True)
            time.sleep(self.s.per_item_delay)
        candidates.sort(reverse=True, key=lambda x:(x[0],x[5]))
        sent = 0
        cooldown = int(float(self.cfg.get("alert_cooldown_minutes",180))*60)
        for score,item,l,ref,conf,total,roi,discount in candidates:
            if sent >= int(self.cfg.get("max_alerts_per_cycle",5)): break
            k=f"deal:{item['id']}:{l['id']}:{l['price']}:{l['quantity']}"; old=self.store.db.execute("SELECT ts FROM alerts WHERE k=?",(k,)).fetchone()
            if old and ts()-old[0] < cooldown: continue
            text=(f"🔥 DEAL FOUND — {item['name']}\nBuy: {money(l['price'])} × {l['quantity']}\nCapital: {money(l['price']*l['quantity'])}\nReference: {money(ref)}\nDiscount: {discount:.1f}%\nEst. total profit: {money(total)}\nROI: {roi:.1f}%\nConfidence: {conf:.0f}%")
            markup=KB([[B("✅ I bought it",f"buy:{item['id']}:{l['price']}:{l['quantity']}"),B("📊 Market",url=market_link(item['id']))],[B("🙈 Ignore","noop")]])
            self.tg.send(text,markup)
            self.store.db.execute("INSERT OR REPLACE INTO alerts VALUES(?,?)",(k,ts()))
            self.store.db.execute("INSERT OR IGNORE INTO deals(ts,item_id,item_name,listing_key,buy_price,qty,reference_price,est_profit,roi,discount,confidence,score) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",(ts(),item['id'],item['name'],k,l['price'],l['quantity'],ref,total,roi,discount,conf,score));self.store.db.commit();sent+=1

    def _scan_exits(self):
        fee=self._fee();target=self._target_exit_roi()
        for iid,name,qty,cost in self.store.holdings()[:20]:
            if cost <= 0: continue
            try:
                listings,avg,ref,conf,support=self._market_data(iid,refresh=True)
                if not listings: continue
                px=ref or listings[0]["price"];net=px*(1-fee/100);roi=(net-cost)/cost*100
                if roi < target or conf < 50: continue
                bucket=int(px//max(1,px*.01));k=f"exit:{iid}:{bucket}";old=self.store.db.execute("SELECT ts FROM alerts WHERE k=?",(k,)).fetchone()
                if old and ts()-old[0] < 3600: continue
                self.tg.send(f"💰 EXIT OPPORTUNITY\n{name} ×{qty}\nAvg cost: {money(cost)}\nReference: {money(px)}\nEst. ROI after fee: {roi:.1f}%",KB([[B("💼 Review",f"pos:{iid}"),B("📊 Market",url=market_link(iid))]]));self.store.db.execute("INSERT OR REPLACE INTO alerts VALUES(?,?)",(k,ts()));self.store.db.commit()
            except Exception:
                pass

    def run(self):
        self.tg.send("✅ Torn Deal Finder Pro v7 started\nClean market → buy confirmation → portfolio → sell workflow enabled.\nUse /menu.", self.main_menu())
        while not STOP:
            self.process_updates()
            if self.store.get("paused", False):
                time.sleep(3); continue
            started=time.time();self.market_cache.clear();self._scan_deals();self._scan_exits()
            end=time.time()+max(5,self.s.scan_interval-(time.time()-started))
            while time.time()<end and not STOP:
                self.process_updates();time.sleep(2)


def load_config():
    with open(CONFIG_PATH,encoding="utf-8") as f:
        return json.load(f)


def main():
    cfg=load_config()
    key=os.getenv("TORN_API_KEY") or cfg.get("torn_api_key")
    token=os.getenv("TELEGRAM_BOT_TOKEN") or cfg.get("telegram",{}).get("bot_token")
    chat=os.getenv("TELEGRAM_CHAT_ID") or cfg.get("telegram",{}).get("chat_id")
    if not key or not token or not chat:
        raise SystemExit("Missing TORN_API_KEY, TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")
    s=Settings(
        scan_interval=max(60,int(cfg.get("scan_interval_seconds",300))),
        per_item_delay=max(.5,float(cfg.get("per_item_delay_seconds",1.2))),
        market_limit=int(cfg.get("market_limit",100)),
        sale_fee_percent=float(cfg.get("sale_fee_percent",5)),
        max_items=int(cfg.get("scanner",{}).get("max_items",60)),
    )
    store=Store(DB_PATH);torn=TornClient(key);tg=Telegram(token,str(chat));Bot(cfg,s,torn,tg,store).run()
