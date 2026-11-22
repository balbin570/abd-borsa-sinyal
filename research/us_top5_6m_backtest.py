"""Six-month historical simulation of existing V1/V2 rules. Research only; never writes live ledgers."""
import concurrent.futures, json, math, os, time
from datetime import date, timedelta
from pathlib import Path
import pandas as pd
import yfinance as yf
from paper_portfolio.top5_paper import get_universe
from paper_portfolio_v2.top5_v2 import SYMBOLS, indicators
from screener.rs_breakout import normalize_bars, build_signal_frame

END = date(2026, 11, 20)
START = date(2026, 5, 22)
WARMUP = START - timedelta(days=440)
INITIAL = 10000.0
FEE = .0015
OUT = Path("research/us_top5_6m")
OUT.mkdir(parents=True, exist_ok=True)

def download(sym):
    for retry in range(3):
        try:
            d = yf.Ticker(sym).history(start=str(WARMUP), end=str(END + timedelta(days=1)), auto_adjust=True)
            if d is None or d.empty: raise ValueError("empty")
            d.index = pd.to_datetime(d.index).tz_localize(None).normalize()
            d.columns = [str(c).lower() for c in d.columns]
            d = d[~d.index.duplicated(keep="last")]
            if not {"open","high","low","close","volume"}.issubset(d.columns): raise ValueError("missing columns")
            return sym,d,""
        except Exception as e:
            error = str(e)[:100]
            time.sleep(.3*(retry+1))
    return sym,None,error

def v1_signals(df,spy):
    # Exact installed screener function; all indicators are calculated causally.
    try:
        frame=build_signal_frame(df,spy["close"],require_delivery=False)
        return frame
    except Exception:
        return None

def simulate(version,frames,spy,universe):
    days=[d for d in spy.index if START <= d.date() <= END]
    state={"cash":INITIAL,"positions":[],"pending":[],"closed":[],"daily":[]}
    if version=="V1":
        signal_frames={}
        for sym in universe:
            if sym in frames:
                x=v1_signals(frames[sym],spy)
                if x is not None: signal_frames[sym]=x
    else:
        signal_frames={}
    for i,day in enumerate(days):
        prev=days[i-1] if i else None
        # Pending orders from previous close: fill at today's open.
        queued=state["pending"]; state["pending"]=[]
        for sig in queued:
            sym=sig["symbol"]
            df=frames.get(sym)
            if prev is None or sig["day"] != str(prev.date()) or df is None or day not in df.index or len(state["positions"])>=5: continue
            px=float(df.at[day,"open"])
            if not math.isfinite(px) or px<=0: continue
            budget=min(state["cash"],INITIAL*.2)
            qty=math.floor(budget/(px*(1+FEE)))
            if qty<1: continue
            cost=qty*px*(1+FEE)
            state["cash"]-=cost
            state["positions"].append({"symbol":sym,"entry":str(day.date()),"entry_px":px,"qty":qty,"cost":cost,"stop":px*.95 if version=="V1" else px-2.5*sig["atr"]})
        kept=[]
        for pos in state["positions"]:
            df=frames[pos["symbol"]]
            if day not in df.index:
                kept.append(pos); continue
            row=df.loc[day]
            held=len(df.loc[pd.Timestamp(pos["entry"]):day])
            op,lo,cl=[float(row[k]) for k in ("open","low","close")]
            if not all(math.isfinite(x) and x>0 for x in (op,lo,cl)):
                raise ValueError("invalid held OHLC "+pos["symbol"])
            reason=None
            if lo<=pos["stop"]: reason="STOP";px=min(op,pos["stop"])
            elif held>=10: reason="TIME10";px=cl
            if reason:
                proceeds=pos["qty"]*px*(1-FEE)
                pnl=proceeds-pos["cost"]
                state["cash"]+=proceeds
                state["closed"].append({"symbol":pos["symbol"],"entry":pos["entry"],"exit":str(day.date()),"pnl":round(pnl,2),"net_pct":round(100*pnl/pos["cost"],3),"reason":reason})
            else:kept.append(pos)
        state["positions"]=kept
        # Signal uses only today's fully closed bar. Execute tomorrow, never today.
        candidates=[]
        if version=="V1":
            for sym,frame in signal_frames.items():
                if day not in frame.index:continue
                row=frame.loc[day]
                try:
                    close,rs,st,week,vol=[float(row[k]) for k in ("close","rs_55","supertrend_value","previous_week_high","volume_ratio")]
                    if all(math.isfinite(x) for x in (close,rs,st,week,vol)) and rs>0 and close>st and close>week and vol>=1.2:
                        candidates.append({"symbol":sym,"day":str(day.date()),"score":rs+2*min(vol,4)})
                except (KeyError,ValueError,TypeError):continue
        else:
            sp=spy.loc[:day,"close"]
            market_ok=len(sp)>=200 and float(sp.iloc[-1])>float(sp.rolling(200).mean().iloc[-1])
            if market_ok:
                for sym in universe:
                    df=frames.get(sym)
                    if df is None or day not in df.index:continue
                    x=indicators(df,day)
                    if x and x["close"]>x["ma50"]>x["ma200"] and x["r63"]>0 and x["volume_ratio"]>=1.0 and x["atr"]/x["close"]<.07:
                        candidates.append({"symbol":sym,"day":str(day.date()),"score":x["score"],"atr":x["atr"]})
        candidates.sort(key=lambda x:(-x["score"],x["symbol"]))
        reserved={p["symbol"] for p in state["positions"]}
        for sig in candidates:
            if len(state["pending"])+len(state["positions"])>=5:break
            if sig["symbol"] not in reserved:
                state["pending"].append(sig);reserved.add(sig["symbol"])
        equity=state["cash"]
        for p in state["positions"]:
            df=frames[p["symbol"]]
            if day not in df.index: raise ValueError("missing held close "+p["symbol"])
            equity+=p["qty"]*float(df.at[day,"close"])
        state["daily"].append({"date":str(day.date()),"equity":round(equity,2)})
    daily=state["daily"]
    peak=INITIAL;max_dd=0
    for d in daily:
        peak=max(peak,d["equity"])
        max_dd=min(max_dd,100*(d["equity"]/peak-1))
    closed=state["closed"]
    wins=[p for p in closed if p["pnl"]>0]
    gp=sum(p["pnl"] for p in closed if p["pnl"]>0)
    gl=-sum(p["pnl"] for p in closed if p["pnl"]<0)
    return {"version":version,"initial_usd":INITIAL,"final_equity_usd":daily[-1]["equity"],"net_return_pct":round(100*(daily[-1]["equity"]/INITIAL-1),2),"closed_trades":len(closed),"win_rate_pct":round(100*len(wins)/len(closed),2) if closed else None,"profit_factor":round(gp/gl,3) if gl else None,"max_drawdown_pct":round(max_dd,2),"open_positions":len(state["positions"]),"daily":daily,"trades":closed}

def main():
    spy_sym,spy,err=download("SPY")
    if spy is None:raise RuntimeError("SPY download failed: "+err)
    v1=get_universe()
    v2=list(SYMBOLS)
    symbols=sorted(set(v1)|set(v2))
    frames={};errors={}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        for j,(sym,df,err) in enumerate(pool.map(download,symbols),1):
            if df is not None and len(df)>=80:frames[sym]=df
            else:errors[sym]=err or "short history"
            if j%50==0:print(f"download {j}/{len(symbols)}",flush=True)
    print(f"coverage V1 {len(set(v1)&frames.keys())}/{len(v1)} V2 {len(set(v2)&frames.keys())}/{len(v2)}",flush=True)
    if len(set(v1)&frames.keys())<.85*len(v1) or len(set(v2)&frames.keys())<.85*len(v2):
        raise RuntimeError("Market data coverage below 85%; refusing to publish misleading results")
    results=[simulate("V1",frames,spy,v1),simulate("V2",frames,spy,v2)]
    days=spy.loc[(spy.index.date>=START)&(spy.index.date<=END)]
    spy_ret=100*(float(days["close"].iloc[-1])/float(days["close"].iloc[0])-1)
    summary={"period":f"{START} to {END}","benchmark_spy_return_pct":round(spy_ret,2),"v1_universe":len(v1),"v2_universe":len(v2),"coverage_v1":len(set(v1)&frames.keys()),"coverage_v2":len(set(v2)&frames.keys()),"missing_symbols":errors,"results":results,"limitations":["Current S&P 500 membership used for past dates: survivorship bias","Yahoo adjusted OHLC may be revised and intraday stop fills are modeled","No market impact; 0.15% fee per side","V1 uses external screener package; V2 uses exact current code indicators","Open positions marked to final close; initial signals require historical warmup"]}
    (OUT/"result.json").write_text(json.dumps(summary,indent=2,allow_nan=False))
    lines=["# US TOP5 six-month historical backtest","",f"Period: {START} – {END}; initial: $10,000 per strategy; SPY: {spy_ret:+.2f}%","",
           "| Metric | V1 | V2 |","|---|---:|---:|"]
    metrics=[("net_return_pct","Net return %"),("final_equity_usd","Final equity USD"),("closed_trades","Closed trades"),("win_rate_pct","Win rate %"),("profit_factor","Profit factor"),("max_drawdown_pct","Max drawdown %"),("open_positions","Open positions")]
    for key,label in metrics:lines.append(f"| {label} | {results[0][key]} | {results[1][key]} |")
    lines+=["","**Limitations:** Current S&P500 members are used historically (survivorship bias). Data adjusted for splits/dividends. Stops simulated with daily OHLC. Fees 0.15% per side. Paper only, no broker orders.",""]
    report="\n".join(lines)
    (OUT/"report.md").write_text(report)
    print(report)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"],"a") as f:f.write(report)

if __name__=="__main__":main()
