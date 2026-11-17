"""IBKR Paper bridge: READ-ONLY position reconciliation. Never places orders."""
import argparse
import json
import threading
import time
from pathlib import Path
from ibapi.client import EClient
from ibapi.wrapper import EWrapper

class PaperReader(EWrapper, EClient):
    def __init__(self):
        EClient.__init__(self, self)
        self.ready = False
        self.done = False
        self.accounts = set()
        self.positions = {}
        self.errors = []

    def nextValidId(self, orderId):
        self.ready = True
        self.reqPositions()

    def position(self, account, contract, position, avgCost):
        self.accounts.add(account)
        if contract.secType == "STK":
            key = (account, contract.symbol)
            self.positions[key] = self.positions.get(key, 0) + float(position)

    def positionEnd(self):
        self.done = True

    def error(self, reqId, errorCode, errorString, advancedOrderRejectJson=""):
        if errorCode not in (2104, 2106, 2107, 2108, 2158):
            self.errors.append(f"{errorCode}: {errorString}")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", required=True, help="Local copy of GitHub paper_portfolio/state.json")
    parser.add_argument("--port", type=int, default=4002)
    args = parser.parse_args()
    state = json.loads(Path(args.state).read_text(encoding="utf-8-sig"))
    if state.get("version") != 1 or not isinstance(state.get("positions"), list):
        raise SystemExit("Invalid strategy state; refusing comparison")
    targets = {}
    for pos in state["positions"]:
        sym, qty = pos["symbol"], int(pos["shares"])
        if qty < 0 or sym in targets:
            raise SystemExit("Invalid or duplicate target")
        targets[sym] = qty
    print("Strategy snapshot:", state.get("last_processed", "UNKNOWN"))
    print("IMPORTANT: Historical strategy holdings are NOT new buy signals.")
    app = PaperReader()
    app.connect("127.0.0.1", args.port, clientId=29)
    threading.Thread(target=app.run, daemon=True).start()
    deadline = time.monotonic() + 15
    while not app.done and time.monotonic() < deadline:
        time.sleep(0.1)
    app.disconnect()
    if not app.ready or not app.done:
        raise SystemExit("IBKR position snapshot incomplete; NO ACTION")
    if app.errors:
        print("IBKR messages:", "; ".join(app.errors))
    print("IBKR paper accounts:", ", ".join(sorted(app.accounts)) or "(no positions reported)")
    actual = {}
    for (_, symbol), qty in app.positions.items():
        actual[symbol] = actual.get(symbol, 0) + qty
    for sym in sorted(set(targets) | set(actual)):
        print(f"{sym:8} strategy={targets.get(sym,0):>6} ibkr={actual.get(sym,0):>6g} difference={targets.get(sym,0)-actual.get(sym,0):>6g}")
    print("READ-ONLY COMPLETE. No orders created, previewed differences are NOT trade instructions.")

if __name__ == "__main__":
    main()
