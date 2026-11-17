"""IBKR paper integration stage 1: read-only connection and dry-run intents.

Never calls placeOrder. Does not infer new orders from historical holdings.
Requires a manually supplied fresh signal file, separate from state.json.
"""
import argparse
import json
import threading
import time
from datetime import date
from pathlib import Path

from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibkr_bridge.paper_order_safety import PaperOrderIntent, validate_paper_intent


class PaperSnapshot(EWrapper, EClient):
    def __init__(self):
        EClient.__init__(self, self)
        self.ready = False
        self.finished = False
        self.accounts = set()
        self.positions = {}
        self.errors = []

    def nextValidId(self, order_id):
        self.ready = True
        self.reqManagedAccts()
        self.reqPositions()

    def managedAccounts(self, accounts_list):
        self.accounts.update(a.strip() for a in accounts_list.split(",") if a.strip())

    def position(self, account, contract, position, avg_cost):
        if contract.secType == "STK":
            key = (account, contract.symbol)
            self.positions[key] = self.positions.get(key, 0) + float(position)

    def positionEnd(self):
        self.finished = True

    def error(self, reqId, errorCode, errorString, advancedOrderRejectJson=""):
        if errorCode not in (2104, 2106, 2107, 2108, 2158):
            self.errors.append(f"{errorCode}: {errorString}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4002)
    parser.add_argument("--signals", required=True, help="JSON array of NEW intents only; never historical state.json")
    parser.add_argument("--paper-account", required=True, help="Exact paper account ID from IBKR")
    args = parser.parse_args()
    if args.host not in ("127.0.0.1", "localhost") or args.port != 4002:
        raise SystemExit("DENIED: only local IBKR paper Gateway port 4002")
    signals = json.loads(Path(args.signals).read_text(encoding="utf-8"))
    if not isinstance(signals, list):
        raise SystemExit("DENIED: signals must be a JSON array")
    app = PaperSnapshot()
    app.connect(args.host, args.port, clientId=30)
    thread = threading.Thread(target=app.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not app.finished and time.monotonic() < deadline:
        time.sleep(0.1)
    app.disconnect()
    if not app.ready or not app.finished or app.errors:
        raise SystemExit("DENIED: incomplete or errored broker snapshot")
    if args.paper_account not in app.accounts or not args.paper_account.startswith("DU"):
        raise SystemExit("DENIED: expected DU paper account not verified")
    if any(account != args.paper_account for account in app.accounts):
        raise SystemExit("DENIED: multiple or unexpected accounts visible")
    positions = {}
    for (account, symbol), qty in app.positions.items():
        if account == args.paper_account and qty != 0:
            positions[symbol] = qty
    print("Verified DU paper account; read-only snapshot complete.")
    print("Existing broker positions:", sorted(positions))
    # No assumption that the account's full equity is available to this strategy.
    # Dry run deliberately supplies zero exposure/equity until broker-side budget
    # reconciliation and a persistent idempotency ledger are implemented.
    for item in signals:
        try:
            intent = PaperOrderIntent(**item)
            validate_paper_intent(
                intent, verified_paper_account=True, gateway_paper_mode=True,
                live_trading_disabled=True, already_processed=False,
                existing_symbols=set(positions), current_exposure_usd="0",
                strategy_equity_usd="0", today=date.today())
        except (ValueError, TypeError, KeyError) as exc:
            print("BLOCKED:", str(exc))
        else:
            print("DRY-RUN ONLY:", intent.symbol, intent.quantity)
    print("NO ORDERS SENT. No execution code exists in this module.")


if __name__ == "__main__":
    main()
