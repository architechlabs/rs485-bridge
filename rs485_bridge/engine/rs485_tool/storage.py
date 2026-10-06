"""Versioned SQLite event store plus lossless JSONL/CSV/JSON/TXT export."""
from __future__ import annotations
import csv, json, sqlite3
from pathlib import Path
from dataclasses import asdict
from .models import Frame, SerialSettings

SCHEMA_VERSION = 1

class CaptureStore:
    def __init__(self, path: str | Path):
        self.path = Path(path); self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS schema_meta(version INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, started_utc TEXT NOT NULL, port TEXT, settings_json TEXT, notes TEXT DEFAULT '');
        CREATE TABLE IF NOT EXISTS frames(id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, timestamp_utc TEXT NOT NULL, timestamp_local TEXT NOT NULL, port TEXT, baudrate INTEGER, bytesize INTEGER, parity TEXT, stopbits REAL, direction TEXT, raw BLOB NOT NULL, previous_byte_gap REAL, previous_frame_gap REAL, parser_status TEXT, decode_json TEXT, notes TEXT, FOREIGN KEY(session_id) REFERENCES sessions(id));
        CREATE TABLE IF NOT EXISTS correlations(id INTEGER PRIMARY KEY AUTOINCREMENT, request_id INTEGER, response_id INTEGER, round_trip_seconds REAL, confidence TEXT);
        CREATE TABLE IF NOT EXISTS annotations(id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, timestamp_utc TEXT, text TEXT);
        CREATE TABLE IF NOT EXISTS replays(id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, frame_id INTEGER, timestamp_utc TEXT, mode TEXT, raw BLOB, outcome TEXT);
        CREATE TABLE IF NOT EXISTS commands(name TEXT PRIMARY KEY, raw BLOB, description TEXT DEFAULT '', created_utc TEXT);
        CREATE TABLE IF NOT EXISTS devices(port TEXT PRIMARY KEY, info_json TEXT, last_seen_utc TEXT);
        """)
        row = self.db.execute("SELECT version FROM schema_meta LIMIT 1").fetchone()
        if row is None: self.db.execute("INSERT INTO schema_meta VALUES (?)", (SCHEMA_VERSION,))
        elif row[0] > SCHEMA_VERSION: raise RuntimeError("capture database is newer than this application")
        self.db.commit()

    def create_session(self, session_id: str, settings: SerialSettings, notes: str = "", *,
                       transport: str = "serial", endpoint: str | None = None,
                       metadata: dict | None = None) -> None:
        from .models import utc_now
        if transport == "serial":
            settings_data = {**asdict(settings), "transport": transport}
            if metadata:
                settings_data.update(metadata)
        else:
            settings_data = {"transport": transport, "serial_settings": asdict(settings), **(metadata or {})}
        self.db.execute("INSERT OR IGNORE INTO sessions(id,started_utc,port,settings_json,notes) VALUES(?,?,?,?,?)",
                        (session_id,utc_now().isoformat(),endpoint or settings.port,json.dumps(settings_data),notes))
        self.db.commit()

    def add_frame(self, frame: Frame) -> int:
        decode_json = json.dumps({**asdict(frame.decode), "payload": frame.decode.payload.hex()} if frame.decode else None)
        cur = self.db.execute("INSERT INTO frames(session_id,timestamp_utc,timestamp_local,port,baudrate,bytesize,parity,stopbits,direction,raw,previous_byte_gap,previous_frame_gap,parser_status,decode_json,notes) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (frame.session_id,frame.timestamp_utc,frame.timestamp_local,frame.port,frame.baudrate,frame.bytesize,frame.parity,frame.stopbits,frame.direction,sqlite3.Binary(frame.raw),frame.previous_byte_gap,frame.previous_frame_gap,frame.parser_status,decode_json,frame.notes))
        frame.frame_id = cur.lastrowid; self.db.commit(); return int(cur.lastrowid)

    def add_annotation(self, session_id: str, text: str) -> None:
        from .models import utc_now
        self.db.execute("INSERT INTO annotations(session_id,timestamp_utc,text) VALUES(?,?,?)", (session_id,utc_now().isoformat(),text)); self.db.commit()

    def frames(self, session_id: str | None = None) -> list[sqlite3.Row]:
        if session_id: return list(self.db.execute("SELECT * FROM frames WHERE session_id=? ORDER BY id",(session_id,)))
        return list(self.db.execute("SELECT * FROM frames ORDER BY id"))

    def sessions(self) -> list[sqlite3.Row]: return list(self.db.execute("SELECT * FROM sessions ORDER BY started_utc DESC"))

    def correlate(self, request_id: int | None, response_id: int | None, rtt: float | None, confidence: str) -> None:
        self.db.execute("INSERT INTO correlations(request_id,response_id,round_trip_seconds,confidence) VALUES(?,?,?,?)",(request_id,response_id,rtt,confidence)); self.db.commit()

    def export(self, session_id: str, path: str | Path, fmt: str | None = None) -> Path:
        target = Path(path); target.parent.mkdir(parents=True,exist_ok=True); fmt = (fmt or target.suffix.lstrip(".")).lower()
        rows = self.frames(session_id)
        def obj(row):
            d = dict(row); d["raw_hex"] = bytes(d.pop("raw")).hex().upper();
            if d["decode_json"]: d["decode"] = json.loads(d.pop("decode_json"))
            else: d.pop("decode_json")
            return d
        items = [obj(r) for r in rows]
        if fmt in ("jsonl","ndjson"):
            with target.open("w",encoding="utf8") as f:
                for item in items: f.write(json.dumps(item,ensure_ascii=False)+"\n")
        elif fmt == "json": target.write_text(json.dumps({"session_id":session_id,"frames":items},indent=2),encoding="utf8")
        elif fmt == "csv":
            with target.open("w",newline="",encoding="utf8") as f:
                cols=list(items[0]) if items else ["id","session_id","timestamp_utc","raw_hex"]
                w=csv.DictWriter(f,fieldnames=cols);w.writeheader();w.writerows(items)
        elif fmt == "txt":
            target.write_text("\n".join(f"{i['id']} {i['timestamp_utc']} {i['direction']} {i['raw_hex']} [{i['parser_status']}]" for i in items),encoding="utf8")
        else: raise ValueError("export format must be jsonl, json, csv, or txt")
        return target

    def close(self): self.db.close()
