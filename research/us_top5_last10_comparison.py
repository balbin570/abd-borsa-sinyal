"""Compare original V1-D2-LOSS2 vs B125_SPY_MOM20_POS for the last 10 SPY sessions.
Research replay starts with equal fresh cash at first session, not existing live ledger.
"""
import concurrent.futures
import json
from pathlib import Path
import pandas as pd
from research.us_top5_6m_backtest import download, get_universe, v1_signals
from research.us_top5_entry_refinement import simulate
from research.us_top5_entry_regime_quality import spy_allowed

OUT = Path("research/us_top5_6m")

def main():
    _, spy, err = download("SPY")
    if spy is None:
        raise RuntimeError("SPY unavailable: " + err)
    # Historical data downloader is pinned to 2026-11-20 in the six-month research.
    # Select last 10 completed SPY sessions available in that dataset.
    days = spy.index[spy.index.date <= pd.Timestamp("2026-11-20").date()][-10:]
    if len(days) != 10:
        raise RuntimeError("Not enough SPY sessions")
    start, end = days[0].date(), days[-1].date()
    universe = get_universe()
    frames = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        for sym, df, error in pool.map(download, universe):
            if df is not None and len(df) >= 80:
                frames[sym] = df
    if len(frames) < .85 * len(universe):
        raise RuntimeError("Insufficient S&P500 coverage")
    raw = {}
    for sym in universe:
        if sym in frames:
            sf = v1_signals(frames[sym], spy)
            if sf is not None:
                raw[sym] = sf
    mom20 = spy_allowed(spy, "SPY_MOM20_POS")
    filtered = {sym: sf.loc[mom20.reindex(sf.index).fillna(False)]
                for sym, sf in raw.items()}
    current = simulate("V1", frames, spy, universe, "V1-D2-LOSS2_ORIGINAL",
                       1.2, 0.0, 0.0, None, start, end, raw)
    candidate = simulate("V1", frames, spy, universe, "V1-D2-LOSS2_B125_SPY_MOM20_POS",
                         1.2, 0.0, .0125, None, start, end, filtered)
    payload = {
        "period_start": str(start), "period_end": str(end),
        "sessions": [str(d.date()) for d in days],
        "original": current, "candidate": candidate,
        "delta_return_pp": round(candidate["net_return_pct"]-current["net_return_pct"],2),
        "methodology": "Two independent $10k portfolios reset on the first of 10 trading sessions; same D2, stop, time and fees. Signal at close, fill at next open. End equity marks open holdings to market.",
        "limitations": [
            "Not the actual last 10-day performance of the existing forward ledger.",
            "Fresh portfolio resets omit positions already open at the beginning of the period.",
            "Research universe uses present-day S&P500 membership (survivorship bias).",
            "Only 10 sessions; highly noisy and selected after inspecting six-month backtests.",
            "Historical Yahoo prices pinned to 2026-11-20; rerun with a rolling downloader to update later.",
        ]
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT/"last10_comparison.json").write_text(json.dumps(payload, indent=2, allow_nan=False))
    lines = [
        "# Last 10 trading sessions: original vs B125_SPY_MOM20_POS",
        "",
        f"Period: {start} to {end}; each model starts with $10,000 cash; no inherited positions.",
        "",
        "| Model | Net % | Equity $ | Closed trades | Win % | PF | Max DD % |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for r in (current, candidate):
        lines.append(f'| {r["mode"]} | {r["net_return_pct"]} | {r["equity_usd"]} | {r["closed_trades"]} | {r["win_rate_pct"]} | {r["profit_factor"]} | {r["max_drawdown_pct"]} |')
    lines += ["", f'Candidate minus original: {payload["delta_return_pp"]:+.2f} percentage points.',
              "", "Research replay, not the live forward ledger. No live files modified.", ""]
    report = "\n".join(lines)
    (OUT/"last10_comparison.md").write_text(report)
    print(report)
    import os
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"],"a") as f:
            f.write(report+"\n")

if __name__=="__main__":
    main()
