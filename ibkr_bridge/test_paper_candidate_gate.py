import unittest
from datetime import date
from ibkr_bridge.paper_candidate_gate import review

STATE={"strategy":"V1-D2-LOSS2+B125_SPY_MOM20_POS","mode":"IBKR_PAPER_READ_ONLY",
       "account":"DU123","activation_date":"2026-11-22","orders_enabled":False}
FEED={"schema_version":1,"source":"sp500_top5_daily_scan","signal_session":"2026-11-23",
      "orders_enabled":False,"candidate_count":1,
      "candidates":[{"symbol":"AAPL","signal_date":"2026-11-23","score":10}]}

class CandidateGateTests(unittest.TestCase):
    def test_fresh_feed_preview_only(self):
        out=review(STATE,FEED,today=date(2026,11,24))
        self.assertEqual(out["status"],"PREVIEW_ONLY")
        self.assertEqual(out["accepted"][0]["symbol"],"AAPL")
        self.assertFalse(out["orders_enabled"])

    def test_pre_activation_feed_rejected(self):
        out=review(STATE,{**FEED,"signal_session":"2026-11-20"},today=date(2026,11,23))
        self.assertEqual(out["status"],"REJECTED_PRE_ACTIVATION")
        self.assertEqual(out["accepted"],[])

    def test_order_enabled_denied(self):
        with self.assertRaisesRegex(ValueError,"orders"):
            review({**STATE,"orders_enabled":True},FEED,today=date(2026,11,24))

    def test_stale_feed_denied(self):
        with self.assertRaisesRegex(ValueError,"stale"):
            review(STATE,FEED,today=date(2026,11,30))

    def test_mismatched_signal_date_denied(self):
        feed={**FEED,"candidates":[{"symbol":"AAPL","signal_date":"2026-11-20"}]}
        with self.assertRaisesRegex(ValueError,"mismatched"):
            review(STATE,feed,today=date(2026,11,24))

    def test_duplicate_symbol_denied(self):
        feed={**FEED,"candidate_count":2,"candidates":[FEED["candidates"][0]]*2}
        with self.assertRaisesRegex(ValueError,"duplicate"):
            review(STATE,feed,today=date(2026,11,24))

if __name__=="__main__":
    unittest.main()
