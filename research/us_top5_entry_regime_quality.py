"""Research-only: market regime and entry-quality filters on V1-D2-LOSS2.
The existing forward paper portfolio and exit engine are unchanged.
Signals use only information available at the signal-day close.
"""
import concurrent.futures
import json
import math
from pathlib import Path
import pandas as pd
from research.us_top5_6m_backtest import START, END, download, get_universe, v1_signals
from research.us_top5_entry_refinement import simulate, PERIODS

OUT = Path("research/us_top5_6m")
# Regime tests are deliberately simple and causal. A blocked signal does not liquidate holdings.
VARIANTS = [
    ("CURRENT", 0.0, "NONE", "NONE"),
    ("BREAKOUT_1_25", 0.0125, "NONE", "NONE"),
    ("B125_SPY_SMA50", 0.0125, "SPY_SMA50", "NONE"),
    ("B125_SPY_SMA100", 0.0125, "SPY_SMA100", "NONE"),
    ("B125_SPY_SMA200", 0.0125, "SPY_SMA200", "NONE"),
    ("B125_SPY_MOM20_POS", 0.0125, "SPY_MOM20_POS", "NONE"),
    ("B125_STOCK_MOM20_POS", 0.0125, "NONE", "STOCK_MOM20_POS"),
    ("B125_STOCK_RSI14_50", 0.0125, "NONE", "STOCK_RSI14_50"),
    ("B125_RS55_MIN5", 0.0125, "NONE", "RS55_MIN5"),
    ("B125_SPY_SMA50_RSI50", 0.0125, "SPY_SMA50", "STOCK_RSI14_50"),
]

def spy_allowed(spy, regime):
    cl = spy["close"]
    if regime == "NONE":
        return pd.Series(True, index=spy.index)
    if regime.startswith("SPY_SMA"):
        n = int(regime.replace("SPY_SMA", ""))
        return (cl > cl.rolling(n, min_periods=n).mean()).fillna(False)
    if regime == "SPY_MOM20_POS":
        return (cl.pct_change(20) > 0).fillna(False)
    raise ValueError(regime)

def stock_allowed(df, signal, quality):
    if quality == "NONE":
        return pd.Series(True, index=signal.index)
    if quality == "RS55_MIN5":
        return (signal["rs_55"] > 5).fillna(False)
    cl = df["close"]
    if quality == "STOCK_MOM20_POS":
        return (cl.pct_change(20) > 0).reindex(signal.index).fillna(False)
    if quality == "STOCK_RSI14_50":
        delta = cl.diff()
        gain = delta.clip(lower=0).ewm(alpha=1/14, adjust=False, min_periods=14).mean()
        loss = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False, min_periods=14).mean()
        rs = gain / loss.replace(0, float("nan"))
        rsi = 100 - 100 / (1 + rs)
        # A zero-loss rising series has RSI=100; flat series does not pass.
        rsi = rsi.mask((loss == 0) & (gain > 0), 100)
        return (rsi >= 50).reindex(signal.index).fillna(False)
    raise ValueError(quality)

def main():
    _, spy, error = download("SPY")
    if spy is None:
        raise RuntimeError("SPY unavailable: " + error)
    universe = get_universe()
    frames, failures = {}, {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        for i, (sym, df, error) in enumerate(pool.map(download, universe), 1):
            if df is not None and len(df) >= 80:
                frames[sym] = df
            else:
                failures[sym] = error or "insufficient history"
            if i % 50 == 0:
                print(f"Downloaded {i}/{len(universe)}", flush=True)
    if len(frames) < .85 * len(universe):
        raise RuntimeError("Insufficient data coverage")
    base_signals = {}
    for sym in universe:
        if sym in frames:
            sf = v1_signals(frames[sym], spy)
            if sf is not None:
                base_signals[sym] = sf
    results = []
    for label, breakout, regime, quality in VARIANTS:
        market = spy_allowed(spy, regime)
        filtered = {}
        for sym, signal in base_signals.items():
            stock = stock_allowed(frames[sym], signal, quality)
            allow = market.reindex(signal.index).fillna(False) & stock
            filtered[sym] = signal.loc[allow]
        for period, start, end in PERIODS:
            # Filtering signal rows leaves the same cash, entry ranking and exits.
            row = simulate("V1", frames, spy, universe, label, 1.2, 0.0,
                           breakout, None, start, end, filtered)
            row["period"] = period
            results.append(row)
            print(label, period, row["net_return_pct"], flush=True)
    for period, _, _ in PERIODS:
        baseline = next(r for r in results if r["period"] == period and r["mode"] == "CURRENT")
        for r in results:
            if r["period"] == period:
                r["vs_current_pp"] = round(r["net_return_pct"] - baseline["net_return_pct"], 2)
    OUT.mkdir(parents=True, exist_ok=True)
    payload = {
        "strategy": "V1-D2-LOSS2",
        "variants": VARIANTS,
        "periods": [(n, str(s), str(e)) for n, s, e in PERIODS],
        "results": results,
        "unavailable_symbols": failures,
        "cautions": [
            "Regime filters gate only new entries at close; existing holdings are not force-liquidated.",
            "All windows reset to $10,000 and should not be added together.",
            "Same 6-month in-sample period; not out-of-sample evidence.",
            "Present-day index membership creates survivorship bias.",
            "No modifications to live paper trading or portfolio ledgers.",
        ],
    }
    (OUT / "entry_regime_quality.json").write_text(json.dumps(payload, indent=2, allow_nan=False))
    lines = [
        "# V1-D2-LOSS2 regime and entry-quality comparison", "",
        "All exits unchanged. Signal-day close filters; next-session open entries. $10,000 starting equity per window.", "",
        "| Period | Model | Net % | vs current pp | Trades | Win % | PF | Max DD % |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for r in results:
        lines.append(f'| {r["period"]} | {r["mode"]} | {r["net_return_pct"]} | {r["vs_current_pp"]} | {r["closed_trades"]} | {r["win_rate_pct"]} | {r["profit_factor"]} | {r["max_drawdown_pct"]} |')
    lines += ["", "Research-only; in-sample selection. Active V1-D2-LOSS2 unchanged.", ""]
    report = "\n".join(lines)
    (OUT / "entry_regime_quality.md").write_text(report)
    print(report)
    import os
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(report + "\n")

if __name__ == "__main__":
    main()
