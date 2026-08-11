#!/usr/bin/env python3
"""Torn Deal Finder Pro v5 — read-only market intelligence + interactive Telegram UI."""
import os, time, json, math, sqlite3, hashlib, signal
from pathlib import Path
from statistics import median
import requests

API_BASE = "https://api.torn.com/v2"
CONFIG_FILE = Path(os.getenv("TORN_CONFIG", "config.json"))
DB_FILE = Path(os.getenv("TORN_DB", "torn_deals.sqlite3"))
STOP = False

def sig(*_):
    global STOP
    STOP = True
signal.signal(signal.SIGINT, sig); signal.signal(signal.SIGTERM, sig)
def now(): return int(time.time())
def money(x): return f"${int(round(float(x))):,}"
def pct(x): return f"{float(x):.1f}%"

def load(path):
    try:
        with open(path, encoding="utf-8") as f: return json.load(f)
    except FileNotFoundError: return None

def api(path, key, params=None, retries=4):
    headers={"Authorization":f"ApiKey {key}","Accept":"application/json","User-Agent":"TornDealFinderPro/5.0"}; delay=3
    for n in range(retries):
        try:
            r=requests.get(API_BASE+path,headers=headers,params=params or {},timeout=25)
            if r.status_code==429:
                time.sleep(max(delay,int(r.headers.get("Retry-After",delay)))); delay*=2; continue
            r.raise_for_status(); data=r.json()
            if isinstance(data,dict) and data.get("error"): raise RuntimeError(str(data["error"]))
            return data
        except Exception:
            if n==retries-1: raise
            time.sleep(delay); delay*=2

def find_list(obj, preferred=()):
    if isinstance(obj,dict):
        for k in preferred:
            v=obj.get(k)
            if isinstance(v,list): return v
            if isinstance(v,dict):
                for kk in ("items","listings","results"):
                    if isinstance(v.get(kk),list): return v[kk]
        for v in obj.values():
            z=find_list(v,preferred)
            if z:return z
    elif isinstance(obj,list):
        if obj and all(isinstance(x,dict) for x in obj): return obj
        for v in obj:
            z=find_list(v,preferred)
            if z:return z
    return []

def catalog(key):
    out={}
    for x in find_list(api("/torn/items",key),("items",)):
        iid=x.get("id") or x.get("ID"); name=x.get("name") or x.get("item_name"); typ=x.get("type") or x.get("category") or x.get("item_type") or "Other"
        if iid is not None and name: out[int(iid)]={"id":int(iid),"name":str(name),"type":str(typ)}
    return out

def market(item_id,key,limit=100):
    payload=api(f"/market/{item_id}/itemmarket",key,params={"limit":limit}); raw=find_list(payload,("itemmarket","item_market","listings")); avg=None
    try: avg=payload.get("itemmarket",{}).get("item",{}).get("average_price")
    except Exception: pass
    out=[]
    for x in raw:
        p=x.get("price",x.get("cost",x.get("unit_price"))); q=x.get("quantity",x.get("qty",x.get("amount",1)))
        if not isinstance(p,(int,float)) or p<=0: continue
        lid=x.get("id") or x.get("listing_id") or hashlib.sha1(json.dumps(x,sort_keys=True).encode()).hexdigest()[:18]
        out.append({"id":str(lid),"price":int(p),"quantity":max(1,int(q or 1))})
    return sorted(out,key=lambda x:x["price"]),(float(avg) if isinstance(avg,(int,float)) and avg>0 else None)

def db():
    c=sqlite3.connect(DB_FILE); c.execute("PRAGMA journal_mode=WAL")
    c.execute("CREATE TABLE IF NOT EXISTS samples(ts INTEGER,item_id INTEGER,item_name TEXT,ref REAL,cheap INTEGER,avg REAL,confidence REAL,support INTEGER,PRIMARY KEY(ts,item_id))")
    c.execute("CREATE TABLE IF NOT EXISTS alerts(k TEXT PRIMARY KEY,ts INTEGER)")
    c.execute("CREATE TABLE IF NOT EXISTS deals(ts INTEGER,item_id INTEGER,item_name TEXT,buy INTEGER,qty INTEGER,ref REAL,profit REAL,total REAL,roi REAL,discount REAL,confidence REAL,score REAL,k TEXT UNIQUE)")
    c.execute("CREATE TABLE IF NOT EXISTS listing_state(item_id INTEGER,k TEXT,first_seen INTEGER,last_seen INTEGER,price INTEGER,PRIMARY KEY(item_id,k))")
    c.execute("CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY,v TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS holdings(item_id INTEGER PRIMARY KEY,item_name TEXT,qty INTEGER,cost_each REAL,updated_ts INTEGER)")
    c.commit(); return c

def get_setting(c,k,default=None):
    row=c.execute("SELECT v FROM settings WHERE k=?",(k,)).fetchone()
    if not row:return default
    try:return json.loads(row[0])
    except Exception:return row[0]
def set_setting(c,k,value): c.execute("INSERT OR REPLACE INTO settings(k,v) VALUES(?,?)",(k,json.dumps(value))); c.commit()

def historical_ref(c,item_id,hours=24):
    cut=now()-int(hours*3600); rows=c.execute("SELECT ref FROM samples WHERE item_id=? AND ts>=? ORDER BY ts DESC LIMIT 300",(item_id,cut)).fetchall(); vals=[float(r[0]) for r in rows if r[0]]
    return float(median(vals)) if len(vals)>=3 else None

def reference(listings,api_avg,hist,cfg):
    prices=[x["price"] for x in listings]
    if len(prices)<int(cfg.get("min_listings_for_reference",6)): return None,0,0,"too few listings"
    n=int(cfg.get("reference_top_n",30)); skip=int(cfg.get("ignore_cheapest_for_reference",2)); sample=sorted(prices)[skip:skip+n]
    if len(sample)<4: sample=sorted(prices)[:n]
    if len(sample)<4:return None,0,0,"thin market"
    m=float(median(sample)); dev=float(cfg.get("reference_max_deviation_percent",22)); trimmed=[p for p in sample if abs(p-m)/m*100<=dev]
    if len(trimmed)>=4: sample=trimmed; m=float(median(sample))
    band=float(cfg.get("support_band_percent",7)); support=sum(1 for p in prices if abs(p-m)/m*100<=band); mean=sum(sample)/len(sample); cv=(sum((p-mean)**2 for p in sample)/len(sample))**.5/mean if mean else 1
    stability=max(0,min(1,1-cv*2.2)); depth=min(1,support/max(5,int(cfg.get("min_support_listings",5)))); anchors=[x for x in (hist,api_avg) if x]; anchor_score=1.0
    if anchors:
        anchor=median(anchors); diff=abs(m-anchor)/anchor*100; anchor_score=max(0,1-diff/float(cfg.get("max_anchor_deviation_percent",35)))
    conf=(.42*stability+.38*depth+.20*anchor_score)*100
    if hist:
        jump=abs(m-hist)/hist*100
        if jump>float(cfg.get("max_reference_jump_percent",30)): return None,conf,support,f"reference jump {jump:.1f}%"
        w=float(cfg.get("live_reference_weight",.7)); m=m*w+hist*(1-w)
    return m,conf,support,"ok"

def build_watch(cat,cfg,c):
    s=cfg.get("scanner",{}); cats=[str(x).lower() for x in s.get("categories",[])]; names={str(x).lower() for x in s.get("item_names",[])}; ids={int(x) for x in s.get("item_ids",[])}; adds=set(get_setting(c,"watch_add",[])); removes=set(get_setting(c,"watch_remove",[])); out=[]
    for iid,x in cat.items():
        typ=x["type"].lower(); name=x["name"].lower(); chosen=iid in ids or name in names or any(k in typ or k in name for k in cats) or iid in adds
        if chosen and iid not in removes: out.append(x)
    return out[:int(s.get("max_items",80))]

def evaluate(item,listings,ref,conf,support,cfg,c):
    r=dict(cfg.get("deal_rules",{})); typ=item["type"].lower()
    for k,v in cfg.get("type_rules",{}).items():
        if k.lower() in typ:r.update(v)
    budget=get_setting(c,"budget"); minprofit=get_setting(c,"minprofit"); minroi=get_setting(c,"minroi")
    if budget is not None:r["max_total_cost"]=float(budget)
    if minprofit is not None:r["min_total_profit"]=float(minprofit)
    if minroi is not None:r["min_roi_percent"]=float(minroi)
    if conf<float(r.get("min_confidence_percent",45)) or support<int(r.get("min_support_listings",cfg.get("min_support_listings",5))): return []
    fee=float(cfg.get("sale_fee_percent",5))/100; maxcost=float(r.get("max_total_cost",cfg.get("bankroll",{}).get("max_capital_per_deal",5000000))); out=[]
    for l in listings:
        q=min(l["quantity"],int(r.get("max_quantity_to_value",1000))); cost=l["price"]*q
        if cost>maxcost:continue
        profit=ref*(1-fee)-l["price"]; total=profit*q; roi=profit/l["price"]*100; disc=(ref-l["price"])/ref*100
        if roi<float(r.get("min_roi_percent",7)) or profit<float(r.get("min_profit_per_item",3000)) or total<float(r.get("min_total_profit",15000)) or disc<float(r.get("min_discount_percent",7)):continue
        score=roi*.45+conf*.30+min(100,math.log10(max(total,10))*14)*.25
        out.append({**l,"ref":ref,"profit":profit,"total":total,"roi":roi,"discount":disc,"confidence":conf,"support":support,"cost":cost,"score":score})
    return sorted(out,key=lambda d:(d["score"],d["total"]),reverse=True)

def tg(token,chat,text,markup=None):
    if not token or not chat: print(text,flush=True); return
    payload={"chat_id":chat,"text":text,"disable_web_page_preview":True}
    if markup:payload["reply_markup"]=markup
    requests.post(f"https://api.telegram.org/bot{token}/sendMessage",json=payload,timeout=20).raise_for_status()
def answer_cb(token,cbid):
    if token and cbid: requests.post(f"https://api.telegram.org/bot{token}/answerCallbackQuery",json={"callback_query_id":cbid},timeout=10)
def get_updates(token,offset):
    if not token:return []
    r=requests.get(f"https://api.telegram.org/bot{token}/getUpdates",params={"offset":offset,"timeout":0,"allowed_updates":json.dumps(["message","callback_query"])},timeout=10); r.raise_for_status(); return r.json().get("result",[])
def kb(rows):return {"inline_keyboard":rows}
def btn(text,data=None,url=None):
    x={"text":text}; x["url" if url else "callback_data"]=url if url else data; return x
def back(target="main"):return [btn("⬅️ Back",f"menu:{target}")]
def main_menu():return kb([[btn("🔥 Deals","menu:deals"),btn("💰 Sell","menu:sell")],[btn("📦 Inventory","menu:inventory"),btn("👀 Watchlist","menu:watch")],[btn("📊 Market","menu:market"),btn("⚙️ Settings","menu:settings")]])
def item_by_name(cat,text):
    q=text.strip().lower(); exact=[x for x in cat.values() if x["name"].lower()==q]
    if exact:return exact[0]
    partial=[x for x in cat.values() if q in x["name"].lower()]
    return partial[0] if len(partial)==1 else None
def holding_rows(c):return c.execute("SELECT item_id,item_name,qty,cost_each FROM holdings WHERE qty>0 ORDER BY item_name").fetchall()
def set_holding(c,item,qty,cost):c.execute("INSERT OR REPLACE INTO holdings VALUES(?,?,?,?,?)",(item["id"],item["name"],qty,float(cost or 0),now()));c.commit()
def suggested_sale(item,c,cfg,key,undercut=None):
    row=c.execute("SELECT qty,cost_each FROM holdings WHERE item_id=?",(item["id"],)).fetchone()
    if not row:return None
    qty,cost=int(row[0]),float(row[1] or 0); ls,avg=market(item["id"],key,int(cfg.get("market_limit",100)))
    if not ls:return {"qty":qty,"cost":cost,"error":"No market listings"}
    hist=historical_ref(c,item["id"],float(cfg.get("historical_reference_hours",24))); ref,conf,support,reason=reference(ls,avg,hist,cfg)
    step=int(cfg.get("selling",{}).get("undercut_amount",1) if undercut is None else undercut); suggested=max(1,ls[0]["price"]-step)
    if ref:suggested=min(suggested,int(ref))
    fee=float(cfg.get("selling",{}).get("sale_fee_percent",cfg.get("sale_fee_percent",5)))/100; net=suggested*(1-fee); profit=net-cost
    return {"qty":qty,"cost":cost,"cheap":ls[0]["price"],"suggested":suggested,"ref":ref,"conf":conf,"support":support,"net":net,"profit":profit,"total":profit*qty}
def sale_card(item,d):
    if not d:return "Item is not in your tracked inventory."
    if d.get("error"):return f"💰 {item['name']}\n{d['error']}"
    lines=[f"💰 SELL ASSISTANT — {item['name']}",f"You hold: {d['qty']:,}",f"Avg cost: {money(d['cost'])}",f"Current cheapest: {money(d['cheap'])}",f"Suggested listing: {money(d['suggested'])}"]
    if d.get("ref"):lines += [f"Reference: {money(d['ref'])}",f"Confidence: {d['conf']:.0f}% | Support: {d['support']}"]
    lines += [f"Est. net after fee: {money(d['net'])} each",f"Est. profit: {money(d['profit'])} each",f"Est. total profit: {money(d['total'])}"]
    if d["profit"]<0:lines.append("⚠️ Below your tracked cost after fees.")
    return "\n".join(lines)
def menu_home(token,chat,c,items):tg(token,chat,f"🤖 TORN DEAL FINDER PRO v5\nStatus: {'⏸ PAUSED' if get_setting(c,'paused',False) else '🟢 RUNNING'}\nWatching: {len(items)} items\nInventory: {len(holding_rows(c))} item types\n\nChoose an option:",main_menu())
def show_inventory(token,chat,c,page=1):
    rows=holding_rows(c)
    if not rows:tg(token,chat,"📦 Inventory is empty.",kb([[btn("➕ Add holding","inv:add")],back()]));return
    per=8;pages=max(1,math.ceil(len(rows)/per));page=max(1,min(page,pages));chunk=rows[(page-1)*per:page*per]; buttons=[[btn(f"{name} ×{qty}",f"inv:item:{iid}")] for iid,name,qty,cost in chunk];nav=[]
    if page>1:nav.append(btn("◀️",f"inv:page:{page-1}"))
    if page<pages:nav.append(btn("▶️",f"inv:page:{page+1}"))
    if nav:buttons.append(nav)
    buttons += [[btn("➕ Add holding","inv:add"),btn("💰 Sell items","menu:sell")],back()]; tg(token,chat,f"📦 TRACKED INVENTORY\nPage {page}/{pages}",kb(buttons))
def show_sell(token,chat,c,page=1):
    rows=holding_rows(c)
    if not rows:tg(token,chat,"💰 Add holdings first.",kb([[btn("➕ Add holding","inv:add")],back()]));return
    per=8;pages=max(1,math.ceil(len(rows)/per));page=max(1,min(page,pages));chunk=rows[(page-1)*per:page*per]; buttons=[[btn(f"💰 {name} ×{qty}",f"sell:item:{iid}")] for iid,name,qty,cost in chunk];nav=[]
    if page>1:nav.append(btn("◀️",f"sell:page:{page-1}"))
    if page<pages:nav.append(btn("▶️",f"sell:page:{page+1}"))
    if nav:buttons.append(nav)
    buttons += [[btn("📋 Sell plan","sell:plan")],back()];tg(token,chat,f"💰 CHOOSE AN ITEM TO SELL\nPage {page}/{pages}",kb(buttons))
def show_settings(token,chat,c,cfg):
    paused=bool(get_setting(c,"paused",False)); budget=get_setting(c,"budget",cfg.get("bankroll",{}).get("max_capital_per_deal",5000000)); minprofit=get_setting(c,"minprofit",cfg.get("deal_rules",{}).get("min_total_profit",15000)); minroi=get_setting(c,"minroi",cfg.get("deal_rules",{}).get("min_roi_percent",7))
    rows=[[btn("▶️ Resume" if paused else "⏸ Pause","set:resume" if paused else "set:pause")],[btn("💵 Budget","set:budget"),btn("📈 Min ROI","set:minroi")],[btn("💰 Min profit","set:minprofit")],back()];tg(token,chat,f"⚙️ SETTINGS\nBudget: {money(budget)}\nMin profit: {money(minprofit)}\nMin ROI: {pct(minroi)}",kb(rows))
def process_pending(token,chat,text,cat,c):
    p=get_setting(c,"pending_input")
    if not p:return False
    try:
        if p["kind"]=="add_item":
            item=item_by_name(cat,text)
            if not item:tg(token,chat,"Item not found. Send exact name or /cancel.");return True
            set_setting(c,"pending_input",{"kind":"add_qty","item_id":item["id"]});tg(token,chat,f"Selected {item['name']}. How many do you own?")
        elif p["kind"]=="add_qty":
            q=int(text.replace(",",""));set_setting(c,"pending_input",{"kind":"add_cost","item_id":p["item_id"],"qty":q});tg(token,chat,"Average cost per item? Send 0 if unknown.")
        elif p["kind"]=="add_cost":
            cost=float(text.replace(",","").replace("$",""));item=cat[int(p["item_id"])];set_holding(c,item,int(p["qty"]),cost);set_setting(c,"pending_input",None);tg(token,chat,f"✅ Added {p['qty']} × {item['name']}.",kb([[btn("💰 Sell it",f"sell:item:{item['id']}")],[btn("📦 Inventory","menu:inventory")],back()]))
        elif p["kind"] in ("budget","minroi","minprofit"):
            val=float(text.replace(",","").replace("$","").replace("%",""));set_setting(c,p["kind"],val);set_setting(c,"pending_input",None);tg(token,chat,"✅ Updated.",kb([[btn("⚙️ Settings","menu:settings")],back()]))
        elif p["kind"]=="find":
            q=text.lower();rows=[x for x in cat.values() if q in x["name"].lower()][:12];set_setting(c,"pending_input",None);buttons=[[btn(x["name"],f"market:item:{x['id']}")] for x in rows];buttons.append(back("market"));tg(token,chat,"🔎 Choose an item:" if rows else "No matches.",kb(buttons))
        return True
    except Exception:tg(token,chat,"Invalid value. Try again or /cancel.");return True
def handle_cb(token,chat,cb,cat,cfg,c,items,key):
    d=cb.get("data","");answer_cb(token,cb.get("id"))
    if d=="menu:main":menu_home(token,chat,c,items)
    elif d=="menu:inventory":show_inventory(token,chat,c)
    elif d=="menu:sell":show_sell(token,chat,c)
    elif d=="menu:deals":
        rows=c.execute("SELECT item_name,buy,total,roi,score FROM deals ORDER BY ts DESC,score DESC LIMIT 8").fetchall();tg(token,chat,"🔥 RECENT DEALS\n"+("\n".join(f"• {n}: buy {money(b)} | profit {money(t)} | ROI {r:.1f}%" for n,b,t,r,s in rows) if rows else "No deals yet."),kb([back()]))
    elif d=="menu:watch":tg(token,chat,"👀 WATCHLIST\n"+"\n".join(f"• {x['name']}" for x in items[:40]),kb([back()]))
    elif d=="menu:market":tg(token,chat,"📊 MARKET TOOLS",kb([[btn("🔎 Find item","market:find")],back()]))
    elif d=="menu:settings":show_settings(token,chat,c,cfg)
    elif d=="inv:add":set_setting(c,"pending_input",{"kind":"add_item"});tg(token,chat,"➕ Send the exact Torn item name. /cancel to stop.")
    elif d.startswith("inv:page:"):show_inventory(token,chat,c,int(d.split(":")[-1]))
    elif d.startswith("sell:page:"):show_sell(token,chat,c,int(d.split(":")[-1]))
    elif d.startswith("inv:item:"):
        iid=int(d.split(":")[-1]);row=c.execute("SELECT item_name,qty,cost_each FROM holdings WHERE item_id=?",(iid,)).fetchone();tg(token,chat,f"📦 {row[0]}\nQty: {row[1]}\nAvg cost: {money(row[2])}",kb([[btn("💰 Sell assistant",f"sell:item:{iid}"),btn("🗑 Remove",f"inv:remove:{iid}")],back("inventory")]))
    elif d.startswith("inv:remove:"):c.execute("DELETE FROM holdings WHERE item_id=?",(int(d.split(":")[-1]),));c.commit();show_inventory(token,chat,c)
    elif d.startswith("sell:item:"):
        iid=int(d.split(":")[-1]);item=cat.get(iid);s=suggested_sale(item,c,cfg,key);tg(token,chat,sale_card(item,s),kb([[btn("🔄 Refresh",f"sell:item:{iid}"),btn("➖ Undercut $1k",f"sell:under:{iid}:1000")],[btn("🌐 Open Torn market",url=f"https://www.torn.com/page.php?sid=ItemMarket#/market/view=search&itemID={iid}")],back("sell")]))
    elif d.startswith("sell:under:"):
        _,_,iid,amt=d.split(":");item=cat[int(iid)];s=suggested_sale(item,c,cfg,key,int(amt));tg(token,chat,sale_card(item,s),kb([[btn("🌐 Open Torn market",url=f"https://www.torn.com/page.php?sid=ItemMarket#/market/view=search&itemID={iid}")],back("sell")]))
    elif d=="sell:plan":
        lines=["📋 SELL PLAN"]
        for iid,name,qty,cost in holding_rows(c)[:15]:
            try:
                s=suggested_sale(cat[iid],c,cfg,key)
                if s and not s.get("error"):lines.append(f"• {name} ×{qty}: list {money(s['suggested'])} | est {money(s['total'])}")
            except Exception:pass
        tg(token,chat,"\n".join(lines),kb([back("sell")]))
    elif d=="set:pause":set_setting(c,"paused",True);show_settings(token,chat,c,cfg)
    elif d=="set:resume":set_setting(c,"paused",False);show_settings(token,chat,c,cfg)
    elif d.startswith("set:"):
        kind=d.split(":",1)[1]
        if kind in ("budget","minroi","minprofit"):set_setting(c,"pending_input",{"kind":kind});tg(token,chat,f"Send new {kind} value. /cancel to stop.")
    elif d=="market:find":set_setting(c,"pending_input",{"kind":"find"});tg(token,chat,"🔎 Send part of the item name.")
    elif d.startswith("market:item:"):
        iid=int(d.split(":")[-1]);item=cat[iid];ls,avg=market(iid,key,int(cfg.get("market_limit",100)));hist=historical_ref(c,iid,float(cfg.get("historical_reference_hours",24)));ref,conf,support,reason=reference(ls,avg,hist,cfg) if ls else (None,0,0,"no listings");lines=[f"📊 {item['name']}",f"Listings: {len(ls)}"]
        if ls:lines += [f"Cheapest: {money(ls[0]['price'])}",f"Qty: {ls[0]['quantity']}"]
        if ref:lines += [f"Reference: {money(ref)}",f"Confidence: {conf:.0f}%",f"Support: {support}"]
        tg(token,chat,"\n".join(lines),kb([[btn("🌐 Open market",url=f"https://www.torn.com/page.php?sid=ItemMarket#/market/view=search&itemID={iid}")],back("market")]))
    return items
def process_updates(token,chat,offset,cat,cfg,c,items,key):
    try:updates=get_updates(token,offset)
    except Exception:return offset,items
    for u in updates:
        offset=max(offset,u.get("update_id",0)+1);cb=u.get("callback_query")
        if cb:
            cid=str(((cb.get("message") or {}).get("chat") or {}).get("id",""))
            if chat and cid!=str(chat):continue
            items=handle_cb(token,chat,cb,cat,cfg,c,items,key);continue
        m=u.get("message") or {};cid=str((m.get("chat") or {}).get("id",""));text=(m.get("text") or "").strip()
        if not text or (chat and cid!=str(chat)):continue
        if text.lower()=="/cancel":set_setting(c,"pending_input",None);menu_home(token,chat,c,items);continue
        if process_pending(token,chat,text,cat,c):continue
        parts=text.split(maxsplit=1);cmd=parts[0].split("@")[0].lower();arg=parts[1] if len(parts)>1 else ""
        if cmd in ("/start","/menu","/help","/status"):menu_home(token,chat,c,items)
        elif cmd=="/inventory":show_inventory(token,chat,c)
        elif cmd=="/sell":show_sell(token,chat,c)
        elif cmd=="/pause":set_setting(c,"paused",True);menu_home(token,chat,c,items)
        elif cmd=="/resume":set_setting(c,"paused",False);menu_home(token,chat,c,items)
        elif cmd=="/item":
            item=item_by_name(cat,arg)
            if item:handle_cb(token,chat,{"data":f"market:item:{item['id']}"},cat,cfg,c,items,key)
            else:tg(token,chat,"Item not found.",main_menu())
        else:tg(token,chat,"Tap /menu to use the interactive controls.",main_menu())
    return offset,items
def render_deal(item,d):return f"🔥 TORN DEAL FOUND\n{item['name']}\nBuy: {money(d['price'])} × {d['quantity']}\nCapital: {money(d['cost'])}\nReference: {money(d['ref'])}\nProfit: {money(d['total'])}\nROI: {d['roi']:.1f}%\nConfidence: {d['confidence']:.0f}%"
def main():
    cfg=load(CONFIG_FILE)
    if not cfg:raise SystemExit("Copy config.example.json to config.json first.")
    key=os.getenv("TORN_API_KEY") or cfg.get("torn_api_key")
    if not key:raise SystemExit("Missing TORN_API_KEY.")
    token=os.getenv("TELEGRAM_BOT_TOKEN") or cfg.get("telegram",{}).get("bot_token");chat=os.getenv("TELEGRAM_CHAT_ID") or cfg.get("telegram",{}).get("chat_id");c=db();cat=catalog(key);items=build_watch(cat,cfg,c);offset=int(get_setting(c,"telegram_offset",0) or 0);tg(token,chat,"✅ Torn Deal Finder Pro v5 started\nInteractive menu enabled. Tap /menu.",main_menu());cycle=0
    while not STOP:
        offset,items=process_updates(token,chat,offset,cat,cfg,c,items,key);set_setting(c,"telegram_offset",offset)
        if get_setting(c,"paused",False):time.sleep(3);continue
        cycle+=1;started=time.time();candidates=[]
        for item in list(items):
            if STOP:break
            offset,items=process_updates(token,chat,offset,cat,cfg,c,items,key);set_setting(c,"telegram_offset",offset)
            try:
                ls,avg=market(item["id"],key,int(cfg.get("market_limit",100)))
                if not ls:continue
                hist=historical_ref(c,item["id"],float(cfg.get("historical_reference_hours",24)));ref,conf,support,reason=reference(ls,avg,hist,cfg)
                if not ref:continue
                c.execute("INSERT OR REPLACE INTO samples VALUES(?,?,?,?,?,?,?,?)",(now(),item["id"],item["name"],ref,ls[0]["price"],avg,conf,support));c.commit()
                for d in evaluate(item,ls,ref,conf,support,cfg,c):candidates.append((item,d))
            except Exception as e:print(f"[ERROR] {item['name']}: {e}",flush=True)
            time.sleep(float(cfg.get("per_item_delay_seconds",1.2)))
        candidates.sort(key=lambda z:(z[1]["score"],z[1]["total"]),reverse=True);sent=0;cooldown=float(cfg.get("alert_cooldown_minutes",180))*60
        for item,d in candidates:
            if sent>=int(cfg.get("max_alerts_per_cycle",5)):break
            k=f"{item['id']}:{d['id']}:{d['price']}:{d['quantity']}";row=c.execute("SELECT ts FROM alerts WHERE k=?",(k,)).fetchone()
            if row and now()-row[0]<cooldown:continue
            tg(token,chat,render_deal(item,d),kb([[btn("📊 Analyze",f"market:item:{item['id']}"),btn("🌐 Open market",url=f"https://www.torn.com/page.php?sid=ItemMarket#/market/view=search&itemID={item['id']}")]]));c.execute("INSERT OR REPLACE INTO alerts VALUES(?,?)",(k,now()));c.execute("INSERT OR IGNORE INTO deals VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",(now(),item["id"],item["name"],d["price"],d["quantity"],d["ref"],d["profit"],d["total"],d["roi"],d["discount"],d["confidence"],d["score"],k));c.commit();sent+=1
        end=time.time()+max(5,max(45,int(cfg.get("scan_interval_seconds",300)))-(time.time()-started))
        while time.time()<end and not STOP:
            offset,items=process_updates(token,chat,offset,cat,cfg,c,items,key);set_setting(c,"telegram_offset",offset);time.sleep(2)
    c.close()

if __name__=="__main__":main()
