"""Read-only comparison of exit rules. Same historical V1/V2 entries, no live state writes.
Conservative OHLC assumptions: open gap fills first; stop before target on ambiguous bar;
a newly activated trailing/breakeven stop cannot fill earlier in that same bar.
"""
import concurrent.futures, json, math
from pathlib import Path
import pandas as pd
from research.us_top5_6m_backtest import START, END, INITIAL, FEE, download, v1_signals, get_universe, SYMBOLS, indicators

OUT=Path("research/us_top5_6m")
MODES=("A_BASELINE","B_TP2","C_BE2","D_TRAIL2","E_HALF2_TRAIL")
def replay(version,mode,frames,spy,universe):
    days=[d for d in spy.index if START<=d.date()<=END]
    signals={}
    if version=="V1":
        for sym in universe:
            if sym in frames:
                f=v1_signals(frames[sym],spy)
                if f is not None:signals[sym]=f
    cash=INITIAL;positions=[];pending=[];closed=[];daily=[]
    for i,day in enumerate(days):
        prev=days[i-1] if i else None
        queued=pending;pending=[]
        for sig in queued:
            sym=sig["symbol"];df=frames.get(sym)
            if prev is None or sig["day"]!=str(prev.date()) or df is None or day not in df.index or len(positions)>=5:continue
            px=float(df.at[day,"open"])
            if not math.isfinite(px) or px<=0:continue
            qty=math.floor(min(cash,INITIAL*.2)/(px*(1+FEE)))
            if qty<1:continue
            cost=qty*px*(1+FEE);cash-=cost
            positions.append({"symbol":sym,"entry":str(day.date()),"entry_px":px,"qty":qty,"cost":cost,
              "stop":px*.95 if version=="V1" else px-2.5*sig["atr"],"high_water":px,"armed":False,"realized":0.0})
        kept=[]
        for p in positions:
            df=frames[p["symbol"]]
            if day not in df.index:kept.append(p);continue
            bar=df.loc[day];op,hi,lo,cl=[float(bar[k]) for k in ("open","high","low","close")]
            if not all(math.isfinite(x) and x>0 for x in (op,hi,lo,cl)):raise ValueError("invalid OHLC "+p["symbol"])
            held=len(df.loc[pd.Timestamp(p["entry"]):day])
            base_stop=p["stop"];entry=p["entry_px"];qty=p["qty"]
            # Only prior-bar high can activate protective stop; no look-ahead.
            protective=base_stop
            if mode=="C_BE2" and p["armed"]:protective=max(protective,entry)
            if mode in ("D_TRAIL2","E_HALF2_TRAIL") and p["armed"]:
                protective=max(protective,p["high_water"]*.985)
            # Gap-down stop takes priority, then intrabar stop; target only if stop not hit.
            exit_px=None;reason=None;partial=False
            if op<=protective:exit_px=op;reason="STOP_GAP"
            elif lo<=protective:exit_px=protective;reason="STOP"
            elif mode=="B_TP2" and hi>=entry*1.02:
                exit_px=max(op,entry*1.02);reason="TP2"
            elif mode=="E_HALF2_TRAIL" and not p["armed"] and hi>=entry*1.02:
                exit_px=max(op,entry*1.02);reason="HALF_TP2" if qty>=2 else "TP2_SINGLE_SHARE";partial=(qty>=2)
            elif held>=10:exit_px=cl;reason="TIME10"
            if exit_px is not None:
                sell_qty=qty//2 if partial else qty\n                if qty<=0 or sell_qty<=0:raise RuntimeError("Invalid zero-share position in exit replay")
                proceeds=sell_qty*exit_px*(1-FEE);cash+=proceeds
                alloc_cost=p["cost"]*sell_qty/qty
                pnl=proceeds-alloc_cost
                if partial:
                    p["qty"]-=sell_qty;p["cost"]-=alloc_cost;p["realized"]+=pnl
                    p["armed"]=True;p["high_water"]=max(p["high_water"],hi)
                    kept.append(p)
                else:
                    closed.append({"symbol":p["symbol"],"entry":p["entry"],"exit":str(day.date()),
                      "pnl":round(pnl+p["realized"],2),"reason":reason})
            else:
                # Activation after exit checks; stop applies starting next session.
                if mode in ("C_BE2","D_TRAIL2","E_HALF2_TRAIL") and hi>=entry*1.02:p["armed"]=True
                p["high_water"]=max(p["high_water"],hi)
                kept.append(p)
        positions=kept
        candidates=[]
        if version=="V1":
            for sym,f in signals.items():
                if day not in f.index:continue
                try:
                    r=f.loc[day];close,rs,st,week,vol=[float(r[k]) for k in ("close","rs_55","supertrend_value","previous_week_high","volume_ratio")]
                    if all(math.isfinite(x) for x in (close,rs,st,week,vol)) and rs>0 and close>st and close>week and vol>=1.2:
                        candidates.append({"symbol":sym,"day":str(day.date()),"score":rs+2*min(vol,4)})
                except (KeyError,ValueError,TypeError):pass
        else:
            sp=spy.loc[:day,"close"]
            if len(sp)>=200 and float(sp.iloc[-1])>float(sp.rolling(200).mean().iloc[-1]):
                for sym in universe:
                    df=frames.get(sym)
                    if df is None or day not in df.index:continue
                    x=indicators(df,day)
                    if x and x["close"]>x["ma50"]>x["ma200"] and x["r63"]>0 and x["volume_ratio"]>=1 and x["atr"]/x["close"]<.07:
                        candidates.append({"symbol":sym,"day":str(day.date()),"score":x["score"],"atr":x["atr"]})
        candidates.sort(key=lambda x:(-x["score"],x["symbol"]))
        reserved={p["symbol"] for p in positions}
        for c in candidates:
            if len(pending)+len(positions)>=5:break
            if c["symbol"] not in reserved:pending.append(c);reserved.add(c["symbol"])
        equity=cash
        for p in positions:
            if day not in frames[p["symbol"]].index:raise ValueError("missing held price")
            equity+=p["qty"]*float(frames[p["symbol"]].at[day,"close"])
        daily.append(round(equity,2))
    peak=INITIAL;max_dd=0
    for val in daily:
        peak=max(peak,val);max_dd=min(max_dd,100*(val/peak-1))
    winners=[x for x in closed if x["pnl"]>0]
    gain=sum(x["pnl"] for x in closed if x["pnl"]>0)
    loss=-sum(x["pnl"] for x in closed if x["pnl"]<0)
    return {"version":version,"mode":mode,"net_return_pct":round(100*(daily[-1]/INITIAL-1),2),
      "equity_usd":daily[-1],"closed_trades":len(closed),
      "win_rate_pct":round(100*len(winners)/len(closed),2) if closed else None,
      "profit_factor":round(gain/loss,3) if loss else None,
      "max_drawdown_pct":round(max_dd,2),"open_positions":len(positions),
      "trades":closed}

def main():
    _,spy,error=download("SPY")
    if spy is None:raise RuntimeError("SPY missing "+error)
    v1=get_universe();v2=list(SYMBOLS)
    symbols=sorted(set(v1)|set(v2));frames={}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        for i,(sym,df,err) in enumerate(pool.map(download,symbols),1):
            if df is not None and len(df)>=80:frames[sym]=df
            if i%50==0:print(f"downloaded {i}/{len(symbols)}",flush=True)
    if len(set(v1)&frames.keys())<.85*len(v1) or len(set(v2)&frames.keys())<.85*len(v2):
        raise RuntimeError("Coverage <85%, no report")
    results=[]
    for version,universe in (("V1",v1),("V2",v2)):
        for mode in MODES:
            result=replay(version,mode,frames,spy,universe)
            print(version,mode,result["net_return_pct"],flush=True)
            results.append(result)
    summary={"period":f"{START} to {END}","modes":list(MODES),"results":results,
      "cautions":["Daily OHLC cannot establish intraday ordering; stop takes priority on ambiguous bars",
      "Protective stop activates next trading session; conservative against look-ahead",
      "Historical universe uses current S&P 500 membership (survivorship bias)",
      "Each strategy reruns entries after exits free capacity; trade sets differ",
      "Fees 0.15% per side, no extra slippage or market impact",
      "Exploratory same-sample optimization; out-of-sample validation required"]}
    OUT.mkdir(parents=True,exist_ok=True)
    (OUT/"exit_comparison.json").write_text(json.dumps(summary,indent=2,allow_nan=False))
    lines=["# US TOP5 exit-rule comparison","",f"Period {START} to {END}; $10,000 per strategy; paper research only.","",
      "| Strategy | Exit | Net return % | Equity $ | Trades | Win % | PF | Max DD % |",
      "|---|---|---:|---:|---:|---:|---:|---:|"]
    for r in results:
        lines.append(f'| {r["version"]} | {r["mode"]} | {r["net_return_pct"]} | {r["equity_usd"]} | {r["closed_trades"]} | {r["win_rate_pct"]} | {r["profit_factor"]} | {r["max_drawdown_pct"]} |')
    lines+=["","Rules: A original exits; B +2% full take-profit; C +2% activates breakeven stop next bar; D +2% activates 1.5%-below-high trailing stop next bar; E half sold at +2%, remaining 1.5% trailing after activation.","",
      "Conservative: stop wins same-bar ambiguity, gaps filled at open, next-bar activation of protective stops. Exit rules change capacity and therefore subsequent entries. This is in-sample exploratory research, not a validated return forecast."]
    report="\n".join(lines)
    (OUT/"exit_comparison.md").write_text(report)
    print(report)
    import os
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"],"a") as f:f.write(report+"\n")
if __name__=="__main__":main()
