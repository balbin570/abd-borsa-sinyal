# IBKR Paper bridge — staged integration

Current state: **read-only / default-deny**. No order execution implementation.

- `paper_readonly_compare.py`: compares historical strategy holdings to IBKR positions. Differences are **not** trade instructions.
- `paper_order_safety.py`: pure validation gate for future fresh BUY intents.
- `paper_dry_run.py`: reads IBKR Gateway on local port 4002, verifies one DU-prefixed paper account, and deliberately blocks all orders until persistent order IDs, reconciliation, and reliable paper-session verification exist.

Run from repository root with IB Gateway logged into **Simulated Trading**, API read-only enabled, and a JSON file containing an array of *fresh* proposed intents (never `paper_portfolio/state.json`):

```bash
python -m ibkr_bridge.paper_dry_run --signals fresh_intents.json --paper-account DUxxxxxxxx
```

Do not commit account identifiers, credentials, Gateway settings or tokens. Keep API port 4002 bound to localhost. Do not expose it on a public IP. The `DU` prefix is a necessary but **not sufficient** verification of paper mode. No automated trading should be enabled until a separate deployment and live-vs-paper guard, fresh-signal scheduler, position/cash reconciliation, order-status ledger, idempotency, market-hours checks, and tests are completed.

**Important:** The existing Top 5 strategy uses end-of-day OHLCV and a virtual ledger; those fills cannot be translated into broker orders after the fact.
