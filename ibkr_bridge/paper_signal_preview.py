"""Preview fresh next-session Top5 candidates against an IBKR read-only snapshot.

No ibapi imports, network calls, journal writes, or order placement.
Activation date must be explicitly set after paper account onboarding.
This is a conservative preview, NOT a trade authorization.
"""
import argparse
import json
from datetime import date, timedelta
from pathlib import Path

def preview(state, snapshot, *, activation_date, execution_date):
    if snapshot.get("mode") != "READ_ONLY_NO_ORDERS":
        raise ValueError("DENIED: snapshot not read-only")
    if snapshot.get("complete") != {"positions": True, "orders": True, "executions": True}:
        raise ValueError("DENIED: incomplete broker snapshot")
    account = snapshot.get("account", "")
    if not isinstance(account, str) or not account.startswith("DU"):
        raise ValueError("DENIED: paper account not verified")
    if execution_date <= activation_date:
        raise ValueError("DENIED: execution date must be after activation")
    if state.get("version") != 1:
        raise ValueError("DENIED: unknown strategy state version")
    if state.get("strategy") != "V1-D2-LOSS2+B125_SPY_MOM20_POS":
        raise ValueError("DENIED: legacy strategy state; only B125_SPY_MOM20_POS allowed")
    # Reject a local broker-facing state for another account; never infer
    # account identity from a DU prefix alone.
    if "account" in state and state["account"] != account:
        raise ValueError("DENIED: local strategy account mismatch")
    if "activation_date" in state and state["activation_date"] != activation_date.isoformat():
        raise ValueError("DENIED: activation date mismatch")
    if state.get("orders_enabled") is not False and "orders_enabled" in state:
        raise ValueError("DENIED: local state must remain read-only")
    held = {p["symbol"] for p in snapshot.get("positions", []) if p.get("quantity", 0) != 0}
    held |= {o["symbol"] for o in snapshot.get("open_orders", [])}
    # Conservative only: no stale, future, historical or same-day entries.
    # The first subsequent weekday is not sufficient for market holidays:
    # a real order engine MUST use an exchange calendar and broker checks.
    candidates = []
    rejected = []
    for signal in state.get("pending", []):
        sym = signal.get("symbol")
        try:
            d = date.fromisoformat(signal["signal_date"])
        except (KeyError, TypeError, ValueError):
            rejected.append({"symbol":sym,"reason":"invalid signal date"})
            continue
        if d < activation_date:
            reason = "historical before activation"
        elif d >= execution_date:
            reason = "not a prior-session signal"
        elif (execution_date-d).days > 4:
            reason = "too old; requires exchange calendar"
        elif sym in held:
            reason = "already held or pending at broker"
        else:
            reason = None
        if reason:
            rejected.append({"symbol":sym,"reason":reason})
        else:
            candidates.append({"symbol":sym,"signal_date":signal["signal_date"],"score":signal.get("score")})
    return {"account":account,"activation_date":activation_date.isoformat(),"execution_date":execution_date.isoformat(),"candidates_for_review":candidates,"rejected":rejected,"orders_enabled":False,"note":"PREVIEW ONLY. No prices, quantities or orders authorized. Exchange calendar and broker reconciliation required."}

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--state",required=True)
    parser.add_argument("--snapshot",required=True)
    parser.add_argument("--activation-date",required=True,help="Explicit YYYY-MM-DD paper onboarding date")
    parser.add_argument("--execution-date",required=True,help="Proposed US market session YYYY-MM-DD")
    args=parser.parse_args()
    try:
        state=json.loads(Path(args.state).read_text(encoding="utf-8"))
        snapshot=json.loads(Path(args.snapshot).read_text(encoding="utf-8"))
        result=preview(state,snapshot,activation_date=date.fromisoformat(args.activation_date),execution_date=date.fromisoformat(args.execution_date))
        print(json.dumps(result,indent=2))
    except (OSError,ValueError,KeyError,TypeError) as exc:
        parser.exit(1,"PREVIEW DENIED: "+str(exc)+"\n")

if __name__=="__main__":
    main()
