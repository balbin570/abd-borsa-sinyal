"""One-shot IBKR paper health watchdog. No order calls, no credential storage.

Run from repository root: python -m ibkr_bridge.paper_health_watchdog
Schedule every five minutes with Windows Task Scheduler.
Optional Telegram alerts: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID environment variables.
"""
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "ibkr_bridge" / "local_runtime"
STATE = RUNTIME / "watchdog_state.json"
EVENTS = RUNTIME / "watchdog_events.jsonl"
EXPECTED = Path(os.getenv("IBKR_PAPER_STATE_PATH", str(Path.home() / "Desktop" / "ibkr_paper_state.json")))

def probe(module):
    try:
        p = subprocess.run([sys.executable, "-m", module, "--port", "4002"],
                           cwd=ROOT, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=65)
        if p.returncode:
            return False, (p.stderr or p.stdout)[-500:]
        data = json.loads(p.stdout)
        if data.get("mode") != "READ_ONLY_NO_ORDERS":
            return False, "unexpected broker mode"
        return True, data
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        return False, str(exc)

def telegram(message):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return "NOT_CONFIGURED"
    payload = urllib.parse.urlencode({"chat_id": chat, "text": message}).encode()
    try:
        req = urllib.request.Request(
            "https://api.telegram.org/bot" + token + "/sendMessage",
            data=payload, method="POST")
        with urllib.request.urlopen(req, timeout=12) as res:
            return "SENT" if json.load(res).get("ok") else "FAILED"
    except Exception:
        return "FAILED"

def main():
    RUNTIME.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat()
    try:
        expected = json.loads(EXPECTED.read_text(encoding="utf-8"))
        if expected.get("orders_enabled") is not False or expected.get("mode") != "IBKR_PAPER_READ_ONLY":
            raise ValueError("local state is not read-only")
        account = expected["account"]
        if not isinstance(account, str) or not account.startswith("DU"):
            raise ValueError("invalid paper account")
    except (OSError, ValueError, KeyError) as exc:
        status, reason = "CONFIG_ERROR", str(exc)
    else:
        ok, snapshot = probe("ibkr_bridge.paper_broker_snapshot")
        if not ok:
            status, reason = "BROKER_DISCONNECTED", str(snapshot)
        elif snapshot.get("account") != account:
            status, reason = "ACCOUNT_MISMATCH", "broker account differs from local paper state"
        else:
            ok, funds = probe("ibkr_bridge.paper_account_funds")
            if not ok:
                status, reason = "FUNDS_CHECK_FAILED", str(funds)
            elif funds.get("account") != account or funds.get("orders_enabled") is not False:
                status, reason = "ACCOUNT_MISMATCH", "funds account or mode mismatch"
            else:
                status, reason = "HEALTHY", "Paper API handshake, account, snapshot and funds verified"
    try:
        previous = json.loads(STATE.read_text(encoding="utf-8")).get("status")
    except (OSError, ValueError):
        previous = None
    alert = "SKIPPED"
    if previous != status:
        alert = telegram("IBKR PAPER WATCHDOG: " + status + "\n" + reason[:350])
    event = {"checked_utc": now, "status": status, "reason": reason[:500],
             "previous_status": previous, "telegram": alert,
             "orders_enabled": False}
    with EVENTS.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")
    temp = STATE.with_suffix(".tmp")
    temp.write_text(json.dumps(event, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(temp, STATE)
    print(json.dumps(event, ensure_ascii=False, indent=2))
    return 0 if status == "HEALTHY" else 1

if __name__ == "__main__":
    raise SystemExit(main())
