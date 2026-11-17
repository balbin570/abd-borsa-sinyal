"""Initialize an independent IBKR PAPER strategy ledger, without broker orders.

No existing Top5 historical holdings are imported. Default deny unless a
complete read-only broker snapshot confirms an empty DU paper account.
The 10k strategy cap is a risk budget, NOT a claim of broker cash.
"""
import argparse
import json
from datetime import date
from pathlib import Path

COMPLETE={"positions":True,"orders":True,"executions":True}

def build_initial_state(snapshot, *, activation_date, strategy_cap_usd=10000):
    if snapshot.get("mode") != "READ_ONLY_NO_ORDERS" or snapshot.get("complete") != COMPLETE:
        raise ValueError("DENIED: complete read-only snapshot required")
    account=snapshot.get("account")
    if not isinstance(account,str) or not account.startswith("DU"):
        raise ValueError("DENIED: DU paper account required")
    if snapshot.get("positions") or snapshot.get("open_orders"):
        raise ValueError("DENIED: account not empty; reconcile before onboarding")
    if type(strategy_cap_usd) not in (int,float) or strategy_cap_usd != 10000:
        raise ValueError("DENIED: strategy cap must be 10000 USD")
    if not isinstance(activation_date,date):
        raise ValueError("DENIED: invalid activation date")
    return {
        "version":1,"account":account,"mode":"IBKR_PAPER_READ_ONLY",
        "activation_date":activation_date.isoformat(),
        "strategy_cap_usd":10000,
        "broker_cash_verified":False,
        "positions":[],"pending":[],"closed":[],"processed_intents":[],
        "last_processed_session":None,
        "orders_enabled":False,
        "notes":"Independent broker-facing ledger. Historical Top5 holdings never imported. Broker cash unverified."
    }

def initialize(path,snapshot,*,activation_date):
    target=Path(path)
    if target.exists():
        raise ValueError("DENIED: state already exists; never overwrite")
    state=build_initial_state(snapshot,activation_date=activation_date)
    target.parent.mkdir(parents=True,exist_ok=True)
    with target.open("x",encoding="utf-8") as f:
        json.dump(state,f,indent=2)
        f.write("\n")
    return state

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--snapshot",required=True)
    parser.add_argument("--output",default="ibkr_bridge/local_paper_state.json")
    parser.add_argument("--activation-date",required=True)
    args=parser.parse_args()
    try:
        snap=json.loads(Path(args.snapshot).read_text(encoding="utf-8"))
        state=initialize(args.output,snap,activation_date=date.fromisoformat(args.activation_date))
        print(json.dumps({"status":"INITIALIZED_READ_ONLY","account":state["account"],"positions":0,"orders_enabled":False,"path":args.output},indent=2))
    except (ValueError,OSError,KeyError,TypeError) as e:
        parser.exit(1,"DENIED: "+str(e)+"\n")

if __name__=="__main__":
    main()
