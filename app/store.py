"""SQLite-backed resource store with serialised, atomic write transactions."""
from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from . import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS resources (
    resource_type TEXT NOT NULL,
    id            TEXT NOT NULL,
    content       TEXT NOT NULL,
    created_at    REAL NOT NULL,
    PRIMARY KEY (resource_type, id)
);

CREATE TABLE IF NOT EXISTS patient_identities (
    system     TEXT NOT NULL,
    value      TEXT NOT NULL,
    patient_id TEXT NOT NULL,
    PRIMARY KEY (system, value)
);
CREATE INDEX IF NOT EXISTS patient_identities_patient_idx
    ON patient_identities(patient_id);

CREATE TABLE IF NOT EXISTS bundle_records (
    bundle_id   TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    response    TEXT NOT NULL,
    created_at  REAL NOT NULL
);

-- Patient merges. source_id is the merged-away patient, target_id the retained
-- patient at merge time. A patient appears as source at most once; targets are
-- always live (unmerged) patients, so chains are acyclic by construction.
CREATE TABLE IF NOT EXISTS patient_merges (
    source_id  TEXT PRIMARY KEY,
    target_id  TEXT NOT NULL,
    merge_id   TEXT NOT NULL,
    created_at REAL NOT NULL
);

-- Idempotency of the merge API: merge number -> canonical body hash + response.
CREATE TABLE IF NOT EXISTS merge_records (
    merge_id    TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    response    TEXT NOT NULL,
    created_at  REAL NOT NULL
);
"""


def final_patient_id(conn: sqlite3.Connection, patient_id: str) -> str:
    """Follow the merge chain from ``patient_id`` to the live (retained) patient.

    Merge targets are validated to be unmerged patients, so the chain is a
    simple path. The visited set is a defensive guard: corrupt data raises
    instead of looping forever.
    """
    seen = {patient_id}
    current = patient_id
    while True:
        row = conn.execute(
            "SELECT target_id FROM patient_merges WHERE source_id = ?", (current,)
        ).fetchone()
        if row is None:
            return current
        current = row["target_id"]
        if current in seen:
            raise RuntimeError(
                f"Patient merge chain is cyclic at {current!r}; the merge log is corrupt"
            )
        seen.add(current)

_lock_registry_guard = threading.Lock()
_transaction_locks: dict[str, threading.Lock] = {}


def _transaction_lock(key: str) -> threading.Lock:
    with _lock_registry_guard:
        lock = _transaction_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _transaction_locks[key] = lock
        return lock


class Store:
    """A small FHIR resource repository.

    Writes from all threads and processes serialise on SQLite's
    ``BEGIN IMMEDIATE`` lock (WAL mode). A per-database Python lock additionally
    queues writers in this process so that concurrent HTTP requests wait
    instead of burning through the SQLite busy timeout.
    """

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        if self.db_path == ":memory:":
            raise ValueError("A file-backed SQLite database is required")
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = _transaction_lock(str(Path(self.db_path).resolve()))
        self._init_schema()

    # ------------------------------------------------------------------ setup
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.db_path,
            isolation_level=None,  # explicit BEGIN/COMMIT
            timeout=settings.SQLITE_BUSY_TIMEOUT_MS / 1000,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {settings.SQLITE_BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA synchronous = FULL")
        return conn

    def _init_schema(self) -> None:
        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(SCHEMA)
        finally:
            conn.close()

    # ------------------------------------------------------------------ reads
    def raw_connection(self) -> sqlite3.Connection:
        """A fresh read connection for callers that need a multi-statement read."""
        return self._connect()

    def get_resource(self, resource_type: str, resource_id: str) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT content FROM resources WHERE resource_type = ? AND id = ?",
                (resource_type, resource_id),
            ).fetchone()
        finally:
            conn.close()
        return None if row is None else json.loads(row["content"])

    def resource_exists(self, resource_type: str, resource_id: str) -> bool:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT 1 FROM resources WHERE resource_type = ? AND id = ?",
                (resource_type, resource_id),
            ).fetchone()
        finally:
            conn.close()
        return row is not None

    def count_resources(self) -> int:
        conn = self._connect()
        try:
            return int(conn.execute("SELECT COUNT(*) FROM resources").fetchone()[0])
        finally:
            conn.close()

    def final_patient_id(self, patient_id: str) -> str:
        """The live patient a (possibly merged-away) patient id resolves to."""
        conn = self._connect()
        try:
            return final_patient_id(conn, patient_id)
        finally:
            conn.close()

    # ------------------------------------------------------------------ write
    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Serialise writers and hold one IMMEDIATE SQLite transaction.

        Anything raised inside the block rolls the entire transaction back, so
        a rejected bundle leaves no new resources behind.
        """
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    yield conn
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise
                else:
                    conn.execute("COMMIT")
            finally:
                conn.close()
