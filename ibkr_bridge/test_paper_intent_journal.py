"""Offline tests of persistent idempotency. No IBKR connection."""
import tempfile
import unittest
from pathlib import Path
from ibkr_bridge.paper_order_safety import PaperOrderIntent
from ibkr_bridge.paper_intent_journal import IntentJournal

class JournalTests(unittest.TestCase):
    def test_duplicate_rejected_across_restarts(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "intents.sqlite"
            intent = PaperOrderIntent("AAPL", "BUY", 2, "100", "2026-11-17", "signal-1")
            a = IntentJournal(db)
            self.assertTrue(a.reserve(intent))
            self.assertFalse(a.reserve(intent))
            a.close()
            b = IntentJournal(db)
            self.assertFalse(b.reserve(intent))
            self.assertEqual(len(b.list_intents()), 1)
            b.close()

    def test_blocked_cannot_be_re_reserved(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = IntentJournal(Path(directory) / "intents.sqlite")
            intent = PaperOrderIntent("MSFT", "BUY", 1, "100", "2026-11-17", "signal-2")
            self.assertTrue(journal.reserve(intent))
            self.assertTrue(journal.block(intent.intent_id))
            self.assertFalse(journal.reserve(intent))
            self.assertFalse(journal.block(intent.intent_id))
            self.assertEqual(journal.list_intents()[0]["status"], "BLOCKED")
            journal.close()

    def test_empty_id_denied(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = IntentJournal(Path(directory) / "intents.sqlite")
            with self.assertRaises(ValueError):
                journal.reserve(PaperOrderIntent("AAPL", "BUY", 1, "100", "2026-11-17", ""))
            journal.close()

if __name__ == "__main__":
    unittest.main()
