import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from ibkr_bridge.paper_account_baseline import build_initial_state,initialize
SNAP={"mode":"READ_ONLY_NO_ORDERS","complete":{"positions":True,"orders":True,"executions":True},"account":"DU123","positions":[],"open_orders":[],"executions":[]}
class BaselineTests(unittest.TestCase):
    def test_independent_empty_state(self):
        s=build_initial_state(SNAP,activation_date=date(2026,11,18))
        self.assertEqual(s["positions"],[])
        self.assertEqual(s["pending"],[])
        self.assertFalse(s["orders_enabled"])
        self.assertFalse(s["broker_cash_verified"])
    def test_live_account_denied(self):
        with self.assertRaises(ValueError):
            build_initial_state({**SNAP,"account":"U123"},activation_date=date(2026,11,18))
    def test_existing_position_denied(self):
        with self.assertRaises(ValueError):
            build_initial_state({**SNAP,"positions":[{"symbol":"AAPL","quantity":1}]},activation_date=date(2026,11,18))
    def test_pending_order_denied(self):
        with self.assertRaises(ValueError):
            build_initial_state({**SNAP,"open_orders":[{"symbol":"AAPL"}]},activation_date=date(2026,11,18))
    def test_incomplete_snapshot_denied(self):
        with self.assertRaises(ValueError):
            build_initial_state({**SNAP,"complete":{}},activation_date=date(2026,11,18))
    def test_cannot_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"baseline.json"
            initialize(path,SNAP,activation_date=date(2026,11,18))
            with self.assertRaises(ValueError):
                initialize(path,SNAP,activation_date=date(2026,11,18))
            self.assertEqual(json.loads(path.read_text())["account"],"DU123")
if __name__=="__main__":
    unittest.main()
