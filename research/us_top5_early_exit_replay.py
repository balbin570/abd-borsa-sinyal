"""Read-only causal portfolio replay of early-loss exits; unchanged entry rules.

Sell at the next session OPEN after a day-1/2/3 CLOSE at/below the specified
entry-relative loss threshold. Baseline original STOP/TIME10 always takes
precedence; a previous-close early-exit overrides today's newly hit stop at
open (the exit was already scheduled before today's OHLC). Retained selection,
cash, stop and time logic match the original V1/V2 historical backtest.
"""
import concurrent.futures, json, math
from pathlib import Path
import pandas as pd
from research.us_top5_6m_backtest import START,END,INITIAL,FEE,download,get_universe,SYMBOLS,indicators,v1_signals

OUT=Path("research/us_top5_6m")
CONFIGS=[("BASELINE",None,None)]+[(f"D{d}_LOSS{abs(t)}",d,t) for d in (1,2,3) for t in (-1,-2)]

def simulate(version,frames,spy,universe,mode,cutoff_day,threshold):
    days=[d for d in spy.index if START<=d.date()<=END]
    signals={}
    if version=="V1":
        for symbol in universe:
            if symbol in frames:
                sf=v1_signals(frames[symbol],spy)
                if sf is not None:signals[symbol]=sf
    cash=INITIAL
    positions=[];pending=[];daily=[];closed=[]
    early_exits=0
    for i,day in enumerate(days):
        prev=days[i-1] if i else None
        # Confirmed previous-close exit is filled at next day's OPEN.
        remain=[]
        for p in positions:
            df=frames[p["symbol"]]
            if p.get("early_due") and day in df.index:
                op=float(df.at[day,"open"])
                if not math.isfinite(op) or op<=0:raise ValueError("Bad early exit open")
                proceeds=p["qty"]*op*(1-FEE)
                pnl=proceeds-p["cost"];cash+=proceeds
                closed.append({"symbol":p["symbol"],"pnl":pnl,"reason":"EARLY_NEXT_OPEN"})
                early_exits+=1
            else:
                remain.append(p)
        positions=remain
        # All orders submitted at yesterday's close enter at today's OPEN.
        queued=pending;pending=[]
        for sig in queued:
            sym=sig["symbol"];df=frames.get(sym)
            if prev is None or sig["day"]!=str(prev.date()) or df is None or day not in df.index or len(positions)>=5:continue
            op=float(df.at[day,"open"])
            if not math.isfinite(op) or op<=0:continue
            qty=math.floor(min(cash,INITIAL*.2)/(op*(1+FEE)))
            if qty<1:continue
            cost=qty*op*(1+FEE);cash-=cost
            stop=op*.95 if version=="V1" else op-2.5*sig["atr"]
            positions.append({"symbol":sym,"entry":str(day.date()),"entry_px":op,
                              "qty":qty,"cost":cost,"stop":stop,"early_due":False})
        kept=[]
        for p in positions:
            df=frames[p["symbol"]]
            if day not in df.index:kept.append(p);continue
            row=df.loc[day]
            op,lo,cl=[float(row[k]) for k in ("open","low","close")]
            if not all(math.isfinite(x) and x>0 for x in (op,lo,cl)):
                raise ValueError("Bad OHLC for held symbol")
            held=len(df.loc[pd.Timestamp(p["entry"]):day])
            reason=None;px=None
            if lo<=p["stop"]:
                reason="STOP";px=min(op,p["stop"])
            elif held>=10:
                reason="TIME10";px=cl
            if reason:
                proceeds=p["qty"]*px*(1-FEE);cash+=proceeds
                closed.append({"symbol":p["symbol"],"pnl":proceeds-p["cost"],"reason":reason})
            else:
                if cutoff_day is not None and held==cutoff_day and 100*(cl/p["entry_px"]-1)<=threshold:
                    p["early_due"]=True
                kept.append(p)
        positions=kept
        candidates=[]
        if version=="V1":
            for sym,f in signals.items():
                if day not in f.index:continue
                try:
                    r=f.loc[day]
                    cl,rs,st,week,vol=[float(r[k]) for k in ("close","rs_55","supertrend_value","previous_week_high","volume_ratio")]
                    if all(math.isfinite(x) for x in (cl,rs,st,week,vol)) and rs>0 and cl>st and cl>week and vol>=1.2:
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
                    if x and x["close"]>x["ma50"]>x["ma200"] and x["r63"]>0 and x["volume_ratio"]>=1 and x["atr"]/x["close"]<.07:
                        candidates.append({"symbol":sym,"day":str(day.date()),"score":x["score"],"atr":x["atr"]})
        candidates.sort(key=lambda x:(-x["score"],x["symbol"]))
        reserved={p["symbol"] for p in positions}
        for candidate in candidates:
            if len(pending)+len(positions)>=5:break
            if candidate["symbol"] not in reserved:
                pending.append(candidate);reserved.add(candidate["symbol"])
        equity=cash
        for p in positions:
            df=frames[p["symbol"]]
            if day not in df.index:raise ValueError("Missing close")
            equity+=p["qty"]*float(df.at[day,"close"])
        daily.append(equity)
    if not daily:raise RuntimeError("No days")
    peak=INITIAL;max_drawdown=0
    for equity in daily:
        peak=max(peak,equity)
        max_drawdown=min(max_drawdown,100*(equity/peak-1))
    wins=[t for t in closed if t["pnl"]>0]
    gains=sum(t["pnl"] for t in closed if t["pnl"]>0)
    losses=-sum(t["pnl"] for t in closed if t["pnl"]<0)
    return {"strategy":version,"mode":mode,"equity_usd":round(daily[-1],2),
       "net_return_pct":round(100*(daily[-1]/INITIAL-1),2),
       "closed_trades":len(closed),"early_exits":early_exits,
       "win_rate_pct":round(100*len(wins)/len(closed),2) if closed else None,
       "profit_factor":round(gains/losses,3) if losses else None,
       "max_drawdown_pct":round(max_drawdown,2),
       "open_positions":len(positions)}

def main():
    _,spy,err=download("SPY")
    if spy is None:raise RuntimeError("SPY download failed "+err)
    v1=get_universe();v2=list(SYMBOLS)
    universe=sorted(set(v1)|set(v2));frames={};failures={}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        for j,(sym,df,error) in enumerate(pool.map(download,universe),1):
            if df is not None and len(df)>=80:frames[sym]=df
            else:failures[sym]=error or "insufficient history"
            if j%50==0:print(f"download {j}/{len(universe)}",flush=True)
    for version,symbols in (("V1",v1),("V2",v2)):
        if len(set(symbols)&frames.keys())<.85*len(symbols):
            raise RuntimeError(f"Coverage below 85% for {version}")
    results=[]
    for version,symbols in (("V1",v1),("V2",v2)):
        for mode,cutoff_day,threshold in CONFIGS:
            row=simulate(version,frames,spy,symbols,mode,cutoff_day,threshold)
            print(version,mode,row["net_return_pct"],flush=True)
            results.append(row)
    base={r["strategy"]:r for r in results if r["mode"]=="BASELINE"}
    for r in results:
        r["vs_baseline_percentage_points"]=round(r["net_return_pct"]-base[r["strategy"]]["net_return_pct"],2)
    data={"period":f"{START} to {END}","source":"Downloaded adjusted Yahoo OHLC",
          "tests":[{"label":mode,"day":d,"threshold":t} for mode,d,t in CONFIGS],
          "results":results,"unavailable_symbols":failures,
          "limitations":["Causal exit signal from close; execution next session open, no same-close fills",
            "Baseline intraday stop precedence and daily OHLC order assumptions unchanged",
            "V2 filters and V1 entry rankings held constant; early exits change later trades",
            "0.15% fee each side; no additional spread/market-impact adjustment",
            "Current S&P500 constituents create survivorship bias",
            "Variants selected using the same six-month sample; external validation still required"]}
    OUT.mkdir(parents=True,exist_ok=True)
    (OUT/"early_exit_replay.json").write_text(json.dumps(data,indent=2,allow_nan=False))
    lines=["# US TOP5 early-loss exits: full portfolio replay","",
           f"Period {START} to {END}; initial $10,000; paper-only, no broker orders.","",
           "A signal observed at day N CLOSE executes at the NEXT session OPEN. Original intraday stop and 10-bar time exit remain. 0.15% fee each side.","",
           "| Strategy | Exit | Net % | Vs baseline pp | Equity $ | Closed | Early exits | Win % | PF | Max DD % |",
           "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for r in results:
        lines.append(f'| {r["strategy"]} | {r["mode"]} | {r["net_return_pct"]} | {r["vs_baseline_percentage_points"]} | {r["equity_usd"]} | {r["closed_trades"]} | {r["early_exits"]} | {r["win_rate_pct"]} | {r["profit_factor"]} | {r["max_drawdown_pct"]} |')
    lines+=["","Important: This is exploratory in-sample research, not an out-of-sample guarantee. Relative results can vary if market data or constituent list change.",""]
    report="\n".join(lines)
    (OUT/"early_exit_replay.md").write_text(report)
    print(report)
    import os
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"],"a") as f:f.write(report+"\n")

if __name__=="__main__":main()
