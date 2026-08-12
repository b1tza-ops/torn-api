import os,time,json
from pathlib import Path
from .runtime import MarketAssistant
from .core import money,kb,b,market_link,reference,now

class FullAssistant(MarketAssistant):
 def scan(self):
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
    self.tg.send(f'💰 SELL OPPORTUNITY\n{name} ×{qty}\nAvg cost: {money(cost)}\nSuggested: {money(suggested)}\nEst. net P/L: {money(pnl)}\nROI: {roi:.1f}%\nConfidence: {conf:.0f}%',kb([[b('💼 Review position',f'pos:{iid}'),b('📊 Market',url=market_link(iid))]]))
   except Exception as e: print('[EXIT]',name,e,flush=True)

def main():
 p=Path(os.getenv('TORN_CONFIG','config.json'))
 if not p.exists():raise SystemExit('Missing config.json')
 cfg=json.loads(p.read_text());required=['TORN_API_KEY','TELEGRAM_BOT_TOKEN','TELEGRAM_CHAT_ID'];missing=[x for x in required if not os.getenv(x)]
 if missing:raise SystemExit('Missing environment variables: '+', '.join(missing))
 FullAssistant(cfg).run()
