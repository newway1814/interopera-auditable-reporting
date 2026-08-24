from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from interopera.utils import canonical_json


GENESIS_HASH = "0" * 64


class AppendOnlyAuditLog:
    """SQLite audit trail protected by database-level UPDATE/DELETE triggers."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._connection = sqlite3.connect(path)
        self._connection.row_factory = sqlite3.Row
        self._connection.executescript(
            """
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS audit_events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                recorded_at TEXT NOT NULL,
                run_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                actor TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                previous_hash TEXT NOT NULL,
                event_hash TEXT NOT NULL UNIQUE
            );
            CREATE TRIGGER IF NOT EXISTS audit_events_no_update
            BEFORE UPDATE ON audit_events
            BEGIN
                SELECT RAISE(ABORT, 'audit_events is append-only: UPDATE forbidden');
            END;
            CREATE TRIGGER IF NOT EXISTS audit_events_no_delete
            BEFORE DELETE ON audit_events
            BEGIN
                SELECT RAISE(ABORT, 'audit_events is append-only: DELETE forbidden');
            END;
            """
        )
        self._connection.commit()

    def append(self, run_id: str, event_type: str, payload: dict[str, Any], actor: str = "interopera-engine/1.0.0") -> str:
        row = self._connection.execute(
            "SELECT event_hash FROM audit_events ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        previous_hash = row["event_hash"] if row else GENESIS_HASH
        payload_json = canonical_json(payload)
        material = canonical_json(
            {"run_id": run_id, "event_type": event_type, "actor": actor, "payload": json.loads(payload_json), "previous_hash": previous_hash}
        )
        event_hash = hashlib.sha256(material.encode("utf-8")).hexdigest()
        recorded_at = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        self._connection.execute(
            "INSERT INTO audit_events (recorded_at, run_id, event_type, actor, payload_json, previous_hash, event_hash) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (recorded_at, run_id, event_type, actor, payload_json, previous_hash, event_hash),
        )
        self._connection.commit()
        return event_hash

    def latest_payload(self, event_type: str) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT payload_json FROM audit_events WHERE event_type = ? ORDER BY sequence DESC LIMIT 1", (event_type,)
        ).fetchone()
        return json.loads(row["payload_json"]) if row else None

    def verify_chain(self) -> bool:
        previous_hash = GENESIS_HASH
        rows = self._connection.execute("SELECT * FROM audit_events ORDER BY sequence").fetchall()
        for row in rows:
            if row["previous_hash"] != previous_hash:
                return False
            payload = json.loads(row["payload_json"])
            material = canonical_json(
                {"run_id": row["run_id"], "event_type": row["event_type"], "actor": row["actor"], "payload": payload, "previous_hash": previous_hash}
            )
            expected = hashlib.sha256(material.encode("utf-8")).hexdigest()
            if expected != row["event_hash"]:
                return False
            previous_hash = row["event_hash"]
        return True

    def count(self) -> int:
        return int(self._connection.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0])

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "AppendOnlyAuditLog":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
