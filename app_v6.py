#!/usr/bin/env python3
"""Torn Deal Finder Pro v6 — market scanner + interactive portfolio/exit assistant.

Read-only with respect to Torn: it never buys or sells. Purchases are recorded only
when the user confirms them in Telegram.
"""
import os, time, json, math, sqlite3, hashlib, signal
from pathlib import Path
from statistics import median
import requests

API_BASE="https://api.torn.com/v2"
CONFIG_FILE=Path(os.getenv("TORN_CONFIG","config.json"))
DB_FILE=Path(os.getenv("TORN_DB","torn_deals.sqlite3"))
STOP=False

def sig(*_):
    global STOP; STOP=True
signal.signal(signal.SIGINT,sig); signal.signal(signal.SIGTERM,sig)
def now(): return int(time.time())
def money(x): return f"${int(round(float(x))):,}"
def load(p):
    try:
        with open(p,encoding="utf-8") as f:return json.load(f)
    except FileNotFoundError:return None

def api(path,key,params=None,retries=4):
    h={"Authorization":f"ApiKey {key}","Accept":"application/json","User-Agent":"TornDealFinderPro/6.0"}; delay=3
    for n in range(retries):
        try:
            r=requests.get(API_BASE+path,headers=h,params=params or {},timeout=25)
            if r.status_code==429: time.sleep(max(delay,int(r.headers.get("Retry-After",delay))));delay*=2;continue
            r.raise_for_status();d=r.json()
            if isinstance(d,dict) and d.get("error"):raise RuntimeError(str(d["error"]))
            return d
        except Exception:
            if n==retries-1:raise
            time.sleep(delay);delay*=2

def find_list(obj,preferred=()):
    if isinstance(obj,dict):
        for k in preferred:
            v=obj.get(k)
            if isinstance(v,list):return v
            if isinstance(v,dict):
                for kk in ("items","listings","results"):
                    if isinstance(v.get(kk),list):return v[kk]
        for v in obj.values():
            z=find_list(v,preferred)
            if z:return z
    elif isinstance(obj,list):
        if obj and all(isinstance(x,dict) for x in obj):return obj
        for v in obj:
            z=find_list(v,preferred)
            if z:return z
    return []

def catalog(key):
    out={}
    for x in find_list(api("/torn/items",key),("items",)):
        iid=x.get("id") or x.get("ID");name=x.get("name") or x.get("item_name");typ=x.get("type") or x.get("category") or x.get("item_type") or "Other"
        if iid is not None and name:out[int(iid)]={"id":int(iid),"name":str(name),"type":str(typ)}
    return out

def market(i,key,limit=100):
    p=api(f"/market/{i}/itemmarket",key,{"limit":limit});raw=find_list(p,("itemmarket","item_market","listings"));avg=None
    try:avg=p.get("itemmarket",{}).get("item",{}).get("average_price")
    except Exception:pass
    out=[]
    for x in raw:
        price=x.get("price",x.get("cost",x.get("unit_price")));qty=x.get("quantity",x.get("qty",x.get("amount",1)))
        if not isinstance(price,(int,float)) or price<=0:continue
        lid=x.get("id") or x.get("listing_id") or hashlib.sha1(json.dumps(x,sort_keys=True).encode()).hexdigest()[:18]
        out.append({"id":str(lid),"price":int(price),"quantity":max(1,int(qty or 1))})
    return sorted(out,key=lambda z:z["price"]),(float(avg) if isinstance(avg,(int,float)) and avg>0 else None)

def db():
    c=sqlite3.connect(DB_FILE);c.execute("PRAGMA journal_mode=WAL")
    c.execute("CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY,v TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS samples(ts INTEGER,item_id INTEGER,item_name TEXT,ref REAL,cheap INTEGER,avg REAL,confidence REAL,support INTEGER,PRIMARY KEY(ts,item_id))")
    c.execute("CREATE TABLE IF NOT EXISTS alerts(k TEXT PRIMARY KEY,ts INTEGER)")
    c.execute("CREATE TABLE IF NOT EXISTS deals(ts INTEGER,item_id INTEGER,item_name TEXT,buy INTEGER,qty INTEGER,ref REAL,profit REAL,total REAL,roi REAL,discount REAL,confidence REAL,score REAL,k TEXT UNIQUE)")
    c.execute("CREATE TABLE IF NOT EXISTS holdings(item_id INTEGER PRIMARY KEY,item_name TEXT,qty INTEGER,cost_each REAL,updated_ts INTEGER)")
    c.execute("CREATE TABLE IF NOT EXISTS lots(id INTEGER PRIMARY KEY AUTOINCREMENT,ts INTEGER,item_id INTEGER,item_name TEXT,qty INTEGER,cost_each REAL,source TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS sales(id INTEGER PRIMARY KEY AUTOINCREMENT,ts INTEGER,item_id INTEGER,item_name TEXT,qty INTEGER,sell_each REAL,cost_each REAL,realized_profit REAL)")
    c.commit();return c

def gs(c,k,d=None):
    r=c.execute("SELECT v FROM settings WHERE k=?",(k,)).fetchone()
    if not r:return d
    try:return json.loads(r[0])
    except:return r[0]
def ss(c,k,v):c.execute("INSERT OR REPLACE INTO settings VALUES(?,?)",(k,json.dumps(v)));c.commit()

def hist_ref(c,i,hours=24):
    cut=now()-int(hours*3600);rs=c.execute("SELECT ref FROM samples WHERE item_id=? AND ts>=? ORDER BY ts DESC LIMIT 200",(i,cut)).fetchall();v=[float(x[0]) for x in rs if x[0]]
    return float(median(v)) if len(v)>=3 else None

def reference(ls,avg,hist,cfg):
    prices=[x["price"] for x in ls]
    if len(prices)<int(cfg.get("min_listings_for_reference",6)):return None,0,0
    n=int(cfg.get("reference_top_n",30));skip=int(cfg.get("ignore_cheapest_for_reference",2));s=sorted(prices)[skip:skip+n]
    if len(s)<4:s=sorted(prices)[:n]
    if len(s)<4:return None,0,0
    m=float(median(s));dev=float(cfg.get("reference_max_deviation_percent",22));f=[p for p in s if abs(p-m)/m*100<=dev]
    if len(f)>=4:s=f;m=float(median(s))
    band=float(cfg.get("support_band_percent",7));support=sum(1 for p in prices if abs(p-m)/m*100<=band);mean=sum(s)/len(s);cv=(sum((p-mean)**2 for p in s)/len(s))**.5/mean if mean else 1
    conf=(.55*max(0,min(1,1-cv*2.2))+.45*min(1,support/max(5,int(cfg.get("min_support_listings",5)))))*100
    anchors=[x for x in (hist,avg) if x]
    if anchors:
        a=median(anchors);diff=abs(m-a)/a*100
        if diff>float(cfg.get("max_anchor_deviation_percent",35)):conf*=.6
    if hist:
        w=float(cfg.get("live_reference_weight",.7));m=m*w+hist*(1-w)
    return m,conf,support

def build_watch(cat,cfg,c):
    s=cfg.get("scanner",{});cats=[str(x).lower() for x in s.get("categories",[])];names={str(x).lower() for x in s.get("item_names",[])};ids={int(x) for x in s.get("item_ids",[])};adds=set(gs(c,"watch_add",[]));rem=set(gs(c,"watch_remove",[]));out=[]
    for iid,x in cat.items():
        t=x["type"].lower();n=x["name"].lower();chosen=iid in ids or n in names or any(k in t or k in n for k in cats) or iid in adds
        if chosen and iid not in rem:out.append(x)
    return out[:int(s.get("max_items",80))]

def tg(token,chat,text,markup=None):
    if not token or not chat:print(text,flush=True);return
    p={"chat_id":chat,"text":text,"disable_web_page_preview":True}
    if markup:p["reply_markup"]=markup
    requests.post(f"https://api.telegram.org/bot{token}/sendMessage",json=p,timeout=20).raise_for_status()
def ack(token,cbid):
    if cbid:requests.post(f"https://api.telegram.org/bot{token}/answerCallbackQuery",json={"callback_query_id":cbid},timeout=10)
def updates(token,offset):
    r=requests.get(f"https://api.telegram.org/bot{token}/getUpdates",params={"offset":offset,"timeout":0,"allowed_updates":json.dumps(["message","callback_query"])},timeout=10);r.raise_for_status();return r.json().get("result",[])
def b(text,data=None,url=None):return {"text":text,("url" if url else "callback_data"):(url if url else data)}
def kb(rows):return {"inline_keyboard":rows}
def market_link(i):return f"https://www.torn.com/page.php?sid=ItemMarket#/market/view=search&itemID={i}"
def menu():return kb([[b("🔥 Deals","m:deals"),b("💼 Portfolio","m:portfolio")],[b("💰 Sell","m:sell"),b("📦 Inventory","m:inventory")],[b("👀 Watchlist","m:watch"),b("📊 Market","m:market")],[b("⚙️ Settings","m:settings")]])
def item_by_name(cat,q):
    q=q.strip().lower();e=[x for x in cat.values() if x["name"].lower()==q]
    if e:return e[0]
    p=[x for x in cat.values() if q in x["name"].lower()]
    return p[0] if len(p)==1 else None

def holding(c,i):return c.execute("SELECT qty,cost_each FROM holdings WHERE item_id=?",(i,)).fetchone()
def add_buy(c,item,qty,price,source="confirmed_deal"):
    old=holding(c,item["id"]);oq=int(old[0]) if old else 0;oc=float(old[1] or 0) if old else 0;newq=oq+qty;newcost=((oq*oc)+(qty*price))/newq
    c.execute("INSERT OR REPLACE INTO holdings VALUES(?,?,?,?,?)",(item["id"],item["name"],newq,newcost,now()));c.execute("INSERT INTO lots(ts,item_id,item_name,qty,cost_each,source) VALUES(?,?,?,?,?,?)",(now(),item["id"],item["name"],qty,price,source));c.commit();return newq,newcost

def mark_sold(c,item,qty,price,fee):
    old=holding(c,item["id"])
    if not old:return None
    oq,cost=int(old[0]),float(old[1]);q=min(qty,oq);real=(price*(1-fee)-cost)*q;left=oq-q
    if left:c.execute("UPDATE holdings SET qty=?,updated_ts=? WHERE item_id=?",(left,now(),item["id"]))
    else:c.execute("DELETE FROM holdings WHERE item_id=?",(item["id"],))
    c.execute("INSERT INTO sales(ts,item_id,item_name,qty,sell_each,cost_each,realized_profit) VALUES(?,?,?,?,?,?,?)",(now(),item["id"],item["name"],q,price,cost,real));c.commit();return q,real

def deal_candidates(item,ls,ref,conf,support,cfg,c):
    r=dict(cfg.get("deal_rules",{}));typ=item["type"].lower()
    for k,v in cfg.get("type_rules",{}).items():
        if k.lower() in typ:r.update(v)
    fee=float(cfg.get("sale_fee_percent",5))/100;maxcost=float(gs(c,"budget",cfg.get("bankroll",{}).get("max_capital_per_deal",5000000)));minroi=float(gs(c,"minroi",r.get("min_roi_percent",7)));minprofit=float(gs(c,"minprofit",r.get("min_total_profit",15000)));out=[]
    if conf<float(r.get("min_confidence_percent",45)) or support<int(r.get("min_support_listings",5)):return out
    for l in ls:
        q=min(l["quantity"],int(r.get("max_quantity_to_value",1000)));cost=l["price"]*q
        if cost>maxcost:continue
        profit=ref*(1-fee)-l["price"];total=profit*q;roi=profit/l["price"]*100;disc=(ref-l["price"])/ref*100
        if roi<minroi or total<minprofit or disc<float(r.get("min_discount_percent",7)):continue
        score=roi*.45+conf*.3+min(100,math.log10(max(total,10))*14)*.25;out.append({**l,"ref":ref,"profit":profit,"total":total,"roi":roi,"discount":disc,"confidence":conf,"score":score})
    return sorted(out,key=lambda d:(d["score"],d["total"]),reverse=True)

def send_deal(token,chat,item,d):
    text=(f"🔥 DEAL FOUND — {item['name']}\nBuy: {money(d['price'])} × {d['quantity']}\nCapital: {money(d['price']*d['quantity'])}\nReference: {money(d['ref'])}\nDiscount: {d['discount']:.1f}%\nEst. profit: {money(d['total'])}\nROI: {d['roi']:.1f}%\nConfidence: {d['confidence']:.0f}%")
    tg(token,chat,text,kb([[b("✅ I bought it",f"buy:{item['id']}:{d['price']}:{d['quantity']}"),b("📊 Market",url=market_link(item['id']))],[b("🙈 Ignore","noop")]]))

def portfolio_rows(c):return c.execute("SELECT item_id,item_name,qty,cost_each FROM holdings WHERE qty>0 ORDER BY item_name").fetchall()
def show_portfolio(token,chat,c,cfg,key,cat):
    rows=portfolio_rows(c)
    if not rows:tg(token,chat,"💼 Portfolio is empty. Confirm a deal with ‘✅ I bought it’ or add a holding.",kb([[b("⬅️ Menu","m:home")]]));return
    fee=float(cfg.get("selling",{}).get("sale_fee_percent",cfg.get("sale_fee_percent",5)))/100;invested=0;value=0;lines=["💼 PORTFOLIO"]
    buttons=[]
    for iid,name,qty,cost in rows[:15]:
        invested+=qty*cost
        try:
            ls,avg=market(iid,key,int(cfg.get("market_limit",100)));hist=hist_ref(c,iid,float(cfg.get("historical_reference_hours",24)));ref,conf,sup=reference(ls,avg,hist,cfg) if ls else (None,0,0);px=(ref or (ls[0]['price'] if ls else cost));net=px*(1-fee);pnl=(net-cost)*qty;value+=net*qty;lines.append(f"• {name} ×{qty}: avg {money(cost)} | est P/L {money(pnl)}");buttons.append([b(f"{name} ×{qty}",f"pos:{iid}")])
        except Exception:lines.append(f"• {name} ×{qty}: avg {money(cost)}")
    realized=c.execute("SELECT COALESCE(SUM(realized_profit),0) FROM sales").fetchone()[0] or 0
    lines += ["",f"Invested: {money(invested)}",f"Est. net value: {money(value)}",f"Unrealized P/L: {money(value-invested)}",f"Realized P/L: {money(realized)}"]
    buttons.append([b("🔄 Refresh","m:portfolio"),b("📜 Sales","m:sales")]);buttons.append([b("⬅️ Menu","m:home")]);tg(token,chat,"\n".join(lines),kb(buttons))
def show_position(token,chat,c,cfg,key,cat,iid):
    item=cat.get(iid);row=holding(c,iid)
    if not item or not row:return
    qty,cost=int(row[0]),float(row[1]);ls,avg=market(iid,key,int(cfg.get("market_limit",100)));hist=hist_ref(c,iid,float(cfg.get("historical_reference_hours",24)));ref,conf,sup=reference(ls,avg,hist,cfg) if ls else (None,0,0);cheap=ls[0]['price'] if ls else 0;und=int(cfg.get("selling",{}).get("undercut_amount",1));suggest=max(1,cheap-und) if cheap else int(ref or cost);suggest=min(suggest,int(ref)) if ref else suggest;fee=float(cfg.get("selling",{}).get("sale_fee_percent",cfg.get("sale_fee_percent",5)))/100;net=suggest*(1-fee);profit=(net-cost)*qty;roi=(net-cost)/cost*100 if cost else 0
    target=float(cfg.get("selling",{}).get("target_profit_percent",5));status="🟢 SELL CANDIDATE" if roi>=target and conf>=55 else "🟡 HOLD / WATCH"
    text=(f"{status}\n{item['name']} ×{qty}\nAvg buy: {money(cost)}\nCheapest market: {money(cheap)}\nSuggested sale: {money(suggest)}\nReference: {money(ref) if ref else 'n/a'}\nConfidence: {conf:.0f}%\nEst. net P/L: {money(profit)}\nEst. ROI: {roi:.1f}%")
    tg(token,chat,text,kb([[b("✅ Mark sold",f"soldask:{iid}:{suggest}"),b("📊 Open market",url=market_link(iid))],[b("🔄 Refresh",f"pos:{iid}"),b("⬅️ Portfolio","m:portfolio")]]))
def show_sales(token,chat,c):
    rs=c.execute("SELECT item_name,qty,sell_each,realized_profit FROM sales ORDER BY id DESC LIMIT 12").fetchall();real=c.execute("SELECT COALESCE(SUM(realized_profit),0) FROM sales").fetchone()[0] or 0
    text="📜 SALES HISTORY\n"+("\n".join(f"• {n} ×{q} @ {money(p)} | P/L {money(r)}" for n,q,p,r in rs) if rs else "No recorded sales yet.")+f"\n\nTotal realized P/L: {money(real)}";tg(token,chat,text,kb([[b("⬅️ Portfolio","m:portfolio")]]))

def home(token,chat,c,items):tg(token,chat,f"🤖 TORN DEAL FINDER PRO v6\n{'⏸ PAUSED' if gs(c,'paused',False) else '🟢 RUNNING'}\nWatching {len(items)} items\nPositions: {len(portfolio_rows(c))}\nChoose:",menu())

def handle(token,chat,offset,cat,cfg,c,items,key,state):
    try:us=updates(token,offset)
    except Exception:return offset,items,state
    for u in us:
        offset=max(offset,u.get("update_id",0)+1);cb=u.get("callback_query");m=u.get("message") or (cb or {}).get("message") or {};cid=str((m.get("chat") or {}).get("id",""));text=(u.get("message") or {}).get("text","").strip();data=(cb or {}).get("data","")
        if chat and cid!=str(chat):continue
        if cb:ack(token,cb.get("id"))
        try:
            if text in ("/start","/menu"):home(token,chat,c,items)
            elif text=="/pause":ss(c,"paused",True);tg(token,chat,"⏸ Paused")
            elif text=="/resume":ss(c,"paused",False);tg(token,chat,"▶️ Resumed")
            elif data=="m:home":home(token,chat,c,items)
            elif data=="m:portfolio":show_portfolio(token,chat,c,cfg,key,cat)
            elif data=="m:sales":show_sales(token,chat,c)
            elif data=="m:deals":
                rs=c.execute("SELECT item_name,buy,total,roi FROM deals ORDER BY ts DESC LIMIT 8").fetchall();tg(token,chat,"🔥 Recent deals\n"+("\n".join(f"• {n}: buy {money(bu)} | profit {money(pr)} | ROI {ro:.1f}%" for n,bu,pr,ro in rs) if rs else "No deals yet."),kb([[b("⬅️ Menu","m:home")]]))
            elif data.startswith("buy:"):
                _,si,sp,sq=data.split(":");iid=int(si);price=float(sp);qty=int(sq);item=cat.get(iid);state={"mode":"buyqty","item":iid,"price":price,"max":qty};tg(token,chat,f"✅ Confirm purchase: {item['name']}\nDetected qty: {qty}\nSend quantity you actually bought (1-{qty}), or /cancel.")
            elif data.startswith("pos:"):show_position(token,chat,c,cfg,key,cat,int(data.split(":")[1]))
            elif data.startswith("soldask:"):
                _,si,sp=data.split(":");iid=int(si);item=cat.get(iid);row=holding(c,iid);state={"mode":"soldqty","item":iid,"price":float(sp),"max":int(row[0])};tg(token,chat,f"Mark {item['name']} sold at {money(float(sp))} each.\nSend quantity sold (1-{row[0]}) or /cancel.")
            elif data=="m:inventory":show_portfolio(token,chat,c,cfg,key,cat)
            elif data=="m:sell":show_portfolio(token,chat,c,cfg,key,cat)
            elif data in ("m:watch","m:market","m:settings"):tg(token,chat,"This section remains available through existing commands: /watch, /unwatch, /item, /settings. Portfolio workflow is the main v6 upgrade.",kb([[b("⬅️ Menu","m:home")]]))
            elif data=="noop":pass
            elif text=="/cancel":state={};tg(token,chat,"Cancelled.",kb([[b("⬅️ Menu","m:home")]]))
            elif state.get("mode") in ("buyqty","soldqty") and text:
                q=int(text.replace(",",""));q=max(1,min(q,int(state["max"])));iid=int(state["item"]);item=cat[iid];price=float(state["price"])
                if state["mode"]=="buyqty":
                    nq,nc=add_buy(c,item,q,price);tg(token,chat,f"✅ Added to portfolio\n{item['name']} +{q} @ {money(price)}\nNow hold: {nq}\nWeighted avg: {money(nc)}",kb([[b("💼 Portfolio","m:portfolio"),b("📊 Market",url=market_link(iid))]]))
                else:
                    fee=float(cfg.get("selling",{}).get("sale_fee_percent",cfg.get("sale_fee_percent",5)))/100;res=mark_sold(c,item,q,price,fee);tg(token,chat,f"✅ Sale recorded\n{item['name']} ×{res[0]} @ {money(price)}\nRealized P/L: {money(res[1])}",kb([[b("💼 Portfolio","m:portfolio")]]))
                state={}
        except Exception as e:tg(token,chat,f"Action error: {e}")
    return offset,items,state

def main():
    cfg=load(CONFIG_FILE)
    if not cfg:raise SystemExit("Missing config.json")
    key=os.getenv("TORN_API_KEY") or cfg.get("torn_api_key");token=os.getenv("TELEGRAM_BOT_TOKEN") or cfg.get("telegram",{}).get("bot_token");chat=os.getenv("TELEGRAM_CHAT_ID") or cfg.get("telegram",{}).get("chat_id")
    if not key or not token or not chat:raise SystemExit("Missing Torn/Telegram environment variables")
    c=db();cat=catalog(key);items=build_watch(cat,cfg,c);offset=int(gs(c,"telegram_offset",0) or 0);state={};tg(token,chat,"✅ Torn Deal Finder Pro v6 started\nDeal → Buy confirmation → Portfolio → Sell exit workflow enabled.\nUse /menu.",menu());cycle=0
    while not STOP:
        offset,items,state=handle(token,chat,offset,cat,cfg,c,items,key,state);ss(c,"telegram_offset",offset)
        if gs(c,"paused",False):time.sleep(3);continue
        cycle+=1;started=time.time();cand=[]
        for item in items:
            if STOP:break
            offset,items,state=handle(token,chat,offset,cat,cfg,c,items,key,state);ss(c,"telegram_offset",offset)
            try:
                ls,avg=market(item["id"],key,int(cfg.get("market_limit",100)))
                if not ls:continue
                hist=hist_ref(c,item["id"],float(cfg.get("historical_reference_hours",24)));ref,conf,sup=reference(ls,avg,hist,cfg)
                if not ref:continue
                c.execute("INSERT OR REPLACE INTO samples VALUES(?,?,?,?,?,?,?,?)",(now(),item["id"],item["name"],ref,ls[0]["price"],avg,conf,sup));c.commit()
                for d in deal_candidates(item,ls,ref,conf,sup,cfg,c):cand.append((item,d))
            except Exception as e:print("[ERROR]",item["name"],e,flush=True)
            time.sleep(float(cfg.get("per_item_delay_seconds",1.2)))
        cand.sort(key=lambda z:(z[1]["score"],z[1]["total"]),reverse=True);sent=0;cool=float(cfg.get("alert_cooldown_minutes",180))*60
        for item,d in cand:
            if sent>=int(cfg.get("max_alerts_per_cycle",5)):break
            k=f"{item['id']}:{d['id']}:{d['price']}:{d['quantity']}";r=c.execute("SELECT ts FROM alerts WHERE k=?",(k,)).fetchone()
            if r and now()-r[0]<cool:continue
            send_deal(token,chat,item,d);c.execute("INSERT OR REPLACE INTO alerts VALUES(?,?)",(k,now()));c.execute("INSERT OR IGNORE INTO deals VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",(now(),item["id"],item["name"],d["price"],d["quantity"],d["ref"],d["profit"],d["total"],d["roi"],d["discount"],d["confidence"],d["score"],k));c.commit();sent+=1
        # proactive exit alerts for owned positions
        target=float(cfg.get("selling",{}).get("target_profit_percent",5));fee=float(cfg.get("selling",{}).get("sale_fee_percent",cfg.get("sale_fee_percent",5)))/100
        for iid,name,qty,cost in portfolio_rows(c):
            try:
                ls,avg=market(iid,key,int(cfg.get("market_limit",100)))
                if not ls:continue
                hist=hist_ref(c,iid,float(cfg.get("historical_reference_hours",24)));ref,conf,sup=reference(ls,avg,hist,cfg);px=(ref or ls[0]['price']);roi=(px*(1-fee)-cost)/cost*100 if cost else 0
                alertkey=f"exit:{iid}:{int(px)}"
                old=c.execute("SELECT ts FROM alerts WHERE k=?",(alertkey,)).fetchone()
                if roi>=target and conf>=55 and (not old or now()-old[0]>3600):
                    tg(token,chat,f"💰 EXIT OPPORTUNITY\n{name} ×{qty}\nAvg cost: {money(cost)}\nReference: {money(px)}\nEst. ROI after fee: {roi:.1f}%",kb([[b("💼 Review position",f"pos:{iid}"),b("📊 Market",url=market_link(iid))]]));c.execute("INSERT OR REPLACE INTO alerts VALUES(?,?)",(alertkey,now()));c.commit()
            except Exception:pass
        interval=max(60,int(cfg.get("scan_interval_seconds",300)));end=time.time()+max(5,interval-(time.time()-started))
        while time.time()<end and not STOP:
            offset,items,state=handle(token,chat,offset,cat,cfg,c,items,key,state);ss(c,"telegram_offset",offset);time.sleep(3)
    c.close()

if __name__=="__main__":main()
