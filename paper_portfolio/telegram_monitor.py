"""Telegram monitoring only; never changes trading ledger or submits orders."""
import json, os, math
from pathlib import Path
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import requests
import yfinance as yf
import exchange_calendars as xc

ROOT = Path("paper_portfolio")
state = json.loads((ROOT / "state.json").read_text())
path = ROOT / "telegram_monitor_state.json"
seen = json.loads(path.read_text()) if path.exists() else {"daily": "", "session": "", "alerts": []}
now = datetime.now(timezone.utc)
ny = now.astimezone(ZoneInfo("America/New_York"))
day = ny.date().isoformat()
cal = xc.get_calendar("XNYS")
session = cal.is_session(day)
def send(message):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        raise RuntimeError("Telegram secrets missing: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID")
    try:
        r = requests.post("https://api.telegram.org/bot"+token+"/sendMessage",
                          json={"chat_id": chat, "text": message}, timeout=20)
        if not r.ok or not r.json().get("ok"):
            raise RuntimeError("Telegram rejected message")
    except requests.RequestException:
        raise RuntimeError("Telegram connection failed") from None
def save():
    path.write_text(json.dumps(seen, indent=2)+"\n")
def quote(symbol):
    t = yf.Ticker(symbol)
    bars = t.history(period="5d", interval="1m", auto_adjust=False, prepost=False).dropna(subset=["Close"])
    if bars.empty:
        raise ValueError("missing")
    ts = bars.index[-1].to_pydatetime()
    price = float(bars.Close.iloc[-1])
    if not math.isfinite(price) or price <= 0 or ts.tzinfo is None or ts.astimezone(ny.tzinfo).date()!=ny.date() or not 0 <= (now-ts).total_seconds() <= 1200:
        raise ValueError("stale")
    daily = t.history(period="10d", interval="1d", auto_adjust=False).dropna(subset=["Close"])
    prior = daily[daily.index.date < ny.date()]
    if prior.empty:
        raise ValueError("missing previous close")
    prev = float(prior.Close.iloc[-1])
    if not math.isfinite(prev) or prev <= 0:
        raise ValueError("invalid previous close")
    return price, (price/prev-1)*100
ledger = state.get("last_processed")
if ledger and ledger > seen.get("daily", ""):
    report = (ROOT / "latest_report.md").read_text()
    if ledger not in report.splitlines()[0]:
        raise RuntimeError("Ledger/report mismatch")
    # Send only persisted ledger, not a partially completed run.
    daily = state["daily"][-1]
    lines = ["ABD TOP5 V2 | Günlük rapor "+ledger,
             f"Portföy: ${daily['equity']:,.2f} | Toplam: {(daily['equity']/state['initial_cash']-1)*100:+.2f}%",
             f"Nakit: ${state['cash']:,.2f} | Açık: {len(state['positions'])} | Bekleyen: {len(state['pending'])}"]
    if len(state["daily"])>1:
        prev=state["daily"][-2]
        lines.append(f"Günlük: {(daily['equity']/prev['equity']-1)*100:+.2f}% | SPY: {(daily['spy_close']/prev['spy_close']-1)*100:+.2f}%")
    for p in state["positions"]:
        try:
            b=yf.Ticker(p["symbol"]).history(start=ledger, end=str(__import__("datetime").date.fromisoformat(ledger)+__import__("datetime").timedelta(days=1)),auto_adjust=False)
            px=float(b.Close.iloc[-1])
            if b.index[-1].date().isoformat()!=ledger or not math.isfinite(px) or px<=0: raise ValueError()
            lines.append(f"{p['symbol']} {p['shares']} adet | ${px:.2f} | K/Z ${px*p['shares']-p['entry_cost']:+.2f}")
        except Exception:
            lines.append(f"{p['symbol']} {p['shares']} adet | Kapanış fiyatı alınamadı")
        if p["entry_date"]==ledger: lines.append("  Yeni sanal alım")
    for p in state["pending"]: lines.append("Yeni alım adayı: "+p["symbol"])
    for p in state["closed"]:
        if p.get("exit_date")==ledger: lines.append("Sanal satış: "+p["symbol"]+" | "+str(p.get("reason",p.get("exit_reason",""))))
    lines.append("Sadece paper; gerçek emir yok.")
    send("\n".join(lines))
    seen["daily"]=ledger
    save()
if not session or not cal.session_open(day) <= now <= cal.session_close(day):
    print("Outside regular session")
    raise SystemExit(0)
if seen.get("session")!=day:
    seen.update(session=day,alerts=[])
alerts=set(seen["alerts"])
messages=[]
total=float(state["cash"])
complete=True
def alert(key, msg):
    if key not in alerts:
        messages.append(msg)
        alerts.add(key)
for p in state["positions"]:
    sym=p["symbol"]
    try: px, change=quote(sym)
    except Exception:
        complete=False
        print("Unavailable/stale:",sym)
        continue
    total+=px*p["shares"]
    ret=(px/p["entry_price"]-1)*100
    if change<=-3: alert(sym+":down",f"{sym}: günlük {change:+.2f}% | ${px:.2f}")
    if change>=5: alert(sym+":up",f"{sym}: günlük {change:+.2f}% | ${px:.2f}")
    if ret<=-5:
        alert(sym+":stop",f"{sym}: STOP seviyesinde ({ret:+.2f}%) | ${px:.2f}")
        alerts.add(sym+":near")
    elif ret<=-4: alert(sym+":near",f"{sym}: stop seviyesine yakın ({ret:+.2f}%) | ${px:.2f}")
if complete and state["daily"]:
    change=(total/state["daily"][-1]["equity"]-1)*100
    if change<=-2: alert("portfolio:down",f"Portföy günlük {change:+.2f}% | ${total:,.2f}")
if messages:
    send("ABD TOP5 V2 | ACİL UYARI\n"+"\n".join(messages)+"\nGecikmeli fiyat olabilir. Sanal izleme; emir gönderilmedi.")
    seen["alerts"]=sorted(alerts)
    save()
print("Monitoring complete; alerts:",len(messages))
