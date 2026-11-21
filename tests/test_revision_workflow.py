import json
import os
import select
import socket
import sqlite3
import threading
import time
import unittest
from contextlib import closing
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from novel_forge.app import create_app
from novel_forge.revision_checks import fingerprint
from novel_forge.revision_store import RevisionStore
from novel_forge.storage import Store


STAMP = "2025-01-01T00:00:00.000Z"


def seed(store, revision, job_id, *, complete, status="completed", total=1, llm_config=None):
    original = {"chapterNumber": 1, "title": "第1章", "content": "当年丢失的书信终于送到。"}
    candidate = {**original, "content": "他拆开了那封尘封多年的书信。"}
    report = {"evaluatedAt": "2025-01-02T00:00:00.000Z", "totalChapters": 1, "mode": "full"}
    job = {
        "id": job_id, "title": "历史修订验收", "status": status, "updatedAt": STAMP, "createdAt": STAMP,
        "genre": "悬疑", "pov": "third", "totalChapters": total, "completedChapters": 1,
        "totalWords": len(original["content"]), "chapterIndex": {"1": original},
        "llmConfig": llm_config or {"provider": "mock", "model": "mock-novel-v1"},
        "evaluationStatus": "completed", "evaluationReport": report,
    }
    if status == "failed":
        job["error"] = {"code": "AUDIT_GATE_FAILED", "message": "完本终审未通过"}
    chapter = {**original, "updatedAt": STAMP, "createdAt": STAMP}
    with closing(sqlite3.connect(store.path)) as db, db:
        db.execute("INSERT INTO chapters VALUES(?, ?, ?)", (job_id, 1, json.dumps(chapter, ensure_ascii=False)))
        db.execute("INSERT INTO jobs VALUES(?, ?)", (job_id, json.dumps(job, ensure_ascii=False)))

    run = "legacy-" + job_id
    revision.create(run, job_id, {"title": job["title"], "chapters": [original], "report": report}, {
        "stage": "finish" if complete else "edit", "done": 1, "total": 1, "chapterCount": 1,
        "changedCount": int(complete), "blockingCount": 0, "volumes": [{"number": 1, "start": 1, "end": 1}],
        "hasCandidate": complete, "hasLedger": True,
    })
    revision.save(run, "ledger", "all", {"rules": [], "checks": [], "financialFlows": [], "unresolved": []})
    if complete:
        unit = {"id": "1", "volume": 1, "chapters": [1]}
        revision.save(run, "map", "all", {"edit": [unit], "audit": [unit], "blind": []})
        revision.save(run, "audit", "1", {"unsupported": []})
        revision.save(run, "edit", "1", {"changed": [candidate], "log": []})
        revision.save(run, "final", "all", {"changedCount": 1, "blocking": [], "outcomes": [], "findings": []})
        revision.update(run, "ready")
    return run


class RevisionArchiveTest(unittest.TestCase):
    def test_pause_cancels_provider_http_request(self):
        received, disconnected = threading.Event(), threading.Event()

        class SlowProvider(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                received.set()
                readable, _, _ = select.select([self.connection], [], [], 8)
                if readable and self.connection.recv(1, socket.MSG_PEEK) == b"":
                    disconnected.set()

        with ThreadingHTTPServer(("127.0.0.1", 0), SlowProvider) as provider:
            thread = threading.Thread(target=provider.serve_forever, daemon=True)
            thread.start()
            try:
                with TemporaryDirectory(ignore_cleanup_errors=os.name == "nt") as temp, patch.dict(os.environ, {
                    "NOVEL_FORGE_AUTO_RECOVER": "0", "NOVEL_FORGE_BACKGROUND_FEEDS": "0",
                }):
                    directory = Path(temp)
                    seed(Store(directory), RevisionStore(directory / "revision.sqlite3"), "slow", complete=False,
                         llm_config={"provider": "openai", "model": "fixture-slow", "apiKey": "local-fixture-key",
                                     "baseUrl": f"http://127.0.0.1:{provider.server_port}/v1"})
                    with TestClient(create_app(directory)) as client:
                        base = "/api/jobs/slow/revisions"
                        response = client.post(base, json={})
                        self.assertEqual(response.status_code, 202, response.text)
                        self.assertTrue(received.wait(6))
                        run = response.json()["revision"]["id"]
                        paused = client.post(f"{base}/{run}/pause", json={})
                        self.assertEqual(paused.status_code, 200, paused.text)
                        self.assertEqual(paused.json()["revision"]["status"], "paused")
                        self.assertTrue(disconnected.wait(4), "The model HTTP request was still open.")
            finally:
                provider.shutdown()
                thread.join(3)

    def test_saved_manuscript_is_revisable_after_failed_audit(self):
        with TemporaryDirectory(ignore_cleanup_errors=os.name == "nt") as temp, patch.dict(os.environ, {
            "NOVEL_FORGE_AUTO_RECOVER": "0", "NOVEL_FORGE_BACKGROUND_FEEDS": "0",
        }):
            directory = Path(temp)
            database = Store(directory)
            archive = RevisionStore(directory / "revision.sqlite3")
            seed(database, archive, "full", complete=False, status="failed")
            seed(database, archive, "short", complete=False, status="failed", total=2)
            seed(database, archive, "paused-writing", complete=False, status="paused")
            with TestClient(create_app(directory)) as client:
                for job_id, code in (("short", "REVISION_CHAPTERS"), ("paused-writing", "REVISION_JOB_BUSY")):
                    result = client.post(f"/api/jobs/{job_id}/revisions", json={})
                    self.assertEqual(result.status_code, 409, result.text)
                    self.assertEqual(result.json()["code"], code)
                base = "/api/jobs/full"
                start = client.post(base + "/revisions", json={})
                self.assertEqual(start.status_code, 202, start.text)
                deadline = time.monotonic() + 12
                while time.monotonic() < deadline:
                    revision = client.get(base + "/revisions").json()["revision"]
                    if revision["status"] != "running":
                        break
                    time.sleep(0.06)
                self.assertEqual(revision["status"], "ready", revision)
                old = client.get(base).json()
                chapter = old["chapters"][0]
                client.portal.call(partial(client.app.state.worker.request, "revision-apply", jobId="full", payload={
                    "revisionId": "writeback-fixture", "sourceVersion": fingerprint([chapter]),
                    "changes": [{**chapter, "content": "终审失败后修订的正文。"}],
                }))
                current = client.get(base).json()
                self.assertEqual(current["chapters"][0]["content"], "终审失败后修订的正文。")
                self.assertEqual(current["job"]["status"], "failed")
                self.assertEqual(current["job"]["error"]["code"], "AUDIT_GATE_FAILED")
                with self.assertRaisesRegex(RuntimeError, "正文在修订期间发生变化"):
                    client.portal.call(partial(client.app.state.worker.request, "revision-apply", jobId="full", payload={
                        "revisionId": "stale-fixture", "sourceVersion": fingerprint([chapter]),
                        "changes": [{**chapter, "content": "过期候选稿。"}],
                    }))

    def test_candidate_assessment_and_editor_retrieval_use_local_adapters(self):
        requests = []

        def provider(request):
            self.assertEqual(request.headers["Authorization"], "Bearer fixture-editor-key")
            payload = json.loads(request.content)
            context = json.loads(payload["messages"][-1]["content"])
            requests.append(context)
            if not context["evidence"]:
                answer = {"tools": [{"kind": "read", "version": "current", "chapter": 1}]}
            else:
                self.assertIn("当年丢失的书信终于送到", json.dumps(context["evidence"], ensure_ascii=False))
                answer = {"reply": "第 1 章交代了书信送达。", "citations": [context["evidence"][0]["id"]]}
            return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(answer)}, "finish_reason": "stop"}]})

        with TemporaryDirectory(ignore_cleanup_errors=os.name == "nt") as temp, patch.dict(os.environ, {
            "NOVEL_FORGE_AUTO_RECOVER": "0", "NOVEL_FORGE_BACKGROUND_FEEDS": "0",
        }):
            directory = Path(temp)
            database = Store(directory)
            seed(database, RevisionStore(directory / "revision.sqlite3"), "book", complete=False)
            database.save_provider_config({"provider": "openai", "baseUrl": "http://127.0.0.1:9001/v1",
                                           "model": "fixture-editor", "apiKey": "fixture-editor-key"})
            upstream = httpx.AsyncClient(transport=httpx.MockTransport(provider))
            with TestClient(create_app(directory, client=upstream)) as client:
                client.portal.call(upstream.__aenter__)
                try:
                    base = "/api/jobs/book"
                    revision_path = base + "/revisions"
                    run = client.post(revision_path, json={}).json()["revision"]["id"]
                    deadline = time.monotonic() + 8
                    while time.monotonic() < deadline:
                        revision = client.get(revision_path).json()["revision"]
                        if revision["status"] != "running":
                            break
                        time.sleep(0.03)
                    self.assertEqual(revision["status"], "ready", revision)
                    path = f"{revision_path}/{run}/round-evaluations/{revision['candidateTurn']}"
                    queued = client.post(path, json={"action": "start"})
                    self.assertEqual(queued.status_code, 202, queued.text)
                    deadline = time.monotonic() + 8
                    while time.monotonic() < deadline:
                        result = client.get(path).json()
                        if result["status"] != "running":
                            break
                        time.sleep(0.03)
                    self.assertEqual(result["status"], "completed", result)
                    self.assertEqual(result["report"]["totalChapters"], 1)
                    self.assertEqual(client.get(base).json()["job"]["evaluationReport"]["evaluatedAt"],
                                     "2025-01-02T00:00:00.000Z")

                    settings = client.put(base + "/editor-desk/settings", json={
                        "model": {"provider": "openai", "model": "fixture-editor"},
                    })
                    self.assertEqual(settings.status_code, 200, settings.text)
                    answer = client.post(base + "/editor-desk/chat", json={"message": "第一章交代了什么？", "requestId": "test-probe"})
                    self.assertEqual(answer.status_code, 200, answer.text)
                    self.assertIn("第 1 章交代了书信送达。", answer.text)
                    self.assertEqual(len(requests), 2)
                finally:
                    client.portal.call(upstream.aclose)

    def test_v1_is_read_only_and_cannot_block_v2_start(self):
        with TemporaryDirectory(ignore_cleanup_errors=os.name == "nt") as temp, patch.dict(os.environ, {
            "NOVEL_FORGE_AUTO_RECOVER": "0", "NOVEL_FORGE_BACKGROUND_FEEDS": "0", "NOVEL_FORGE_REVISION_V2": "0",
        }):
            directory = Path(temp)
            database = Store(directory)
            archive = RevisionStore(directory / "revision.sqlite3")
            complete = seed(database, archive, "complete", complete=True)
            paused = seed(database, archive, "paused", complete=False)
            prior = {run: archive.get(run) for run in (complete, paused)}

            with TestClient(create_app(directory)) as client:
                for job_id, run in (("complete", complete), ("paused", paused)):
                    base = f"/api/jobs/{job_id}/revisions"
                    self.assertEqual(client.get(base).json(), {"revision": None, "legacyRevision": prior[run]})
                    self.assertEqual(client.get(f"{base}/{run}/export/original").status_code, 200)
                    self.assertEqual(client.get(f"{base}/{run}/export/evaluation").status_code, 200)
                    for action in ("resume", "pause", "archive", "apply", "repair", "decide", "restore"):
                        response = client.post(f"{base}/{run}/{action}", json={})
                        self.assertEqual(response.status_code, 410, response.text)
                        self.assertEqual(response.json()["code"], "REVISION_V1_RETIRED")

                archived_candidate = client.get(f"/api/jobs/complete/revisions/{complete}/export/candidate")
                self.assertIn("尘封多年的书信", archived_candidate.text)
                self.assertIn("修订记录", client.get(f"/api/jobs/complete/revisions/{complete}/export/report").text)

                base = "/api/jobs/paused/revisions"
                response = client.post(base, json={"concurrency": 1})
                self.assertEqual(response.status_code, 202, response.text)
                self.assertEqual(response.json()["revision"]["workflowVersion"], 2)
                deadline = time.monotonic() + 12
                while time.monotonic() < deadline:
                    detail = client.get(base).json()
                    if detail["revision"]["status"] != "running":
                        break
                    time.sleep(0.06)
                self.assertEqual(detail["revision"]["status"], "ready", detail)
                self.assertEqual(detail["legacyRevision"], prior[paused])
                self.assertEqual(client.get(f"{base}/{paused}/export/original").status_code, 200)
            self.assertEqual({run: archive.get(run) for run in prior}, prior)

