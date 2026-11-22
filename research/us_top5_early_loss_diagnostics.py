"""Read-only first-three-session behavior of completed US TOP5 V1/V2 trades.

This is diagnostic association, not a causal early-exit backtest. A trade's eventual
outcome is used only for grouping after the fact; no forward strategy is changed.
"""
import json, math
from collections import Counter
from pathlib import Path
import pandas as pd
import yfinance as yf

OUT=Path("research/us_top5_6m")
def fetch(sym):
    d=yf.Ticker(sym).history(start="2026-05-20",end="2026-11-22",auto_adjust=True)
    if d.empty:raise ValueError("empty")
    d.index=pd.to_datetime(d.index).tz_localize(None).normalize()
    d.columns=[str(x).lower() for x in d.columns]
    return d

def avg(items,key):
    vals=[x[key] for x in items if x.get(key) is not None]
    return round(sum(vals)/len(vals),3) if vals else None

def percent(items,fn):
    return round(100*sum(bool(fn(x)) for x in items)/len(items),2) if items else None

def main():
    source=json.loads((OUT/"result.json").read_text())
    symbols=sorted({t["symbol"] for m in source["results"] for t in m["trades"]})
    prices={};errors={}
    for sym in symbols:
        try:prices[sym]=fetch(sym)
        except Exception as exc:errors[sym]=str(exc)[:120]
    models=[]
    for model in source["results"]:
        rows=[]
        for trade in model["trades"]:
            sym=trade["symbol"]
            if sym not in prices:continue
            d=prices[sym]
            start=pd.Timestamp(trade["entry"]);end=pd.Timestamp(trade["exit"])
            if start not in d.index or end not in d.index:continue
            period=d.loc[start:end]
            if period.empty:continue
            entry=float(period.iloc[0]["open"])
            if not math.isfinite(entry) or entry<=0:continue
            record={"symbol":sym,"entry":trade["entry"],"exit":trade["exit"],
              "reason":trade["reason"],"net_pct":trade["net_pct"],"winner":trade["pnl"]>0,
              "held_bars":len(period)}
            for k in (1,2,3):
                # Only observed bars up to actual exit, never beyond it.
                if len(period)>=k:
                    b=period.iloc[k-1]
                    record[f"day{k}_close_pct"]=round(100*(float(b["close"])/entry-1),3)
                    record[f"day{k}_low_pct"]=round(100*(float(b["low"])/entry-1),3)
                    record[f"day{k}_high_pct"]=round(100*(float(b["high"])/entry-1),3)
                else:
                    for field in ("close","low","high"):record[f"day{k}_{field}_pct"]=None
            rows.append(record)
        winners=[x for x in rows if x["winner"]]
        losers=[x for x in rows if not x["winner"]]
        stops=[x for x in rows if x["reason"]=="STOP"]
        timeouts=[x for x in rows if x["reason"]=="TIME10"]
        groups={"all":rows,"eventual_winners":winners,"eventual_losers":losers,
          "stop_exits":stops,"time_exits":timeouts}
        metrics={}
        for label,group in groups.items():
            metric={"count":len(group),"mean_net_pct":avg(group,"net_pct"),
              "mean_holding_bars":avg(group,"held_bars")}
            for k in (1,2,3):
                eligible=[x for x in group if x[f"day{k}_close_pct"] is not None]
                metric[f"day{k}_eligible"]=len(eligible)
                metric[f"day{k}_close_positive_pct"]=percent(eligible,lambda x:x[f"day{k}_close_pct"]>0)
                metric[f"day{k}_close_below_minus1_pct"]=percent(eligible,lambda x:x[f"day{k}_close_pct"]<=-1)
                metric[f"day{k}_close_below_minus2_pct"]=percent(eligible,lambda x:x[f"day{k}_close_pct"]<=-2)
                metric[f"day{k}_mean_close_pct"]=avg(eligible,f"day{k}_close_pct")
            metrics[label]=metric
        # Contingency: an observable day-1 loss threshold vs eventual outcome.
        # These are retrospective frequencies, not probabilities for future trades.
        contingency={}
        for k in (1,2,3):
            eligible=[x for x in rows if x[f"day{k}_close_pct"] is not None]
            for threshold in (-1,-2):
                bad=[x for x in eligible if x[f"day{k}_close_pct"]<=threshold]
                good=[x for x in eligible if x[f"day{k}_close_pct"]>threshold]
                contingency[f"day{k}_close_le_{threshold}"]={
                  "flagged_count":len(bad),
                  "flagged_eventual_loser_pct":percent(bad,lambda x:not x["winner"]),
                  "flagged_eventual_winner_count":sum(x["winner"] for x in bad),
                  "unflagged_count":len(good),
                  "unflagged_eventual_loser_pct":percent(good,lambda x:not x["winner"])}
        models.append({"version":model["version"],"trades_analyzed":len(rows),
          "trades_missing":len(model["trades"])-len(rows),
          "groups":metrics,"contingency":contingency,"trades":rows})
    result={"period":source["period"],"models":models,"missing_symbols":errors,
      "limitations":["Grouping by eventual outcome is retrospective, not predictive",
        "Only observed daily bars until actual exit; day 2/3 samples exclude earlier exits",
        "Early-exit signals evaluated on close; cannot assume same-close execution",
        "Prices adjusted; daily OHLC do not establish intraday event order",
        "No replacement-trade, transaction-cost, or portfolio replay for early exits",
        "Current constituent universe creates survivorship bias"]}
    (OUT/"early_loss_diagnostics.json").write_text(json.dumps(result,indent=2,allow_nan=False))
    lines=["# US TOP5: first 1-3 sessions and eventual exits","",
      "Retrospective diagnostics only. No changes to V1/V2 forward bots or live paper ledgers.","",
      "| Strategy | Outcome | N | Day 1 close > 0 % | Day 1 <= -1% % | Day 2 <= -1% % | Day 3 <= -1% % | Mean net % |",
      "|---|---|---:|---:|---:|---:|---:|---:|"]
    for m in models:
        for label in ("eventual_winners","eventual_losers","stop_exits","time_exits"):
            g=m["groups"][label]
            lines.append(f'| {m["version"]} | {label} | {g["count"]} | {g["day1_close_positive_pct"]} | {g["day1_close_below_minus1_pct"]} | {g["day2_close_below_minus1_pct"]} | {g["day3_close_below_minus1_pct"]} | {g["mean_net_pct"]} |')
    lines+=["","## Threshold diagnostics (eventual outcome, not an exit simulation)","",
      "| Strategy | Signal | Flagged N | Eventually lost % | Winners falsely flagged | Unflagged N | Unflagged eventually lost % |",
      "|---|---|---:|---:|---:|---:|---:|"]
    for m in models:
        for key,c in m["contingency"].items():
            lines.append(f'| {m["version"]} | {key} | {c["flagged_count"]} | {c["flagged_eventual_loser_pct"]} | {c["flagged_eventual_winner_count"]} | {c["unflagged_count"]} | {c["unflagged_eventual_loser_pct"]} |')
    lines+=["","Caution: Day 2/3 percentages are conditional on the trade remaining open. These figures cannot establish improved returns from an early-exit rule. Next step would be a separate next-open portfolio replay, with transaction costs and fresh entries, followed by out-of-sample validation.",""]
    report="\n".join(lines)
    (OUT/"early_loss_diagnostics.md").write_text(report)
    print(report)
    import os
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"],"a") as f:f.write(report)

if __name__=="__main__":main()
