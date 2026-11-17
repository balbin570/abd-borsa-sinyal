"""Conservative, offline IBKR PAPER sizing preview. Never submits orders.

Only computes share quantities from independently supplied, positive reference
prices. Uses broker snapshot to block existing positions/open orders. A quote
is NOT executable market data; this is a preview, not authorization.
"""
from decimal import Decimal, InvalidOperation, ROUND_DOWN

MAX_ORDER = Decimal("2000")
MAX_EQUITY = Decimal("10000")
MAX_POSITIONS = 5
FEE_BUFFER = Decimal("1.005")

def _money(value):
    try:
        v=Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("DENIED: invalid numeric value") from None
    if not v.is_finite() or v < 0:
        raise ValueError("DENIED: invalid numeric value")
    return v

def size_candidates(preview, snapshot, prices, *, paper_cash_usd, strategy_budget_usd=10000):
    if preview.get("orders_enabled") is not False or snapshot.get("mode") != "READ_ONLY_NO_ORDERS":
        raise ValueError("DENIED: preview-only broker snapshot required")
    if preview.get("account") != snapshot.get("account") or not str(snapshot.get("account","")).startswith("DU"):
        raise ValueError("DENIED: account mismatch")
    if snapshot.get("complete") != {"positions":True,"orders":True,"executions":True}:
        raise ValueError("DENIED: incomplete broker snapshot")
    cash = _money(paper_cash_usd)
    budget = min(_money(strategy_budget_usd),MAX_EQUITY)
    # Conservative: never assume holdings or open orders are available capacity.
    existing={p["symbol"] for p in snapshot.get("positions",[]) if _money(p.get("quantity",0)) > 0}
    existing |= {o["symbol"] for o in snapshot.get("open_orders",[])}
    capacity=max(0,MAX_POSITIONS-len(existing))
    allocations=[]
    rejected=[]
    for candidate in preview.get("candidates_for_review",[]):
        sym=candidate.get("symbol")
        if capacity<=0:
            rejected.append({"symbol":sym,"reason":"portfolio full"})
            continue
        if sym in existing or any(x["symbol"]==sym for x in allocations):
            rejected.append({"symbol":sym,"reason":"duplicate broker or preview symbol"})
            continue
        if sym not in prices:
            rejected.append({"symbol":sym,"reason":"missing reference price"})
            continue
        price=_money(prices[sym])
        if price<=0:
            rejected.append({"symbol":sym,"reason":"nonpositive price"})
            continue
        allowance=min(MAX_ORDER,budget/Decimal(MAX_POSITIONS),cash)
        shares=int((allowance/(price*FEE_BUFFER)).to_integral_value(rounding=ROUND_DOWN))
        if shares<1:
            rejected.append({"symbol":sym,"reason":"insufficient cash or budget"})
            continue
        estimated=price*shares*FEE_BUFFER
        cash-=estimated
        allocations.append({"symbol":sym,"shares_preview":shares,"reference_price":str(price),"reserved_cash_estimate":str(estimated.quantize(Decimal("0.01"))),"signal_date":candidate.get("signal_date")})
        capacity-=1
    return {"account":snapshot["account"],"allocations_preview_only":allocations,"rejected":rejected,"remaining_cash_estimate":str(cash.quantize(Decimal("0.01"))),"orders_enabled":False,"warning":"NO ORDERS. Reference prices, broker cash and fill conditions must be independently verified."}
