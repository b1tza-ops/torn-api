"""Private Island goal analytics for Torn Market Assistant."""
from __future__ import annotations
import time

GOAL = 500_000_000.0
MILESTONES = (25_000_000, 50_000_000, 100_000_000, 250_000_000, 500_000_000)


def money(x):
    return f"${int(round(float(x))):,}"


def snapshot(db, goal: float = GOAL):
    """Return goal metrics based on user-entered liquid cash + tracked portfolio."""
    cash = float(db.get('liquid_cash', 0) or 0)
    holdings = db.holdings()
    invested = sum(int(r[2]) * float(r[3] or 0) for r in holdings)
    realized = float(db.c.execute('SELECT COALESCE(SUM(realized_profit),0) FROM sales').fetchone()[0] or 0)
    start = float(db.get('island_start_bankroll', 0) or 0)
    equity = cash + invested
    if not start and equity:
        start = equity
        db.set('island_start_bankroll', start)
        db.set('island_start_ts', int(time.time()))
    progress = equity / goal * 100 if goal else 0
    nxt = next((m for m in MILESTONES if equity < m), goal)
    return {'cash': cash, 'invested': invested, 'equity_cost': equity, 'realized': realized,
            'start': start, 'goal': goal, 'progress': progress, 'next': nxt}


def dashboard_text(db, goal: float = GOAL):
    s = snapshot(db, goal)
    width = 12
    filled = min(width, int(s['progress'] / 100 * width))
    bar = '█' * filled + '░' * (width - filled)
    return (f"🏝️ PRIVATE ISLAND FUND\n\n"
            f"[{bar}] {s['progress']:.2f}%\n"
            f"Tracked capital: {money(s['equity_cost'])}\n"
            f"Liquid cash: {money(s['cash'])}\n"
            f"Portfolio cost: {money(s['invested'])}\n"
            f"Realized trading P/L: {money(s['realized'])}\n\n"
            f"Next milestone: {money(s['next'])}\n"
            f"Remaining to $500m: {money(max(0, goal-s['equity_cost']))}\n\n"
            "Use /cash <amount> whenever your available Torn cash changes.\n"
            "The assistant will use the goal as a capital-allocation guardrail; it does not buy or sell automatically.")


def capital_limits(db, cfg):
    s = snapshot(db)
    bankroll = max(s['equity_cost'], float(cfg.get('max_capital_per_deal', 5_000_000)))
    per_deal_pct = float(db.get('max_deal_bankroll_percent', 20) or 20)
    per_item_pct = float(db.get('max_item_bankroll_percent', 30) or 30)
    reserve_pct = float(db.get('cash_reserve_percent', 20) or 20)
    return {
        'max_deal': bankroll * per_deal_pct / 100,
        'max_item': bankroll * per_item_pct / 100,
        'reserve': bankroll * reserve_pct / 100,
        'bankroll': bankroll,
    }
