import unittest
from datetime import date
from ibkr_bridge.paper_signal_preview import preview

SNAP={"mode":"READ_ONLY_NO_ORDERS","account":"DU123","complete":{"positions":True,"orders":True,"executions":True},"positions":[],"open_orders":[]}
STATE={"version":1,"strategy":"V1-D2-LOSS2+B125_SPY_MOM20_POS","pending":[{"symbol":"AAPL","signal_date":"2026-11-17","score":3}]}
class PreviewTests(unittest.TestCase):
    def test_fresh_signal_review_only(self):
        out=preview(STATE,SNAP,activation_date=date(2026,11,17),execution_date=date(2026,11,18))
        self.assertEqual(len(out["candidates_for_review"]),1)
        self.assertFalse(out["orders_enabled"])
    def test_missing_strategy_tag_denied(self):
        # Existing legacy paper_portfolio/state.json has no strategy marker.
        # Never infer the new strategy from old holdings or pending signals.
        legacy={**STATE}
        legacy.pop("strategy")
        with self.assertRaisesRegex(ValueError, "legacy strategy"):
            preview(legacy,SNAP,activation_date=date(2026,11,20),execution_date=date(2026,11,23))
    def test_old_pending_signal_cannot_enter_new_broker_account(self):
        historical={**STATE,"pending":[{"symbol":"TSN","signal_date":"2026-11-20"}]}
        out=preview(historical,SNAP,activation_date=date(2026,11,22),execution_date=date(2026,11,23))
        self.assertEqual(out["candidates_for_review"],[])
        self.assertEqual(out["rejected"][0]["reason"],"historical before activation")
    def test_legacy_strategy_denied(self):
        with self.assertRaisesRegex(ValueError, "legacy strategy"):
            preview({**STATE,"strategy":"V1-D2-LOSS2"},SNAP,activation_date=date(2026,11,17),execution_date=date(2026,11,18))
    def test_historical_denied(self):
        out=preview(STATE,SNAP,activation_date=date(2026,11,18),execution_date=date(2026,11,19))
        self.assertEqual(out["candidates_for_review"],[])
    def test_existing_broker_position_denied(self):
        snap={**SNAP,"positions":[{"symbol":"AAPL","quantity":2}]}
        out=preview(STATE,snap,activation_date=date(2026,11,17),execution_date=date(2026,11,18))
        self.assertEqual(out["candidates_for_review"],[])
    def test_incomplete_snapshot_denied(self):
        with self.assertRaises(ValueError):
            preview(STATE,{**SNAP,"complete":{}},activation_date=date(2026,11,17),execution_date=date(2026,11,18))
    def test_local_account_mismatch_denied(self):
        with self.assertRaisesRegex(ValueError, "account mismatch"):
            preview({**STATE,"account":"DU999"},SNAP,activation_date=date(2026,11,17),execution_date=date(2026,11,18))
    def test_local_activation_mismatch_denied(self):
        with self.assertRaisesRegex(ValueError, "activation date mismatch"):
            preview({**STATE,"activation_date":"2026-11-16"},SNAP,activation_date=date(2026,11,17),execution_date=date(2026,11,18))
    def test_local_orders_enabled_denied(self):
        with self.assertRaisesRegex(ValueError, "read-only"):
            preview({**STATE,"orders_enabled":True},SNAP,activation_date=date(2026,11,17),execution_date=date(2026,11,18))
    def test_live_account_denied(self):
        with self.assertRaises(ValueError):
            preview(STATE,{**SNAP,"account":"U123"},activation_date=date(2026,11,17),execution_date=date(2026,11,18))
if __name__=="__main__":
    unittest.main()
