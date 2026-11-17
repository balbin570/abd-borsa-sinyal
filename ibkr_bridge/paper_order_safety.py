"""Safety gate for a future IBKR PAPER-only order bridge.

Pure validation: no IBKR imports, connections or order placement.
Default-deny; caller must independently verify the IBKR account through
a trusted paper-account allowlist and confirm the session is paper mode.
"""
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

MAX_STRATEGY_NOTIONAL_USD = Decimal("10000")
MAX_POSITIONS = 5
MAX_ORDER_NOTIONAL_USD = Decimal("2000")

@dataclass(frozen=True)
class PaperOrderIntent:
    symbol: str
    side: str
    quantity: int
    reference_price: str
    signal_date: str
    intent_id: str

def validate_paper_intent(intent: PaperOrderIntent, *, verified_paper_account: bool,
                          gateway_paper_mode: bool, live_trading_disabled: bool,
                          already_processed: bool, existing_symbols: set[str],
                          current_exposure_usd: str, strategy_equity_usd: str,
                          today: date) -> None:
    """Raise ValueError unless all independent safety checks pass.

    Does not validate market hours, broker order status, contract identity,
    cash, exchange rules or fills. Those require additional broker-side checks.
    """
    if not (verified_paper_account and gateway_paper_mode and live_trading_disabled):
        raise ValueError("DENIED: paper account and paper session not verified")
    if already_processed or not intent.intent_id.strip():
        raise ValueError("DENIED: duplicate or missing idempotency key")
    if intent.side != "BUY":
        raise ValueError("DENIED: initial bridge only supports new BUY intents")
    if not intent.symbol.isascii() or not intent.symbol.replace(".", "").isalpha() or len(intent.symbol) > 8:
        raise ValueError("DENIED: invalid symbol")
    if type(intent.quantity) is not int or intent.quantity <= 0:
        raise ValueError("DENIED: invalid share quantity")
    try:
        price = Decimal(intent.reference_price)
        exposure = Decimal(current_exposure_usd)
        equity = Decimal(strategy_equity_usd)
    except (InvalidOperation, ValueError):
        raise ValueError("DENIED: invalid numeric value") from None
    if not all(v.is_finite() for v in (price, exposure, equity)):
        raise ValueError("DENIED: nonfinite numeric value")
    if price <= 0 or exposure < 0 or equity <= 0:
        raise ValueError("DENIED: invalid price or exposure")
    if date.fromisoformat(intent.signal_date) != today:
        raise ValueError("DENIED: stale or future signal")
    if intent.symbol in existing_symbols or len(existing_symbols) >= MAX_POSITIONS:
        raise ValueError("DENIED: duplicate symbol or full portfolio")
    notional = price * intent.quantity
    if notional > MAX_ORDER_NOTIONAL_USD or exposure + notional > min(equity, MAX_STRATEGY_NOTIONAL_USD):
        raise ValueError("DENIED: notional/exposure limit exceeded")
