"""Durable snapshots and isolated work products for manuscript revision."""

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from uuid import uuid4

from .revision_checks import compact_json


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class RevisionStore:
    def __init__(self, path):
        self.path = path
        with closing(self.connect()) as db, db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS revision_runs (
                    id TEXT PRIMARY KEY, job_id TEXT NOT NULL,
                    created_at TEXT NOT NULL, status TEXT NOT NULL,
                    snapshot TEXT NOT NULL, metadata TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS revision_jobs ON revision_runs(job_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS revision_units (
                    run_id TEXT NOT NULL REFERENCES revision_runs(id),
                    kind TEXT NOT NULL, slot TEXT NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(run_id, kind, slot)
                );
            """)
            db.execute(
                """UPDATE revision_runs SET status='paused'
                   WHERE status='running' AND json_extract(metadata, '$.workflowVersion')=2"""
            )

    def connect(self):
        db = sqlite3.connect(self.path, timeout=8)
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def create(self, run_id, job_id, snapshot, metadata):
        with closing(self.connect()) as db, db:
            db.execute("INSERT INTO revision_runs VALUES(?, ?, ?, 'paused', ?, ?)",
                       (run_id, job_id, timestamp(), compact_json(snapshot), compact_json(metadata)))

    def get(self, run_id):
        with closing(self.connect()) as db:
            row = db.execute("SELECT job_id,status,metadata FROM revision_runs WHERE id=?", (run_id,)).fetchone()
        return {"id": run_id, "jobId": row[0], "status": row[1], **json.loads(row[2])} if row else None

    def latest(self, job_id):
        with closing(self.connect()) as db:
            row = db.execute(
                "SELECT id FROM revision_runs WHERE job_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1", (job_id,)
            ).fetchone()
        return self.get(row[0]) if row else None

    def latest_archive(self, job_id):
        with closing(self.connect()) as db:
            row = db.execute(
                """SELECT id FROM revision_runs
                   WHERE job_id=? AND COALESCE(json_extract(metadata, '$.workflowVersion'), 1)=1
                   ORDER BY created_at DESC, rowid DESC LIMIT 1""",
                (job_id,),
            ).fetchone()
        return self.get(row[0]) if row else None

    def record_failure(self, run_id, message, diagnostic, *, code, http_status=None, details=None):
        record = self.get(run_id)
        products = []
        event_id = None
        if not record.get("isMock"):
            event = {
                "stage": record.get("stage"), "done": record.get("done"), "total": record.get("total"),
                "ledgerVersion": record.get("ledgerVersion"), "candidateTurn": record.get("candidateTurn"),
                "message": diagnostic, "code": code, "httpStatus": http_status, "occurredAt": timestamp(),
                "details": details or {}, "afterRecovery": bool(record.get("recovery")),
            }
            event_id = uuid4().hex
            products.append(("editor-failure", event_id, event))
        self.commit_revision(run_id, products=products, status="error", error=message, pauseRequested=False,
                             failureEvent=event_id)
        return record["jobId"]

    def backfill_editor_failures(self):
        """Queue each book's current failed run once, including failures from before this release."""
        with closing(self.connect()) as db, db:
            db.execute("""
                INSERT OR IGNORE INTO revision_units(run_id, kind, slot, payload)
                SELECT r.id, 'editor-failure', 'legacy', json_object(
                    'stage', json_extract(r.metadata, '$.stage'), 'done', json_extract(r.metadata, '$.done'),
                    'total', json_extract(r.metadata, '$.total'), 'ledgerVersion', json_extract(r.metadata, '$.ledgerVersion'),
                    'candidateTurn', json_extract(r.metadata, '$.candidateTurn'),
                    'message', CASE WHEN json_extract(r.metadata, '$.error') LIKE '资金台账%'
                                    THEN json_extract(r.metadata, '$.error')
                                    ELSE '请查看修订面板中的原始报错。' END,
                    'occurredAt', json_extract(r.metadata, '$.updatedAt')
                )
                FROM revision_runs r
                WHERE r.status='error' AND json_extract(r.metadata, '$.workflowVersion')=2
                  AND COALESCE(json_extract(r.metadata, '$.isMock'), 0)=0
                  AND r.id=(SELECT latest.id FROM revision_runs latest WHERE latest.job_id=r.job_id
                            ORDER BY latest.created_at DESC, latest.rowid DESC LIMIT 1)
                  AND NOT EXISTS (SELECT 1 FROM revision_units u WHERE u.run_id=r.id AND u.kind='editor-failure')
                """)
        return self.pending_editor_failures()

    def pending_editor_failures(self, job_id=None):
        with closing(self.connect()) as db:
            rows = db.execute("""
                SELECT r.job_id, u.run_id, u.slot, u.payload
                FROM revision_units u JOIN revision_runs r ON r.id=u.run_id
                WHERE u.kind='editor-failure' AND json_extract(u.payload, '$.delivered') IS NULL
                  AND (? IS NULL OR r.job_id=?)
                ORDER BY u.rowid
                """, (job_id, job_id)).fetchall()
        return [(job, run, slot, json.loads(payload)) for job, run, slot, payload in rows]

    def latest_editor_failure(self, run_id):
        with closing(self.connect()) as db:
            row = db.execute("""
                SELECT slot, payload FROM revision_units WHERE run_id=? AND kind='editor-failure'
                ORDER BY json_extract(payload, '$.occurredAt') DESC, rowid DESC LIMIT 1
                """, (run_id,)).fetchone()
        return (row[0], json.loads(row[1])) if row else None

    def update(self, run_id, status=None, **patch):
        with closing(self.connect()) as db, db:
            row = db.execute("SELECT status,metadata FROM revision_runs WHERE id=?", (run_id,)).fetchone()
            data = {**json.loads(row[1]), **patch, "updatedAt": timestamp()}
            db.execute("UPDATE revision_runs SET status=?,metadata=? WHERE id=?",
                       (status or row[0], compact_json(data), run_id))

    def snapshot(self, run_id):
        with closing(self.connect()) as db:
            row = db.execute("SELECT snapshot FROM revision_runs WHERE id=?", (run_id,)).fetchone()
        return json.loads(row[0])

    def load(self, run_id, kind, slot):
        with closing(self.connect()) as db:
            row = db.execute(
                "SELECT payload FROM revision_units WHERE run_id=? AND kind=? AND slot=?", (run_id, kind, slot)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def save(self, run_id, kind, slot, value):
        with closing(self.connect()) as db, db:
            db.execute("INSERT OR REPLACE INTO revision_units VALUES(?, ?, ?, ?)",
                       (run_id, kind, slot, compact_json(value)))

    def count(self, run_id, kind):
        with closing(self.connect()) as db:
            return db.execute("SELECT COUNT(*) FROM revision_units WHERE run_id=? AND kind=?", (run_id, kind)).fetchone()[0]

    def products(self, run_id, kind):
        with closing(self.connect()) as db:
            rows = db.execute("SELECT payload FROM revision_units WHERE run_id=? AND kind=?", (run_id, kind)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def commit_revision(self, run_id, *, expected=None, products=(), status=None, **patch):
        """Publish products and their workflow state together, rejecting old inputs."""
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            old_status, metadata = db.execute(
                "SELECT status,metadata FROM revision_runs WHERE id=?", (run_id,),
            ).fetchone()
            current = json.loads(metadata)
            if expected and any(current.get(key) != value for key, value in expected.items()):
                return False
            db.executemany(
                "INSERT OR REPLACE INTO revision_units VALUES (?, ?, ?, ?)",
                [(run_id, kind, str(slot), compact_json(value)) for kind, slot, value in products],
            )
            current.update(patch, updatedAt=timestamp())
            db.execute("UPDATE revision_runs SET status=?,metadata=? WHERE id=?",
                       (status or old_status, compact_json(current), run_id))
        return True
