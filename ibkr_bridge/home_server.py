"""Windows home-server health monitor for IBKR PAPER; never sends orders.

Run via: python -m ibkr_bridge.home_server
"""
import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
DATA=ROOT/"ibkr_bridge"/"local_runtime"
LOG=DATA/"server.log"
STOP=False

def handle_stop(signum,frame):
    global STOP
    STOP=True

def run_check(module,timeout=45):
    cmd=[sys.executable,"-m",module,"--port","4002"]
    try:
        proc=subprocess.run(cmd,cwd=ROOT,capture_output=True,text=True,
                            encoding="utf-8",errors="replace",timeout=timeout)
        return {"ok":proc.returncode==0,"output":(proc.stdout if proc.returncode==0 else proc.stderr+"\n"+proc.stdout)[-1200:]}
    except (OSError,subprocess.TimeoutExpired) as exc:
        return {"ok":False,"output":str(exc)}

def once():
    stamp=datetime.now(timezone.utc).isoformat()
    snapshot=run_check("ibkr_bridge.paper_broker_snapshot")
    funds=run_check("ibkr_bridge.paper_account_funds") if snapshot["ok"] else {"ok":False,"output":"Skipped: snapshot unavailable"}
    result={"checked_utc":stamp,"snapshot_ok":snapshot["ok"],"funds_ok":funds["ok"],
            "orders_enabled":False,"snapshot_message":snapshot["output"] if not snapshot["ok"] else "Complete",
            "funds_message":funds["output"] if not funds["ok"] else "Complete"}
    DATA.mkdir(parents=True,exist_ok=True)
    tmp=DATA/"health.json.tmp"
    tmp.write_text(json.dumps(result,indent=2)+"\n",encoding="utf-8")
    os.replace(tmp,DATA/"health.json")
    return result

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--interval",type=int,default=300,help="Health interval in seconds, minimum 60")
    parser.add_argument("--once",action="store_true")
    args=parser.parse_args()
    if args.interval<60:parser.error("interval must be >=60")
    DATA.mkdir(parents=True,exist_ok=True)
    logging.basicConfig(filename=LOG,level=logging.INFO,format="%(asctime)s %(levelname)s %(message)s")
    for sig in (signal.SIGINT,signal.SIGTERM):
        signal.signal(sig,handle_stop)
    while not STOP:
        status=once()
        msg=f"IBKR PAPER health snapshot={status['snapshot_ok']} funds={status['funds_ok']} orders=False"
        print(msg,flush=True)
        logging.info(msg)
        if args.once:break
        for _ in range(args.interval):
            if STOP:break
            time.sleep(1)
if __name__=="__main__":
    main()
