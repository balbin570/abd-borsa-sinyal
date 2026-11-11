"""Read-only indicative quote snapshot for the existing US paper portfolio.

Does not alter the ledger, simulate fills, or submit orders.
Quotes from Yahoo Finance may be delayed or unavailable.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

import yfinance as yf

state = json.loads(Path("paper_portfolio/state.json").read_text())
positions = state["positions"]
symbols = [p["symbol"] for p in positions]
lines = ["# Indicative US paper portfolio snapshot", "",
         f"Generated UTC: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
         f"Ledger session: {state['last_processed']}",
         "**Read-only indicative valuation; not executable quotes or a trading signal.**", "",
         "| Symbol | Shares | Entry | Latest price | P/L (after entry fee) | Quote timestamp (UTC) |",
         "|---|---:|---:|---:|---:|---|"]
total = float(state["cash"])
missing = []
for p in positions:
    symbol = p["symbol"]
    try:
        ticker = yf.Ticker(symbol)
        intraday = ticker.history(period="5d", interval="1m", auto_adjust=True, prepost=False)
        if intraday.empty:
            raise ValueError("No intraday quote")
        intraday = intraday.dropna(subset=["Close"])
        if intraday.empty:
            raise ValueError("No valid price")
        row = intraday.iloc[-1]
        price = float(row["Close"])
        if not (0 < price < 1e9):
            raise ValueError("Invalid price")
        timestamp = intraday.index[-1].tz_convert("UTC").isoformat(timespec="minutes")
        total += p["shares"] * price
        pnl = p["shares"] * price - float(p["entry_cost"])
        lines.append(f"| {symbol} | {p['shares']} | ${p['entry_price']:.2f} | ${price:.2f} | ${pnl:+.2f} | {timestamp} |")
    except Exception as exc:
        missing.append(symbol)
        lines.append(f"| {symbol} | {p['shares']} | ${p['entry_price']:.2f} | unavailable | — | — |")
        print(f"Quote unavailable for {symbol}: {exc}", flush=True)
lines.extend(["", f"Cash: ${state['cash']:,.2f}", ""])
if missing:
    lines.append(f"**TOTAL UNAVAILABLE** — missing quotes: {', '.join(missing)}. No partial total published.")
else:
    lines.append(f"**Indicative equity: ${total:,.2f} | Return vs initial capital: {(total/float(state['initial_cash'])-1)*100:+.2f}%**")
lines.extend(["", "Quotes may be delayed, especially outside US market hours. Daily ledger and stop/exit decisions remain unchanged."])
report="\n".join(lines)+"\n"
print(report)
summary = __import__("os").environ.get("GITHUB_STEP_SUMMARY")
if summary:
    with open(summary, "a") as f:
        f.write(report)
