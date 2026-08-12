import os,time,json
from pathlib import Path
from .core import App,DB,Torn,TG,STOP,b,kb,money,mnum,market_link,main as _old_main

class MarketAssistant(App):
 def handle(self):
  try: ups=self.tg.updates(self.offset)
  except Exception:return
  for u in ups:
   self.offset=max(self.offset,u.get('update_id',0)+1); cb=u.get('callback_query'); msg=u.get('message') or (cb or {}).get('message') or {}; cid=str((msg.get('chat') or {}).get('id','')); text=(u.get('message') or {}).get('text','').strip(); data=(cb or {}).get('data','')
   if cid!=self.tg.chat:continue
   if cb:self.tg.ack(cb.get('id'))
   try:
    if text in ('/start','/menu') or data=='m:home':self.home()
    elif text=='/cancel':self.state={};self.home()
    elif data=='noop':pass
    elif data=='m:portfolio' or data=='m:sell':self.portfolio()
    elif data.startswith('pos:'):self.position(int(data.split(':')[1]))
    elif data.startswith('sell:'):self.sellcard(int(data.split(':')[1]))
    elif data=='m:add':self.state={'mode':'add_search'};self.tg.send('➕ Add existing item\nSend part of the item name, or /cancel.')
    elif data=='m:market':self.state={'mode':'market_search'};self.tg.send('📊 Market search\nSend an item name or partial name.')
    elif data=='m:watch':
     rows=self.db.c.execute('SELECT item_id,item_name,min_roi,min_profit,max_buy FROM watchlist WHERE enabled=1 ORDER BY item_name').fetchall(); buttons=[[b('➕ Add watch','watch:add')]]+[[b(f'❌ {n}',f'watch:remove:{i}')] for i,n,*_ in rows[:15]]+[[b('⬅️ Menu','m:home')]]; self.tg.send('👀 WATCHLIST\n'+('\n'.join(f'• {n}' for _,n,*_ in rows) if rows else 'Empty'),kb(buttons))
    elif data=='watch:add':self.state={'mode':'watch_search'};self.tg.send('Send item name to add to watchlist.')
    elif data.startswith('watch:remove:'):
     i=int(data.split(':')[-1]);self.db.c.execute('DELETE FROM watchlist WHERE item_id=?',(i,));self.db.c.commit();self.watch=self.build_watch();self.tg.send('✅ Removed from watchlist.')
    elif data=='m:deals':
     rows=self.db.c.execute('SELECT item_name,buy_price,qty,est_profit,roi FROM deals ORDER BY ts DESC LIMIT 10').fetchall();self.tg.send('🔥 RECENT DEALS\n'+('\n'.join(f'• {n}: {money(p)} ×{q} | profit {money(pr)} | ROI {r:.1f}%' for n,p,q,pr,r in rows) if rows else 'No deals recorded yet.'),kb([[b('⬅️ Menu','m:home')]]))
    elif data=='m:history':
     real=float(self.db.c.execute('SELECT COALESCE(SUM(realized_profit),0) FROM sales').fetchone()[0] or 0);buys=self.db.c.execute('SELECT COUNT(*) FROM purchases').fetchone()[0];sales=self.db.c.execute('SELECT COUNT(*) FROM sales').fetchone()[0];self.tg.send(f'📈 PROFIT & HISTORY\nPurchases recorded: {buys}\nSales recorded: {sales}\nRealized P/L: {money(real)}',kb([[b('⬅️ Menu','m:home')]]))
    elif data=='m:settings':
     self.tg.send(f"⚙️ SETTINGS\nMax capital/deal: {money(self.db.get('budget',self.cfg.get('bankroll',{}).get('max_capital_per_deal',5000000)))}\nMin ROI: {self.db.get('minroi',self.cfg.get('deal_rules',{}).get('min_roi_percent',7))}%\nMin profit: {money(self.db.get('minprofit',self.cfg.get('deal_rules',{}).get('min_total_profit',15000)))}\nExit ROI: {self.db.get('exitroi',self.cfg.get('selling',{}).get('target_profit_percent',5))}%\nSale fee: {self.db.get('sale_fee_percent',self.cfg.get('sale_fee_percent',5))}%",kb([[b('💵 Budget','set:budget'),b('📈 Min ROI','set:minroi')],[b('💰 Min profit','set:minprofit'),b('🎯 Exit ROI','set:exitroi')],[b('🧾 Sale fee','set:sale_fee_percent')],[b('⬅️ Menu','m:home')]]))
    elif data.startswith('set:'):self.state={'mode':'setting','key':data.split(':',1)[1]};self.tg.send('Send the new numeric value.')
    elif data=='m:pause':self.paused=not self.paused;self.db.set('paused',self.paused);self.home()
    elif data.startswith('pickadd:'):
     iid=int(data.split(':')[1]);self.state={'mode':'add_qty','item':iid};self.tg.send(f'How many {self.cat[iid]["name"]} do you currently own?')
    elif data.startswith('pickmarket:'):
     iid=int(data.split(':')[1]);item=self.cat[iid];ls,avg,ref,conf,sup=self.snap(item);lines=[f'📊 {item["name"]} [{item["type"]}]',f'Listings: {len(ls)}']
     if ls:lines += [f'Cheapest: {money(ls[0]["price"])}',f'Qty at cheapest: {ls[0]["quantity"]}']
     lines += [f'API average: {money(avg)}',f'Reference: {money(ref)}',f'Confidence: {conf:.0f}%',f'Support: {sup} listings'];self.tg.send('\n'.join(lines),kb([[b('🌐 Open Torn market',url=market_link(iid))],[b('⬅️ Menu','m:home')]]))
    elif data.startswith('pickwatch:'):
     iid=int(data.split(':')[1]);item=self.cat[iid];self.db.c.execute('INSERT OR REPLACE INTO watchlist(item_id,item_name,enabled) VALUES(?,?,1)',(iid,item['name']));self.db.c.commit();self.watch=self.build_watch();self.tg.send(f'✅ Watching {item["name"]}.')
    elif data.startswith('buy:'):
     _,si,sp,sq=data.split(':');iid=int(si);self.state={'mode':'buy_qty','item':iid,'price':float(sp),'max':int(sq)};maxq=int(sq);opts=[b(str(x),f'buyqty:{iid}:{sp}:{x}') for x in range(1,min(maxq,5)+1)];self.tg.send(f'✅ {self.cat[iid]["name"]}\nHow many did you actually buy?',kb([opts,[b('Custom quantity','buycustom:'+si+':'+sp+':'+sq)],[b('Cancel','m:home')]]))
    elif data.startswith('buyqty:'):
     _,si,sp,sq=data.split(':');iid=int(si);q=int(sq);nq,nc=self.db.add_holding(self.cat[iid],q,float(sp),'deal_confirmation');self.tg.send(f'✅ Purchase recorded\n{self.cat[iid]["name"]} +{q} @ {money(float(sp))}\nNow hold: {nq}\nWeighted avg: {money(nc)}',kb([[b('💼 Portfolio','m:portfolio')]]));self.state={}
    elif data.startswith('buycustom:'):
     _,si,sp,sq=data.split(':');self.state={'mode':'buy_qty','item':int(si),'price':float(sp),'max':int(sq)};self.tg.send(f'Send quantity actually bought (1-{sq}).')
    elif data.startswith('soldask:'):
     _,si,sp=data.split(':');self.state={'mode':'sold_qty','item':int(si),'price':float(sp)};self.tg.send('Send quantity sold. I will then ask for the actual sale price.')
    elif data.startswith('more:'):
     iid=int(data.split(':')[1]);self.state={'mode':'more_qty','item':iid};self.tg.send(f'How many more {self.cat[iid]["name"]} did you buy?')
    elif data.startswith('edit:'):
     iid=int(data.split(':')[1]);self.state={'mode':'edit_qty','item':iid};self.tg.send('Send the total quantity you currently hold.')
    elif data.startswith('remove:'):
     iid=int(data.split(':')[1]);self.db.remove_holding(iid);self.tg.send('✅ Removed from portfolio.')
    elif text and self.state:self.conversation2(text)
   except Exception as e:self.tg.send(f'Action error: {e}')
  self.db.set('telegram_offset',self.offset)
 def conversation2(self,text):
  mode=self.state.get('mode')
  if mode in ('add_search','market_search','watch_search'):
   q=text.lower();matches=[x for x in self.cat.values() if q in x['name'].lower()][:12]
   if not matches:self.tg.send('No matching items. Try another search.');return
   pref={'add_search':'pickadd','market_search':'pickmarket','watch_search':'pickwatch'}[mode];self.tg.send('Select item:',kb([[b(x['name'],f'{pref}:{x["id"]}')] for x in matches]+[[b('⬅️ Menu','m:home')]]));self.state={};return
  if mode=='add_qty':self.state['qty']=max(1,int(text.replace(',','')));self.state['mode']='add_cost';self.tg.send('Average purchase price per item? Send 0 if unknown.');return
  if mode=='add_cost':
   item=self.cat[self.state['item']];qty=self.state['qty'];cost=mnum(text);self.db.set_holding(item,qty,cost);self.db.c.execute('INSERT INTO purchases(ts,item_id,item_name,qty,cost_each,source) VALUES(?,?,?,?,?,?)',(int(time.time()),item['id'],item['name'],qty,cost,'existing_import'));self.db.c.commit();self.tg.send(f'✅ Added {item["name"]} ×{qty} @ {money(cost)}',kb([[b('💼 Portfolio','m:portfolio')]]));self.state={};return
  if mode=='buy_qty':
   q=max(1,min(int(text.replace(',','')),int(self.state['max'])));item=self.cat[self.state['item']];nq,nc=self.db.add_holding(item,q,float(self.state['price']),'deal_confirmation');self.tg.send(f'✅ Purchase recorded\n{item["name"]} +{q}\nNow hold: {nq}\nWeighted avg: {money(nc)}');self.state={};return
  if mode=='setting':self.db.set(self.state['key'],mnum(text));self.state={};self.tg.send('✅ Setting updated.');return
  if mode=='sold_qty':self.state['qty']=max(1,int(text.replace(',','')));self.state['mode']='sold_price';self.tg.send('Send actual sale price per item.');return
  if mode=='sold_price':
   item=self.cat[self.state['item']];fee=float(self.db.get('sale_fee_percent',self.cfg.get('sale_fee_percent',5)));q,left,real=self.db.sell(item,self.state['qty'],mnum(text),fee);self.tg.send(f'✅ Sale recorded\n{item["name"]} ×{q}\nRealized P/L: {money(real)}\nRemaining: {left}',kb([[b('💼 Portfolio','m:portfolio')]]));self.state={};return
  if mode=='more_qty':self.state['qty']=max(1,int(text.replace(',','')));self.state['mode']='more_price';self.tg.send('Purchase price per item?');return
  if mode=='more_price':
   item=self.cat[self.state['item']];nq,nc=self.db.add_holding(item,self.state['qty'],mnum(text),'manual_more');self.tg.send(f'✅ Now hold {nq}\nWeighted avg: {money(nc)}');self.state={};return
  if mode=='edit_qty':self.state['qty']=max(1,int(text.replace(',','')));self.state['mode']='edit_cost';self.tg.send('Send corrected average cost per item.');return
  if mode=='edit_cost':
   item=self.cat[self.state['item']];self.db.set_holding(item,self.state['qty'],mnum(text));self.tg.send('✅ Position corrected.');self.state={};return

def main():
 p=Path(os.getenv('TORN_CONFIG','config.json'))
 if not p.exists():raise SystemExit('Missing config.json')
 cfg=json.loads(p.read_text());required=['TORN_API_KEY','TELEGRAM_BOT_TOKEN','TELEGRAM_CHAT_ID'];missing=[x for x in required if not os.getenv(x)]
 if missing:raise SystemExit('Missing environment variables: '+', '.join(missing))
 app=MarketAssistant(cfg);app.run()
