"""Offline safety-gate tests. No broker connection, credentials or orders."""
import unittest
from dataclasses import replace
from datetime import date, timedelta
from ibkr_bridge.paper_order_safety import PaperOrderIntent, validate_paper_intent

TODAY = date(2026, 11, 17)

class PaperSafetyTests(unittest.TestCase):
    def setUp(self):
        self.intent = PaperOrderIntent("AAPL", "BUY", 10, "100", TODAY.isoformat(), "new-001")
        self.kwargs = dict(
            verified_paper_account=True, gateway_paper_mode=True,
            live_trading_disabled=True, already_processed=False,
            existing_symbols=set(), current_exposure_usd="0",
            strategy_equity_usd="10000", today=TODAY,
        )

    def allowed(self, intent=None, **overrides):
        args = {**self.kwargs, **overrides}
        return validate_paper_intent(intent or self.intent, **args)

    def denied(self, intent=None, **overrides):
        with self.assertRaises(ValueError):
            self.allowed(intent, **overrides)

    def test_valid_new_buy_passes_pure_gate(self):
        self.assertIsNone(self.allowed())

    def test_unverified_account_denied(self):
        self.denied(verified_paper_account=False)

    def test_wrong_gateway_mode_denied(self):
        self.denied(gateway_paper_mode=False)

    def test_live_trading_enabled_denied(self):
        self.denied(live_trading_disabled=False)

    def test_duplicate_intent_denied(self):
        self.denied(already_processed=True)

    def test_missing_id_denied(self):
        self.denied(replace(self.intent, intent_id=""))

    def test_sell_denied(self):
        self.denied(replace(self.intent, side="SELL"))

    def test_stale_signal_denied(self):
        self.denied(replace(self.intent, signal_date=(TODAY-timedelta(days=1)).isoformat()))

    def test_future_signal_denied(self):
        self.denied(replace(self.intent, signal_date=(TODAY+timedelta(days=1)).isoformat()))

    def test_existing_symbol_denied(self):
        self.denied(existing_symbols={"AAPL"})

    def test_five_positions_denied(self):
        self.denied(existing_symbols={"MSFT", "NVDA", "AMZN", "GOOG", "META"})

    def test_order_over_2000_denied(self):
        self.denied(replace(self.intent, quantity=21))

    def test_total_exposure_over_10000_denied(self):
        self.denied(current_exposure_usd="9500")

    def test_invalid_price_denied(self):
        self.denied(replace(self.intent, reference_price="NaN"))

    def test_zero_equity_denied(self):
        self.denied(strategy_equity_usd="0")

    def test_zero_quantity_denied(self):
        self.denied(replace(self.intent, quantity=0))

    def test_bad_symbol_denied(self):
        self.denied(replace(self.intent, symbol="../AAPL"))

if __name__ == "__main__":
    unittest.main()
