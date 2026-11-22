"""Offline, fail-closed IBKR paper candidate feed gate. NEVER sends orders."""
import argparse
import json
from datetime import date
from pathlib import Path

STRATEGY = "V1-D2-LOSS2+B125_SPY_MOM20_POS"

def review(state, feed, *, today):
    if state.get("strategy") != STRATEGY or state.get("mode") != "IBKR_PAPER_READ_ONLY":
        raise ValueError("DENIED: unrecognized paper strategy or mode")
    if state.get("orders_enabled") is not False:
        raise ValueError("DENIED: orders must be disabled")
    if not str(state.get("account", "")).startswith("DU"):
        raise ValueError("DENIED: paper account required")
    activation = date.fromisoformat(state["activation_date"])
    if today <= activation:
        raise ValueError("DENIED: not after activation")
    if feed.get("schema_version") != 1 or feed.get("source") != "sp500_top5_daily_scan":
        raise ValueError("DENIED: unknown feed")
    if feed.get("orders_enabled") is not False:
        raise ValueError("DENIED: feed must be read-only")
    signal_date = date.fromisoformat(feed["signal_session"])
    if signal_date < activation:
        return {"status":"REJECTED_PRE_ACTIVATION","signal_session":signal_date.isoformat(),
                "accepted":[],"orders_enabled":False}
    if signal_date >= today or (today-signal_date).days > 4:
        raise ValueError("DENIED: stale or same-day signal")
    candidates = feed.get("candidates")
    if not isinstance(candidates,list) or feed.get("candidate_count",0) < len(candidates):
        raise ValueError("DENIED: invalid candidate list")
    accepted=[]
    seen=set()
    for item in candidates:
        symbol=item.get("symbol")
        if item.get("signal_date") != signal_date.isoformat():
            raise ValueError("DENIED: mismatched candidate session")
        if not isinstance(symbol,str) or not symbol.replace("-","").isalpha() or symbol in seen:
            raise ValueError("DENIED: invalid or duplicate symbol")
        seen.add(symbol)
        accepted.append({"symbol":symbol,"signal_date":signal_date.isoformat(),"score":item.get("score")})
    return {"status":"PREVIEW_ONLY","signal_session":signal_date.isoformat(),
            "accepted":accepted[:5],"orders_enabled":False,
            "warning":"Requires exchange calendar, broker snapshot, cash and pricing verification before any order."}

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--state",required=True)
    p.add_argument("--feed",required=True)
    p.add_argument("--today",required=True)
    args=p.parse_args()
    try:
        state=json.loads(Path(args.state).read_text(encoding="utf-8"))
        feed=json.loads(Path(args.feed).read_text(encoding="utf-8"))
        print(json.dumps(review(state,feed,today=date.fromisoformat(args.today)),indent=2))
    except (ValueError,TypeError,KeyError,OSError) as exc:
        p.exit(1,"DENIED: "+str(exc)+"\n")

if __name__=="__main__":
    main()
