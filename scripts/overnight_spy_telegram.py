"""OVERNIGHT-SPY V1 - Telegram sinyali (sadece mesaj, gerçek emir YOK).

Strateji: Kapanışa yakın AL, ertesi işlem günü açılışta SAT.
  * Kapanıştan ~25-3 dk önce : 🟢 AL mesajı (trend filtresi geçerse)
  * Açılıştan ~40 dk önce     : 🔴 SAT (açılışta) mesajı
  * Açılıştan sonra          : ✅ Sonuç mesajı (kapanış -> açılış getirisi)

GitHub Actions her 5 dakikada bir çalıştırır; tekrar mesajı state.json ile engellenir.
Tatil/yarım gün: exchange_calendars (XNYS).
"""
import csv
import json
import math
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

SYMBOL = os.environ.get("OVERNIGHT_SYMBOL", "SPY")
ALLOC_USD = float(os.environ.get("OVERNIGHT_ALLOC_USD", "5000"))
SMA_DAYS = 200
ROOT = Path("overnight_spy")
STATE = ROOT / "state.json"
LOG = ROOT / "islemler.csv"
NY = ZoneInfo("America/New_York")
TR = ZoneInfo("Europe/Istanbul")

BUY_FROM, BUY_TO = timedelta(minutes=25), timedelta(minutes=3)   # kapanıştan önce
SELL_FROM = timedelta(minutes=40)                                # açılıştan önce
RESULT_AFTER = timedelta(minutes=2)                              # açılıştan sonra


def send(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        raise RuntimeError("Telegram secrets missing: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID")
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat, "text": text}, timeout=20)
    if not r.ok or not r.json().get("ok"):
        raise RuntimeError("Telegram rejected message")
    print("Telegram:", text.replace("\n", " | "))


def load_state():
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {"position": None, "last_buy_check": "", "results": 0}


def save_state(st):
    ROOT.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(st, indent=2, ensure_ascii=False) + "\n")


def tr(ts):
    return ts.astimezone(TR).strftime("%H:%M")


# ------------------------------------------------------------------ veri
def last_price(now):
    import yfinance as yf
    bars = yf.Ticker(SYMBOL).history(period="1d", interval="1m", auto_adjust=False,
                                     prepost=False).dropna(subset=["Close"])
    if bars.empty:
        return None
    ts = bars.index[-1].to_pydatetime()
    px = float(bars.Close.iloc[-1])
    if not math.isfinite(px) or px <= 0 or (now - ts).total_seconds() > 1200:
        return None
    return px


def sma_before(day):
    import yfinance as yf
    d = yf.Ticker(SYMBOL).history(period="1y", interval="1d", auto_adjust=False).dropna(subset=["Close"])
    closes = [float(c) for i, c in zip(d.index, d.Close) if i.date().isoformat() < day]
    if len(closes) < SMA_DAYS:
        return None
    return sum(closes[-SMA_DAYS:]) / SMA_DAYS


def official_close(day):
    import yfinance as yf
    d = yf.Ticker(SYMBOL).history(period="10d", interval="1d", auto_adjust=False).dropna(subset=["Close"])
    for i, c in zip(d.index, d.Close):
        if i.date().isoformat() == day:
            return float(c)
    return None


def official_open(day, open_ts):
    import yfinance as yf
    bars = yf.Ticker(SYMBOL).history(period="1d", interval="1m", auto_adjust=False,
                                     prepost=False).dropna(subset=["Open"])
    bars = bars[bars.index >= open_ts]
    if bars.empty or bars.index[0].date().isoformat() != day:
        return None
    return float(bars.Open.iloc[0])


# ------------------------------------------------------------------ ana akış
def main(test=False):
    if test:
        send(f"🧪 OVERNIGHT-{SYMBOL} bağlantı testi\n{datetime.now(TR):%Y-%m-%d %H:%M} TR\nSinyal değildir.")
        return

    import exchange_calendars as xc
    now = datetime.now(timezone.utc)
    day = now.astimezone(NY).date().isoformat()
    cal = xc.get_calendar("XNYS")
    if not cal.is_session(day):
        print("Borsa kapalı:", day)
        return
    open_ts = cal.session_open(day).to_pydatetime()
    close_ts = cal.session_close(day).to_pydatetime()
    st = load_state()
    pos = st.get("position")

    # 1) SAT mesajı (açılıştan önce)
    if pos and pos["buy_date"] < day and pos.get("sell_msg_day") != day \
            and open_ts - SELL_FROM <= now < open_ts + timedelta(hours=1):
        send(f"🔴 SAT  {SYMBOL}  (OVERNIGHT)\n"
             f"{pos['qty']} adet - açılışta sat (TR {tr(open_ts)}).\n"
             f"Alış sinyali: {pos['buy_date']}")
        pos["sell_msg_day"] = day
        save_state(st)

    # 2) Sonuç mesajı (açılıştan sonra)
    if pos and pos.get("sell_msg_day") == day and now >= open_ts + RESULT_AFTER:
        o = official_open(day, open_ts)
        c = official_close(pos["buy_date"]) or pos["signal_price"]
        if o and c:
            pct = (o / c - 1) * 100
            pnl = (o - c) * pos["qty"]
            st["results"] = st.get("results", 0) + 1
            send(f"✅ SONUÇ  {SYMBOL}  (OVERNIGHT)\n"
                 f"Kapanış {c:.2f} -> Açılış {o:.2f}\n"
                 f"Gece getirisi: {pct:+.2f}%   K/Z: {pnl:+.2f} USD ({pos['qty']} adet)")
            new = not LOG.exists()
            with LOG.open("a", newline="") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(["buy_date", "sell_date", "symbol", "qty", "close", "open", "pct", "pnl_usd"])
                w.writerow([pos["buy_date"], day, SYMBOL, pos["qty"], round(c, 4), round(o, 4),
                            round(pct, 4), round(pnl, 2)])
            st["position"] = pos = None
            save_state(st)
        else:
            print("Açılış fiyatı henüz yok, sonraki çalışmada denenecek")

    # 3) AL mesajı (kapanışa yakın)
    if not pos and st.get("last_buy_check") != day \
            and close_ts - BUY_FROM <= now < close_ts - BUY_TO:
        px = last_price(now)
        if px is None:
            print("Güncel fiyat yok, sonraki çalışmada denenecek")
            return
        sma = sma_before(day)
        st["last_buy_check"] = day
        if sma and px <= sma:
            send(f"⏸ OVERNIGHT {SYMBOL}: bu gece alım YOK\n"
                 f"Fiyat {px:.2f} < {SMA_DAYS} günlük ortalama {sma:.2f}")
        else:
            qty = max(1, int(ALLOC_USD // px))
            st["position"] = {"buy_date": day, "signal_price": px, "qty": qty}
            send(f"🟢 AL  {SYMBOL}  (OVERNIGHT)\n"
                 f"{qty} adet  ~{px:.2f}  (~{qty * px:,.0f} USD)\n"
                 f"Kapanıştan önce al (kapanış TR {tr(close_ts)}).\n"
                 f"Yarın açılışta SAT mesajı gelecek.")
        save_state(st)


if __name__ == "__main__":
    main(test="--test" in sys.argv)
