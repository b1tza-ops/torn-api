import os,time,json
from pathlib import Path
from .runtime import MarketAssistant
from .core import money,kb,b,now
from .island import dashboard_text, capital_limits, snapshot
from .links import market_link, fallback_market_link, register_catalog
from . import core as core_module
from . import runtime as runtime_module

class FullAssistant(MarketAssistant):
 def __init__(self,cfg):
  super().__init__(cfg)
  # Register Torn's item metadata so every market link contains ID + name + type.
  # Patch the legacy module globals too, because inherited scanner/menu methods
  # resolve market_link from those modules at runtime.
  register_catalog(self.cat)
  core_module.market_link=market_link
  runtime_module.market_link=market_link

 def market_buttons(self,iid,back_data=None):
  rows=[[b('🌐 Open item market',url=market_link(iid)),b('↪️ Fallback link',url=fallback_market_link(iid))]]
  if back_data: rows.append([b('⬅️ Back',back_data)])
  return kb(rows)

 def home(self):
  s=snapshot(self.db)
  self.tg.send(
   f"🤖 TORN MARKET ASSISTANT 1.1\n{'⏸ PAUSED' if self.paused else '🟢 RUNNING'}\n"
   f"🏝️ Island fund: {money(s['equity_cost'])} / $500,000,000 ({s['progress']:.2f}%)\n"
   f"Watching: {len(self.watch)} | Portfolio items: {len(self.db.holdings())}\nChoose:",
   kb([[b('🏝️ Island Fund','m:island'),b('🔥 Deals','m:deals')],
       [b('💼 Portfolio','m:portfolio'),b('➕ Add existing','m:add')],
       [b('💰 Sell','m:sell'),b('📊 Market','m:market')],
       [b('👀 Watchlist','m:watch'),b('📈 Profit & History','m:history')],
       [b('⚙️ Settings','m:settings'),b('⏯ Pause / Resume','m:pause')]]))

 def island(self):
  limits=capital_limits(self.db,self.cfg.get('bankroll',{}))
  text=dashboard_text(self.db)+(
   f"\n\n🛡️ CAPITAL GUARDRAILS\n"
   f"Max one deal: {money(limits['max_deal'])}\n"
   f"Max one item exposure: {money(limits['max_item'])}\n"
   f"Target cash reserve: {money(limits['reserve'])}")
  self.tg.send(text,kb([[b('💵 Update cash','island:cash')],[b('⚙️ Risk limits','island:risk')],[b('⬅️ Menu','m:home')]]))

 def handle(self):
  # Let the existing handler process normal commands/callbacks first.
  # Island-specific updates are intercepted by temporarily reading updates here.
  try: ups=self.tg.updates(self.offset)
  except Exception: return super().handle()
  island_events=[]; other=[]
  for u in ups:
   cb=u.get('callback_query'); msg=u.get('message') or (cb or {}).get('message') or {}; cid=str((msg.get('chat') or {}).get('id',''))
   if cid!=self.tg.chat: other.append(u); continue
   text=(u.get('message') or {}).get('text','').strip(); data=(cb or {}).get('data','')
   if text.startswith('/cash') or text=='/island' or data.startswith('m:island') or data.startswith('island:') or self.state.get('mode','').startswith('island_'):
    island_events.append(u)
   else: other.append(u)
  # We cannot push updates back to Telegram, so directly process this batch.
  if not island_events:
   return super().handle()
  for u in ups:
   self.offset=max(self.offset,u.get('update_id',0)+1); cb=u.get('callback_query'); msg=u.get('message') or (cb or {}).get('message') or {}; cid=str((msg.get('chat') or {}).get('id','')); text=(u.get('message') or {}).get('text','').strip(); data=(cb or {}).get('data','')
   if cid!=self.tg.chat: continue
   if cb:self.tg.ack(cb.get('id'))
   try:
    if text=='/island' or data=='m:island': self.island()
    elif text.startswith('/cash'):
     parts=text.split(maxsplit=1)
     if len(parts)==2:
      self.db.set('liquid_cash',float(parts[1].replace(',','').replace('$','')));self.tg.send('✅ Liquid cash updated.');self.island()
     else:self.state={'mode':'island_cash'};self.tg.send('Send your current liquid Torn cash, e.g. 23000000.')
    elif data=='island:cash':self.state={'mode':'island_cash'};self.tg.send('Send your current liquid Torn cash, e.g. 23000000.')
    elif data=='island:risk':
     self.tg.send(
      f"⚙️ ISLAND RISK LIMITS\nMax/deal: {self.db.get('max_deal_bankroll_percent',20)}% of tracked capital\n"
      f"Max/item: {self.db.get('max_item_bankroll_percent',30)}%\nCash reserve: {self.db.get('cash_reserve_percent',20)}%",
      kb([[b('Deal %','island:set:deal'),b('Item %','island:set:item')],[b('Reserve %','island:set:reserve')],[b('⬅️ Island','m:island')]]))
    elif data.startswith('island:set:'):
     self.state={'mode':'island_setting','key':data.split(':')[-1]};self.tg.send('Send percentage as a number, e.g. 20.')
    elif self.state.get('mode')=='island_cash' and text:
     self.db.set('liquid_cash',float(text.replace(',','').replace('$','')));self.state={};self.tg.send('✅ Liquid cash updated.');self.island()
    elif self.state.get('mode')=='island_setting' and text:
     mp={'deal':'max_deal_bankroll_percent','item':'max_item_bankroll_percent','reserve':'cash_reserve_percent'};v=max(1,min(90,float(text.replace('%','').strip())));self.db.set(mp[self.state['key']],v);self.state={};self.tg.send('✅ Risk limit updated.');self.island()
    else:
     # Process common navigation locally so an island batch does not swallow it.
     if data=='m:home' or text in ('/start','/menu'): self.home()
   except Exception as e:self.tg.send(f'Action error: {e}')
  self.db.set('telegram_offset',self.offset)

 def scan(self):
  # Dynamically constrain scanner capital to the island strategy before each cycle.
  limits=capital_limits(self.db,self.cfg.get('bankroll',{}))
  manual_budget=self.db.get('budget',None)
  strategic_budget=limits['max_deal']
  if manual_budget is None or float(manual_budget)>strategic_budget:
   self.db.set('budget',strategic_budget)
  super().scan()
  if self.paused:return
  target=float(self.db.get('exitroi',self.cfg.get('selling',{}).get('target_profit_percent',5)))
  fee=float(self.db.get('sale_fee_percent',self.cfg.get('sale_fee_percent',5)))
  for iid,name,qty,cost in self.db.holdings():
   try:
    item=self.cat.get(iid)
    if not item or cost<=0:continue
    ls,avg,ref,conf,sup=self.snap(item)
    if not ls:continue
    cheap=ls[0]['price']; suggested=max(1,cheap-1); suggested=min(suggested,int(ref)) if ref else suggested
    roi=(suggested*(1-fee/100)-cost)/cost*100
    if roi<target or conf<55 or sup<5:continue
    bucket=int(roi*2)/2; key=f'exit:{iid}:{bucket}'
    old=self.db.c.execute('SELECT ts FROM alerts WHERE k=?',(key,)).fetchone()
    if old and now()-old[0]<3600:continue
    self.db.c.execute('INSERT OR REPLACE INTO alerts VALUES(?,?)',(key,now()));self.db.c.commit()
    pnl=(suggested*(1-fee/100)-cost)*qty
    label='⚡ QUICK EXIT' if roi < target+3 else ('💎 VALUE EXIT' if roi < target+10 else '🐋 BIG PROFIT EXIT')
    self.tg.send(
     f'{label}\n{name} ×{qty}\nAvg cost: {money(cost)}\nSuggested: {money(suggested)}\nEst. net P/L: {money(pnl)}\nROI: {roi:.1f}%\nConfidence: {conf:.0f}%',
     kb([[b('💼 Review position',f'pos:{iid}')],
         [b('🌐 Open market',url=market_link(iid)),b('↪️ Fallback',url=fallback_market_link(iid))],
         [b('🏝️ Island Fund','m:island')]]))
   except Exception as e: print('[EXIT]',name,e,flush=True)

def main():
 p=Path(os.getenv('TORN_CONFIG','config.json'))
 if not p.exists():raise SystemExit('Missing config.json')
 cfg=json.loads(p.read_text());required=['TORN_API_KEY','TELEGRAM_BOT_TOKEN','TELEGRAM_CHAT_ID'];missing=[x for x in required if not os.getenv(x)]
 if missing:raise SystemExit('Missing environment variables: '+', '.join(missing))
 FullAssistant(cfg).run()
