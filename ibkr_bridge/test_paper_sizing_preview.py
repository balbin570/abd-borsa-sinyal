import unittest
from ibkr_bridge.paper_sizing_preview import size_candidates

SNAP={"account":"DU123","mode":"READ_ONLY_NO_ORDERS","complete":{"positions":True,"orders":True,"executions":True},"positions":[],"open_orders":[]}
PREVIEW={"account":"DU123","orders_enabled":False,"candidates_for_review":[{"symbol":"AAPL","signal_date":"2026-11-17"}]}
class SizingTests(unittest.TestCase):
    def test_sizing_and_budget(self):
        result=size_candidates(PREVIEW,SNAP,{"AAPL":"100"},paper_cash_usd=10000)
        self.assertEqual(result["allocations_preview_only"][0]["shares_preview"],19)
        self.assertFalse(result["orders_enabled"])
    def test_duplicate_broker_order_denied(self):
        snap={**SNAP,"open_orders":[{"symbol":"AAPL"}]}
        self.assertEqual(size_candidates(PREVIEW,snap,{"AAPL":100},paper_cash_usd=10000)["allocations_preview_only"],[])
    def test_invalid_cash_denied(self):
        with self.assertRaises(ValueError):
            size_candidates(PREVIEW,SNAP,{"AAPL":100},paper_cash_usd="-1")
    def test_wrong_account_denied(self):
        with self.assertRaises(ValueError):
            size_candidates({**PREVIEW,"account":"DU456"},SNAP,{"AAPL":100},paper_cash_usd=10000)
    def test_missing_price_denied(self):
        self.assertEqual(size_candidates(PREVIEW,SNAP,{},paper_cash_usd=10000)["allocations_preview_only"],[])
    def test_insufficient_cash(self):
        self.assertEqual(size_candidates(PREVIEW,SNAP,{"AAPL":100},paper_cash_usd=50)["allocations_preview_only"],[])
if __name__=="__main__":
    unittest.main()
