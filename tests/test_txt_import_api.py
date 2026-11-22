import io
import os
import sqlite3
import tempfile
import time
import unittest
import xml.etree.ElementTree as ET
import zipfile
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from novel_forge.app import create_app
from novel_forge.storage import StorageError, Store, count_chinese_words


FIRST = "门房听见钟响。\n\n他把信留在抽屉里。"
SECOND = "夜半，来客敲了三次门。"
MANUSCRIPT = f"《雾港旧信》\n\n第1章 潮声\n\n{FIRST}\n\n第2章 值夜\n\n{SECOND}"
IMPORT = "/api/jobs/import-txt"
WORD = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


class TxtImportApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, {
            "NOVEL_FORGE_AUTO_RECOVER": "0",
            "NOVEL_FORGE_BACKGROUND_FEEDS": "0",
        }))
        self.client = self.enterContext(TestClient(create_app(self.directory)))

    def import_book(self, text=MANUSCRIPT, **options):
        result = self.client.post(IMPORT, json={"text": text, **options})
        self.assertEqual(result.status_code, 201, result.text)
        return result.json()

    def test_imported_manuscript_is_committed_readable_and_exportable(self):
        result = self.import_book("\ufeff" + MANUSCRIPT.replace("\n", "\r\n"), splitMode="auto")
        job = result["job"]
        total_words = sum(map(count_chinese_words, (FIRST, SECOND)))
        self.assertEqual(result["inferredTitle"], "雾港旧信")
        self.assertEqual(result["detectedBy"], "headings")
        self.assertEqual((result["totalChapters"], result["totalWords"]), (2, total_words))
        self.assertIsInstance(result["warnings"], list)
        self.assertEqual(job["title"], "雾港旧信")
        self.assertEqual(job["status"], "completed")
        self.assertEqual((job["totalChapters"], job["completedChapters"], job["totalWords"]), (2, 2, total_words))
        self.assertIsNone(job["checkpoint"])
        self.assertIsNone(job.get("auditGate"))
        self.assertIsNone(job.get("evaluationReport"))
        self.assertEqual(job["canonicalFacts"]["factHistory"], [])
        self.assertEqual(job["tokenUsage"]["totalTokens"], 0)

        base = f"/api/jobs/{job['id']}"
        jobs = self.client.get("/api/jobs").json()["jobs"]
        self.assertEqual([item["id"] for item in jobs], [job["id"]])
        response = self.client.get(base)
        self.assertEqual(response.status_code, 200, response.text)
        chapters = response.json()["chapters"]
        self.assertEqual([(row["chapterNumber"], row["title"], row["content"], row["status"]) for row in chapters], [
            (1, "第1章 潮声", FIRST, "completed"),
            (2, "第2章 值夜", SECOND, "completed"),
        ])
        self.assertTrue(all(row.get("verification") is None for row in chapters))
        self.assertEqual(self.client.get(base + "/chapters/2").json()["chapter"], chapters[1])

        txt = self.client.get(base + "/export?format=txt")
        self.assertEqual(txt.status_code, 200, txt.text)
        self.assertIn("attachment", txt.headers["content-disposition"])
        self.assertIn("《雾港旧信》", txt.text)
        self.assertEqual(txt.text.count("第1章 潮声"), 1)
        self.assertIn(FIRST, txt.text)
        self.assertIn(SECOND, txt.text)
        copied = self.import_book(txt.text)
        self.assertEqual(copied["totalWords"], total_words)
        copied_chapters = self.client.get(f"/api/jobs/{copied['job']['id']}").json()["chapters"]
        self.assertEqual([(row["title"], row["content"]) for row in copied_chapters],
                         [(row["title"], row["content"]) for row in chapters])

        docx = self.client.get(base + "/export?format=docx")
        self.assertEqual(docx.status_code, 200)
        self.assertEqual(int(docx.headers["x-export-size-bytes"]), len(docx.content))
        ns = {"w": WORD}
        with zipfile.ZipFile(io.BytesIO(docx.content)) as package:
            document = ET.fromstring(package.read("word/document.xml"))
        paragraphs = document.findall("w:body/w:p", ns)
        heading_texts = []
        for paragraph in paragraphs:
            style = paragraph.find("w:pPr/w:pStyle", ns)
            if style is not None and style.get(f"{{{WORD}}}val") == "Heading1":
                heading_texts.append("".join(node.text or "" for node in paragraph.findall(".//w:t", ns)))
        self.assertEqual(heading_texts, ["第1章 潮声", "第2章 值夜"])
        document_text = "".join(node.text or "" for node in document.findall(".//w:t", ns))
        self.assertIn("门房听见钟响。", document_text)
        self.assertIn(SECOND, document_text)

    def test_single_chapter_title_and_mock_evaluation_and_revision(self):
        config = self.client.put("/api/config/providers", json={
            "provider": "openai", "model": "fixture-editor", "apiKey": "unexposed-fixture-key",
        })
        self.assertEqual(config.status_code, 200, config.text)
        text = "\n\n".join((
            "归来的船靠上堤岸。他握着信封，没有立刻拆开。船家把船系在木桩上，说这一阵风来得急，灯船今夜恐怕不会回港。"
            "他借了船家的油灯，顺着石阶往上走。信封一角被雨打湿，露出半枚水蓝色的邮戳。",
            "守门人正把晾在廊下的网搬进仓库。他问钟楼可曾开门，守门人将一串钥匙搁在桌上，让他自己去看。"
            "前几个晚上也有人来找那口旧钟，留下了一张收据，角落里写着卸货的栈桥号。",
            "他用袖口擦亮灯罩，在账房的木柜底下找到一只布袋。袋子里没有钱，只装着三张码头工人的领班名签。"
            "其中一张已被刮掉姓名，背面画了两道细线，恰好对应退潮后露出来的航标。",
            "总而言之，雨停以前，他得找到能辨认邮戳的人。泊船处传来一声短哨，巡夜人正沿栈桥挨个点名。",
        ))
        result = self.import_book(f"《旧稿原名》\n\n{text}", title="编辑给定的书名", splitMode="single")
        self.assertEqual(result["inferredTitle"], "旧稿原名")
        self.assertEqual(result["job"]["title"], "编辑给定的书名")
        connection = result["job"]["llmConfig"]
        self.assertEqual(connection["model"], "fixture-editor")
        self.assertTrue(connection["useSavedConnection"])
        self.assertNotIn("apiKey", connection)
        self.assertEqual((result["detectedBy"], result["totalChapters"]), ("single", 1))
        base = f"/api/jobs/{result['job']['id']}"
        source = self.client.get(base + "/chapters/1").json()["chapter"]
        self.assertEqual(source["content"], text)
        model = {"provider": "mock", "model": "mock-novel-v1", "useSavedConnection": False}
        evaluation = self.client.post(base + "/evaluate", json={"mode": "full", **model})
        self.assertEqual(evaluation.status_code, 200, evaluation.text)
        self.assertEqual(evaluation.json()["evaluationReport"]["totalChapters"], 1)
        self.assertEqual(evaluation.json()["job"]["evaluationStatus"], "completed")

        start = self.client.post(base + "/revisions", json={"editor": model, "verifier": model})
        self.assertEqual(start.status_code, 202, start.text)
        revision = start.json()["revision"]
        self.assertEqual(revision["chapterCount"], 1)
        deadline = time.monotonic() + 10
        while revision["status"] == "running" and time.monotonic() < deadline:
            time.sleep(0.03)
            revision = self.client.get(base + "/revisions").json()["revision"]
        self.assertIn(revision["status"], ("ready", "needs_review"), revision)
        self.assertEqual(self.client.get(base + "/chapters/1").json()["chapter"]["content"], text)

        refined = self.client.post(base + "/de-ai", json={"chapterNumbers": [1], **model})
        self.assertEqual(refined.status_code, 200, refined.text)
        self.assertEqual([row["chapterNumber"] for row in refined.json()["updatedChapters"]], [1])
        self.assertEqual(refined.json()["job"]["chapterIndex"]["1"]["status"], "repaired")
        fixed = self.client.get(base + "/chapters/1").json()["chapter"]
        self.assertEqual(fixed["status"], "repaired")
        self.assertNotIn("总而言之，", fixed["content"])

    def test_invalid_input_creates_no_jobs_or_chapters(self):
        for payload in ({}, {"text": None}, {"text": 42}, {"text": " \r\n\ufeff\n\t"},
                        {"text": FIRST, "splitMode": "unknown"}, {"text": FIRST, "splitMode": "chunk", "chunkWords": 0}):
            with self.subTest(payload=payload):
                response = self.client.post(IMPORT, json=payload)
                self.assertEqual(response.status_code, 400, response.text)
                self.assertTrue(response.json()["error"])
        self.assertEqual(self.client.get("/api/jobs").json()["jobs"], [])
        with closing(sqlite3.connect(self.directory / "novel-forge.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM chapters").fetchone()[0], 0)

    def test_midway_chapter_write_failure_rolls_back_new_manuscript(self):
        original = self.import_book(text=FIRST, title="原有作品", splitMode="single")["job"]
        original_writer = Store._write_chapter
        writes = []

        def interrupted(db, job_id, chapter):
            writes.append(chapter["chapterNumber"])
            if chapter["chapterNumber"] == 2:
                raise StorageError("模拟第二章写入失败", 500, "IMPORT_WRITE_TEST")
            original_writer(db, job_id, chapter)

        with patch.object(Store, "_write_chapter", staticmethod(interrupted)):
            response = self.client.post(IMPORT, json={"text": MANUSCRIPT})
        self.assertEqual(response.status_code, 500, response.text)
        self.assertEqual(writes, [1, 2])
        jobs = self.client.get("/api/jobs").json()["jobs"]
        self.assertEqual([job["id"] for job in jobs], [original["id"]])
        with closing(sqlite3.connect(self.directory / "novel-forge.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT job_id, number FROM chapters").fetchall(), [(original["id"], 1)])
