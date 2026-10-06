"""Thread-safe raw-event and audit persistence. Decode failures never erase bytes."""
from __future__ import annotations
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

class EventStore:
    def __init__(self, path: Path):
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, utc TEXT NOT NULL, local TEXT NOT NULL,
          gateway TEXT, unit TEXT, direction TEXT, raw BLOB, details TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, utc TEXT NOT NULL, action TEXT, details TEXT);
        PRAGMA user_version=1;
        """)
        self.db.commit()

    def event(self, gateway, unit, direction, raw, details):
        now = datetime.now(timezone.utc)
        with self.lock:
            row = self.db.execute("INSERT INTO events(utc,local,gateway,unit,direction,raw,details) VALUES(?,?,?,?,?,?,?)",
                (now.isoformat(), now.astimezone().isoformat(), gateway, unit, direction, sqlite3.Binary(raw), json.dumps(details)))
            self.db.commit()
            return row.lastrowid

    def audit(self, action, details):
        with self.lock:
            self.db.execute("INSERT INTO audit(utc,action,details) VALUES(?,?,?)", (datetime.now(timezone.utc).isoformat(), action, json.dumps(details)))
            self.db.commit()

    def recent(self, limit=100):
        with self.lock:
            rows = self.db.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (min(limit, 1000),)).fetchall()
        return [self.serialize(row) for row in rows]

    @staticmethod
    def serialize(row):
        result = dict(row)
        result["raw_hex"] = bytes(result.pop("raw")).hex(" ").upper()
        result["details"] = json.loads(result["details"])
        return result

    def export(self):
        # Iterate bounded batches; exports include every byte, not just decoded values.
        last = 0
        with self.lock:
            upper = self.db.execute("SELECT COALESCE(MAX(id),0) FROM events").fetchone()[0]
        while True:
            with self.lock:
                rows = self.db.execute("SELECT * FROM events WHERE id>? AND id<=? ORDER BY id LIMIT 500", (last,upper)).fetchall()
            if not rows:
                break
            for row in rows:
                last = row["id"]
                yield json.dumps(self.serialize(row), ensure_ascii=False) + "\n"

    def close(self):
        with self.lock:
            self.db.close()
