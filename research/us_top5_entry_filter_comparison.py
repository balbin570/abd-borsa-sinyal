"""Read-only entry-filter comparison for V1-D2-LOSS2; live code untouched.

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
# One-variable-at-a-time variants; every case keeps D2-LOSS2 exits fixed.
VARIANTS = [
    ("CURRENT",1.2,0.0,0.0,None),
    ("VOL_1_0",1.0,0.0,0.0,None),
    ("VOL_1_5",1.5,0.0,0.0,None),
    ("VOL_2_0",2.0,0.0,0.0,None),
    ("RS55_MIN_5",1.2,5.0,0.0,None),
    ("RS55_MIN_10",1.2,10.0,0.0,None),
    ("BREAKOUT_0_5PCT",1.2,0.0,0.005,None),
    ("BREAKOUT_1PCT",1.2,0.0,0.01,None),
    ("VOL_1_5_RS55_5",1.5,5.0,0.0,None),
    ("SPY_ABOVE_SMA200",1.2,0.0,0.0,"SMA200"),
]

def simulate(version,frames,spy,universe,mode,volume_min,rs_min,breakout_min,market_filter):
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
                if held==2 and 100*(cl/p["entry_px"]-1)<=-2:
                    p["early_due"]=True
                kept.append(p)
        positions=kept
        candidates=[]
        if version=="V1" and (market_filter is None or (len(spy.loc[:day])>=200 and float(spy.loc[:day,"close"].iloc[-1])>float(spy.loc[:day,"close"].rolling(200).mean().iloc[-1]))):
            for sym,f in signals.items():
                if day not in f.index:continue
                try:
                    r=f.loc[day]
                    cl,rs,st,week,vol=[float(r[k]) for k in ("close","rs_55","supertrend_value","previous_week_high","volume_ratio")]
                    if all(math.isfinite(x) for x in (cl,rs,st,week,vol)) and rs>rs_min and cl>st and cl>week*(1+breakout_min) and vol>=volume_min:
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
    _,spy,error=download("SPY")
    if spy is None:
        raise RuntimeError("SPY unavailable: "+error)
    universe=get_universe()
    frames={};failures={}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        for i,(sym,df,error) in enumerate(pool.map(download,universe),1):
            if df is not None and len(df)>=80:
                frames[sym]=df
            else:
                failures[sym]=error or "insufficient history"
            if i%50==0:
                print(f"downloaded {i}/{len(universe)}",flush=True)
    if len(set(universe)&frames.keys())<.85*len(universe):
        raise RuntimeError("S&P 500 data coverage below 85%")
    results=[]
    for label,vol,rs,breakout,market_filter in VARIANTS:
        result=simulate("V1",frames,spy,universe,label,vol,rs,breakout,market_filter)
        results.append(result)
        print(label,result["net_return_pct"],flush=True)
    baseline=results[0]
    for row in results:
        row["vs_current_pp"]=round(row["net_return_pct"]-baseline["net_return_pct"],2)
    output={"period":f"{START} to {END}","strategy":"V1-D2-LOSS2",
            "current_variant":baseline,"variants":VARIANTS,"results":results,
            "missing_symbols":failures,
            "cautions":["Same S&P500 constituent list and adjusted Yahoo OHLC",
              "Signal close to next open; D2 -2% next-open exit; 5% stop and 10-bar time exit",
              "0.15% fee per side; no additional slippage",
              "Same six-month sample used to select candidate variants: in-sample optimization",
              "Survivorship bias and intraday stop sequencing uncertainty",
              "Forward-only strategy code and paper ledger are not modified"]}
    OUT.mkdir(parents=True,exist_ok=True)
    (OUT/"entry_filter_comparison.json").write_text(json.dumps(output,indent=2,allow_nan=False))
    lines=["# V1-D2-LOSS2 entry-filter comparison","",
           f"Period {START} to {END}; $10,000 starting equity; exits unchanged.","",
           "| Entry filter | Net % | vs current pp | Equity $ | Trades | Early exits | Win % | PF | Max DD % |",
           "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for row in results:
        lines.append(f'| {row["mode"]} | {row["net_return_pct"]} | {row["vs_current_pp"]} | {row["equity_usd"]} | {row["closed_trades"]} | {row["early_exits"]} | {row["win_rate_pct"]} | {row["profit_factor"]} | {row["max_drawdown_pct"]} |')
    lines+=["","Filters: CURRENT = RS55 > 0, Supertrend and previous-week-high breakout, volume >=1.2. VOL changes volume minimum; RS55_MIN changes RS55 minimum; BREAKOUT changes percentage above previous-week-high; SPY_ABOVE_SMA200 adds market regime. Score unchanged.","",
      "Every variant: second-day close <= -2% from entry schedules next-session open exit, 5% stop, 10-bar time exit, 0.15% fee each side.","",
      "This is in-sample exploratory research. The best result is not validated for future trading. No changes to the active forward paper bot.",""]
    report="\\n".join(lines)
    (OUT/"entry_filter_comparison.md").write_text(report)
    print(report)
    import os
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"],"a") as f:
            f.write(report+"\\n")

if __name__=="__main__":
    main()
