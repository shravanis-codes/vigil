"""
SQLite persistence: sessions, evidence events, decisions, actions, speakers, raw events.
One file (data/vigil.db). Every decision is stored with the evidence it was based on.
"""
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    mode TEXT NOT NULL,              -- recorded | live | simulated
    status TEXT NOT NULL,            -- open | closed
    speaker_id TEXT,
    action_type TEXT,
    amount REAL,
    currency TEXT DEFAULT 'INR',
    details TEXT DEFAULT '{}',
    call_sid TEXT,
    stream_id TEXT,
    audio_path TEXT,
    current_decision TEXT,
    current_rule TEXT,
    meta TEXT DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_call ON sessions(call_sid);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    source TEXT NOT NULL,
    verdict TEXT NOT NULL,
    final INTEGER NOT NULL,
    origin TEXT,
    dedupe_key TEXT UNIQUE,
    evidence TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id);
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    decision TEXT NOT NULL,
    rule_id TEXT NOT NULL,
    reason TEXT,
    next_step TEXT,
    risk_tier TEXT,
    action_allowed INTEGER,
    trace TEXT,
    evidence_snapshot TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_decisions_session ON decisions(session_id);
CREATE TABLE IF NOT EXISTS actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    action TEXT NOT NULL,
    status TEXT NOT NULL,
    detail TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS speakers (
    speaker_id TEXT PRIMARY KEY,
    display_name TEXT,
    phone TEXT,
    samples INTEGER DEFAULT 0,
    source TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS raw_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel TEXT NOT NULL,           -- webhook | stream | twilio
    session_id TEXT,
    payload TEXT NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL
);
"""

JSON_FIELDS = {"details", "meta", "evidence", "trace", "evidence_snapshot", "payload"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class Store:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    # ---------- helpers ----------
    def _exec(self, sql: str, params=()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def _rows(self, sql: str, params=()) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            for k in JSON_FIELDS & d.keys():
                if d[k]:
                    try:
                        d[k] = json.loads(d[k])
                    except ValueError:
                        pass
            out.append(d)
        return out

    def _one(self, sql: str, params=()) -> Optional[Dict[str, Any]]:
        rows = self._rows(sql, params)
        return rows[0] if rows else None

    # ---------- sessions ----------
    def create_session(self, mode: str, speaker_id: Optional[str] = None, action_type: Optional[str] = None,
                       amount: Optional[float] = None, currency: str = "INR",
                       details: Optional[dict] = None, call_sid: Optional[str] = None,
                       audio_path: Optional[str] = None, meta: Optional[dict] = None) -> Dict[str, Any]:
        sid = "ses_" + uuid.uuid4().hex[:12]
        t = now()
        self._exec(
            "INSERT INTO sessions (id, mode, status, speaker_id, action_type, amount, currency, details,"
            " call_sid, audio_path, meta, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, mode, "open", speaker_id, action_type, amount, currency, json.dumps(details or {}),
             call_sid, audio_path, json.dumps(meta or {}), t, t))
        return self.get_session(sid)

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        return self._one("SELECT * FROM sessions WHERE id = ?", (session_id,))

    def get_session_by_call(self, call_sid: str) -> Optional[Dict[str, Any]]:
        return self._one("SELECT * FROM sessions WHERE call_sid = ? ORDER BY created_at DESC LIMIT 1", (call_sid,))

    def update_session(self, session_id: str, **fields) -> None:
        if not fields:
            return
        cols, vals = [], []
        for k, v in fields.items():
            cols.append(f"{k} = ?")
            vals.append(json.dumps(v) if k in JSON_FIELDS else v)
        cols.append("updated_at = ?")
        vals.append(now())
        vals.append(session_id)
        self._exec(f"UPDATE sessions SET {', '.join(cols)} WHERE id = ?", vals)

    def update_meta(self, session_id: str, **items) -> Dict[str, Any]:
        with self._lock:
            s = self.get_session(session_id) or {}
            meta = s.get("meta") or {}
            meta.update(items)
            self.update_session(session_id, meta=meta)
            return meta

    def list_sessions(self, mode: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        if mode:
            return self._rows("SELECT * FROM sessions WHERE mode = ? ORDER BY created_at DESC LIMIT ?", (mode, limit))
        return self._rows("SELECT * FROM sessions ORDER BY created_at DESC LIMIT ?", (limit,))

    # ---------- events ----------
    def add_event(self, evidence: Dict[str, Any]) -> bool:
        """Returns False if this event was already stored (duplicate)."""
        try:
            self._exec(
                "INSERT INTO events (session_id, source, verdict, final, origin, dedupe_key, evidence, created_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (evidence["session_id"], evidence["source"], evidence["verdict"], int(bool(evidence.get("final"))),
                 evidence.get("origin"), evidence.get("dedupe_key"), json.dumps(evidence), now()))
            return True
        except sqlite3.IntegrityError:
            return False

    def list_events(self, session_id: str) -> List[Dict[str, Any]]:
        return self._rows("SELECT * FROM events WHERE session_id = ? ORDER BY id", (session_id,))

    # ---------- decisions ----------
    def add_decision(self, session_id: str, d: Dict[str, Any], snapshot: Dict[str, Any]) -> None:
        self._exec(
            "INSERT INTO decisions (session_id, decision, rule_id, reason, next_step, risk_tier, action_allowed,"
            " trace, evidence_snapshot, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (session_id, d["decision"], d["rule_id"], d["reason"], d["next_step"], d.get("risk_tier"),
             int(bool(d.get("action_allowed"))), json.dumps(d.get("trace", [])), json.dumps(snapshot), now()))
        self.update_session(session_id, current_decision=d["decision"], current_rule=d["rule_id"])

    def list_decisions(self, session_id: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
        if session_id:
            return self._rows("SELECT * FROM decisions WHERE session_id = ? ORDER BY id", (session_id,))
        return self._rows("SELECT * FROM decisions ORDER BY id DESC LIMIT ?", (limit,))

    def latest_decision(self, session_id: str) -> Optional[Dict[str, Any]]:
        return self._one("SELECT * FROM decisions WHERE session_id = ? ORDER BY id DESC LIMIT 1", (session_id,))

    # ---------- actions ----------
    def add_action(self, session_id: str, action: str, status: str, detail: str = "") -> None:
        self._exec("INSERT INTO actions (session_id, action, status, detail, created_at) VALUES (?,?,?,?,?)",
                   (session_id, action, status, detail, now()))

    def list_actions(self, session_id: str) -> List[Dict[str, Any]]:
        return self._rows("SELECT * FROM actions WHERE session_id = ? ORDER BY id", (session_id,))

    # ---------- speakers ----------
    def upsert_speaker(self, speaker_id: str, display_name: Optional[str] = None, phone: Optional[str] = None,
                       samples: Optional[int] = None, source: str = "local") -> None:
        existing = self._one("SELECT * FROM speakers WHERE speaker_id = ?", (speaker_id,))
        if existing:
            self._exec("UPDATE speakers SET display_name = COALESCE(?, display_name), phone = COALESCE(?, phone),"
                       " samples = COALESCE(?, samples), source = ? WHERE speaker_id = ?",
                       (display_name, phone, samples, source, speaker_id))
        else:
            self._exec("INSERT INTO speakers (speaker_id, display_name, phone, samples, source, created_at)"
                       " VALUES (?,?,?,?,?,?)", (speaker_id, display_name, phone, samples or 0, source, now()))

    def list_speakers(self) -> List[Dict[str, Any]]:
        return self._rows("SELECT * FROM speakers ORDER BY created_at DESC")

    def get_speaker(self, speaker_id: str) -> Optional[Dict[str, Any]]:
        return self._one("SELECT * FROM speakers WHERE speaker_id = ?", (speaker_id,))

    def speaker_by_phone(self, phone: str) -> Optional[Dict[str, Any]]:
        return self._one("SELECT * FROM speakers WHERE phone = ?", (phone,))

    # ---------- raw events (debugging what providers actually send) ----------
    def add_raw(self, channel: str, payload: Any, session_id: Optional[str] = None, note: str = "") -> None:
        self._exec("INSERT INTO raw_events (channel, session_id, payload, note, created_at) VALUES (?,?,?,?,?)",
                   (channel, session_id, json.dumps(payload, default=str), note, now()))

    def list_raw(self, limit: int = 100) -> List[Dict[str, Any]]:
        return self._rows("SELECT * FROM raw_events ORDER BY id DESC LIMIT ?", (limit,))
