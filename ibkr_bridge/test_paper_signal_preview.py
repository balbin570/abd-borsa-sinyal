import unittest
from datetime import date
from ibkr_bridge.paper_signal_preview import preview

SNAP={"mode":"READ_ONLY_NO_ORDERS","account":"DU123","complete":{"positions":True,"orders":True,"executions":True},"positions":[],"open_orders":[]}
STATE={"version":1,"pending":[{"symbol":"AAPL","signal_date":"2026-11-17","score":3}]}
class PreviewTests(unittest.TestCase):
    def test_fresh_signal_review_only(self):
        out=preview(STATE,SNAP,activation_date=date(2026,11,17),execution_date=date(2026,11,18))
        self.assertEqual(len(out["candidates_for_review"]),1)
        self.assertFalse(out["orders_enabled"])
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
    def test_live_account_denied(self):
        with self.assertRaises(ValueError):
            preview(STATE,{**SNAP,"account":"U123"},activation_date=date(2026,11,17),execution_date=date(2026,11,18))
if __name__=="__main__":
    unittest.main()
