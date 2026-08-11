#!/usr/bin/env python3
"""Torn Deal Finder Pro v3 — read-only market intelligence + Telegram alerts."""
import os,time,json,math,sqlite3,hashlib,signal
from pathlib import Path
from statistics import median
from datetime import datetime,timezone
import requests
API_BASE="https://api.torn.com/v2"; CONFIG_FILE=Path(os.getenv("TORN_CONFIG","config.json")); DB_FILE=Path(os.getenv("TORN_DB","torn_deals.sqlite3")); STOP=False
def sig(*_):
 global STOP; STOP=True
signal.signal(signal.SIGINT,sig); signal.signal(signal.SIGTERM,sig)
def now(): return int(time.time())
def money(x): return f"${int(round(float(x))):,}"
def load(p):
 try:
  with open(p,encoding="utf-8") as f:return json.load(f)
 except FileNotFoundError:return None
def api(path,key,retries=4):
 h={"Authorization":f"ApiKey {key}","Accept":"application/json","User-Agent":"TornDealFinderPro/3.0"}; delay=3
 for n in range(retries):
  try:
   r=requests.get(API_BASE+path,headers=h,timeout=25)
   if r.status_code==429: time.sleep(int(r.headers.get("Retry-After",delay))); delay*=2; continue
   r.raise_for_status(); d=r.json()
   if isinstance(d,dict) and d.get("error"): raise RuntimeError(d["error"])
   return d
  except Exception:
   if n==retries-1: raise
   time.sleep(delay); delay*=2
def lists(obj):
 if isinstance(obj,list) and (not obj or all(isinstance(x,dict) for x in obj)): return obj
 if isinstance(obj,dict):
  for v in obj.values():
   z=lists(v)
   if z:return z
 return []
def catalog(key):
 out={}
 for x in lists(api("/torn/items",key)):
  i=x.get("id") or x.get("ID"); name=x.get("name") or x.get("item_name"); typ=x.get("type") or x.get("category") or ""
  if i is not None and name: out[int(i)]={"id":int(i),"name":str(name),"type":str(typ)}
 return out
def market(i,key):
 raw=lists(api(f"/market/{i}/itemmarket",key)); out=[]
 for x in raw:
  p=x.get("price",x.get("cost",x.get("unit_price"))); q=x.get("quantity",x.get("qty",x.get("amount",1)))
  if isinstance(p,(int,float)) and p>0:
   lid=x.get("id") or x.get("listing_id") or hashlib.sha1(json.dumps(x,sort_keys=True).encode()).hexdigest()[:16]
   out.append({"id":str(lid),"price":int(p),"quantity":max(1,int(q))})
 return sorted(out,key=lambda x:x["price"])
def db():
 c=sqlite3.connect(DB_FILE); c.execute("CREATE TABLE IF NOT EXISTS samples(ts INTEGER,item_id INTEGER,ref REAL)"); c.execute("CREATE TABLE IF NOT EXISTS alerts(k TEXT PRIMARY KEY,ts INTEGER)"); c.commit(); return c
def reference(ls,cfg):
 ps=[x["price"] for x in ls]; minimum=int(cfg.get("min_listings_for_reference",5))
 if len(ps)<minimum:return None,0
 n=int(cfg.get("reference_top_n",25)); skip=int(cfg.get("ignore_cheapest_for_reference",2)); s=sorted(ps)[skip:skip+n]
 if len(s)<3:s=sorted(ps)[:n]
 if len(s)<3:return None,0
 m=float(median(s)); dev=float(cfg.get("reference_max_deviation_percent",25)); f=[p for p in s if abs(p-m)/m*100<=dev]
 if len(f)>=3:s=f;m=float(median(s))
 mean=sum(s)/len(s); cv=(sum((p-mean)**2 for p in s)/len(s))**.5/mean if mean else 1; conf=(.55*min(1,len(s)/max(5,n))+.45*max(0,min(1,1-cv)))*100
 return m,conf
def watch(cat,cfg):
 s=cfg.get("scanner",{}); cats=[str(x).lower() for x in s.get("categories",[])]; names={str(x).lower() for x in s.get("item_names",[])}; ids={int(x) for x in s.get("item_ids",[])}; out=[]
 for i,x in cat.items():
  t=x["type"].lower(); n=x["name"].lower()
  if i in ids or n in names or any(c in t or c in n for c in cats):out.append(x)
 return out[:int(s.get("max_items",60))]
def tg(token,chat,text):
 if not token or not chat: print(text,flush=True); return
 requests.post(f"https://api.telegram.org/bot{token}/sendMessage",json={"chat_id":chat,"text":text,"disable_web_page_preview":True},timeout=20).raise_for_status()
def main():
 cfg=load(CONFIG_FILE)
 if not cfg: raise SystemExit("Copy config.example.json to config.json first.")
 key=os.getenv("TORN_API_KEY") or cfg.get("torn_api_key")
 if not key: raise SystemExit("Missing TORN_API_KEY.")
 token=os.getenv("TELEGRAM_BOT_TOKEN") or cfg.get("telegram",{}).get("bot_token"); chat=os.getenv("TELEGRAM_CHAT_ID") or cfg.get("telegram",{}).get("chat_id"); c=db(); items=watch(catalog(key),cfg)
 if not items: raise SystemExit("No watchlist items matched.")
 tg(token,chat,f"✅ Torn Deal Finder Pro started\nWatching {len(items)} items")
 while not STOP:
  started=time.time(); candidates=[]
  for item in items:
   if STOP:break
   try:
    ls=market(item["id"],key); ref,conf=reference(ls,cfg)
    if not ref:continue
    c.execute("INSERT INTO samples VALUES(?,?,?)",(now(),item["id"],ref)); c.commit(); fee=float(cfg.get("sale_fee_percent",5))/100; rules=dict(cfg.get("deal_rules",{})); typ=item["type"].lower()
    for k,v in cfg.get("type_rules",{}).items():
     if k.lower() in typ:rules.update(v)
    for l in ls:
     q=min(l["quantity"],int(rules.get("max_quantity_to_value",1000))); cost=l["price"]*q; maxcost=float(rules.get("max_total_cost",cfg.get("bankroll",{}).get("max_capital_per_deal",5000000)))
     profit=ref*(1-fee)-l["price"]; roi=profit/l["price"]*100; disc=(ref-l["price"])/ref*100; total=profit*q
     if cost<=maxcost and conf>=float(rules.get("min_confidence_percent",35)) and roi>=float(rules.get("min_roi_percent",7)) and profit>=float(rules.get("min_profit_per_item",5000)) and total>=float(rules.get("min_total_profit",15000)) and disc>=float(rules.get("min_discount_percent",7)):
      score=roi*.45+min(50,math.log10(max(total,1))*8)*.30+conf*.25; candidates.append((score,item,l,ref,conf,profit,total,roi,disc,cost))
   except Exception as e: print(f"[ERROR] {item['name']}: {e}",flush=True)
   time.sleep(float(cfg.get("per_item_delay_seconds",2)))
  candidates.sort(reverse=True,key=lambda x:x[0]); sent=0
  for score,item,l,ref,conf,profit,total,roi,disc,cost in candidates:
   if sent>=int(cfg.get("max_alerts_per_cycle",5)):break
   k=f"{item['id']}:{l['id']}:{l['price']}:{l['quantity']}"; row=c.execute("SELECT ts FROM alerts WHERE k=?",(k,)).fetchone(); cooldown=float(cfg.get("alert_cooldown_minutes",180))*60
   if row and now()-row[0]<cooldown:continue
   text=f"🔥 TORN DEAL FOUND\n{item['name']} | {item['type']}\nBuy: {money(l['price'])} × {l['quantity']:,}\nCapital: {money(cost)}\nReference: {money(ref)}\nDiscount: {disc:.1f}%\nEst. profit: {money(profit)} each\nEst. total: {money(total)}\nROI: {roi:.1f}%\nConfidence: {conf:.0f}% | Score: {score:.1f}\nhttps://www.torn.com/page.php?sid=ItemMarket#/market/view=search&itemID={item['id']}"; tg(token,chat,text); c.execute("INSERT OR REPLACE INTO alerts VALUES(?,?)",(k,now())); c.commit(); sent+=1
  delay=max(5,int(cfg.get("scan_interval_seconds",300))-(time.time()-started)); print(f"Cycle: {len(candidates)} candidates, {sent} alerts. Sleep {delay:.0f}s",flush=True); time.sleep(delay)
if __name__=="__main__":main()
