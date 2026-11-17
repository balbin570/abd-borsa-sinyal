import unittest
from unittest.mock import patch
from datetime import date
from ibkr_bridge.paper_account_funds import trading_day_check,read_account
class FundsTests(unittest.TestCase):
    def test_weekend_closed(self):
        self.assertFalse(trading_day_check(date(2026,11,21)))
    def test_thanksgiving_closed(self):
        self.assertFalse(trading_day_check(date(2026,11,26)))
    def test_normal_wednesday_open(self):
        self.assertTrue(trading_day_check(date(2026,11,18)))
    def test_nonpaper_port_denied(self):
        with self.assertRaises(ValueError):
            read_account(7497)
if __name__=="__main__":
    unittest.main()
