"""Forward-only S&P 500 Top 5 paper portfolio. No brokerage integration or orders."""
import concurrent.futures
import csv
import io
import json
import math
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import yfinance as yf
from screener.rs_breakout import normalize_bars, build_signal_frame

ROOT = Path("paper_portfolio")
STATE = ROOT / "state.json"
REPORT = ROOT / "latest_report.md"
INITIAL_CASH = 10000.0
MAX_POSITIONS = 5
ALLOCATION = 0.20
ENTRY_FEE = 0.0015
EXIT_FEE = 0.0015
STOP_PCT = 0.05
MAX_HOLD_BARS = 10
MIN_VOLUME_RATIO = 1.2
BENCHMARK = "SPY"


def get_bars(symbol, start, end):
    error = ""
    for attempt in range(3):
        try:
            df = yf.Ticker(symbol).history(start=str(start), end=str(end), auto_adjust=True)
            if df is None or df.empty:
                raise ValueError("No OHLCV bars")
            df.columns = [str(c).lower() for c in df.columns]
            return normalize_bars(df, end - timedelta(days=1)), ""
        except Exception as exc:
            error = f"{type(exc).__name__}: {str(exc)[:100]}"
            time.sleep(0.5 * (attempt + 1))
    return pd.DataFrame(), error


def get_universe():
    url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
    response = requests.get(url, headers={"User-Agent": "Mozilla/5.0 (compatible; ResearchScanner/1.0)"}, timeout=30)
    response.raise_for_status()
    tables = pd.read_html(io.StringIO(response.text))
    table = next(t for t in tables if "Symbol" in t.columns and len(t) > 450)
    names = sorted({str(s).strip().replace(".", "-") for s in table["Symbol"].dropna()})
    if len(names) < 450:
        raise RuntimeError("S&P 500 constituent list is unexpectedly short")
    return names


def load_state():
    if STATE.exists():
        state = json.loads(STATE.read_text())
        if state.get("version") != 1:
            raise RuntimeError("Unsupported portfolio state version")
        return state
    return {"version": 1, "initial_cash": INITIAL_CASH, "cash": INITIAL_CASH,
            "positions": [], "pending": [], "closed": [], "daily": [], "last_processed": None}


def number(value):
    try:
        n = float(value)
        return n if math.isfinite(n) else None
    except (TypeError, ValueError):
        return None


def scan_symbol(item):
    symbol, bars, spy = item
    if len(bars) < 65:
        return None
    try:
        frame = build_signal_frame(bars, spy["close"], require_delivery=False)
        last = frame.iloc[-1]
        close = number(last.get("close"))
        rs = number(last.get("rs_55"))
        st = number(last.get("supertrend_value"))
        week = number(last.get("previous_week_high"))
        vol = number(last.get("volume_ratio"))
        if None in (close, rs, st, week, vol):
            return None
        if rs <= 0 or close <= st or close <= week or vol < MIN_VOLUME_RATIO:
            return None
        return {"symbol": symbol, "signal_date": str(frame.index[-1].date()),
                "rs55": round(rs, 4), "volume_ratio": round(vol, 4),
                "signal_close": round(close, 4),
                "score": round(rs + min(vol, 4.0) * 2.0, 4)}
    except Exception as exc:
        print(f"Indicator failed for {symbol}: {exc}", flush=True)
        return None


def main():
    ROOT.mkdir(exist_ok=True)
    now = datetime.now(ZoneInfo("America/New_York"))
    cutoff = now.date()
    if now.hour < 17:
        cutoff -= timedelta(days=1)
    while cutoff.weekday() >= 5:
        cutoff -= timedelta(days=1)
    state = load_state()
    spy, err = get_bars(BENCHMARK, cutoff - timedelta(days=240), cutoff + timedelta(days=1))
    if len(spy) < 65:
        raise RuntimeError(f"SPY data missing: {err}")
    session = spy.index[-1].date().isoformat()
    if state["last_processed"] == session:
        print(f"Already processed {session}; no duplicate portfolio update")
        return
    if state["last_processed"] and session < state["last_processed"]:
        raise RuntimeError("Market data older than last processed session")

    # Fetch all symbols once, then reuse OHLCV for portfolio updates and new signals.
    symbols = get_universe()
    extras = {p["symbol"] for p in state["positions"]} | {p["symbol"] for p in state["pending"]}
    symbols = sorted(set(symbols) | extras)
    print(f"Downloading {len(symbols)} stocks; latest SPY session {session}", flush=True)
    def fetch(sym):
        bars, error = get_bars(sym, cutoff - timedelta(days=240), cutoff + timedelta(days=1))
        return sym, bars, error
    frames = {}
    errors = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        for i, (sym, bars, error) in enumerate(pool.map(fetch, symbols), 1):
            if len(bars) and bars.index[-1].date().isoformat() == session:
                frames[sym] = bars
            else:
                errors[sym] = error or "stale or empty daily bars"
            if i % 50 == 0:
                print(f"Downloaded {i}/{len(symbols)}", flush=True)
    valid = len(frames)
    if valid < len(symbols) * 0.90:
        raise RuntimeError(f"Data quality failure: {valid}/{len(symbols)} valid. No state saved.")

    # Missing/stale holdings data must not fabricate orders or prices.
    # If a data provider returns NaN for the current session, mark the
    # valuation as unavailable rather than crashing the entire portfolio.
    required = {p["symbol"] for p in state["positions"]} | {p["symbol"] for p in state["pending"]}
    missing = sorted(required - frames.keys())
    if missing:
        raise RuntimeError(f"Missing complete current-session bars for held/pending stocks: {missing}")
    unpriced = []
    valid_held_prices = {}
    for pos in state["positions"]:
        sym = pos["symbol"]
        current = frames[sym].iloc[-1]
        close = number(current.get("close"))
        if close is None or close <= 0:
            unpriced.append(sym)
        else:
            valid_held_prices[sym] = close
    # IMPORTANT: no state mutation on unpriced holding. Never publish a
    # partial portfolio equity figure presented as an actual daily result.
    if unpriced:
        print(f"DATA_INCOMPLETE: invalid daily close for {', '.join(unpriced)}; skipping daily paper update", flush=True)
        return

    # Pending signals are entered on the next available market session OPEN.
    # On a missed run, a pending entry is canceled rather than filled days late.
    remaining_pending = []
    for order in state["pending"]:
        sym = order["symbol"]
        df = frames[sym]
        future = df[df.index.date > pd.Timestamp(order["signal_date"]).date()]
        if future.empty:
            remaining_pending.append(order)
            continue
        if future.index[0].date().isoformat() != session:
            print(f"Canceled stale pending entry {sym} from {order['signal_date']}")
            continue
        if len(state["positions"]) >= MAX_POSITIONS:
            continue
        open_price = number(future.iloc[0]["open"])
        if not open_price or open_price <= 0:
            continue
        budget = min(state["cash"], INITIAL_CASH * ALLOCATION)
        qty = math.floor(budget / (open_price * (1 + ENTRY_FEE)))
        if qty < 1:
            continue
        cost = qty * open_price * (1 + ENTRY_FEE)
        state["cash"] -= cost
        state["positions"].append({"symbol": sym, "entry_date": session,
            "entry_price": round(open_price, 6), "shares": qty, "entry_cost": round(cost, 6),
            "signal_date": order["signal_date"], "signal_score": order["score"]})
        print(f"PAPER ENTRY {sym}: {qty} shares at {open_price:.2f}")
    state["pending"] = remaining_pending

    # Process today's bar. Stops use open if gapped below stop, otherwise stop.
    # If the exit day is the entry day, the day's low is valid after the open.
    kept = []
    for pos in state["positions"]:
        sym = pos["symbol"]
        df = frames[sym]
        since = df[df.index.date >= pd.Timestamp(pos["entry_date"]).date()]
        if since.empty:
            kept.append(pos)
            continue
        last = since.iloc[-1]
        open_px, low_px, close_px = (number(last[k]) for k in ("open", "low", "close"))
        if None in (open_px, low_px, close_px) or min(open_px, low_px, close_px) <= 0:
            print(f"DATA_INCOMPLETE: invalid daily OHLC for {sym}; no invented exit", flush=True)
            return
        held = len(since)
        stop = pos["entry_price"] * (1 - STOP_PCT)
        reason = None
        exit_price = None
        if low_px <= stop:
            reason = "STOP"
            exit_price = min(open_px, stop)
        elif held >= MAX_HOLD_BARS:
            reason = "TIME"
            exit_price = close_px
        if reason:
            proceeds = pos["shares"] * exit_price * (1 - EXIT_FEE)
            pnl = proceeds - pos["entry_cost"]
            state["cash"] += proceeds
            state["closed"].append({**pos, "exit_date": session, "exit_price": round(exit_price, 6),
                                    "reason": reason, "net_pnl": round(pnl, 4),
                                    "net_return_pct": round(100 * pnl / pos["entry_cost"], 4)})
            print(f"PAPER EXIT {sym}: {reason}, net {pnl:.2f}")
        else:
            kept.append(pos)
    state["positions"] = kept

    # New signals are queued, NOT entered at the already-known close.
    items = [(sym, frame, spy) for sym, frame in frames.items() if sym in symbols]
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        candidates = [c for c in pool.map(scan_symbol, items) if c is not None]
    candidates.sort(key=lambda c: (-c["score"], c["symbol"]))
    reserved = {p["symbol"] for p in state["positions"]} | {p["symbol"] for p in state["pending"]}
    free = MAX_POSITIONS - len(state["positions"]) - len(state["pending"])
    for c in candidates:
        if free <= 0:
            break
        if c["symbol"] in reserved:
            continue
        state["pending"].append(c)
        reserved.add(c["symbol"])
        free -= 1

    value = state["cash"] + sum(p["shares"] * float(frames[p["symbol"]].iloc[-1]["close"]) for p in state["positions"])
    state["daily"].append({"date": session, "equity": round(value, 2), "cash": round(state["cash"], 2),
                           "positions": len(state["positions"]), "pending": len(state["pending"]),
                           "spy_close": round(float(spy.iloc[-1]["close"]), 6)})
    state["last_processed"] = session
    state["cash"] = round(state["cash"], 6)
    state["positions"].sort(key=lambda p: p["symbol"])
    state["pending"].sort(key=lambda p: -p["score"])
    lines = [f"# S&P 500 Top 5 — Forward Paper Portfolio ({session})", "",
             "**Paper only; no orders.** This is a forward-only ledger, not a backtest.", "",
             f"- Universe: {len(symbols)}; valid data: {valid}; failed: {len(errors)}",
             f"- Cash: ${state['cash']:,.2f}; equity: ${value:,.2f}; net return from inception: {(value/INITIAL_CASH-1)*100:+.2f}%",
             f"- Open positions: {len(state['positions'])}; next-session pending entries: {len(state['pending'])}; closed trades: {len(state['closed'])}",
             f"- All qualifying signals today: {len(candidates)}; selection: top score = RS55 + 2 × min(volume ratio, 4)",
             "- Volume threshold: 1.2; stop: 5%; maximum hold: 10 trading bars; 0.15% fee each side",
             "- Next-session OPEN entry, no same-close execution. Missing-run pending entries canceled.",
             "", "## Open positions", "",
             "| Symbol | Entry date | Entry price | Shares |", "|---|---|---:|---:|"]
    for p in state["positions"]:
        lines.append(f"| {p['symbol']} | {p['entry_date']} | {p['entry_price']:.2f} | {p['shares']} |")
    lines += ["", "## Queued for next session open", "", "| Symbol | Signal date | RS55 | Volume ratio | Score |",
              "|---|---|---:|---:|---:|"]
    for p in state["pending"]:
        lines.append(f"| {p['symbol']} | {p['signal_date']} | {p['rs55']:.2f} | {p['volume_ratio']:.2f} | {p['score']:.2f} |")
    lines += ["", "## Most recent closed trades", "",
              "| Symbol | Entry | Exit | Reason | Net P&L | Net return |",
              "|---|---|---|---|---:|---:|"]
    for p in state["closed"][-10:]:
        lines.append(f"| {p['symbol']} | {p['entry_date']} | {p['exit_date']} | {p['reason']} | ${p['net_pnl']:+.2f} | {p['net_return_pct']:+.2f}% |")
    lines += ["", "## Data issues", "", ", ".join(sorted(errors)) if errors else "None", ""]
    report="\n".join(lines)
    REPORT.write_text(report)
    STATE.write_text(json.dumps(state, indent=2) + "\n")
    print(report)
    summary=os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write(report + "\n")


if __name__ == "__main__":
    main()
