"""Read-only IBKR paper account snapshot. No order submission API calls.

Requires running IB Gateway in Simulated Trading, API read-only, localhost:4002.
"""
import argparse
import json
import threading
import time
from ibapi.client import EClient
from ibapi.wrapper import EWrapper

class Reader(EWrapper, EClient):
    def __init__(self):
        EClient.__init__(self, self)
        self.accounts = []
        self.positions = []
        self.open_orders = []
        self.executions = []
        self.errors = []
        self.account_done = threading.Event()
        self.positions_done = threading.Event()
        self.orders_done = threading.Event()
        self.executions_done = threading.Event()
        self.connected = threading.Event()

    def nextValidId(self, orderId):
        self.connected.set()

    def managedAccounts(self, accountsList):
        self.accounts = [a.strip() for a in accountsList.split(",") if a.strip()]
        self.account_done.set()

    def position(self, account, contract, position, avgCost):
        self.positions.append({"account":account,"symbol":contract.symbol,"secType":contract.secType,"currency":contract.currency,"quantity":float(position),"average_cost":float(avgCost)})

    def positionEnd(self):
        self.positions_done.set()

    def openOrder(self, orderId, contract, order, orderState):
        self.open_orders.append({"order_id":orderId,"account":order.account,"symbol":contract.symbol,"side":order.action,"quantity":str(order.totalQuantity),"status":orderState.status})

    def openOrderEnd(self):
        self.orders_done.set()

    def execDetails(self, reqId, contract, execution):
        self.executions.append({"execution_id":execution.execId,"account":execution.acctNumber,"symbol":contract.symbol,"side":execution.side,"shares":str(execution.shares),"price":execution.price,"time":execution.time})

    def execDetailsEnd(self, reqId):
        self.executions_done.set()

    def error(self, reqId, errorCode, errorString, advancedOrderRejectJson=""):
        if errorCode not in (2104,2106,2107,2108,2158):
            self.errors.append({"request_id":reqId,"code":errorCode,"message":errorString})

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--host",default="127.0.0.1")
    p.add_argument("--port",type=int,default=4002)
    p.add_argument("--timeout",type=float,default=15)
    args = p.parse_args()
    if args.host not in ("127.0.0.1","localhost") or args.port != 4002:
        p.error("Only local paper Gateway port 4002 is allowed")
    app = Reader()
    try:
        app.connect(args.host,args.port,clientId=79)
        thread = threading.Thread(target=app.run,daemon=True)
        thread.start()
        if not app.connected.wait(args.timeout):
            raise RuntimeError("Gateway handshake timed out")
        app.reqManagedAccts()
        if not app.account_done.wait(args.timeout):
            raise RuntimeError("Account list timed out")
        if len(app.accounts)!=1 or not app.accounts[0].startswith("DU"):
            raise RuntimeError("DENIED: expected exactly one DU paper account")
        app.reqPositions()
        app.reqAllOpenOrders()
        from ibapi.execution import ExecutionFilter
        filt=ExecutionFilter()
        filt.acctCode=app.accounts[0]
        app.reqExecutions(701,filt)
        completed = {
            "positions":app.positions_done.wait(args.timeout),
            "orders":app.orders_done.wait(args.timeout),
            "executions":app.executions_done.wait(args.timeout)
        }
        if not all(completed.values()):
            raise RuntimeError("Incomplete broker snapshot: "+str(completed))
        if app.errors:
            raise RuntimeError("Broker API errors: "+str(app.errors))
        print(json.dumps({"account":app.accounts[0],"positions":app.positions,"open_orders":app.open_orders,"executions":app.executions,"complete":completed,"mode":"READ_ONLY_NO_ORDERS"},indent=2))
    except Exception as exc:
        print("SNAPSHOT FAILED (NO ORDERS):",str(exc))
        raise SystemExit(1)
    finally:
        if app.isConnected():
            app.disconnect()

if __name__=="__main__":
    main()
