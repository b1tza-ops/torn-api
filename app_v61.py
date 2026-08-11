#!/usr/bin/env python3
"""Torn Deal Finder Pro v6.1 UI extension.

Adds an interactive 'existing inventory' flow on top of v6. Torn remains read-only:
this only records items the user says they already own.
"""
import app_v6 as base


def menu():
    return base.kb([
        [base.b("🔥 Deals","m:deals"), base.b("💼 Portfolio","m:portfolio")],
        [base.b("➕ Add Existing","manual:add"), base.b("💰 Sell","m:sell")],
        [base.b("📦 Inventory","m:inventory"), base.b("👀 Watchlist","m:watch")],
        [base.b("📊 Market","m:market"), base.b("⚙️ Settings","m:settings")],
    ])


def show_inventory(token,chat,c):
    rows=base.portfolio_rows(c)
    if not rows:
        base.tg(token,chat,"📦 TRACKED INVENTORY\nNothing added yet.\n\nTap below to add something you already own.",base.kb([[base.b("➕ Add existing item","manual:add")],[base.b("⬅️ Menu","m:home")]]))
        return
    lines=["📦 TRACKED INVENTORY"]
    buttons=[]
    for iid,name,qty,cost in rows[:20]:
        lines.append(f"• {name} ×{qty} | avg cost {base.money(cost)}")
        buttons.append([base.b(f"{name} ×{qty}",f"pos:{iid}")])
    buttons.append([base.b("➕ Add existing item","manual:add"),base.b("💼 Portfolio","m:portfolio")])
    buttons.append([base.b("⬅️ Menu","m:home")])
    base.tg(token,chat,"\n".join(lines),base.kb(buttons))


def handle(token,chat,offset,cat,cfg,c,items,key,state):
    try:
        us=base.updates(token,offset)
    except Exception:
        return offset,items,state

    for u in us:
        offset=max(offset,u.get("update_id",0)+1)
        cb=u.get("callback_query")
        m=u.get("message") or (cb or {}).get("message") or {}
        cid=str((m.get("chat") or {}).get("id",""))
        text=(u.get("message") or {}).get("text","").strip()
        data=(cb or {}).get("data","")
        if chat and cid!=str(chat):
            continue
        if cb:
            base.ack(token,cb.get("id"))
        try:
            # Main navigation + manual existing-holding workflow.
            if text in ("/start","/menu"):
                base.tg(token,chat,f"🤖 TORN DEAL FINDER PRO v6.1\n{'⏸ PAUSED' if base.gs(c,'paused',False) else '🟢 RUNNING'}\nWatching {len(items)} items\nPositions: {len(base.portfolio_rows(c))}\nChoose:",menu())
            elif data=="m:home":
                base.tg(token,chat,f"🤖 TORN DEAL FINDER PRO v6.1\n{'⏸ PAUSED' if base.gs(c,'paused',False) else '🟢 RUNNING'}\nWatching {len(items)} items\nPositions: {len(base.portfolio_rows(c))}\nChoose:",menu())
            elif data=="m:inventory":
                show_inventory(token,chat,c)
            elif data=="manual:add":
                state={"mode":"manual_item"}
                base.tg(token,chat,"➕ ADD EXISTING ITEM\n\nSend the Torn item name you already own.\nExample: Xanax\n\nSend /cancel to stop.")
            elif state.get("mode")=="manual_item" and text:
                if text=="/cancel":
                    state={};base.tg(token,chat,"Cancelled.",base.kb([[base.b("⬅️ Menu","m:home")]]));continue
                item=base.item_by_name(cat,text)
                if not item:
                    base.tg(token,chat,"I couldn't identify that item uniquely. Send a more exact Torn item name, or /cancel.")
                    continue
                state={"mode":"manual_qty","item":item["id"]}
                base.tg(token,chat,f"📦 {item['name']}\nHow many do you currently own?\nSend a whole number, e.g. 10.")
            elif state.get("mode")=="manual_qty" and text:
                q=int(text.replace(",",""))
                if q<1: raise ValueError("Quantity must be at least 1")
                state={"mode":"manual_cost","item":state["item"],"qty":q}
                item=cat[int(state["item"])]
                base.tg(token,chat,f"💵 {item['name']} ×{q}\nWhat was your approximate average purchase price EACH?\nSend 0 if you don't know.\nExample: 720000")
            elif state.get("mode")=="manual_cost" and text:
                raw=text.replace(",","").replace("$","").strip()
                price=float(raw)
                if price<0: raise ValueError("Price cannot be negative")
                iid=int(state["item"]);q=int(state["qty"]);item=cat[iid]
                nq,nc=base.add_buy(c,item,q,price,source="manual_existing")
                state={}
                note="Cost basis unknown" if price==0 else f"Weighted avg: {base.money(nc)}"
                base.tg(token,chat,f"✅ Existing holding added\n{item['name']} +{q}\nNow tracked: {nq}\n{note}\n\nThe bot will now monitor this position for sell opportunities.",base.kb([[base.b("💼 Portfolio","m:portfolio"),base.b("➕ Add another","manual:add")],[base.b("📊 Market",url=base.market_link(iid)),base.b("⬅️ Menu","m:home")]]))

            # Existing v6 actions, copied through so callback updates are not lost.
            elif text=="/pause":base.ss(c,"paused",True);base.tg(token,chat,"⏸ Paused")
            elif text=="/resume":base.ss(c,"paused",False);base.tg(token,chat,"▶️ Resumed")
            elif data=="m:portfolio":base.show_portfolio(token,chat,c,cfg,key,cat)
            elif data=="m:sales":base.show_sales(token,chat,c)
            elif data=="m:deals":
                rs=c.execute("SELECT item_name,buy,total,roi FROM deals ORDER BY ts DESC LIMIT 8").fetchall()
                base.tg(token,chat,"🔥 Recent deals\n"+("\n".join(f"• {n}: buy {base.money(bu)} | profit {base.money(pr)} | ROI {ro:.1f}%" for n,bu,pr,ro in rs) if rs else "No deals yet."),base.kb([[base.b("⬅️ Menu","m:home")]]))
            elif data.startswith("buy:"):
                _,si,sp,sq=data.split(":");iid=int(si);price=float(sp);qty=int(sq);item=cat.get(iid);state={"mode":"buyqty","item":iid,"price":price,"max":qty};base.tg(token,chat,f"✅ Confirm purchase: {item['name']}\nDetected qty: {qty}\nSend quantity you actually bought (1-{qty}), or /cancel.")
            elif data.startswith("pos:"):
                base.show_position(token,chat,c,cfg,key,cat,int(data.split(":")[1]))
            elif data.startswith("soldask:"):
                _,si,sp=data.split(":");iid=int(si);item=cat.get(iid);row=base.holding(c,iid);state={"mode":"soldqty","item":iid,"price":float(sp),"max":int(row[0])};base.tg(token,chat,f"Mark {item['name']} sold at {base.money(float(sp))} each.\nSend quantity sold (1-{row[0]}) or /cancel.")
            elif data=="m:sell":base.show_portfolio(token,chat,c,cfg,key,cat)
            elif data in ("m:watch","m:market","m:settings"):
                base.tg(token,chat,"This section remains available through existing commands: /watch, /unwatch, /item, /settings.",base.kb([[base.b("⬅️ Menu","m:home")]]))
            elif data=="noop":pass
            elif text=="/cancel":state={};base.tg(token,chat,"Cancelled.",base.kb([[base.b("⬅️ Menu","m:home")]]))
            elif state.get("mode") in ("buyqty","soldqty") and text:
                q=int(text.replace(",",""));q=max(1,min(q,int(state["max"])));iid=int(state["item"]);item=cat[iid];price=float(state["price"])
                if state["mode"]=="buyqty":
                    nq,nc=base.add_buy(c,item,q,price);base.tg(token,chat,f"✅ Added to portfolio\n{item['name']} +{q} @ {base.money(price)}\nNow hold: {nq}\nWeighted avg: {base.money(nc)}",base.kb([[base.b("💼 Portfolio","m:portfolio"),base.b("📊 Market",url=base.market_link(iid))]]))
                else:
                    fee=float(cfg.get("selling",{}).get("sale_fee_percent",cfg.get("sale_fee_percent",5)))/100;res=base.mark_sold(c,item,q,price,fee);base.tg(token,chat,f"✅ Sale recorded\n{item['name']} ×{res[0]} @ {base.money(price)}\nRealized P/L: {base.money(res[1])}",base.kb([[base.b("💼 Portfolio","m:portfolio")]]))
                state={}
        except Exception as e:
            base.tg(token,chat,f"Action error: {e}")
    return offset,items,state


def main():
    # Patch v6's UI hooks, retain its scanner/portfolio/exit engine.
    base.menu=menu
    base.handle=handle
    base.main()


if __name__=="__main__":
    main()
