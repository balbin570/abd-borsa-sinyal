"""Fiyat alarmi -> Telegram (sadece bildirim, emir yok).

ALERTS listesindeki her enstruman icin son fiyat esigin altina inerse bir kez mesaj atar.
Fiyat esigin %3 ustune cikinca alarm yeniden kurulur (tekrar tekrar mesaj gelmesin diye).
"""
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

ALERTS = [
    {"id": "IGDA_below_33", "ticker": "IGDA.L", "name": "IGDA (Invesco DJ Islamic Global Developed)",
     "below": 33.00, "note": "Al-tut planinda dususte ekstra alim firsati olabilir."},
]
REARM_PCT = 3.0
STATE = Path("price_alerts/state.json")
TR = ZoneInfo("Europe/Istanbul")


def send(text):
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        raise RuntimeError("Telegram secrets missing")
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat, "text": text}, timeout=20)
    if not r.ok or not r.json().get("ok"):
        raise RuntimeError("Telegram rejected message")


def last_price(ticker):
    import yfinance as yf
    h = yf.Ticker(ticker).history(period="5d", interval="1d", auto_adjust=False).dropna(subset=["Close"])
    if h.empty:
        return None, None
    return float(h["Close"].iloc[-1]), h.index[-1].date().isoformat()


def main(test=False):
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    lines = []
    for a in ALERTS:
        px, day = last_price(a["ticker"])
        print(a["ticker"], px, day)
        if px is None:
            continue
        s = state.setdefault(a["id"], {"armed": True})
        s.update(last_price=px, last_date=day)
        if test:
            lines.append(f"{a['name']}: {px:.2f} $ ({day}) | alarm: {a['below']:.2f} $ alti")
            continue
        if s["armed"] and px <= a["below"]:
            send(f"🔔 FIYAT ALARMI\n{a['name']}\nFiyat: {px:.2f} $  (esik {a['below']:.2f} $)\n"
                 f"{a['note']}\nBilgi amaclidir, emir verilmedi.")
            s.update(armed=False, triggered_at=datetime.now(TR).isoformat(timespec="minutes"))
        elif not s["armed"] and px >= a["below"] * (1 + REARM_PCT / 100):
            s["armed"] = True
    if test:
        send("🧪 Fiyat alarmi testi\n" + "\n".join(lines) + "\nKurulum tamam.")
    STATE.parent.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(state, indent=2) + "\n")


if __name__ == "__main__":
    main(test="--test" in sys.argv)
