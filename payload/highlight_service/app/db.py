from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.initialize()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialize(self) -> None:
        schema = """
        CREATE TABLE IF NOT EXISTS recording_segments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            path TEXT NOT NULL UNIQUE,
            file_hash TEXT NOT NULL UNIQUE,
            size_bytes INTEGER NOT NULL,
            duration REAL NOT NULL DEFAULT 0,
            timeline_start REAL NOT NULL DEFAULT 0,
            timeline_end REAL NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'discovered',
            error TEXT NOT NULL DEFAULT '',
            transcribed_at TEXT NOT NULL DEFAULT '',
            model_submitted_at TEXT NOT NULL DEFAULT '',
            analyzed_at TEXT NOT NULL DEFAULT '',
            gpt_windows_done_json TEXT NOT NULL DEFAULT '[]',
            deepseek_windows_done_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_segments_session_time
            ON recording_segments(session_id, timeline_start);

        CREATE TABLE IF NOT EXISTS transcript_spans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            segment_id INTEGER NOT NULL REFERENCES recording_segments(id) ON DELETE CASCADE,
            session_id TEXT NOT NULL,
            start_time REAL NOT NULL,
            end_time REAL NOT NULL,
            text TEXT NOT NULL,
            confidence REAL NOT NULL DEFAULT 0,
            words_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_spans_session_time
            ON transcript_spans(session_id, start_time);

        CREATE TABLE IF NOT EXISTS highlight_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            start_time REAL NOT NULL,
            end_time REAL NOT NULL,
            source_ranges_json TEXT NOT NULL DEFAULT '[]',
            captions_json TEXT NOT NULL DEFAULT '[]',
            sales_score REAL NOT NULL DEFAULT 0,
            coherence_score REAL NOT NULL DEFAULT 0,
            product_score REAL NOT NULL DEFAULT 0,
            confidence REAL NOT NULL DEFAULT 0,
            risk_json TEXT NOT NULL DEFAULT '[]',
            reason TEXT NOT NULL DEFAULT '',
            keyframes_json TEXT NOT NULL DEFAULT '[]',
            kept_clauses_json TEXT NOT NULL DEFAULT '[]',
            removed_clauses_json TEXT NOT NULL DEFAULT '[]',
            compliance_hits_json TEXT NOT NULL DEFAULT '[]',
            analysis_version TEXT NOT NULL DEFAULT '',
            prompt_version TEXT NOT NULL DEFAULT '',
            rule_version TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'visual_review',
            version INTEGER NOT NULL DEFAULT 1,
            preview_path TEXT NOT NULL DEFAULT '',
            output_path TEXT NOT NULL DEFAULT '',
            exported_at TEXT NOT NULL DEFAULT '',
            render_timeline_version TEXT NOT NULL DEFAULT '',
            catalog_item_id INTEGER REFERENCES catalog_items(id),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_candidates_status_created
            ON highlight_candidates(status, created_at DESC);

        CREATE TABLE IF NOT EXISTS catalog_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            internal_code TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            aliases TEXT NOT NULL DEFAULT '',
            qianchuan_product_id TEXT NOT NULL DEFAULT '',
            qianchuan_plan_id TEXT NOT NULL DEFAULT '',
            reference_images TEXT NOT NULL DEFAULT '',
            notes TEXT NOT NULL DEFAULT '',
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS live_rooms (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sequence TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            url TEXT NOT NULL UNIQUE,
            source_key TEXT NOT NULL UNIQUE,
            recorder_prefix TEXT NOT NULL DEFAULT '',
            recorder_suffix TEXT NOT NULL DEFAULT '',
            enabled INTEGER NOT NULL DEFAULT 1,
            archived INTEGER NOT NULL DEFAULT 0,
            review_mode TEXT NOT NULL DEFAULT 'manual',
            default_catalog_item_id INTEGER REFERENCES catalog_items(id),
            notes TEXT NOT NULL DEFAULT '',
            last_detected_at TEXT NOT NULL DEFAULT '',
            last_recording_at TEXT NOT NULL DEFAULT '',
            last_processed_at TEXT NOT NULL DEFAULT '',
            last_error TEXT NOT NULL DEFAULT '',
            live_status TEXT NOT NULL DEFAULT 'unknown',
            live_checked_at TEXT NOT NULL DEFAULT '',
            live_check_error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_rooms_sequence ON live_rooms(sequence);

        CREATE TABLE IF NOT EXISTS publish_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            candidate_id INTEGER NOT NULL UNIQUE REFERENCES highlight_candidates(id) ON DELETE CASCADE,
            room_id INTEGER REFERENCES live_rooms(id),
            catalog_item_id INTEGER REFERENCES catalog_items(id),
            internal_code_snapshot TEXT NOT NULL DEFAULT '',
            qianchuan_product_id_snapshot TEXT NOT NULL DEFAULT '',
            qianchuan_plan_id_snapshot TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'ready',
            scheduled_at TEXT NOT NULL DEFAULT '',
            attempts INTEGER NOT NULL DEFAULT 0,
            error TEXT NOT NULL DEFAULT '',
            external_material_id TEXT NOT NULL DEFAULT '',
            package_path TEXT NOT NULL DEFAULT '',
            handoff_confirmed_at TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_publish_jobs_status ON publish_jobs(status,created_at DESC);

        CREATE TABLE IF NOT EXISTS review_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            candidate_id INTEGER NOT NULL REFERENCES highlight_candidates(id) ON DELETE CASCADE,
            action TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            start_time REAL NOT NULL,
            end_time REAL NOT NULL,
            captions_json TEXT NOT NULL DEFAULT '[]',
            catalog_item_id INTEGER REFERENCES catalog_items(id),
            candidate_version INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS service_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            level TEXT NOT NULL,
            event_type TEXT NOT NULL,
            message TEXT NOT NULL,
            details_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );
        """
        # SQLite resolves referenced table names at statement execution time, but
        # create the catalog table first to keep the schema portable.
        catalog = """
        CREATE TABLE IF NOT EXISTS catalog_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            internal_code TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            aliases TEXT NOT NULL DEFAULT '',
            qianchuan_product_id TEXT NOT NULL DEFAULT '',
            qianchuan_plan_id TEXT NOT NULL DEFAULT '',
            reference_images TEXT NOT NULL DEFAULT '',
            notes TEXT NOT NULL DEFAULT '',
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
        with self.connect() as conn:
            conn.executescript(catalog)
            conn.executescript(schema)
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(highlight_candidates)")}
            migrations = {
                "kept_clauses_json": "TEXT NOT NULL DEFAULT '[]'",
                "removed_clauses_json": "TEXT NOT NULL DEFAULT '[]'",
                "compliance_hits_json": "TEXT NOT NULL DEFAULT '[]'",
                "analysis_version": "TEXT NOT NULL DEFAULT ''",
                "prompt_version": "TEXT NOT NULL DEFAULT ''",
                "rule_version": "TEXT NOT NULL DEFAULT ''",
                "room_id": "INTEGER REFERENCES live_rooms(id)",
                "render_timeline_version": "TEXT NOT NULL DEFAULT ''",
                "exported_at": "TEXT NOT NULL DEFAULT ''",
                "media_cleaned_at": "TEXT NOT NULL DEFAULT ''",
                "media_released_bytes": "INTEGER NOT NULL DEFAULT 0",
                "render_phase": "TEXT NOT NULL DEFAULT ''",
                "render_started_at": "TEXT NOT NULL DEFAULT ''",
                "render_worker": "TEXT NOT NULL DEFAULT ''",
                "render_encoder": "TEXT NOT NULL DEFAULT ''",
            }
            for name, definition in migrations.items():
                if name not in columns:
                    conn.execute(f"ALTER TABLE highlight_candidates ADD COLUMN {name} {definition}")
            conn.execute(
                """UPDATE highlight_candidates SET exported_at=updated_at
                   WHERE status='exported' AND exported_at=''"""
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_candidates_exported_at ON highlight_candidates(exported_at DESC)"
            )
            segment_columns = {row["name"] for row in conn.execute("PRAGMA table_info(recording_segments)")}
            segment_migrations = {
                "room_id": "INTEGER REFERENCES live_rooms(id)",
                "catalog_item_id": "INTEGER REFERENCES catalog_items(id)",
                "transcribed_at": "TEXT NOT NULL DEFAULT ''",
                "model_submitted_at": "TEXT NOT NULL DEFAULT ''",
                "analyzed_at": "TEXT NOT NULL DEFAULT ''",
                "gpt_windows_done_json": "TEXT NOT NULL DEFAULT '[]'",
                "deepseek_windows_done_json": "TEXT NOT NULL DEFAULT '[]'",
            }
            for name, definition in segment_migrations.items():
                if name not in segment_columns:
                    conn.execute(f"ALTER TABLE recording_segments ADD COLUMN {name} {definition}")
            room_columns = {row["name"] for row in conn.execute("PRAGMA table_info(live_rooms)")}
            room_migrations = {
                "live_status": "TEXT NOT NULL DEFAULT 'unknown'",
                "live_checked_at": "TEXT NOT NULL DEFAULT ''",
                "live_check_error": "TEXT NOT NULL DEFAULT ''",
            }
            for name, definition in room_migrations.items():
                if name not in room_columns:
                    conn.execute(f"ALTER TABLE live_rooms ADD COLUMN {name} {definition}")
            publish_columns = {row["name"] for row in conn.execute("PRAGMA table_info(publish_jobs)")}
            publish_migrations = {
                "package_path": "TEXT NOT NULL DEFAULT ''",
                "handoff_confirmed_at": "TEXT NOT NULL DEFAULT ''",
            }
            for name, definition in publish_migrations.items():
                if name not in publish_columns:
                    conn.execute(f"ALTER TABLE publish_jobs ADD COLUMN {name} {definition}")
            segment_columns = {row["name"] for row in conn.execute("PRAGMA table_info(recording_segments)")}
            cleanup_migrations = {
                "cleaned_at": "TEXT NOT NULL DEFAULT ''",
                "released_bytes": "INTEGER NOT NULL DEFAULT 0",
            }
            for name, definition in cleanup_migrations.items():
                if name not in segment_columns:
                    conn.execute(f"ALTER TABLE recording_segments ADD COLUMN {name} {definition}")
            # Keep prior previews and decisions for comparison, but make it clear
            # that they were produced by the superseded continuous-window logic.
            conn.execute(
                """UPDATE highlight_candidates SET status='superseded', updated_at=?
                   WHERE analysis_version='' AND status IN
                   ('visual_review','rendering','pending_review','render_error')""",
                (utc_now(),),
            )
            # Do not infer that a completed segment needs another paid AI pass merely
            # because it produced zero candidates. Zero-output is a valid result.

    def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        with self._lock, self.connect() as conn:
            cursor = conn.execute(sql, tuple(params))
            return int(cursor.lastrowid)

    def execute_changes(self, sql: str, params: Iterable[Any] = ()) -> int:
        """Execute an update and return affected rows, for atomic job claims."""
        with self._lock, self.connect() as conn:
            cursor = conn.execute(sql, tuple(params))
            return int(cursor.rowcount)

    def execute_many(self, sql: str, rows: Iterable[Iterable[Any]]) -> None:
        with self._lock, self.connect() as conn:
            conn.executemany(sql, [tuple(row) for row in rows])

    def one(self, sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(sql, tuple(params)).fetchone()
            return dict(row) if row else None

    def all(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(sql, tuple(params)).fetchall()]

    def event(self, level: str, event_type: str, message: str, details: dict | None = None) -> None:
        self.execute(
            "INSERT INTO service_events(level,event_type,message,details_json,created_at) VALUES(?,?,?,?,?)",
            (level, event_type, message, json.dumps(details or {}, ensure_ascii=False), utc_now()),
        )

    def register_segment(
        self,
        *,
        source_id: str,
        session_id: str,
        path: Path,
        file_hash: str,
        size_bytes: int,
        duration: float,
        room_id: int | None = None,
        catalog_item_id: int | None = None,
    ) -> dict[str, Any]:
        existing = self.one(
            "SELECT * FROM recording_segments WHERE path=? OR file_hash=?",
            (str(path), file_hash),
        )
        if existing:
            return existing
        previous = self.one(
            "SELECT timeline_end FROM recording_segments WHERE session_id=? ORDER BY timeline_end DESC LIMIT 1",
            (session_id,),
        )
        start = float(previous["timeline_end"]) if previous else 0.0
        now = utc_now()
        segment_id = self.execute(
            """INSERT INTO recording_segments
               (source_id,session_id,path,file_hash,size_bytes,duration,timeline_start,timeline_end,status,
                room_id,catalog_item_id,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (source_id, session_id, str(path), file_hash, size_bytes, duration, start, start + duration,
             "discovered", room_id, catalog_item_id, now, now),
        )
        return self.one("SELECT * FROM recording_segments WHERE id=?", (segment_id,)) or {}

    def refresh_grown_segment(
        self, segment_id: int, *, file_hash: str, size_bytes: int, duration: float
    ) -> dict[str, Any]:
        """Reset derived work when a prematurely observed file later grows."""
        now = utc_now()
        with self._lock, self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM recording_segments WHERE id=?", (segment_id,)
            ).fetchone()
            if not row:
                return {}
            conn.execute(
                """UPDATE highlight_candidates SET status='superseded',updated_at=?
                   WHERE session_id=? AND status NOT IN ('superseded','rejected','exported')
                     AND NOT(end_time<=? OR start_time>=?)""",
                (now, row["session_id"], row["timeline_start"], row["timeline_end"]),
            )
            conn.execute("DELETE FROM transcript_spans WHERE segment_id=?", (segment_id,))
            conn.execute(
                """UPDATE recording_segments
                   SET file_hash=?,size_bytes=?,duration=?,timeline_end=timeline_start+?,
                       status='discovered',error='',transcribed_at='',model_submitted_at='',analyzed_at='',
                       gpt_windows_done_json='[]',deepseek_windows_done_json='[]',updated_at=?
                   WHERE id=?""",
                (file_hash, size_bytes, duration, duration, now, segment_id),
            )
        return self.one("SELECT * FROM recording_segments WHERE id=?", (segment_id,)) or {}

    def update_segment_status(self, segment_id: int, status: str, error: str = "") -> None:
        self.execute(
            "UPDATE recording_segments SET status=?,error=?,updated_at=? WHERE id=?",
            (status, error[:2000], utc_now(), segment_id),
        )

    def mark_segment_stage(self, segment_id: int, stage: str) -> None:
        columns = {
            "transcribed": "transcribed_at",
            "model_submitted": "model_submitted_at",
            "analyzed": "analyzed_at",
        }
        column = columns.get(stage)
        if not column:
            raise ValueError(f"unknown segment stage: {stage}")
        now = utc_now()
        self.execute(
            f"UPDATE recording_segments SET {column}=CASE WHEN {column}='' THEN ? ELSE {column} END,updated_at=? WHERE id=?",
            (now, now, segment_id),
        )

    def spans_for_session(self, session_id: str, since: float = 0) -> list[dict[str, Any]]:
        return self.all(
            "SELECT * FROM transcript_spans WHERE session_id=? AND end_time>=? ORDER BY start_time",
            (session_id, since),
        )

    def overlapping_candidates(self, session_id: str, start: float, end: float) -> list[dict[str, Any]]:
        return self.all(
            """SELECT * FROM highlight_candidates
               WHERE session_id=? AND status NOT IN ('superseded','rejected')
                 AND NOT(end_time<=? OR start_time>=?)""",
            (session_id, start, end),
        )

    def active_candidates_for_session(self, session_id: str) -> list[dict[str, Any]]:
        return self.all(
            """SELECT * FROM highlight_candidates
               WHERE session_id=? AND status NOT IN ('superseded','rejected')""",
            (session_id,),
        )


def json_field(row: dict[str, Any], key: str, default: Any) -> Any:
    try:
        return json.loads(row.get(key) or "")
    except (TypeError, json.JSONDecodeError):
        return default
