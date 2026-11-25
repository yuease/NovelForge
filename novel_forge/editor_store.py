"""Per-novel editorial conversations, model choices and compressed context."""

import json
import sqlite3
from contextlib import closing
from secrets import token_bytes

from .revision_checks import compact_json
from .revision_store import timestamp


class EditorStore:
    def __init__(self, path):
        self.path = path
        with closing(self.connect()) as db, db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS editor_settings (
                    job_id TEXT PRIMARY KEY, model TEXT
                );
                CREATE TABLE IF NOT EXISTS editor_turns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, request_id TEXT NOT NULL,
                    question TEXT NOT NULL, answer TEXT NOT NULL, created_at TEXT NOT NULL,
                    UNIQUE(job_id, request_id)
                );
                CREATE INDEX IF NOT EXISTS editor_history ON editor_turns(job_id, id DESC);
                CREATE TABLE IF NOT EXISTS editor_context (
                    job_id TEXT PRIMARY KEY, through_id INTEGER NOT NULL, summary TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS editor_signing_key (
                    id INTEGER PRIMARY KEY CHECK(id = 1), secret BLOB NOT NULL
                );
            """)
            db.execute("INSERT OR IGNORE INTO editor_signing_key VALUES (1, ?)", (token_bytes(32),))
            self.secret = db.execute("SELECT secret FROM editor_signing_key WHERE id = 1").fetchone()[0]

    def connect(self):
        return sqlite3.connect(self.path, timeout=8)

    def settings(self, job_id):
        with closing(self.connect()) as db:
            row = db.execute("SELECT model FROM editor_settings WHERE job_id = ?", (job_id,)).fetchone()
        return {"model": json.loads(row[0]) if row and row[0] else None}

    def save_model(self, job_id, model):
        with closing(self.connect()) as db, db:
            db.execute("""
                INSERT INTO editor_settings(job_id, model) VALUES (?, ?)
                ON CONFLICT(job_id) DO UPDATE SET model = excluded.model
                """, (job_id, compact_json(model)))
        return {"model": model}

    @staticmethod
    def turn_view(row):
        turn, question, answer, created_at = row
        return {"id": turn, "question": question, "answer": json.loads(answer), "createdAt": created_at}

    def history(self, job_id, before=None, limit=20):
        with closing(self.connect()) as db:
            rows = db.execute("""
                SELECT id,question,answer,created_at FROM editor_turns
                WHERE job_id = ? AND (? IS NULL OR id < ?) ORDER BY id DESC LIMIT ?
                """, (job_id, before, before, limit + 1)).fetchall()
        return {"turns": [self.turn_view(row) for row in reversed(rows[:limit])], "hasOlder": len(rows) > limit}

    def turn(self, job_id, request_id):
        with closing(self.connect()) as db:
            row = db.execute("SELECT id,question,answer,created_at FROM editor_turns WHERE job_id = ? AND request_id = ?",
                             (job_id, request_id)).fetchone()
        return self.turn_view(row) if row else None

    def context(self, job_id):
        with closing(self.connect()) as db:
            db.execute("BEGIN")
            through, summary = db.execute("SELECT through_id,summary FROM editor_context WHERE job_id = ?",
                                          (job_id,)).fetchone() or (0, "")
            recent = db.execute("""
                SELECT id,question,answer,created_at FROM editor_turns
                WHERE job_id = ? AND id > ? ORDER BY id DESC LIMIT 6
                """, (job_id, through)).fetchall()[::-1]
            earlier = db.execute("""
                SELECT id,question,answer,created_at FROM editor_turns
                WHERE job_id = ? AND id > ? AND id < ? ORDER BY id LIMIT 8
                """, (job_id, through, recent[0][0])).fetchall() if recent else []
        # Keep the first few older turns verbatim until there is enough material to compact.
        if len(earlier) < 4:
            recent = earlier + recent
            earlier = []
        return summary, [self.turn_view(row) for row in recent], [self.turn_view(row) for row in earlier]

    def save_turn(self, job_id, request_id, question, answer, compacted):
        with closing(self.connect()) as db, db:
            db.execute("INSERT INTO editor_turns(job_id,request_id,question,answer,created_at) VALUES (?,?,?,?,?)",
                       (job_id, request_id, question, compact_json(answer), timestamp()))
            if compacted:
                through, summary = compacted
                db.execute("""
                    INSERT INTO editor_context VALUES (?, ?, ?)
                    ON CONFLICT(job_id) DO UPDATE SET through_id = excluded.through_id, summary = excluded.summary
                    WHERE excluded.through_id > editor_context.through_id
                    """, (job_id, through, summary))
        return self.turn(job_id, request_id)

    def mark_dispatched(self, job_id, request_id):
        with closing(self.connect()) as db, db:
            db.execute("""
                UPDATE editor_turns SET answer = json_set(answer, '$.plan.dispatched', json('true'))
                WHERE job_id = ? AND request_id = ? AND json_type(answer, '$.plan') = 'object'
                """, (job_id, request_id))

    def mark_recovery_dispatched(self, job_id, request_id):
        with closing(self.connect()) as db, db:
            db.execute("""
                UPDATE editor_turns SET answer = json_set(answer, '$.recoveryPlan.dispatched', json('true'))
                WHERE job_id = ? AND request_id = ? AND json_type(answer, '$.recoveryPlan') = 'object'
                """, (job_id, request_id))

