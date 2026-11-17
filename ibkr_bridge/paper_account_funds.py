"""Read-only IBKR PAPER account funds and NYSE session check.

Never places orders. Requires local Simulated Trading Gateway port 4002.
"""
import argparse
import json
import threading
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo
from ibapi.client import EClient
from ibapi.wrapper import EWrapper

class AccountReader(EWrapper,EClient):
    def __init__(self):
        EClient.__init__(self,self)
        self.ready=threading.Event()
        self.accounts_done=threading.Event()
        self.summary_done=threading.Event()
        self.accounts=[]
        self.values={}
        self.errors=[]
    def nextValidId(self,orderId):
        self.ready.set()
    def managedAccounts(self,accountsList):
        self.accounts=[a.strip() for a in accountsList.split(",") if a.strip()]
        self.accounts_done.set()
    def accountSummary(self,reqId,account,tag,value,currency):
        if account in self.accounts and currency in ("USD",""):
            self.values[tag]={"value":value,"currency":currency}
    def accountSummaryEnd(self,reqId):
        self.summary_done.set()
    def error(self,reqId,errorCode,errorString,advancedOrderRejectJson=""):
        if errorCode not in (2104,2106,2107,2108,2158):
            self.errors.append({"code":errorCode,"message":errorString})

def trading_day_check(day):
    import exchange_calendars as xcals
    calendar=xcals.get_calendar("XNYS")
    return bool(calendar.is_session(day.isoformat()))

def read_account(port=4002,timeout=15):
    if port!=4002:
        raise ValueError("DENIED: only paper Gateway port 4002")
    app=AccountReader()
    try:
        app.connect("127.0.0.1",port,clientId=80)
        threading.Thread(target=app.run,daemon=True).start()
        if not app.ready.wait(timeout):
            raise RuntimeError("Gateway connection timeout")
        app.reqManagedAccts()
        if not app.accounts_done.wait(timeout):
            raise RuntimeError("Account list timeout")
        if len(app.accounts)!=1 or not app.accounts[0].startswith("DU"):
            raise RuntimeError("DENIED: expected exactly one paper DU account")
        app.reqAccountSummary(810,"All","AvailableFunds,TotalCashValue,NetLiquidation,BuyingPower")
        if not app.summary_done.wait(timeout):
            raise RuntimeError("Account summary timeout")
        if app.errors:
            raise RuntimeError("IBKR errors: "+str(app.errors))
        result={}
        for tag in ("AvailableFunds","TotalCashValue","NetLiquidation","BuyingPower"):
            raw=app.values.get(tag,{}).get("value")
            try:
                val=Decimal(raw)
                if not val.is_finite() or val<0:
                    raise ValueError()
            except (InvalidOperation,TypeError,ValueError):
                raise RuntimeError("DENIED: missing/invalid USD "+tag)
            result[tag]=str(val)
        return {"account":app.accounts[0],"funds_usd":result,"mode":"READ_ONLY_NO_ORDERS"}
    finally:
        if app.isConnected():
            app.disconnect()

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--port",type=int,default=4002)
    p.add_argument("--date",default=None,help="US exchange date YYYY-MM-DD (default today New York)")
    args=p.parse_args()
    try:
        session_date=date.fromisoformat(args.date) if args.date else datetime.now(ZoneInfo("America/New_York")).date()
        result=read_account(args.port)
        result["nyse_date"]=session_date.isoformat()
        result["nyse_session"]=trading_day_check(session_date)
        result["orders_enabled"]=False
        print(json.dumps(result,indent=2))
    except Exception as exc:
        p.exit(1,"READ-ONLY CHECK FAILED: "+str(exc)+"\n")
if __name__=="__main__":
    main()
