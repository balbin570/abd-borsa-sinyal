"""Analyze whether V1/V2 trades were profitable before their actual exits. Read-only."""
import json, math
from pathlib import Path
import pandas as pd
import yfinance as yf

ROOT=Path("research/us_top5_6m")
source=json.loads((ROOT/"result.json").read_text())
results=[]
for model in source["results"]:
    trades=model["trades"]
    symbols=sorted({t["symbol"] for t in trades})
    bars={}
    for sym in symbols:
        try:
            df=yf.Ticker(sym).history(start="2026-05-20",end="2026-11-22",auto_adjust=True)
            df.index=pd.to_datetime(df.index).tz_localize(None).normalize()
            df.columns=[str(x).lower() for x in df.columns]
            bars[sym]=df
        except Exception as e: print("MISSING",sym,str(e))
    enriched=[]
    for t in trades:
        df=bars.get(t["symbol"])
        if df is None:continue
        entry=pd.Timestamp(t["entry"]); exit=pd.Timestamp(t["exit"])
        period=df.loc[entry:exit]
        if len(period)==0 or entry not in period.index:continue
        entry_px=float(period.loc[entry,"open"])
        if not math.isfinite(entry_px) or entry_px<=0:continue
        # Gross MFE/MAE are upper/lower bounds based on daily OHLC, not executable fills.
        # Stop exits are censored at their actual exit day; same-bar high/low order unknown.
        mfe=100*(float(period["high"].max())/entry_px-1)
        mae=100*(float(period["low"].min())/entry_px-1)
        first=period.iloc[0]
        first_close=100*(float(first["close"])/entry_px-1)
        first_high=100*(float(first["high"])/entry_px-1)
        first3=period.iloc[:min(3,len(period))]
        max3=100*(float(first3["high"].max())/entry_px-1)
        enriched.append({**t,"mfe_pct":round(mfe,3),"mae_pct":round(mae,3),"first_day_close_pct":round(first_close,3),"first_day_high_pct":round(first_high,3),"first_3_bars_max_pct":round(max3,3),"bars":len(period)})
    losers=[t for t in enriched if t["pnl"]<0]
    def pct(items,pred):return round(100*sum(pred(x) for x in items)/len(items),2) if items else None
    def mean(items,key):return round(sum(x[key] for x in items)/len(items),3) if items else None
    metrics={"strategy":model["version"],"analyzed_trades":len(enriched),"missing_trades":len(trades)-len(enriched),
       "first_day_close_positive_pct":pct(enriched,lambda t:t["first_day_close_pct"]>0),
       "first_day_high_above_entry_pct":pct(enriched,lambda t:t["first_day_high_pct"]>0),
       "first_3_bars_high_ge_1pct_pct":pct(enriched,lambda t:t["first_3_bars_max_pct"]>=1),
       "first_3_bars_high_ge_2pct_pct":pct(enriched,lambda t:t["first_3_bars_max_pct"]>=2),
       "losing_trades_count":len(losers),
       "losers_ever_high_ge_1pct_pct":pct(losers,lambda t:t["mfe_pct"]>=1),
       "losers_ever_high_ge_2pct_pct":pct(losers,lambda t:t["mfe_pct"]>=2),
       "losers_ever_high_ge_3pct_pct":pct(losers,lambda t:t["mfe_pct"]>=3),
       "mean_mfe_pct":mean(enriched,"mfe_pct"),
       "mean_mae_pct":mean(enriched,"mae_pct"),
       "mean_holding_bars":mean(enriched,"bars"),
       "stop_exits":sum(t["reason"]=="STOP" for t in enriched),
       "time_exits":sum(t["reason"]=="TIME10" for t in enriched),
       "trades":enriched}
    results.append(metrics)
out={"method":"daily OHLC upper/lower bounds, no intraday event ordering; first 3 bars includes entry day","models":results}
(ROOT/"exit_diagnostics.json").write_text(json.dumps(out,indent=2,allow_nan=False))
lines=["# V1 / V2 entry and exit diagnostics","",
"Note: MFE is highest observed daily HIGH before exit, not a realizable profit; stop-day high may occur after the stop and cannot establish profit-first sequencing.","",
"| Metric | V1 | V2 |","|---|---:|---:|"]
fields=[("analyzed_trades","Analyzed closed trades"),("first_day_close_positive_pct","First-day close positive %"),("first_day_high_above_entry_pct","First-day high above entry %"),("first_3_bars_high_ge_1pct_pct","First 3 bars high >= +1% %"),("first_3_bars_high_ge_2pct_pct","First 3 bars high >= +2% %"),("losing_trades_count","Losing trades"),("losers_ever_high_ge_1pct_pct","Losing trades with observed high >= +1% %"),("losers_ever_high_ge_2pct_pct","Losing trades with observed high >= +2% %"),("losers_ever_high_ge_3pct_pct","Losing trades with observed high >= +3% %"),("mean_mfe_pct","Mean observed MFE %"),("mean_mae_pct","Mean observed MAE %"),("mean_holding_bars","Mean holding bars"),("stop_exits","Stop exits"),("time_exits","Time exits")]
for key,label in fields:lines.append(f"| {label} | {results[0][key]} | {results[1][key]} |")
lines+=["","These diagnostics do not imply that a take-profit or trailing-stop strategy would have achieved those highs. Any exit change needs a separate causal replay with transaction costs and fill assumptions."]
report="\n".join(lines)
(ROOT/"exit_diagnostics.md").write_text(report)
print(report)
