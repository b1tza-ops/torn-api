#!/usr/bin/env python3
"""Torn Deal Finder Pro v4 — read-only market intelligence + interactive Telegram control."""
import os, time, json, math, sqlite3, hashlib, signal
from pathlib import Path
from statistics import median
from datetime import datetime, timezone
import requests

API_BASE = "https://api.torn.com/v2"
CONFIG_FILE = Path(os.getenv("TORN_CONFIG", "config.json"))
DB_FILE = Path(os.getenv("TORN_DB", "torn_deals.sqlite3"))
STOP = False


def sig(*_):
    global STOP
    STOP = True


signal.signal(signal.SIGINT, sig)
signal.signal(signal.SIGTERM, sig)


def now(): return int(time.time())
def money(x): return f"${int(round(float(x))):,}"
def pct(x): return f"{float(x):.1f}%"


def load(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def api(path, key, params=None, retries=4):
    headers = {"Authorization": f"ApiKey {key}", "Accept": "application/json", "User-Agent": "TornDealFinderPro/4.0"}
    delay = 3
    for n in range(retries):
        try:
            r = requests.get(API_BASE + path, headers=headers, params=params or {}, timeout=25)
            if r.status_code == 429:
                time.sleep(max(delay, int(r.headers.get("Retry-After", delay))))
                delay *= 2
                continue
            r.raise_for_status()
            data = r.json()
            if isinstance(data, dict) and data.get("error"):
                raise RuntimeError(str(data["error"]))
            return data
        except Exception:
            if n == retries - 1:
                raise
            time.sleep(delay)
            delay *= 2


def find_list(obj, preferred=()):
    if isinstance(obj, dict):
        for k in preferred:
            v = obj.get(k)
            if isinstance(v, list): return v
            if isinstance(v, dict):
                for kk in ("items", "listings", "results"):
                    if isinstance(v.get(kk), list): return v[kk]
        for v in obj.values():
            z = find_list(v, preferred)
            if z: return z
    elif isinstance(obj, list):
        if obj and all(isinstance(x, dict) for x in obj): return obj
        for v in obj:
            z = find_list(v, preferred)
            if z: return z
    return []


def catalog(key):
    out = {}
    for x in find_list(api("/torn/items", key), ("items",)):
        i = x.get("id") or x.get("ID")
        name = x.get("name") or x.get("item_name")
        typ = x.get("type") or x.get("category") or x.get("item_type") or ""
        if i is not None and name:
            out[int(i)] = {"id": int(i), "name": str(name), "type": str(typ)}
    return out


def market(item_id, key, limit=100):
    payload = api(f"/market/{item_id}/itemmarket", key, params={"limit": limit})
    raw = find_list(payload, ("itemmarket", "item_market", "listings"))
    avg = None
    try:
        root = payload.get("itemmarket", {})
        avg = root.get("item", {}).get("average_price")
    except Exception:
        pass
    out = []
    for x in raw:
        p = x.get("price", x.get("cost", x.get("unit_price")))
        q = x.get("quantity", x.get("qty", x.get("amount", 1)))
        if not isinstance(p, (int, float)) or p <= 0: continue
        lid = x.get("id") or x.get("listing_id") or hashlib.sha1(json.dumps(x, sort_keys=True).encode()).hexdigest()[:18]
        seller = x.get("seller_id") or x.get("user_id") or x.get("seller")
        out.append({"id": str(lid), "price": int(p), "quantity": max(1, int(q or 1)), "seller": str(seller) if seller else None})
    return sorted(out, key=lambda x: x["price"]), (float(avg) if isinstance(avg, (int, float)) and avg > 0 else None)


def db():
    c = sqlite3.connect(DB_FILE)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("CREATE TABLE IF NOT EXISTS samples(ts INTEGER,item_id INTEGER,item_name TEXT,ref REAL,cheap INTEGER,avg REAL,confidence REAL,support INTEGER,PRIMARY KEY(ts,item_id))")
    c.execute("CREATE TABLE IF NOT EXISTS alerts(k TEXT PRIMARY KEY,ts INTEGER)")
    c.execute("CREATE TABLE IF NOT EXISTS deals(ts INTEGER,item_id INTEGER,item_name TEXT,buy INTEGER,qty INTEGER,ref REAL,profit REAL,total REAL,roi REAL,discount REAL,confidence REAL,score REAL,k TEXT UNIQUE)")
    c.execute("CREATE TABLE IF NOT EXISTS listing_state(item_id INTEGER,k TEXT,first_seen INTEGER,last_seen INTEGER,price INTEGER,PRIMARY KEY(item_id,k))")
    c.execute("CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY,v TEXT)")
    c.commit()
    return c


def get_setting(c, k, default=None):
    row = c.execute("SELECT v FROM settings WHERE k=?", (k,)).fetchone()
    if not row: return default
    try: return json.loads(row[0])
    except Exception: return row[0]


def set_setting(c, k, value):
    c.execute("INSERT OR REPLACE INTO settings(k,v) VALUES(?,?)", (k, json.dumps(value)))
    c.commit()


def historical_ref(c, item_id, hours=24):
    cut = now() - int(hours * 3600)
    rows = c.execute("SELECT ref FROM samples WHERE item_id=? AND ts>=? ORDER BY ts DESC LIMIT 300", (item_id, cut)).fetchall()
    vals = [float(r[0]) for r in rows if r[0]]
    return float(median(vals)) if len(vals) >= 3 else None


def update_presence(c, item_id, listings):
    t = now()
    for x in listings:
        k = f"{x['id']}:{x['price']}:{x['quantity']}"
        c.execute("INSERT INTO listing_state(item_id,k,first_seen,last_seen,price) VALUES(?,?,?,?,?) ON CONFLICT(item_id,k) DO UPDATE SET last_seen=excluded.last_seen", (item_id, k, t, t, x["price"]))
    c.commit()


def turnover_score(c, item_id, hours=6):
    cut = now() - int(hours * 3600)
    active_cut = now() - 900
    total = c.execute("SELECT COUNT(*) FROM listing_state WHERE item_id=? AND first_seen>=?", (item_id, cut)).fetchone()[0]
    disappeared = c.execute("SELECT COUNT(*) FROM listing_state WHERE item_id=? AND first_seen>=? AND last_seen<?", (item_id, cut, active_cut)).fetchone()[0]
    if total < 5: return 50.0
    return max(0.0, min(100.0, disappeared / total * 160.0))


def reference(listings, api_avg, hist, cfg):
    prices = [x["price"] for x in listings]
    minimum = int(cfg.get("min_listings_for_reference", 6))
    if len(prices) < minimum: return None, 0, 0, "too few listings"
    n = int(cfg.get("reference_top_n", 30)); skip = int(cfg.get("ignore_cheapest_for_reference", 2))
    sample = sorted(prices)[skip:skip+n]
    if len(sample) < 4: sample = sorted(prices)[:n]
    if len(sample) < 4: return None, 0, 0, "thin market"
    m = float(median(sample))
    dev = float(cfg.get("reference_max_deviation_percent", 22))
    trimmed = [p for p in sample if abs(p-m)/m*100 <= dev]
    if len(trimmed) >= 4: sample = trimmed; m = float(median(sample))
    support_band = float(cfg.get("support_band_percent", 7))
    support = sum(1 for p in prices if abs(p-m)/m*100 <= support_band)
    mean = sum(sample)/len(sample)
    cv = (sum((p-mean)**2 for p in sample)/len(sample))**0.5/mean if mean else 1
    stability = max(0, min(1, 1-cv*2.2))
    depth = min(1, support / max(5, int(cfg.get("min_support_listings", 5))))
    anchors = []
    if hist: anchors.append(hist)
    if api_avg: anchors.append(api_avg)
    anchor_score = 1.0
    if anchors:
        anchor = median(anchors)
        diff = abs(m-anchor)/anchor*100
        anchor_score = max(0, 1-diff/float(cfg.get("max_anchor_deviation_percent", 35)))
    confidence = (0.42*stability + 0.38*depth + 0.20*anchor_score)*100
    if hist:
        max_jump = float(cfg.get("max_reference_jump_percent", 30))
        jump = abs(m-hist)/hist*100
        if jump > max_jump:
            return None, confidence, support, f"reference jump {jump:.1f}%"
        w = float(cfg.get("live_reference_weight", .7))
        m = m*w + hist*(1-w)
    return m, confidence, support, "ok"


def build_watch(cat, cfg, c):
    s = cfg.get("scanner", {})
    cats = [str(x).lower() for x in s.get("categories", [])]
    names = {str(x).lower() for x in s.get("item_names", [])}
    ids = {int(x) for x in s.get("item_ids", [])}
    runtime_add = set(get_setting(c, "watch_add", [])); runtime_remove = set(get_setting(c, "watch_remove", []))
    out = []
    for iid, x in cat.items():
        typ = x["type"].lower(); name = x["name"].lower()
        chosen = iid in ids or name in names or any(k in typ or k in name for k in cats) or iid in runtime_add
        if chosen and iid not in runtime_remove: out.append(x)
    return out[:int(s.get("max_items", 80))]


def rules_for(item, cfg, c):
    r = dict(cfg.get("deal_rules", {}))
    typ = item["type"].lower()
    for k,v in cfg.get("type_rules", {}).items():
        if k.lower() in typ: r.update(v)
    budget = get_setting(c, "budget", None)
    minprofit = get_setting(c, "minprofit", None)
    minroi = get_setting(c, "minroi", None)
    if budget is not None: r["max_total_cost"] = float(budget)
    if minprofit is not None: r["min_total_profit"] = float(minprofit)
    if minroi is not None: r["min_roi_percent"] = float(minroi)
    return r


def evaluate(item, listings, ref, conf, support, turnover, cfg, c):
    r = rules_for(item, cfg, c)
    fee = float(r.get("sale_fee_percent", cfg.get("sale_fee_percent", 5))) / 100
    min_roi = float(r.get("min_roi_percent", 7)); min_each = float(r.get("min_profit_per_item", 3000)); min_total = float(r.get("min_total_profit", 15000)); min_disc = float(r.get("min_discount_percent", 7)); min_conf = float(r.get("min_confidence_percent", 45))
    min_support = int(r.get("min_support_listings", cfg.get("min_support_listings", 5)))
    maxcost = float(r.get("max_total_cost", cfg.get("bankroll", {}).get("max_capital_per_deal", 5000000)))
    maxqty = int(r.get("max_quantity_to_value", 1000))
    if conf < min_conf or support < min_support: return []
    net_sale = ref*(1-fee); out=[]
    for l in listings:
        q=min(l["quantity"],maxqty); cost=l["price"]*q
        if cost>maxcost: continue
        profit=net_sale-l["price"]; total=profit*q; roi=profit/l["price"]*100; disc=(ref-l["price"])/ref*100
        if roi<min_roi or profit<min_each or total<min_total or disc<min_disc: continue
        capital_eff = min(100, max(0, total/max(cost,1)*100*4))
        score = roi*.34 + conf*.24 + turnover*.18 + capital_eff*.14 + min(100, math.log10(max(total,10))*14)*.10
        out.append({**l,"ref":ref,"profit":profit,"total":total,"roi":roi,"discount":disc,"confidence":conf,"support":support,"turnover":turnover,"cost":cost,"score":score})
    return sorted(out,key=lambda d:(d["score"],d["total"]),reverse=True)


def tg(token, chat, text):
    if not token or not chat: print(text, flush=True); return
    requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id":chat,"text":text,"disable_web_page_preview":True}, timeout=20).raise_for_status()


def get_updates(token, offset):
    if not token: return []
    r=requests.get(f"https://api.telegram.org/bot{token}/getUpdates",params={"offset":offset,"timeout":0,"allowed_updates":"message"},timeout=10)
    r.raise_for_status(); return r.json().get("result",[])


def item_by_name(cat, text):
    q=text.strip().lower()
    exact=[x for x in cat.values() if x["name"].lower()==q]
    if exact:return exact[0]
    partial=[x for x in cat.values() if q in x["name"].lower()]
    return partial[0] if len(partial)==1 else None


def cmd_help():
    return ("🤖 Torn Deal Finder Pro v4\n"
            "/status — scanner state\n/top — best recent deals\n/history <item> — recent reference prices\n"
            "/watch <item> — add item\n/unwatch <item> — remove item\n/watchlist — show watchlist\n"
            "/budget <amount> — max capital per deal\n/minprofit <amount> — minimum total profit\n/minroi <percent> — minimum ROI\n"
            "/pause — pause scanning\n/resume — resume scanning\n/settings — show runtime settings\n/help")


def process_commands(token, chat, offset, cat, cfg, c, watch_items):
    try: updates=get_updates(token,offset)
    except Exception: return offset, watch_items
    for u in updates:
        offset=max(offset,u.get("update_id",0)+1)
        m=u.get("message") or {}; cid=str((m.get("chat") or {}).get("id","")); text=(m.get("text") or "").strip()
        if not text or (chat and cid!=str(chat)): continue
        parts=text.split(maxsplit=1); cmd=parts[0].split("@")[0].lower(); arg=parts[1] if len(parts)>1 else ""
        try:
            if cmd in ("/start","/help"): tg(token,chat,cmd_help())
            elif cmd=="/status":
                paused=bool(get_setting(c,"paused",False)); tg(token,chat,f"💚 Status: {'PAUSED' if paused else 'RUNNING'}\nWatching: {len(watch_items)} items\nDB: {DB_FILE.name}")
            elif cmd=="/top":
                rows=c.execute("SELECT item_name,buy,total,roi,score,ts FROM deals ORDER BY ts DESC,score DESC LIMIT 8").fetchall()
                tg(token,chat,"🏆 Recent top deals\n"+"\n".join(f"• {n}: buy {money(b)}, profit {money(t)}, ROI {r:.1f}%, score {s:.1f}" for n,b,t,r,s,_ in rows) if rows else "No deals recorded yet.")
            elif cmd=="/history":
                item=item_by_name(cat,arg)
                if not item: tg(token,chat,"Item not found or ambiguous."); continue
                rows=c.execute("SELECT ts,ref FROM samples WHERE item_id=? ORDER BY ts DESC LIMIT 12",(item["id"],)).fetchall()
                tg(token,chat,f"📈 {item['name']} history\n"+"\n".join(f"{datetime.fromtimestamp(ts).strftime('%H:%M')} — {money(r)}" for ts,r in rows) if rows else "No history yet.")
            elif cmd in ("/watch","/unwatch"):
                item=item_by_name(cat,arg)
                if not item: tg(token,chat,"Item not found or ambiguous. Use a more exact name."); continue
                add=set(get_setting(c,"watch_add",[])); rem=set(get_setting(c,"watch_remove",[]))
                if cmd=="/watch": add.add(item["id"]); rem.discard(item["id"])
                else: rem.add(item["id"]); add.discard(item["id"])
                set_setting(c,"watch_add",sorted(add)); set_setting(c,"watch_remove",sorted(rem)); watch_items=build_watch(cat,cfg,c); tg(token,chat,f"✅ {item['name']} {'added' if cmd=='/watch' else 'removed'}. Watching {len(watch_items)} items.")
            elif cmd=="/watchlist": tg(token,chat,"👀 Watchlist\n"+"\n".join(f"• {x['name']}" for x in watch_items[:40])+("\n…" if len(watch_items)>40 else ""))
            elif cmd in ("/budget","/minprofit","/minroi"):
                val=float(arg.replace(",","").replace("$","")); key=cmd[1:]; set_setting(c,key,val); tg(token,chat,f"✅ {key} set to {money(val) if key!='minroi' else pct(val)}")
            elif cmd=="/pause": set_setting(c,"paused",True); tg(token,chat,"⏸ Scanner paused. Telegram commands still work.")
            elif cmd=="/resume": set_setting(c,"paused",False); tg(token,chat,"▶️ Scanner resumed.")
            elif cmd=="/settings":
                tg(token,chat,f"⚙️ Settings\nBudget: {money(get_setting(c,'budget',cfg.get('bankroll',{}).get('max_capital_per_deal',5000000)))}\nMin profit: {money(get_setting(c,'minprofit',cfg.get('deal_rules',{}).get('min_total_profit',15000)))}\nMin ROI: {pct(get_setting(c,'minroi',cfg.get('deal_rules',{}).get('min_roi_percent',7)))}")
        except Exception as e: tg(token,chat,f"Command error: {e}")
    return offset, watch_items


def render(item,d):
    return (f"🔥 TORN DEAL FOUND\n{item['name']} | {item['type']}\n"
            f"Buy: {money(d['price'])} × {d['quantity']:,}\nCapital: {money(d['cost'])}\nReference: {money(d['ref'])}\n"
            f"Discount: {d['discount']:.1f}%\nEst. profit: {money(d['profit'])} each\nEst. total: {money(d['total'])}\nROI: {d['roi']:.1f}%\n"
            f"Confidence: {d['confidence']:.0f}% | Support: {d['support']} listings\nTurnover score: {d['turnover']:.0f}/100 | Deal score: {d['score']:.1f}\n"
            f"https://www.torn.com/page.php?sid=ItemMarket#/market/view=search&itemID={item['id']}")


def main():
    cfg=load(CONFIG_FILE)
    if not cfg: raise SystemExit("Copy config.example.json to config.json first.")
    key=os.getenv("TORN_API_KEY") or cfg.get("torn_api_key")
    if not key: raise SystemExit("Missing TORN_API_KEY.")
    token=os.getenv("TELEGRAM_BOT_TOKEN") or cfg.get("telegram",{}).get("bot_token")
    chat=os.getenv("TELEGRAM_CHAT_ID") or cfg.get("telegram",{}).get("chat_id")
    c=db(); cat=catalog(key); items=build_watch(cat,cfg,c)
    if not items: raise SystemExit("No watchlist items matched.")
    offset=int(get_setting(c,"telegram_offset",0) or 0)
    tg(token,chat,f"✅ Torn Deal Finder Pro v4 started\nWatching {len(items)} items\nUse /help for commands")
    cycle=0
    while not STOP:
        offset,items=process_commands(token,chat,offset,cat,cfg,c,items); set_setting(c,"telegram_offset",offset)
        if get_setting(c,"paused",False): time.sleep(5); continue
        cycle+=1; started=time.time(); candidates=[]
        for item in items:
            if STOP: break
            offset,items=process_commands(token,chat,offset,cat,cfg,c,items); set_setting(c,"telegram_offset",offset)
            try:
                ls,api_avg=market(item["id"],key,int(cfg.get("market_limit",100)))
                if not ls: continue
                update_presence(c,item["id"],ls)
                hist=historical_ref(c,item["id"],float(cfg.get("historical_reference_hours",24)))
                ref,conf,support,reason=reference(ls,api_avg,hist,cfg)
                if not ref: print(f"[SKIP] {item['name']}: {reason}",flush=True); continue
                turn=turnover_score(c,item["id"],float(cfg.get("turnover_window_hours",6)))
                c.execute("INSERT OR REPLACE INTO samples VALUES(?,?,?,?,?,?,?,?)",(now(),item["id"],item["name"],ref,ls[0]["price"],api_avg,conf,support)); c.commit()
                for d in evaluate(item,ls,ref,conf,support,turn,cfg,c): candidates.append((item,d))
            except Exception as e: print(f"[ERROR] {item['name']}: {e}",flush=True)
            time.sleep(float(cfg.get("per_item_delay_seconds",1.2)))
        candidates.sort(key=lambda z:(z[1]["score"],z[1]["total"]),reverse=True)
        sent=0; cooldown=float(cfg.get("alert_cooldown_minutes",180))*60
        for item,d in candidates:
            if sent>=int(cfg.get("max_alerts_per_cycle",5)): break
            k=f"{item['id']}:{d['id']}:{d['price']}:{d['quantity']}"; row=c.execute("SELECT ts FROM alerts WHERE k=?",(k,)).fetchone()
            if row and now()-row[0]<cooldown: continue
            tg(token,chat,render(item,d)); c.execute("INSERT OR REPLACE INTO alerts VALUES(?,?)",(k,now())); c.execute("INSERT OR IGNORE INTO deals VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",(now(),item["id"],item["name"],d["price"],d["quantity"],d["ref"],d["profit"],d["total"],d["roi"],d["discount"],d["confidence"],d["score"],k)); c.commit(); sent+=1
        print(f"Cycle {cycle}: {len(candidates)} candidates, {sent} alerts",flush=True)
        interval=max(45,int(cfg.get("scan_interval_seconds",300))); end=time.time()+max(5,interval-(time.time()-started))
        while time.time()<end and not STOP:
            offset,items=process_commands(token,chat,offset,cat,cfg,c,items); set_setting(c,"telegram_offset",offset); time.sleep(3)
    c.close()


if __name__=="__main__": main()
