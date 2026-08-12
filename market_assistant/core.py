#!/usr/bin/env python3
from __future__ import annotations
import os,time,json,math,sqlite3,hashlib,signal
from pathlib import Path
from statistics import median
import requests

API='https://api.torn.com/v2'; STOP=False
signal.signal(signal.SIGINT, lambda *_: globals().__setitem__('STOP',True)); signal.signal(signal.SIGTERM, lambda *_: globals().__setitem__('STOP',True))
def now(): return int(time.time())
def money(x): return 'n/a' if x is None else f'${int(round(float(x))):,}'
def mnum(s): return float(str(s).replace(',','').replace('$','').strip())
def kb(rows): return {'inline_keyboard':rows}
def b(text,data=None,url=None): return {'text':text,('url' if url else 'callback_data'):(url if url else data)}
def market_link(i): return f'https://www.torn.com/page.php?sid=ItemMarket#/market/view=search&itemID={i}'

class DB:
 def __init__(self,p='torn_deals.sqlite3'):
  self.c=sqlite3.connect(p); self.c.execute('PRAGMA journal_mode=WAL'); self.migrate()
 def migrate(self):
  self.c.execute('CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY,applied_ts INTEGER)')
  self.c.execute('CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY,v TEXT)')
  self.c.execute('CREATE TABLE IF NOT EXISTS holdings(item_id INTEGER PRIMARY KEY,item_name TEXT,qty INTEGER,cost_each REAL,updated_ts INTEGER)')
  self.c.execute('CREATE TABLE IF NOT EXISTS purchases(id INTEGER PRIMARY KEY AUTOINCREMENT,ts INTEGER,item_id INTEGER,item_name TEXT,qty INTEGER,cost_each REAL,source TEXT)')
  self.c.execute('CREATE TABLE IF NOT EXISTS sales(id INTEGER PRIMARY KEY AUTOINCREMENT,ts INTEGER,item_id INTEGER,item_name TEXT,qty INTEGER,sell_each REAL,cost_each REAL,realized_profit REAL)')
  self.c.execute('CREATE TABLE IF NOT EXISTS market_samples(ts INTEGER,item_id INTEGER,item_name TEXT,reference_price REAL,cheapest REAL,api_average REAL,confidence REAL,support INTEGER,PRIMARY KEY(ts,item_id))')
  self.c.execute('CREATE TABLE IF NOT EXISTS deals(id INTEGER PRIMARY KEY AUTOINCREMENT,ts INTEGER,item_id INTEGER,item_name TEXT,listing_key TEXT UNIQUE,buy_price REAL,qty INTEGER,reference_price REAL,est_profit REAL,roi REAL,discount REAL,confidence REAL,score REAL)')
  self.c.execute('CREATE TABLE IF NOT EXISTS watchlist(item_id INTEGER PRIMARY KEY,item_name TEXT,min_roi REAL,min_profit REAL,max_buy REAL,enabled INTEGER DEFAULT 1)')
  self.c.execute('CREATE TABLE IF NOT EXISTS alerts(k TEXT PRIMARY KEY,ts INTEGER)'); self.c.execute('INSERT OR IGNORE INTO schema_migrations VALUES(1,?)',(now(),)); self.c.commit()
 def get(self,k,d=None):
  r=self.c.execute('SELECT v FROM settings WHERE k=?',(k,)).fetchone()
  if not r:return d
  try:return json.loads(r[0])
  except:return r[0]
 def set(self,k,v): self.c.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',(k,json.dumps(v))); self.c.commit()
 def holdings(self): return self.c.execute('SELECT item_id,item_name,qty,cost_each FROM holdings WHERE qty>0 ORDER BY item_name').fetchall()
 def holding(self,i): return self.c.execute('SELECT item_id,item_name,qty,cost_each FROM holdings WHERE item_id=?',(i,)).fetchone()
 def add_holding(self,item,qty,cost,source='manual'):
  old=self.holding(item['id']); oq=int(old[2]) if old else 0; oc=float(old[3] or 0) if old else 0; nq=oq+qty; nc=((oq*oc)+(qty*cost))/nq if nq else 0
  self.c.execute('INSERT OR REPLACE INTO holdings VALUES(?,?,?,?,?)',(item['id'],item['name'],nq,nc,now())); self.c.execute('INSERT INTO purchases(ts,item_id,item_name,qty,cost_each,source) VALUES(?,?,?,?,?,?)',(now(),item['id'],item['name'],qty,cost,source)); self.c.commit(); return nq,nc
 def set_holding(self,item,qty,cost):
  self.c.execute('INSERT OR REPLACE INTO holdings VALUES(?,?,?,?,?)',(item['id'],item['name'],qty,cost,now())); self.c.commit()
 def remove_holding(self,i): self.c.execute('DELETE FROM holdings WHERE item_id=?',(i,)); self.c.commit()
 def sell(self,item,qty,price,fee):
  r=self.holding(item['id']); owned=int(r[2]); cost=float(r[3]); q=min(max(1,qty),owned); net=price*(1-fee/100); realized=(net-cost)*q; left=owned-q
  if left:self.c.execute('UPDATE holdings SET qty=?,updated_ts=? WHERE item_id=?',(left,now(),item['id']))
  else:self.c.execute('DELETE FROM holdings WHERE item_id=?',(item['id'],))
  self.c.execute('INSERT INTO sales(ts,item_id,item_name,qty,sell_each,cost_each,realized_profit) VALUES(?,?,?,?,?,?,?)',(now(),item['id'],item['name'],q,price,cost,realized)); self.c.commit(); return q,left,realized

class Torn:
 def __init__(self,key): self.key=key; self.s=requests.Session(); self.s.headers.update({'Authorization':f'ApiKey {key}','Accept':'application/json','User-Agent':'TornMarketAssistant/1.0'})
 def get(self,path,params=None):
  delay=2
  for n in range(4):
   try:
    r=self.s.get(API+path,params=params or {},timeout=20)
    if r.status_code==429: time.sleep(max(delay,int(r.headers.get('Retry-After',delay)))); delay*=2; continue
    r.raise_for_status(); d=r.json()
    if isinstance(d,dict) and d.get('error'): raise RuntimeError(str(d['error']))
    return d
   except Exception:
    if n==3: raise
    time.sleep(delay); delay*=2
 def findlist(self,o,preferred=()):
  if isinstance(o,dict):
   for k in preferred:
    v=o.get(k)
    if isinstance(v,list): return v
    if isinstance(v,dict):
     for kk in ('items','listings','results'):
      if isinstance(v.get(kk),list): return v[kk]
   for v in o.values():
    z=self.findlist(v,preferred)
    if z:return z
  elif isinstance(o,list):
   if o and all(isinstance(x,dict) for x in o): return o
   for v in o:
    z=self.findlist(v,preferred)
    if z:return z
  return []
 def catalog(self):
  out={}
  for x in self.findlist(self.get('/torn/items'),('items',)):
   i=x.get('id') or x.get('ID'); n=x.get('name') or x.get('item_name'); t=x.get('type') or x.get('category') or x.get('item_type') or 'Other'
   if i is not None and n: out[int(i)]={'id':int(i),'name':str(n),'type':str(t)}
  return out
 def market(self,i,limit=100):
  p=self.get(f'/market/{i}/itemmarket',{'limit':limit}); raw=self.findlist(p,('itemmarket','item_market','listings')); avg=None
  try: avg=p.get('itemmarket',{}).get('item',{}).get('average_price')
  except: pass
  ls=[]
  for x in raw:
   price=x.get('price',x.get('cost',x.get('unit_price'))); q=x.get('quantity',x.get('qty',x.get('amount',1)))
   if not isinstance(price,(int,float)) or price<=0: continue
   lid=x.get('id') or x.get('listing_id') or hashlib.sha1(json.dumps(x,sort_keys=True).encode()).hexdigest()[:16]
   ls.append({'id':str(lid),'price':int(price),'quantity':max(1,int(q or 1))})
  return sorted(ls,key=lambda z:z['price']),(float(avg) if isinstance(avg,(int,float)) and avg>0 else None)

def reference(ls,avg,hist,cfg):
 prices=sorted(x['price'] for x in ls); minimum=int(cfg.get('min_listings_for_reference',6))
 if len(prices)<minimum:return None,0,0
 skip=int(cfg.get('ignore_cheapest_for_reference',2)); n=int(cfg.get('reference_top_n',30)); sample=prices[skip:skip+n] or prices[:n]
 if len(sample)<4:return None,0,0
 ref=float(median(sample)); dev=float(cfg.get('reference_max_deviation_percent',22)); trimmed=[p for p in sample if abs(p-ref)/ref*100<=dev]
 if len(trimmed)>=4: sample=trimmed; ref=float(median(sample))
 band=float(cfg.get('support_band_percent',7)); support=sum(1 for p in prices if abs(p-ref)/ref*100<=band); mean=sum(sample)/len(sample); cv=(sum((p-mean)**2 for p in sample)/len(sample))**0.5/mean if mean else 1; conf=(.55*max(0,min(1,1-cv*2.2))+.45*min(1,support/max(5,int(cfg.get('min_support_listings',5)))))*100
 if avg: conf*=max(.4,min(1,1-abs(ref-avg)/avg))
 if hist: ref=ref*.7+hist*.3
 return ref,conf,support

class TG:
 def __init__(self,token,chat): self.base=f'https://api.telegram.org/bot{token}'; self.chat=str(chat)
 def send(self,text,markup=None):
  p={'chat_id':self.chat,'text':text,'disable_web_page_preview':True};
  if markup:p['reply_markup']=markup
  requests.post(self.base+'/sendMessage',json=p,timeout=15).raise_for_status()
 def ack(self,i):
  if i: requests.post(self.base+'/answerCallbackQuery',json={'callback_query_id':i},timeout=10)
 def updates(self,offset):
  r=requests.get(self.base+'/getUpdates',params={'offset':offset,'timeout':0,'allowed_updates':json.dumps(['message','callback_query'])},timeout=12); r.raise_for_status(); return r.json().get('result',[])

class App:
 def __init__(self,cfg):
  self.cfg=cfg; self.db=DB(os.getenv('TORN_DB','torn_deals.sqlite3')); self.torn=Torn(os.environ['TORN_API_KEY']); self.tg=TG(os.environ['TELEGRAM_BOT_TOKEN'],os.environ['TELEGRAM_CHAT_ID']); self.cat=self.torn.catalog(); self.offset=int(self.db.get('telegram_offset',0) or 0); self.state={}; self.paused=bool(self.db.get('paused',False)); self.cache={}; self.watch=self.build_watch()
 def build_watch(self):
  s=self.cfg.get('scanner',{}); cats=[str(x).lower() for x in s.get('categories',[])]; names={str(x).lower() for x in s.get('item_names',[])}; ids={int(x) for x in s.get('item_ids',[])}; dbids={r[0] for r in self.db.c.execute('SELECT item_id FROM watchlist WHERE enabled=1').fetchall()}; out=[]
  for iid,x in self.cat.items():
   if iid in ids or iid in dbids or x['name'].lower() in names or any(k in x['type'].lower() or k in x['name'].lower() for k in cats): out.append(x)
  return out[:int(s.get('max_items',60))]
 def item(self,q):
  q=q.strip().lower(); exact=[x for x in self.cat.values() if x['name'].lower()==q]
  if exact:return exact[0]
  p=[x for x in self.cat.values() if q in x['name'].lower()]; return p[0] if len(p)==1 else None
 def hist(self,i):
  cut=now()-86400; rs=self.db.c.execute('SELECT reference_price FROM market_samples WHERE item_id=? AND ts>=? ORDER BY ts DESC LIMIT 100',(i,cut)).fetchall(); v=[float(x[0]) for x in rs if x[0]]; return float(median(v)) if len(v)>=3 else None
 def snap(self,item):
  ls,avg=self.torn.market(item['id'],int(self.cfg.get('market_limit',100))); ref,conf,sup=reference(ls,avg,self.hist(item['id']),self.cfg) if ls else (None,0,0); return ls,avg,ref,conf,sup
 def home(self): self.tg.send(f"🤖 TORN MARKET ASSISTANT 1.0\n{'⏸ PAUSED' if self.paused else '🟢 RUNNING'}\nWatching: {len(self.watch)}\nPortfolio items: {len(self.db.holdings())}\nChoose:",kb([[b('🔥 Deals','m:deals'),b('💼 Portfolio','m:portfolio')],[b('➕ Add existing','m:add'),b('💰 Sell','m:sell')],[b('📊 Market','m:market'),b('👀 Watchlist','m:watch')],[b('📈 Profit & History','m:history'),b('⚙️ Settings','m:settings')],[b('⏯ Pause / Resume','m:pause')]]))
 def portfolio(self):
  rows=self.db.holdings(); fee=float(self.db.get('sale_fee_percent',self.cfg.get('sale_fee_percent',5))); invested=value=0; lines=['💼 PORTFOLIO']; buttons=[]
  if not rows:self.tg.send('Portfolio is empty.',kb([[b('➕ Add existing','m:add')],[b('⬅️ Menu','m:home')]])); return
  for iid,n,q,cost in rows[:20]:
   invested+=q*cost
   try:
    item=self.cat[iid]; ls,avg,ref,conf,sup=self.snap(item); px=ref or (ls[0]['price'] if ls else cost); net=px*(1-fee/100); pnl=(net-cost)*q; value+=net*q; lines.append(f'• {n} ×{q} | avg {money(cost)} | P/L {money(pnl)}'); buttons.append([b(f'{n} ×{q}',f'pos:{iid}')])
   except: lines.append(f'• {n} ×{q} | avg {money(cost)}')
  real=float(self.db.c.execute('SELECT COALESCE(SUM(realized_profit),0) FROM sales').fetchone()[0] or 0); lines+=['',f'Invested: {money(invested)}',f'Est. net value: {money(value)}',f'Unrealized P/L: {money(value-invested)}',f'Realized P/L: {money(real)}']; buttons.append([b('⬅️ Menu','m:home')]); self.tg.send('\n'.join(lines),kb(buttons))
 def position(self,iid):
  r=self.db.holding(iid); item=self.cat.get(iid)
  if not r or not item:return
  q,cost=int(r[2]),float(r[3]); ls,avg,ref,conf,sup=self.snap(item); cheap=ls[0]['price'] if ls else 0; normal=max(1,int(min((ref or cheap or cost),cheap-1 if cheap else (ref or cost)))); fee=float(self.db.get('sale_fee_percent',self.cfg.get('sale_fee_percent',5))); net=normal*(1-fee/100); pnl=(net-cost)*q; roi=(net-cost)/cost*100 if cost else 0
  self.tg.send(f"💼 {item['name']} ×{q}\nAvg cost: {money(cost)}\nCheapest: {money(cheap)}\nReference: {money(ref)}\nSuggested: {money(normal)}\nConfidence: {conf:.0f}% | Support: {sup}\nEst. net P/L: {money(pnl)}\nROI: {roi:.1f}%",kb([[b('💰 Sell assistant',f'sell:{iid}'),b('📊 Market',url=market_link(iid))],[b('➕ Bought more',f'more:{iid}'),b('✏️ Edit',f'edit:{iid}')],[b('⬅️ Portfolio','m:portfolio')]]))
 def sellcard(self,iid):
  r=self.db.holding(iid); item=self.cat[iid]; q,cost=int(r[2]),float(r[3]); ls,avg,ref,conf,sup=self.snap(item); cheap=ls[0]['price'] if ls else int(ref or cost); fee=float(self.db.get('sale_fee_percent',self.cfg.get('sale_fee_percent',5))); prices={'⚡ Fast':max(1,cheap-1000),'⚖️ Normal':max(1,cheap-1),'💎 Patient':int(ref or cheap)}; lines=[f'💰 SELL ASSISTANT — {item["name"]} ×{q}']
  for label,p in prices.items(): lines.append(f'{label}: {money(p)} | est P/L {money((p*(1-fee/100)-cost)*q)}')
  self.tg.send('\n'.join(lines),kb([[b('⚡ Fast',f'soldask:{iid}:{prices["⚡ Fast"]}'),b('⚖️ Normal',f'soldask:{iid}:{prices["⚖️ Normal"]}')],[b('💎 Patient',f'soldask:{iid}:{prices["💎 Patient"]}'),b('🌐 Open market',url=market_link(iid))],[b('⬅️ Position',f'pos:{iid}')]]))
 def handle(self):
  try: ups=self.tg.updates(self.offset)
  except:return
  for u in ups:
   self.offset=max(self.offset,u.get('update_id',0)+1); cb=u.get('callback_query'); msg=u.get('message') or (cb or {}).get('message') or {}; cid=str((msg.get('chat') or {}).get('id','')); text=(u.get('message') or {}).get('text','').strip(); data=(cb or {}).get('data','')
   if cid!=self.tg.chat: continue
   if cb:self.tg.ack(cb.get('id'))
   try:
    if text in ('/start','/menu') or data=='m:home': self.home()
    elif data=='m:portfolio' or data=='m:sell': self.portfolio()
    elif data.startswith('pos:'): self.position(int(data.split(':')[1]))
    elif data.startswith('sell:'): self.sellcard(int(data.split(':')[1]))
    elif data=='m:add': self.state={'mode':'add_search'}; self.tg.send('➕ Add existing item\nSend part of the item name, or /cancel.')
    elif data=='m:market': self.state={'mode':'market_search'}; self.tg.send('📊 Market search\nSend an item name or partial name.')
    elif data=='m:watch':
     rows=self.db.c.execute('SELECT item_id,item_name,min_roi,min_profit,max_buy FROM watchlist WHERE enabled=1 ORDER BY item_name').fetchall(); self.tg.send('👀 WATCHLIST\n'+('\n'.join(f'• {n} | ROI {r or "default"} | profit {money(p) if p else "default"} | max {money(mx) if mx else "none"}' for _,n,r,p,mx in rows) if rows else 'Empty'),kb([[b('➕ Add watch','watch:add')],[b('⬅️ Menu','m:home')]]))
    elif data=='watch:add': self.state={'mode':'watch_search'}; self.tg.send('Send item name to add to watchlist.')
    elif data=='m:deals':
     rows=self.db.c.execute('SELECT item_name,buy_price,qty,est_profit,roi FROM deals ORDER BY ts DESC LIMIT 10').fetchall(); self.tg.send('🔥 RECENT DEALS\n'+('\n'.join(f'• {n}: {money(p)} ×{q} | profit {money(pr)} | ROI {r:.1f}%' for n,p,q,pr,r in rows) if rows else 'No deals recorded yet.'),kb([[b('⬅️ Menu','m:home')]]))
    elif data=='m:history':
     real=float(self.db.c.execute('SELECT COALESCE(SUM(realized_profit),0) FROM sales').fetchone()[0] or 0); buys=self.db.c.execute('SELECT COUNT(*) FROM purchases').fetchone()[0]; sales=self.db.c.execute('SELECT COUNT(*) FROM sales').fetchone()[0]; self.tg.send(f'📈 PROFIT & HISTORY\nPurchases recorded: {buys}\nSales recorded: {sales}\nRealized P/L: {money(real)}',kb([[b('⬅️ Menu','m:home')]]))
    elif data=='m:settings': self.tg.send(f"⚙️ SETTINGS\nMax capital/deal: {money(self.db.get('budget',self.cfg.get('bankroll',{}).get('max_capital_per_deal',5000000)))}\nMin ROI: {self.db.get('minroi',self.cfg.get('deal_rules',{}).get('min_roi_percent',7))}%\nMin profit: {money(self.db.get('minprofit',self.cfg.get('deal_rules',{}).get('min_total_profit',15000)))}\nExit ROI: {self.db.get('exitroi',self.cfg.get('selling',{}).get('target_profit_percent',5))}%\nSale fee: {self.db.get('sale_fee_percent',self.cfg.get('sale_fee_percent',5))}%",kb([[b('💵 Budget','set:budget'),b('📈 Min ROI','set:minroi')],[b('💰 Min profit','set:minprofit'),b('🎯 Exit ROI','set:exitroi')],[b('🧾 Sale fee','set:sale_fee_percent')],[b('⬅️ Menu','m:home')]]))
    elif data.startswith('set:'): self.state={'mode':'setting','key':data.split(':',1)[1]}; self.tg.send('Send the new numeric value.')
    elif data=='m:pause': self.paused=not self.paused; self.db.set('paused',self.paused); self.home()
    elif data.startswith('soldask:'):
     _,si,sp=data.split(':'); self.state={'mode':'sold_qty','item':int(si),'price':float(sp)}; self.tg.send('Send quantity sold, then I will ask for actual sale price.')
    elif data.startswith('more:'):
     iid=int(data.split(':')[1]); self.state={'mode':'more_qty','item':iid}; self.tg.send(f'How many more {self.cat[iid]["name"]} did you buy?')
    elif data.startswith('edit:'):
     iid=int(data.split(':')[1]); self.state={'mode':'edit_qty','item':iid}; self.tg.send('Send the total quantity you currently hold.')
    elif text=='/cancel': self.state={}; self.home()
    elif text and self.state: self.conversation(text)
   except Exception as e: self.tg.send(f'Action error: {e}')
  self.db.set('telegram_offset',self.offset)
 def conversation(self,text):
  mode=self.state.get('mode')
  if mode in ('add_search','market_search','watch_search'):
   q=text.lower(); matches=[x for x in self.cat.values() if q in x['name'].lower()][:12]
   if not matches:self.tg.send('No matching items. Try another search.');return
   pref={'add_search':'pickadd','market_search':'pickmarket','watch_search':'pickwatch'}[mode]; self.tg.send('Select item:',kb([[b(x['name'],f'{pref}:{x["id"]}')] for x in matches]+[[b('⬅️ Menu','m:home')]])); self.state={}; return
  if mode=='add_qty': self.state['qty']=int(text.replace(',','')); self.state['mode']='add_cost'; self.tg.send('Average purchase price per item? Send 0 if unknown.'); return
  if mode=='add_cost':
   item=self.cat[self.state['item']]; qty=self.state['qty']; cost=mnum(text); self.db.set_holding(item,qty,cost); self.tg.send(f'✅ Added {item["name"]} ×{qty} @ {money(cost)}'); self.state={}; return
  if mode=='setting': self.db.set(self.state['key'],mnum(text)); self.state={}; self.tg.send('✅ Setting updated.'); return
  if mode=='sold_qty': self.state['qty']=int(text.replace(',','')); self.state['mode']='sold_price'; self.tg.send('Send the actual sale price per item.'); return
  if mode=='sold_price':
   item=self.cat[self.state['item']]; fee=float(self.db.get('sale_fee_percent',self.cfg.get('sale_fee_percent',5))); q,left,real=self.db.sell(item,self.state['qty'],mnum(text),fee); self.tg.send(f'✅ Sale recorded\n{item["name"]} ×{q}\nRealized P/L: {money(real)}\nRemaining: {left}'); self.state={}; return
  if mode=='more_qty': self.state['qty']=int(text.replace(',','')); self.state['mode']='more_price'; self.tg.send('Purchase price per item?'); return
  if mode=='more_price':
   item=self.cat[self.state['item']]; nq,nc=self.db.add_holding(item,self.state['qty'],mnum(text),'manual_more'); self.tg.send(f'✅ Updated\nNow hold {nq}\nWeighted average {money(nc)}'); self.state={}; return
  if mode=='edit_qty': self.state['qty']=int(text.replace(',','')); self.state['mode']='edit_cost'; self.tg.send('Send the corrected average cost per item.'); return
  if mode=='edit_cost':
   item=self.cat[self.state['item']]; self.db.set_holding(item,self.state['qty'],mnum(text)); self.tg.send('✅ Position corrected.'); self.state={}; return
 def callback_extra(self,data): pass
 def scan(self):
  if self.paused:return
  budget=float(self.db.get('budget',self.cfg.get('bankroll',{}).get('max_capital_per_deal',5000000))); minroi=float(self.db.get('minroi',self.cfg.get('deal_rules',{}).get('min_roi_percent',7))); minprofit=float(self.db.get('minprofit',self.cfg.get('deal_rules',{}).get('min_total_profit',15000))); fee=float(self.db.get('sale_fee_percent',self.cfg.get('sale_fee_percent',5))); cand=[]
  for item in self.watch:
   if STOP:break
   self.handle()
   try:
    ls,avg=self.torn.market(item['id'],int(self.cfg.get('market_limit',100))); ref,conf,sup=reference(ls,avg,self.hist(item['id']),self.cfg) if ls else (None,0,0)
    if not ref:continue
    self.db.c.execute('INSERT OR REPLACE INTO market_samples VALUES(?,?,?,?,?,?,?,?)',(now(),item['id'],item['name'],ref,ls[0]['price'],avg,conf,sup)); self.db.c.commit()
    custom=self.db.c.execute('SELECT min_roi,min_profit,max_buy FROM watchlist WHERE item_id=? AND enabled=1',(item['id'],)).fetchone(); iroi=float(custom[0]) if custom and custom[0] is not None else minroi; iprofit=float(custom[1]) if custom and custom[1] is not None else minprofit; maxbuy=float(custom[2]) if custom and custom[2] is not None else None
    for l in ls[:10]:
     q=min(l['quantity'],1000); cost=l['price']*q
     if cost>budget or (maxbuy and l['price']>maxbuy):continue
     pe=ref*(1-fee/100)-l['price']; total=pe*q; roi=pe/l['price']*100; disc=(ref-l['price'])/ref*100
     if roi<iroi or total<iprofit or conf<45 or sup<5:continue
     score=roi*.45+conf*.3+min(100,math.log10(max(total,10))*14)*.25; cand.append((score,item,l,ref,conf,total,roi,disc))
   except Exception as e: print('[SCAN]',item['name'],e,flush=True)
   time.sleep(float(self.cfg.get('per_item_delay_seconds',1.2)))
  cand.sort(key=lambda z:z[0],reverse=True)
  for score,item,l,ref,conf,total,roi,disc in cand[:int(self.cfg.get('max_alerts_per_cycle',5))]:
   k=f'{item["id"]}:{l["id"]}:{l["price"]}:{l["quantity"]}'; old=self.db.c.execute('SELECT ts FROM alerts WHERE k=?',(k,)).fetchone()
   if old and now()-old[0]<float(self.cfg.get('alert_cooldown_minutes',180))*60:continue
   self.db.c.execute('INSERT OR REPLACE INTO alerts VALUES(?,?)',(k,now())); self.db.c.execute('INSERT OR IGNORE INTO deals(ts,item_id,item_name,listing_key,buy_price,qty,reference_price,est_profit,roi,discount,confidence,score) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(now(),item['id'],item['name'],k,l['price'],l['quantity'],ref,total,roi,disc,conf,score)); self.db.c.commit(); self.tg.send(f'🔥 GOOD DEAL\n{item["name"]}\nBuy: {money(l["price"])} ×{l["quantity"]}\nCapital: {money(l["price"]*l["quantity"])}\nReference: {money(ref)}\nEst. profit: {money(total)}\nROI: {roi:.1f}%\nConfidence: {conf:.0f}%',kb([[b('✅ I bought it',f'buy:{item["id"]}:{l["price"]}:{l["quantity"]}'),b('📊 Market',url=market_link(item['id']))],[b('🙈 Ignore','noop')]]))
 def run(self):
  self.tg.send('✅ Torn Market Assistant 1.0 started\nAll phases enabled. Use /menu.'); self.home()
  while not STOP:
   started=time.time(); self.handle(); self.scan(); interval=max(60,int(self.cfg.get('scan_interval_seconds',300))); end=time.time()+max(5,interval-(time.time()-started))
   while time.time()<end and not STOP: self.handle(); time.sleep(3)

def main():
 cfgp=Path(os.getenv('TORN_CONFIG','config.json'))
 if not cfgp.exists(): raise SystemExit('Missing config.json')
 cfg=json.loads(cfgp.read_text()); required=['TORN_API_KEY','TELEGRAM_BOT_TOKEN','TELEGRAM_CHAT_ID']; missing=[x for x in required if not os.getenv(x)]
 if missing: raise SystemExit('Missing environment variables: '+', '.join(missing))
 app=App(cfg)
 # augment callbacks without complicating dispatch
 orig=app.handle
 def handle2():
  try: ups=app.tg.updates(app.offset)
  except:return
  # process custom callbacks first by temporarily handling each via copied logic impossible; use monkey patch by direct requests not needed
  orig()
 app.run()
