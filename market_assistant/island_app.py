#!/usr/bin/env python3
from __future__ import annotations
import json, math, os, time
from pathlib import Path
from statistics import median

from .core import DB, Torn, TG, reference, now, money, mnum, kb, b, market_link, STOP
from .island import dashboard_text, capital_limits, snapshot, GOAL


class IslandApp:
    def __init__(self, cfg):
        self.cfg = cfg
        self.db = DB(os.getenv('TORN_DB','torn_deals.sqlite3'))
        self.torn = Torn(os.environ['TORN_API_KEY'])
        self.tg = TG(os.environ['TELEGRAM_BOT_TOKEN'], os.environ['TELEGRAM_CHAT_ID'])
        self.cat = self.torn.catalog()
        self.offset = int(self.db.get('telegram_offset',0) or 0)
        self.state = {}
        self.paused = bool(self.db.get('paused',False))
        self.watch = self.build_watch()

    def build_watch(self):
        s=self.cfg.get('scanner',{}); cats=[str(x).lower() for x in s.get('categories',[])]; names={str(x).lower() for x in s.get('item_names',[])}; ids={int(x) for x in s.get('item_ids',[])}
        dbids={r[0] for r in self.db.c.execute('SELECT item_id FROM watchlist WHERE enabled=1').fetchall()}
        out=[]
        for iid,x in self.cat.items():
            if iid in ids or iid in dbids or x['name'].lower() in names or any(k in x['type'].lower() or k in x['name'].lower() for k in cats): out.append(x)
        return out[:int(s.get('max_items',60))]

    def hist(self,i):
        cut=now()-86400; rs=self.db.c.execute('SELECT reference_price FROM market_samples WHERE item_id=? AND ts>=? ORDER BY ts DESC LIMIT 100',(i,cut)).fetchall(); vals=[float(x[0]) for x in rs if x[0]]
        return float(median(vals)) if len(vals)>=3 else None

    def snap(self,item):
        ls,avg=self.torn.market(item['id'],int(self.cfg.get('market_limit',100))); ref,conf,sup=reference(ls,avg,self.hist(item['id']),self.cfg) if ls else (None,0,0)
        return ls,avg,ref,conf,sup

    def main_menu(self):
        s=snapshot(self.db,GOAL)
        return kb([
            [b('🏝️ Island Fund','m:island'), b('🔥 Deals','m:deals')],
            [b('💼 Portfolio','m:portfolio'), b('💰 Sell','m:sell')],
            [b('➕ Add existing','m:add'), b('📊 Market','m:market')],
            [b('👀 Watchlist','m:watch'), b('⚙️ Settings','m:settings')],
            [b('⏯ Pause / Resume','m:pause')]
        ])

    def home(self):
        s=snapshot(self.db,GOAL)
        self.tg.send(
            f"🤖 TORN MARKET ASSISTANT — ISLAND MODE\n"
            f"{'⏸ PAUSED' if self.paused else '🟢 RUNNING'}\n"
            f"🏝️ {s['progress']:.2f}% of $500m\n"
            f"Tracked capital: {money(s['equity_cost'])}\n"
            f"Watching: {len(self.watch)} | Positions: {len(self.db.holdings())}",
            self.main_menu())

    def island(self):
        lim=capital_limits(self.db,self.cfg.get('bankroll',{}))
        txt=dashboard_text(self.db,GOAL)
        txt += (f"\n\n🛡️ CAPITAL RULES\nMax/deal: {money(lim['max_deal'])}\n"
                f"Max/item exposure: {money(lim['max_item'])}\nCash reserve: {money(lim['reserve'])}")
        self.tg.send(txt,kb([[b('💵 Set cash','island:cash')],[b('⬅️ Menu','m:home')]]))

    def portfolio(self):
        rows=self.db.holdings(); fee=float(self.db.get('sale_fee_percent',self.cfg.get('sale_fee_percent',5)))
        if not rows:
            self.tg.send('💼 Portfolio is empty.',kb([[b('➕ Add existing','m:add')],[b('⬅️ Menu','m:home')]])); return
        invested=value=0; lines=['💼 PORTFOLIO']; buttons=[]
        for iid,n,q,cost in rows[:20]:
            invested += q*cost
            try:
                item=self.cat[iid]; ls,avg,ref,conf,sup=self.snap(item); px=ref or (ls[0]['price'] if ls else cost); net=px*(1-fee/100); pnl=(net-cost)*q; value += net*q
                lines.append(f'• {n} ×{q} | P/L {money(pnl)}'); buttons.append([b(f'{n} ×{q}',f'pos:{iid}')])
            except Exception: lines.append(f'• {n} ×{q} | avg {money(cost)}')
        real=float(self.db.c.execute('SELECT COALESCE(SUM(realized_profit),0) FROM sales').fetchone()[0] or 0)
        lines += ['',f'Invested: {money(invested)}',f'Est. net value: {money(value)}',f'Unrealized P/L: {money(value-invested)}',f'Realized P/L: {money(real)}']
        buttons.append([b('⬅️ Menu','m:home')]); self.tg.send('\n'.join(lines),kb(buttons))

    def position(self,iid):
        r=self.db.holding(iid); item=self.cat.get(iid)
        if not r or not item:return
        q,cost=int(r[2]),float(r[3]); ls,avg,ref,conf,sup=self.snap(item); cheap=ls[0]['price'] if ls else 0
        normal=max(1,int(min((ref or cheap or cost),cheap-1 if cheap else (ref or cost))))
        fee=float(self.db.get('sale_fee_percent',self.cfg.get('sale_fee_percent',5))); net=normal*(1-fee/100); pnl=(net-cost)*q; roi=(net-cost)/cost*100 if cost else 0
        self.tg.send(f"💼 {item['name']} ×{q}\nAvg cost: {money(cost)}\nCheapest: {money(cheap)}\nReference: {money(ref)}\nSuggested: {money(normal)}\nConfidence: {conf:.0f}% | Support: {sup}\nEst. net P/L: {money(pnl)}\nROI: {roi:.1f}%",kb([[b('💰 Sell assistant',f'sell:{iid}'),b('📊 Market',url=market_link(iid))],[b('➕ Bought more',f'more:{iid}'),b('✏️ Edit',f'edit:{iid}')],[b('⬅️ Portfolio','m:portfolio')]]))

    def sellcard(self,iid):
        r=self.db.holding(iid); item=self.cat[iid]; q,cost=int(r[2]),float(r[3]); ls,avg,ref,conf,sup=self.snap(item); cheap=ls[0]['price'] if ls else int(ref or cost)
        fee=float(self.db.get('sale_fee_percent',self.cfg.get('sale_fee_percent',5)))
        prices={'⚡ Fast':max(1,cheap-1000),'⚖️ Normal':max(1,cheap-1),'💎 Patient':int(ref or cheap)}
        lines=[f'💰 SELL ASSISTANT — {item["name"]} ×{q}']
        for label,p in prices.items(): lines.append(f'{label}: {money(p)} | est P/L {money((p*(1-fee/100)-cost)*q)}')
        self.tg.send('\n'.join(lines),kb([[b('⚡ Fast',f'soldask:{iid}:{prices["⚡ Fast"]}'),b('⚖️ Normal',f'soldask:{iid}:{prices["⚖️ Normal"]}')],[b('💎 Patient',f'soldask:{iid}:{prices["💎 Patient"]}'),b('🌐 Open market',url=market_link(iid))],[b('⬅️ Position',f'pos:{iid}')]]))

    def deal_type(self,roi,total,conf):
        if total >= 1_000_000 and conf >= 60:return '🐋 BIG PROFIT'
        if roi >= 12:return '💎 VALUE FLIP'
        return '🔥 QUICK FLIP'

    def score_deal(self,roi,total,conf,capital):
        efficiency=(total/max(capital,1))*100
        absolute=min(100,math.log10(max(total,10))*14)
        return roi*.32 + conf*.27 + efficiency*.26 + absolute*.15

    def handle(self):
        try: ups=self.tg.updates(self.offset)
        except Exception:return
        for u in ups:
            self.offset=max(self.offset,u.get('update_id',0)+1); cb=u.get('callback_query'); msg=u.get('message') or (cb or {}).get('message') or {}; cid=str((msg.get('chat') or {}).get('id','')); text=(u.get('message') or {}).get('text','').strip(); data=(cb or {}).get('data','')
            if cid!=self.tg.chat:continue
            if cb:self.tg.ack(cb.get('id'))
            try:
                if text in ('/start','/menu') or data=='m:home': self.home()
                elif text.startswith('/cash '): self.db.set('liquid_cash',mnum(text.split(' ',1)[1])); self.island()
                elif data=='m:island': self.island()
                elif data=='island:cash': self.state={'mode':'cash'}; self.tg.send('Send your current liquid Torn cash, e.g. 23000000')
                elif data in ('m:portfolio','m:sell'): self.portfolio()
                elif data.startswith('pos:'): self.position(int(data.split(':')[1]))
                elif data.startswith('sell:'): self.sellcard(int(data.split(':')[1]))
                elif data=='m:add': self.state={'mode':'search','purpose':'add'}; self.tg.send('Send part of the item name.')
                elif data=='m:market': self.state={'mode':'search','purpose':'market'}; self.tg.send('Send item name or partial name.')
                elif data=='m:watch': self.show_watch()
                elif data=='watch:add': self.state={'mode':'search','purpose':'watch'}; self.tg.send('Send item name to add.')
                elif data=='m:deals': self.show_deals()
                elif data=='m:settings': self.show_settings()
                elif data.startswith('set:'): self.state={'mode':'setting','key':data.split(':',1)[1]}; self.tg.send('Send the new numeric value.')
                elif data=='m:pause': self.paused=not self.paused; self.db.set('paused',self.paused); self.home()
                elif data.startswith('pickadd:'): iid=int(data.split(':')[1]); self.state={'mode':'add_qty','item':iid}; self.tg.send(f"Quantity of {self.cat[iid]['name']}?")
                elif data.startswith('pickmarket:'): self.market_card(int(data.split(':')[1]))
                elif data.startswith('pickwatch:'): self.add_watch(int(data.split(':')[1]))
                elif data.startswith('buy:'):
                    _,si,sp,sq=data.split(':'); self.state={'mode':'buy_qty','item':int(si),'price':float(sp),'max':int(sq)}; self.tg.send(f'How many did you actually buy? Max detected: {sq}')
                elif data.startswith('soldask:'):
                    _,si,sp=data.split(':'); self.state={'mode':'sold_qty','item':int(si),'price':float(sp)}; self.tg.send('Send quantity sold.')
                elif data.startswith('more:'): iid=int(data.split(':')[1]); self.state={'mode':'more_qty','item':iid}; self.tg.send('How many more did you buy?')
                elif data.startswith('edit:'): iid=int(data.split(':')[1]); self.state={'mode':'edit_qty','item':iid}; self.tg.send('Send corrected total quantity.')
                elif text=='/cancel': self.state={}; self.home()
                elif text and self.state:self.conversation(text)
            except Exception as e:self.tg.send(f'Action error: {e}')
        self.db.set('telegram_offset',self.offset)

    def conversation(self,text):
        mode=self.state.get('mode')
        if mode=='cash': self.db.set('liquid_cash',mnum(text)); self.state={}; self.island(); return
        if mode=='search':
            q=text.lower(); matches=[x for x in self.cat.values() if q in x['name'].lower()][:12]
            if not matches:self.tg.send('No matches.');return
            pref={'add':'pickadd','market':'pickmarket','watch':'pickwatch'}[self.state['purpose']]
            self.tg.send('Select item:',kb([[b(x['name'],f'{pref}:{x["id"]}')] for x in matches]+[[b('⬅️ Menu','m:home')]])); self.state={}; return
        if mode=='add_qty': self.state['qty']=int(text.replace(',','')); self.state['mode']='add_cost'; self.tg.send('Average purchase price each? Send 0 if unknown.'); return
        if mode=='add_cost':
            item=self.cat[self.state['item']]; self.db.set_holding(item,self.state['qty'],mnum(text)); self.tg.send('✅ Existing holding added.'); self.state={}; return
        if mode=='buy_qty':
            q=max(1,min(int(text.replace(',','')),self.state['max'])); item=self.cat[self.state['item']]; nq,nc=self.db.add_holding(item,q,self.state['price'],'deal_confirmed'); self.tg.send(f'✅ Purchase recorded\n{item["name"]} +{q}\nNow hold: {nq}\nWeighted avg: {money(nc)}'); self.state={}; return
        if mode=='setting': self.db.set(self.state['key'],mnum(text)); self.state={}; self.tg.send('✅ Updated.'); return
        if mode=='sold_qty': self.state['qty']=int(text.replace(',','')); self.state['mode']='sold_price'; self.tg.send('Send actual sale price each.'); return
        if mode=='sold_price':
            item=self.cat[self.state['item']]; fee=float(self.db.get('sale_fee_percent',self.cfg.get('sale_fee_percent',5))); q,left,real=self.db.sell(item,self.state['qty'],mnum(text),fee); self.tg.send(f'✅ Sale recorded\n{item["name"]} ×{q}\nRealized P/L: {money(real)}\nRemaining: {left}'); self.state={}; return
        if mode=='more_qty': self.state['qty']=int(text.replace(',','')); self.state['mode']='more_price'; self.tg.send('Purchase price each?'); return
        if mode=='more_price':
            item=self.cat[self.state['item']]; nq,nc=self.db.add_holding(item,self.state['qty'],mnum(text),'manual_more'); self.tg.send(f'✅ Updated\nNow hold: {nq}\nWeighted avg: {money(nc)}'); self.state={}; return
        if mode=='edit_qty': self.state['qty']=int(text.replace(',','')); self.state['mode']='edit_cost'; self.tg.send('Corrected average cost each?'); return
        if mode=='edit_cost':
            item=self.cat[self.state['item']]; self.db.set_holding(item,self.state['qty'],mnum(text)); self.tg.send('✅ Position corrected.'); self.state={}; return

    def market_card(self,iid):
        item=self.cat[iid]; ls,avg,ref,conf,sup=self.snap(item); lines=[f'📊 {item["name"]}']
        if ls: lines += [f'Cheapest: {money(ls[0]["price"])}',f'Qty: {ls[0]["quantity"]}']
        lines += [f'Reference: {money(ref)}',f'API avg: {money(avg)}',f'Confidence: {conf:.0f}%',f'Support: {sup}']
        self.tg.send('\n'.join(lines),kb([[b('🌐 Open market',url=market_link(iid))],[b('⬅️ Menu','m:home')]]))

    def add_watch(self,iid):
        item=self.cat[iid]; self.db.c.execute('INSERT OR REPLACE INTO watchlist(item_id,item_name,enabled) VALUES(?,?,1)',(iid,item['name'])); self.db.c.commit(); self.watch=self.build_watch(); self.tg.send(f'✅ Watching {item["name"]}.')

    def show_watch(self):
        rows=self.db.c.execute('SELECT item_name,min_roi,min_profit,max_buy FROM watchlist WHERE enabled=1 ORDER BY item_name').fetchall(); txt='👀 WATCHLIST\n'+('\n'.join(f'• {n} | ROI {r or "default"} | profit {money(p) if p else "default"} | max {money(mx) if mx else "none"}' for n,r,p,mx in rows) if rows else 'Empty')
        self.tg.send(txt,kb([[b('➕ Add watch','watch:add')],[b('⬅️ Menu','m:home')]]))

    def show_deals(self):
        rows=self.db.c.execute('SELECT item_name,buy_price,qty,est_profit,roi,confidence,score FROM deals ORDER BY ts DESC LIMIT 10').fetchall()
        self.tg.send('🔥 RECENT DEALS\n'+('\n'.join(f'• {n}: {money(p)} ×{q} | {money(pr)} | ROI {r:.1f}% | conf {c:.0f}% | score {s:.1f}' for n,p,q,pr,r,c,s in rows) if rows else 'No deals yet.'),kb([[b('⬅️ Menu','m:home')]]))

    def show_settings(self):
        lim=capital_limits(self.db,self.cfg.get('bankroll',{}))
        self.tg.send(f"⚙️ SETTINGS\nCash: {money(self.db.get('liquid_cash',0))}\nMin ROI: {self.db.get('minroi',self.cfg.get('deal_rules',{}).get('min_roi_percent',7))}%\nMin profit: {money(self.db.get('minprofit',self.cfg.get('deal_rules',{}).get('min_total_profit',15000)))}\nExit ROI: {self.db.get('exitroi',self.cfg.get('selling',{}).get('target_profit_percent',5))}%\nMax/deal: {money(lim['max_deal'])}\nCash reserve: {money(lim['reserve'])}",kb([[b('💵 Cash','set:liquid_cash'),b('📈 Min ROI','set:minroi')],[b('💰 Min profit','set:minprofit'),b('🎯 Exit ROI','set:exitroi')],[b('📦 Max deal %','set:max_deal_bankroll_percent'),b('🛡️ Reserve %','set:cash_reserve_percent')],[b('⬅️ Menu','m:home')]]))

    def scan(self):
        if self.paused:return
        minroi=float(self.db.get('minroi',self.cfg.get('deal_rules',{}).get('min_roi_percent',7))); minprofit=float(self.db.get('minprofit',self.cfg.get('deal_rules',{}).get('min_total_profit',15000))); fee=float(self.db.get('sale_fee_percent',self.cfg.get('sale_fee_percent',5)))
        lim=capital_limits(self.db,self.cfg.get('bankroll',{})); cash=float(self.db.get('liquid_cash',0) or 0); deployable=max(0,cash-lim['reserve']); maxdeal=min(lim['max_deal'],deployable if cash>0 else lim['max_deal']); cand=[]
        for item in self.watch:
            if STOP:break
            self.handle()
            try:
                ls,avg=self.torn.market(item['id'],int(self.cfg.get('market_limit',100))); ref,conf,sup=reference(ls,avg,self.hist(item['id']),self.cfg) if ls else (None,0,0)
                if not ref:continue
                self.db.c.execute('INSERT OR REPLACE INTO market_samples VALUES(?,?,?,?,?,?,?,?)',(now(),item['id'],item['name'],ref,ls[0]['price'],avg,conf,sup)); self.db.c.commit()
                custom=self.db.c.execute('SELECT min_roi,min_profit,max_buy FROM watchlist WHERE item_id=? AND enabled=1',(item['id'],)).fetchone(); iroi=float(custom[0]) if custom and custom[0] is not None else minroi; iprofit=float(custom[1]) if custom and custom[1] is not None else minprofit; maxbuy=float(custom[2]) if custom and custom[2] is not None else None
                held=self.db.holding(item['id']); existing_exposure=(int(held[2])*float(held[3])) if held else 0
                for l in ls[:10]:
                    q=min(l['quantity'],1000); capital=l['price']*q
                    if capital>maxdeal or existing_exposure+capital>lim['max_item'] or (maxbuy and l['price']>maxbuy):continue
                    pe=ref*(1-fee/100)-l['price']; total=pe*q; roi=pe/l['price']*100; disc=(ref-l['price'])/ref*100
                    if roi<iroi or total<iprofit or conf<45 or sup<5:continue
                    score=self.score_deal(roi,total,conf,capital); cand.append((score,item,l,ref,conf,total,roi,disc,capital))
            except Exception as e: print('[SCAN]',item['name'],e,flush=True)
            time.sleep(float(self.cfg.get('per_item_delay_seconds',1.2)))
        cand.sort(key=lambda z:z[0],reverse=True)
        for score,item,l,ref,conf,total,roi,disc,capital in cand[:int(self.cfg.get('max_alerts_per_cycle',5))]:
            k=f'{item["id"]}:{l["id"]}:{l["price"]}:{l["quantity"]}'; old=self.db.c.execute('SELECT ts FROM alerts WHERE k=?',(k,)).fetchone()
            if old and now()-old[0]<float(self.cfg.get('alert_cooldown_minutes',180))*60:continue
            self.db.c.execute('INSERT OR REPLACE INTO alerts VALUES(?,?)',(k,now())); self.db.c.execute('INSERT OR IGNORE INTO deals(ts,item_id,item_name,listing_key,buy_price,qty,reference_price,est_profit,roi,discount,confidence,score) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(now(),item['id'],item['name'],k,l['price'],l['quantity'],ref,total,roi,disc,conf,score)); self.db.c.commit()
            dtype=self.deal_type(roi,total,conf)
            self.tg.send(f'{dtype}\n{item["name"]}\nBuy: {money(l["price"])} ×{l["quantity"]}\nCapital: {money(capital)}\nReference: {money(ref)}\nEst. profit: {money(total)}\nROI: {roi:.1f}%\nConfidence: {conf:.0f}%\nIsland score: {score:.1f}',kb([[b('✅ I bought it',f'buy:{item["id"]}:{l["price"]}:{l["quantity"]}'),b('📊 Market',url=market_link(item['id']))],[b('🏝️ Island Fund','m:island')]]))
        self.exit_alerts()

    def exit_alerts(self):
        target=float(self.db.get('exitroi',self.cfg.get('selling',{}).get('target_profit_percent',5))); fee=float(self.db.get('sale_fee_percent',self.cfg.get('sale_fee_percent',5)))
        for iid,name,q,cost in self.db.holdings():
            if not cost:continue
            try:
                item=self.cat[iid]; ls,avg,ref,conf,sup=self.snap(item)
                if not ls:continue
                px=ref or ls[0]['price']; roi=(px*(1-fee/100)-cost)/cost*100; key=f'exit:{iid}:{int(px//1000)}'; old=self.db.c.execute('SELECT ts FROM alerts WHERE k=?',(key,)).fetchone()
                if roi>=target and conf>=55 and (not old or now()-old[0]>3600):
                    self.db.c.execute('INSERT OR REPLACE INTO alerts VALUES(?,?)',(key,now())); self.db.c.commit(); self.tg.send(f'💰 EXIT OPPORTUNITY\n{name} ×{q}\nAvg cost: {money(cost)}\nReference: {money(px)}\nEst. ROI after fee: {roi:.1f}%',kb([[b('💼 Review',f'pos:{iid}'),b('📊 Market',url=market_link(iid))]]))
            except Exception:pass

    def run(self):
        self.tg.send('🏝️ Island Mode enabled — target $500,000,000. Use /cash <amount> then /menu.')
        self.home()
        while not STOP:
            started=time.time(); self.handle(); self.scan(); interval=max(60,int(self.cfg.get('scan_interval_seconds',300))); end=time.time()+max(5,interval-(time.time()-started))
            while time.time()<end and not STOP:self.handle();time.sleep(3)


def main():
    cfgp=Path(os.getenv('TORN_CONFIG','config.json'))
    if not cfgp.exists():raise SystemExit('Missing config.json')
    cfg=json.loads(cfgp.read_text())
    required=['TORN_API_KEY','TELEGRAM_BOT_TOKEN','TELEGRAM_CHAT_ID']; missing=[x for x in required if not os.getenv(x)]
    if missing:raise SystemExit('Missing environment variables: '+', '.join(missing))
    IslandApp(cfg).run()
