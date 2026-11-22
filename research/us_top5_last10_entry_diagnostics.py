"""Read-only day-by-day entry funnel for the 2026-11-09..20 comparison."""
import concurrent.futures
import json
import math
from pathlib import Path
import pandas as pd
from research.us_top5_6m_backtest import download, get_universe, v1_signals
from research.us_top5_entry_regime_quality import spy_allowed

OUT = Path("research/us_top5_6m")
START = pd.Timestamp("2026-11-09")
END = pd.Timestamp("2026-11-20")

def main():
    _, spy, err = download("SPY")
    if spy is None:
        raise RuntimeError("SPY unavailable: " + err)
    days = spy.loc[START:END].index
    if len(days) != 10:
        raise RuntimeError(f"Expected 10 sessions, got {len(days)}")
    universe = get_universe()
    frames = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        for sym, df, error in pool.map(download, universe):
            if df is not None and len(df) >= 80:
                frames[sym] = df
    if len(frames) < .85 * len(universe):
        raise RuntimeError("Insufficient data coverage")
    signals = {}
    for sym in universe:
        if sym in frames:
            sf = v1_signals(frames[sym], spy)
            if sf is not None:
                signals[sym] = sf
    momentum = spy_allowed(spy, "SPY_MOM20_POS")
    records = []
    for day in days:
        original = []
        breakout = []
        for sym, sf in signals.items():
            if day not in sf.index:
                continue
            r = sf.loc[day]
            try:
                close, rs, st, week, vol = [float(r[k]) for k in ("close", "rs_55", "supertrend_value", "previous_week_high", "volume_ratio")]
            except (ValueError, KeyError, TypeError):
                continue
            if not all(math.isfinite(v) for v in (close, rs, st, week, vol)):
                continue
            if rs > 0 and close > st and close > week and vol >= 1.2:
                original.append(sym)
                if close > week * 1.0125:
                    breakout.append(sym)
        spy_close = float(spy.loc[day, "close"])
        spy_prior20 = float(spy["close"].loc[:day].iloc[-21])
        market_ok = bool(momentum.loc[day])
        records.append({
            "date": str(day.date()),
            "spy_close": round(spy_close, 4),
            "spy_close_20_sessions_ago": round(spy_prior20, 4),
            "spy_momentum20_pct": round(100*(spy_close/spy_prior20-1), 3),
            "spy_mom20_positive": market_ok,
            "original_qualifying": len(original),
            "b125_before_spy_filter": len(breakout),
            "b125_after_spy_filter": len(breakout) if market_ok else 0,
            "original_symbols": original[:30],
            "b125_symbols_before_spy_filter": breakout[:30],
        })
    totals = {
        "days_spy_momentum_positive": sum(x["spy_mom20_positive"] for x in records),
        "original_signal_days": sum(x["original_qualifying"] > 0 for x in records),
        "b125_signal_days_before_spy": sum(x["b125_before_spy_filter"] > 0 for x in records),
        "b125_signal_days_after_spy": sum(x["b125_after_spy_filter"] > 0 for x in records),
        "original_total_qualifying": sum(x["original_qualifying"] for x in records),
        "b125_total_before_spy": sum(x["b125_before_spy_filter"] for x in records),
        "b125_total_after_spy": sum(x["b125_after_spy_filter"] for x in records),
    }
    payload = {
        "period": "2026-11-09 to 2026-11-20",
        "totals": totals,
        "days": records,
        "notes": [
            "Signal counts are before portfolio capacity, ranking, pending, and next-open execution.",
            "Daily counts are not trade counts; no look-ahead beyond each signal-day close.",
            "Only the 10 historical sessions; not a fresh forward performance evaluation.",
            "Active bot, workflows, and state remain unchanged.",
        ],
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT/"last10_entry_diagnostics.json").write_text(json.dumps(payload, indent=2, allow_nan=False))
    lines = [
        "# Last 10 trading days: why B125_SPY_MOM20_POS did or did not signal",
        "",
        "| Date | SPY MOM20 % | SPY pass? | Original signals | B125 before SPY | B125 after SPY |",
        "|---|---:|---|---:|---:|---:|",
    ]
    for r in records:
        lines.append(f'| {r["date"]} | {r["spy_momentum20_pct"]:+.3f} | {"YES" if r["spy_mom20_positive"] else "NO"} | {r["original_qualifying"]} | {r["b125_before_spy_filter"]} | {r["b125_after_spy_filter"]} |')
    lines += ["", "## Totals", ""]
    for k, v in totals.items():
        lines.append(f"- {k}: {v}")
    lines += ["", "Research only. Counts are qualifying close signals, not executed orders.", ""]
    report = "\n".join(lines)
    (OUT/"last10_entry_diagnostics.md").write_text(report)
    print(report)
    import os
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(report + "\n")

if __name__ == "__main__":
    main()
