"""Durable, default-deny intent journal for future IBKR PAPER integration.

No ibapi imports and no order submission. A reserved intent is NOT permission
to submit: execution remains disabled until broker reconciliation is implemented.
"""
import json
import os
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS intents (
 intent_id TEXT PRIMARY KEY,
 symbol TEXT NOT NULL,
 side TEXT NOT NULL,
 quantity INTEGER NOT NULL CHECK(quantity > 0),
 signal_date TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('RESERVED','BLOCKED','CANCELLED')),
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

class IntentJournal:
    def __init__(self, db_path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path), timeout=15)
        self.db.execute("PRAGMA busy_timeout=15000")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    def reserve(self, intent):
        """Atomically reserve a new key; False if already recorded."""
        if not intent.intent_id.strip():
            raise ValueError("empty intent ID")
        try:
            with self.db:
                self.db.execute(
                    "INSERT INTO intents(intent_id,symbol,side,quantity,signal_date,status) VALUES(?,?,?,?,?,'RESERVED')",
                    (intent.intent_id, intent.symbol, intent.side, intent.quantity, intent.signal_date),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def block(self, intent_id):
        with self.db:
            cur = self.db.execute(
                "UPDATE intents SET status='BLOCKED' WHERE intent_id=? AND status='RESERVED'",
                (intent_id,),
            )
            return cur.rowcount == 1

    def list_intents(self):
        rows = self.db.execute(
            "SELECT intent_id,symbol,side,quantity,signal_date,status FROM intents ORDER BY created_at,intent_id"
        ).fetchall()
        return [dict(zip(("intent_id","symbol","side","quantity","signal_date","status"), row)) for row in rows]

    def close(self):
        self.db.close()

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()
    journal = IntentJournal(args.db)
    try:
        print(json.dumps(journal.list_intents(), indent=2))
        print("READ-ONLY JOURNAL INSPECTION. ORDERS DISABLED.")
    finally:
        journal.close()

if __name__ == "__main__":
    main()
