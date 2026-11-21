import io
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
import zipfile
from contextlib import closing
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


class FastApiBackendTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="novel-forge-fastapi-")
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.client = httpx.Client(timeout=30, trust_env=False)
        self.addCleanup(self.client.close)
        self.process = None
        self.log = self.directory / "backend.log"
        self.addCleanup(self.stop_server)

    def start_server(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        self.base = f"http://127.0.0.1:{port}"
        with self.log.open("ab") as log:
            self.process = subprocess.Popen(
                [sys.executable, "-B", "-m", "novel_forge"], cwd=ROOT, stdout=log, stderr=log,
                env={**os.environ, "PORT": str(port), "NOVEL_FORGE_HOST": "127.0.0.1",
                     "NOVEL_FORGE_DATA_DIR": str(self.directory),
                     "NOVEL_FORGE_BACKGROUND_FEEDS": "0", "NOVEL_FORGE_AUTO_RECOVER": "1"},
            )
        for _ in range(400):
            if self.process.poll() is not None:
                break
            try:
                health = self.client.get(self.base + "/api/health", timeout=0.3)
                if health.headers.get("x-novel-forge-backend") == "fastapi":
                    return
            except httpx.TransportError:
                pass
            time.sleep(0.05)
        self.fail(self.log.read_text(encoding="utf-8", errors="replace"))

    def stop_server(self):
        if self.process and self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=10)
            if os.name == "nt":
                # Windows can release file handles shortly after a forced process exit.
                time.sleep(0.2)

    def request(self, method, path, json_body=None, *, status=200):
        response = self.client.request(method, self.base + path, json=json_body)
        self.assertEqual(response.status_code, status, response.text[:1000])
        return response

    def create_job(self, n=4, scenario=None, **fields):
        result = self.request("POST", "/api/jobs", {
            "title": "Python 迁移验收", "premise": "主角追踪旧城钟楼的青铜钥匙",
            "targetWords": n * 900, "wordsPerChapter": 900, "totalChapters": n,
            "llmConfig": {"provider": "mock", "model": "mock-novel-v1"},
            "testScenario": {"mode": "normal", "runId": uuid.uuid4().hex, **(scenario or {})},
            **fields,
        }, status=201)
        return "/api/jobs/" + result.json()["job"]["id"]

    def wait_for(self, path, condition):
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            detail = self.request("GET", path).json()
            if condition(detail):
                return detail
            if detail["job"]["status"] == "failed":
                self.fail(str(detail["job"]["error"]))
            time.sleep(0.035)
        self.fail(self.log.read_text(encoding="utf-8", errors="replace"))

    def test_react_api_and_chapter_repair(self):
        self.start_server()
        self.assertIn("<html", self.request("GET", "/").text)
        self.assertTrue(self.request("GET", "/api/hotspots").json()["tropes"])
        self.request("GET", "/api/memes")
        self.request("GET", "/api/config/providers?provider=mock")
        concept = self.request("POST", "/api/hotspots/concept", {
            "keywords": "重生 商战", "llmConfig": {"provider": "mock"},
        }).json()
        self.assertTrue(concept["premise"])

        job_url = self.create_job(
            scenario={"mode": "fault-injection", "faultChapter": 2},
            llmConfig={"provider": "mock", "apiKey": "test-key-for-masking"},
            enableEvaluation=True,
            autoStart=False,
        )
        snapshot = self.request("GET", job_url + "/workflow").json()
        self.assertIsNone(snapshot["activeNode"])
        self.assertEqual(snapshot["jobStatus"], "pending")
        with self.client.stream("GET", self.base + job_url + "/workflow/stream") as events:
            self.assertEqual(events.status_code, 200)
            self.assertIn("text/event-stream", events.headers["content-type"])
            frames = (json.loads(line[6:]) for line in events.iter_lines() if line.startswith("data: "))
            first = next(frames)
            self.assertIsNone(first["activeNode"])
            self.request("POST", job_url + "/resume")
            saw_repair = False
            last_revision = first["revision"]
            for frame in frames:
                self.assertGreater(frame["revision"], last_revision)
                self.assertNotIn("test-key-for-masking", json.dumps(frame))
                last_revision = frame["revision"]
                if frame["activeNode"] == "repair":
                    self.assertEqual(frame["activeChapter"], 2)
                    saw_repair = True
                if not frame["runActive"] and frame["jobStatus"] == "completed":
                    self.assertIsNone(frame["activeNode"])
                    break
            self.assertTrue(saw_repair)
        with self.client.stream("GET", self.base + job_url + "/stream") as events:
            self.assertEqual(events.status_code, 200)
            self.assertIn("text/event-stream", events.headers["content-type"])
            lines = events.iter_lines()
            self.assertEqual(next(lines), "event: snapshot")
            self.assertIn("test****king", next(lines))

        detail = self.wait_for(job_url, lambda d: d["job"]["status"] == "completed")
        self.assertTrue(detail["job"]["auditGate"]["passed"])
        self.assertTrue(detail["job"]["evaluationReport"])
        self.assertGreater(detail["chapters"][1]["repairCount"], 0)
        self.assertEqual(detail["job"]["llmConfig"]["apiKey"], "test****king")
        self.assertEqual(len(detail["chapters"]), 4)
        self.assertTrue(self.request("GET", "/api/jobs").json()["jobs"])

        chapter_url = job_url + "/chapters/1"
        chapter = self.request("GET", chapter_url).json()["chapter"]
        self.request("PUT", chapter_url, {"content": chapter["content"] + "\n\n验收编辑。", "reverify": False})
        self.assertIn("验收编辑。", self.request("GET", chapter_url).json()["chapter"]["content"])

        self.assertIn("Python 迁移验收", self.request("GET", job_url + "/export?format=txt").text)
        self.assertIn("# Python 迁移验收", self.request("GET", job_url + "/export?format=md").text)
        docx = self.request("GET", job_url + "/export?format=docx")
        self.assertIn("attachment", docx.headers["content-disposition"])
        self.assertEqual(int(docx.headers["x-export-size-bytes"]), len(docx.content))
        self.assertEqual(docx.headers["x-fanqie-within-limit"], "true")
        with zipfile.ZipFile(io.BytesIO(docx.content)) as package:
            self.assertIn("word/document.xml", package.namelist())

        with closing(sqlite3.connect(self.directory / "workflow.sqlite3")) as db:
            self.assertGreater(db.execute("SELECT count(*) FROM checkpoints").fetchone()[0], 12)
        with closing(sqlite3.connect(self.directory / "novel-forge.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM chapters").fetchone()[0], 4)

        self.request("POST", job_url + "/extend", {"addChapters": 2, "direction": "调查新线索"})
        extended = self.wait_for(job_url, lambda d: d["job"]["status"] == "completed" and len(d["chapters"]) == 6)
        self.assertEqual(extended["chapters"][1]["content"], detail["chapters"][1]["content"])

    def test_direct_api_contracts_for_config_feeds_and_evaluation(self):
        self.start_server()
        raw_key = "test-private-provider-key"
        config = self.request("PUT", "/api/config/providers", {
            "provider": "vertex", "baseUrl": "https://aiplatform.googleapis.com/v1",
            "apiKey": raw_key, "model": "fixture-model",
        })
        self.assertNotIn(raw_key, config.text)
        self.assertEqual(config.json()["activeProvider"], "vertex")
        model = self.request("PUT", "/api/model-catalog/vertex", {
            "models": ["fixture-model"], "selectedModel": "fixture-model",
        }).json()["provider"]
        self.assertEqual(model["selectedModel"], "fixture-model")
        self.assertEqual(self.request("GET", "/api/model-catalog").json()["activeProvider"], "vertex")
        discover = self.client.post(self.base + "/api/model-catalog/vertex/discover")
        self.assertEqual(discover.json()["code"], "MODEL_DISCOVERY_UNSUPPORTED")
        for path in ("/api/providers/test", "/api/config/test-connection"):
            self.assertTrue(self.request("POST", path, {"provider": "mock"}).json()["ok"])

        feed = self.request("POST", "/api/hotspots/refresh", {
            "sourceName": "模拟榜单", "customItems": [{"title": "雾港轮渡", "summary": "班次恢复"}],
        }).json()
        self.assertTrue(feed["ok"])
        self.assertEqual(self.request("GET", "/api/hotspots?query=雾港").json()["liveItems"][0]["title"], "雾港轮渡")
        memes = self.request("POST", "/api/memes/sync", {
            "limit": 1, "verifyDetails": False, "customItems": [{
                "slug": "fixture-ferry", "title": "轮渡梗", "updated_at": "2026-11-09T00:00:00Z",
                "earliestSpreadAt": "2026-11-09T00:00:00Z",
            }], "now": "2026-11-10T00:00:00Z",
        }).json()
        self.assertEqual(memes["counts"]["all"], 1)
        self.assertEqual(self.request("GET", "/api/memes?limit=5000").json()["limit"], 500)
        self.assertEqual(self.request("GET", "/api/memes/fixture-ferry").json()["item"]["title"], "轮渡梗")
        self.assertEqual(self.request("GET", "/api/memes/missing", status=404).json()["error"], "Meme not found")
        self.assertEqual(self.request("GET", "/api/absent", status=404).json()["error"], "API route not found")

        book = self.create_job(n=3)
        self.wait_for(book, lambda item: item["job"]["status"] == "completed")
        response = self.request("POST", book + "/evaluate", {"mode": "full", "background": True}, status=202).json()
        self.assertTrue(response["started"])
        finished = self.wait_for(book, lambda item: item["job"]["evaluationStatus"] == "completed")
        self.assertEqual(finished["job"]["evaluationReport"]["totalChapters"], 3)
        chapter = finished["chapters"][0]
        edited = self.request("PUT", book + "/chapters/1", {
            "content": chapter["content"] + "\n\n总而言之，雾港尚未天亮。", "reverify": True, "recheckDownstream": True,
        }).json()
        self.assertTrue(edited["verification"]["passed"])
        self.assertEqual(edited["downstreamAudit"]["checkedCount"], 2)
        polished = self.request("POST", book + "/de-ai", {"provider": "mock", "chapterNumbers": [1]}).json()
        self.assertEqual([item["chapterNumber"] for item in polished["updatedChapters"]], [1])
        self.assertNotIn("总而言之，", self.request("GET", book + "/chapters/1").json()["chapter"]["content"])

    def test_pause_and_timeout_resume(self):
        self.start_server()
        job_url = self.create_job(scenario={"mode": "timeout-once", "failAtChapter": 3}, autoStart=False)
        self.assertEqual(self.request("GET", job_url).json()["job"]["status"], "pending")
        paused = self.request("POST", job_url + "/pause").json()["job"]
        self.assertEqual(paused["status"], "paused")
        self.request("PATCH", job_url + "/config", {"llmConfig": {"timeoutMs": 1800}})
        self.request("POST", job_url + "/resume")
        before = self.wait_for(job_url, lambda d: d["job"]["status"] == "interrupted")
        self.assertEqual(before["job"]["error"]["code"], "TIMEOUT")
        self.assertEqual(len(before["chapters"]), 2)
        restarted = self.request("POST", job_url + "/resume").json()["job"]
        self.assertIn(restarted["status"], ("running", "completed"))
        after = self.wait_for(job_url, lambda d: d["job"]["status"] == "completed")
        self.assertEqual(after["chapters"][:2], before["chapters"])
        self.assertEqual(len(after["chapters"]), 4)

    def test_recovers_after_process_kill(self):
        self.start_server()
        job_url = self.create_job(n=16)
        before = self.wait_for(job_url, lambda d: len(d["chapters"]) >= 2 and d["job"]["status"] == "running")
        self.stop_server()
        self.start_server()
        after = self.wait_for(job_url, lambda d: d["job"]["status"] == "completed")
        self.assertEqual(after["chapters"][:len(before["chapters"])], before["chapters"])
        self.assertEqual([c["chapterNumber"] for c in after["chapters"]], list(range(1, 17)))
        self.assertEqual(len(after["job"]["canonicalFacts"]["factHistory"]), 16)
        self.assertTrue(any("服务重启前" in log["message"] for log in after["job"]["logs"]))

    def test_builtin_acceptance_suites(self):
        self.start_server()
        result = self.request("POST", "/api/test/run-acceptance", {"suite": "all"}).json()
        self.assertTrue(result["passed"], json.dumps(result, ensure_ascii=False))
        self.assertEqual(len(result["results"]), 6)

    def test_legacy_jobs_are_imported(self):
        chapters = self.directory / "jobs" / "job_legacy" / "chapters"
        chapters.mkdir(parents=True)
        old_job = self.directory / "jobs" / "job_legacy.json"
        source = json.dumps({"id": "job_legacy", "title": "旧项目", "status": "completed"}, ensure_ascii=False)
        old_job.write_text(source, encoding="utf-8")
        (chapters / "ch_0001.json").write_text('{"chapterNumber":1,"content":"旧小说"}', encoding="utf-8")
        self.start_server()
        detail = self.request("GET", "/api/jobs/job_legacy").json()
        self.assertEqual(detail["job"]["title"], "旧项目")
        self.assertEqual(detail["chapters"][0]["content"], "旧小说")
        self.assertEqual(old_job.read_text(encoding="utf-8"), source)
