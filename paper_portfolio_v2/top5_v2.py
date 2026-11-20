"""Independent US TOP5 V2 forward-only paper portfolio. No broker integration."""
import json, math, time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
import pandas as pd
import yfinance as yf

ROOT = Path("paper_portfolio_v2")
STATE = ROOT / "state.json"
REPORT = ROOT / "latest_report.md"
INITIAL = 10000.0
FEE = 0.0015
MAX_POS = 5
# Liquid, large-cap US universe. Static to keep forward comparisons reproducible.
SYMBOLS = """AAPL MSFT NVDA AMZN META GOOGL GOOG AVGO TSLA BRK-B JPM V MA XOM UNH COST NFLX WMT PG JNJ HD ABBV BAC CRM ORCL CVX KO MRK AMD CSCO ADBE PEP TMO LIN ACN MCD ABT QCOM TXN AMGN IBM GE CAT GS ISRG DIS NOW INTU VZ T AMAT BKNG SPGI PFE NKE RTX HON LOW UPS DE LMT SBUX BLK SCHW C BA GILD ADP MDLZ PLD SYK TJX MMC CB PGR ELV CME DHR BMY MO SO DUK NEE COP SLB EOG OXY PANW CRWD MU LRCX KLAC SNPS CDNS ANET UBER ABNB PYPL SQ FISV APP VTRS HUM TGT AES""".split()

def finite(v):
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except (ValueError, TypeError):
        return None

def history(sym, end):
    for attempt in range(3):
        try:
            df = yf.Ticker(sym).history(start=str(end-timedelta(days=330)), end=str(end+timedelta(days=1)), auto_adjust=True)
            if df is None or df.empty: raise ValueError("empty")
            df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
            df.columns = [str(x).lower() for x in df.columns]
            df = df[~df.index.duplicated(keep="last")]
            if len(df) < 80: raise ValueError("short")
            return df
        except Exception:
            time.sleep(0.3*(attempt+1))
    return None

def indicators(df, day):
    h = df.loc[:day].copy()
    if len(h) < 205: return None
    close, high, low = h["close"], h["high"], h["low"]
    prev = close.shift(1)
    tr = pd.concat([high-low, (high-prev).abs(), (low-prev).abs()],axis=1).max(axis=1)
    atr = finite(tr.rolling(14).mean().iloc[-1])
    c = finite(close.iloc[-1])
    ma50 = finite(close.rolling(50).mean().iloc[-1])
    ma200 = finite(close.rolling(200).mean().iloc[-1])
    prior63 = finite(close.iloc[-64])
    prior21 = finite(close.iloc[-22])
    if None in (atr,c,ma50,ma200,prior63,prior21) or min(atr,c,ma50,ma200,prior63,prior21)<=0:return None
    r63 = c/prior63-1
    r21 = c/prior21-1
    atr_pct = atr/c
    vol = finite(h["volume"].iloc[-1])
    volavg = finite(h["volume"].iloc[-21:-1].mean())
    vr = vol/volavg if vol is not None and volavg and volavg>0 else 0
    score = 100*(0.7*r63+0.3*r21)-30*atr_pct
    return {"close":c,"atr":atr,"score":score,"ma50":ma50,"ma200":ma200,"volume_ratio":vr,"r63":r63}

def main():
    ROOT.mkdir(exist_ok=True)
    now = datetime.now(ZoneInfo("America/New_York"))
    cutoff = now.date() if now.hour>=17 else now.date()-timedelta(days=1)
    spy = history("SPY", cutoff)
    if spy is None: raise RuntimeError("SPY data unavailable; no changes")
    dates = list(spy.index)
    latest = dates[-1]
    if latest.date()>cutoff: raise RuntimeError("Incomplete market session")
    if STATE.exists():
        state=json.loads(STATE.read_text())
    else:
        state={"version":2,"initial_cash":INITIAL,"cash":INITIAL,"positions":[],"pending":[],"closed":[],"daily":[],"last_processed":None}
    if state["version"]!=2:raise RuntimeError("Unknown state version")
    if state["last_processed"]==str(latest.date()):
        print("Already processed", latest.date());return
    last = pd.Timestamp(state["last_processed"]) if state["last_processed"] else None
    todo = [d for d in dates if (last is None and d==latest) or (last is not None and d>last)]
    if not todo:print("No new session");return
    # Do not invent fills or retroactively create signals on missed sessions.
    if len(todo)>1:raise RuntimeError(f"Missed {len(todo)} sessions; manual reconciliation required, ledger unchanged")
    day=todo[0]
    required={p["symbol"] for p in state["positions"]}|{p["symbol"] for p in state["pending"]}
    data={}
    for sym in sorted(set(SYMBOLS)|required):
        df=history(sym,cutoff)
        if df is not None and day in df.index and all(finite(df.loc[day,k]) is not None and finite(df.loc[day,k])>0 for k in ("open","high","low","close")):
            data[sym]=df
    if len(set(SYMBOLS)&set(data)) < int(len(SYMBOLS)*0.85):
        raise RuntimeError(f"Coverage {len(set(SYMBOLS)&set(data))}/{len(SYMBOLS)} below 85%; no changes")
    missing=required-set(data)
    if missing:raise RuntimeError(f"Missing held/pending OHLC: {sorted(missing)}; no changes")
    # Entry at today's OPEN only for signals from immediately preceding trading session.
    previous_session=dates[dates.index(day)-1] if dates.index(day)>0 else None
    pending=state["pending"];state["pending"]=[]
    for signal in pending:
        if previous_session is None or signal["signal_date"]!=str(previous_session.date()):continue
        if len(state["positions"])>=MAX_POS:continue
        px=float(data[signal["symbol"]].loc[day,"open"])
        budget=min(state["cash"],INITIAL/MAX_POS)
        qty=math.floor(budget/(px*(1+FEE)))
        if qty<1:continue
        cost=qty*px*(1+FEE)
        state["cash"]-=cost
        state["positions"].append({"symbol":signal["symbol"],"entry_date":str(day.date()),"entry_price":px,"shares":qty,"entry_cost":cost,"initial_atr":signal["atr"],"stop":px-2.5*signal["atr"]})
    kept=[]
    for pos in state["positions"]:
        bar=data[pos["symbol"]].loc[day]
        op,lo,cl=[float(bar[k]) for k in ("open","low","close")]
        stop=pos["stop"]
        held=len(data[pos["symbol"]].loc[pd.Timestamp(pos["entry_date"]):day])
        reason=None
        if lo<=stop:reason="ATR_STOP";price=min(op,stop)
        elif held>=10:reason="TIME10";price=cl
        if reason:
            proceeds=pos["shares"]*price*(1-FEE)
            pnl=proceeds-pos["entry_cost"]
            state["cash"]+=proceeds
            state["closed"].append({**pos,"exit_date":str(day.date()),"exit_price":price,"reason":reason,"net_pnl":round(pnl,2),"net_return_pct":round(100*pnl/pos["entry_cost"],3)})
        else:
            kept.append(pos)
    state["positions"]=kept
    sp=spy.loc[:day,"close"]
    market_ok=len(sp)>=200 and float(sp.iloc[-1])>float(sp.rolling(200).mean().iloc[-1])
    candidates=[]
    if market_ok:
        reserved={p["symbol"] for p in state["positions"]}
        for sym,df in data.items():
            if sym in reserved or sym not in SYMBOLS:continue
            x=indicators(df,day)
            if x and x["close"]>x["ma50"]>x["ma200"] and x["r63"]>0 and x["volume_ratio"]>=1.0 and x["atr"]/x["close"]<0.07:
                candidates.append({"symbol":sym,"signal_date":str(day.date()),"score":round(x["score"],5),"atr":round(x["atr"],6)})
        candidates.sort(key=lambda x:(-x["score"],x["symbol"]))
        state["pending"]=candidates[:max(0,MAX_POS-len(state["positions"]))]
    equity=state["cash"]+sum(p["shares"]*float(data[p["symbol"]].loc[day,"close"]) for p in state["positions"])
    state["daily"].append({"date":str(day.date()),"equity":round(equity,2),"cash":round(state["cash"],2),"market_ok":market_ok,"open":len(state["positions"]),"pending":len(state["pending"])})
    state["last_processed"]=str(day.date())
    report=f"""# US TOP5 V2 — {day.date()}

Paper only; no brokerage orders. Independent V2 ledger, V1 untouched.

- Universe: {len(SYMBOLS)} liquid US equities; valid: {len(set(SYMBOLS)&set(data))}
- SPY > SMA200 regime: {market_ok}
- Equity: ${equity:,.2f}; return: {(equity/INITIAL-1)*100:+.2f}%
- Cash: ${state["cash"]:,.2f}; open: {len(state["positions"])}; closed: {len(state["closed"])}; pending: {len(state["pending"])}
- Selection: 63d/21d momentum, SMA50/SMA200 trend, volume and ATR filter
- Risk: initial 2.5 ATR stop, 10 trading-bar time exit, 0.15% fee each side
- Next session open entries only; no backtest and no live orders

## Open
"""+"\n".join(f'- {p["symbol"]}: {p["shares"]} shares at ${p["entry_price"]:.2f}' for p in state["positions"])+"\n\n## Pending\n"+"\n".join(f'- {p["symbol"]}: score {p["score"]:.2f}' for p in state["pending"])+"\n\n## Closed\n"+"\n".join(f'- {p["symbol"]}: ${p["net_pnl"]:+.2f} ({p["reason"]})' for p in state["closed"][-10:])+"\n"
    # Validate before publishing either file.
    payload=json.dumps(state,indent=2,allow_nan=False)+"\n"
    (ROOT/"latest_report.md").write_text(report)
    STATE.write_text(payload)
    print(report)

if __name__=="__main__":main()
