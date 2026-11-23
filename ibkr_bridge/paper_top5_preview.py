"""One-command GitHub Top5 -> IBKR paper preview. Never sends orders."""
import argparse
import json
import subprocess
import sys
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from ibkr_bridge.paper_candidate_gate import review

FEED_URL = "https://raw.githubusercontent.com/balbin570/abd-borsa-sinyal/main/paper_portfolio/ibkr_paper_candidates.json"
DEFAULT_STATE = Path.home() / "Desktop" / "ibkr_paper_state.json"

def load_feed(url):
    with urllib.request.urlopen(url, timeout=20) as response:
        if response.status != 200:
            raise RuntimeError("Feed HTTP status " + str(response.status))
        raw = response.read(1000001)
    if len(raw) > 1000000:
        raise ValueError("Feed too large")
    return json.loads(raw.decode("utf-8"))

def broker_snapshot():
    proc = subprocess.run(
        [sys.executable, "-m", "ibkr_bridge.paper_broker_snapshot", "--port", "4002"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=65)
    if proc.returncode:
        raise RuntimeError("Paper broker snapshot failed: " + (proc.stderr or proc.stdout)[-300:])
    return json.loads(proc.stdout)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", default=str(DEFAULT_STATE))
    parser.add_argument("--feed-file", help="Use a local JSON feed instead of GitHub")
    args = parser.parse_args()
    output = {"mode":"READ_ONLY_NO_ORDERS", "orders_enabled":False,
              "signal_status":"BLOCKED", "candidates_for_review":[]}
    try:
        state = json.loads(Path(args.state).read_text(encoding="utf-8"))
        if state.get("orders_enabled") is not False or state.get("mode") != "IBKR_PAPER_READ_ONLY":
            raise ValueError("Local paper state not read-only")
        snapshot = broker_snapshot()
        if snapshot.get("account") != state.get("account"):
            raise ValueError("Broker account mismatch")
        if snapshot.get("complete") != {"positions":True,"orders":True,"executions":True}:
            raise ValueError("Incomplete broker snapshot")
        feed = json.loads(Path(args.feed_file).read_text(encoding="utf-8")) if args.feed_file else load_feed(FEED_URL)
        today = datetime.now(ZoneInfo("America/New_York")).date()
        gated = review(state, feed, today=today)
        output.update({"account_verified":True, "signal_status":gated["status"],
                       "signal_session":gated["signal_session"],
                       "broker_positions":len(snapshot["positions"]),
                       "broker_open_orders":len(snapshot["open_orders"]),
                       "candidates_for_review":gated.get("accepted",[])})
        if gated["status"] == "PREVIEW_ONLY" and (snapshot["positions"] or snapshot["open_orders"]):
            output["signal_status"] = "REVIEW_REQUIRED_BROKER_EXPOSURE"
            output["candidates_for_review"] = []
        print(json.dumps(output,indent=2))
        return 0
    except Exception as exc:
        output["error"] = str(exc)
        print(json.dumps(output,indent=2))
        return 1

if __name__ == "__main__":
    raise SystemExit(main())
