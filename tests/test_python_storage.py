import json
import os
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from novel_forge.storage import (
    StorageError, Store, arc_context_version, count_chinese_words, manuscript_fingerprint,
    revision_manuscript_state, volume_source_hash,
)


FIXTURE = Path(__file__).with_name("fixtures") / "test_python_storage.json"
SOURCE = "潮门只在退潮时开启。姚青说天亮前回来。"
INITIAL_MEMORY = {"memory": {
    "characterStates": {"姚青": {"seen": True, "currentGoal": "找回船灯", "quote": SOURCE}},
    "constraints": [{"text": "潮门只能在退潮时开启", "quote": SOURCE}],
    "commitments": [{"text": "天亮前回来", "deadline": "次日天亮", "quote": SOURCE}],
    "subplots": [{"text": "调查旧船", "quote": SOURCE}],
}}


class PythonStorageTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.store = Store(Path(directory))

    def test_arc_archive_validates_evidence_with_fail_soft_and_rejects_stale_sources(self):
        job_id = self.store.create_job({"totalChapters": 9})["id"]
        arcs = [{"arcIndex": index, "startChapter": start, "endChapter": end, "arcTitle": "行动", "arcGoal": "取证"}
                for index, start, end in ((1, 1, 3), (2, 4, 6))]
        self.assertEqual(self.store.get_canonical_facts(job_id)["arcSummaries"], [])
        self.store.update_job(job_id, {"arcPlans": arcs})
        story = "港口ＡＢ、换岗完毕。船工掀开暗门，交出了古铜刻印。"
        for chapter in range(1, 10):
            self.store.save_chapter(job_id, chapter, {"content": story, "status": "needs_review" if chapter == 2 else "verified"})
        first = self.store.get_chapters(job_id, start_chapter=1, end_chapter=3)
        second = self.store.get_chapters(job_id, start_chapter=4, end_chapter=6)
        source1, source2 = volume_source_hash(first), volume_source_hash(second)
        raw = {
            "mainline": "门" * 290,
            "turningPoints": [{"chapter": n, "text": "变化"} for n in (1, 2, 3, 2, 7)],
            "usedBeats": [
                {"chapter": 2, "text": "实证换岗", "quote": "港口 AB,换岗完毕."},
                {"chapter": 2, "text": "编造机制", "quote": "根本没有这句"},
                {"chapter": 5, "text": "超出本单元", "quote": "船工掀开暗门"},
            ],
            "usedHooks": [{"chapter": 3, "text": "从实物带出新进展", "quote": "船工掀开暗门……古铜刻印"}],
            "villainStatus": "退" * 130,
        }
        self.assertTrue(self.store.save_arc_summary(job_id, arcs[0], source1, raw))
        summary = self.store.get_canonical_facts(job_id)["arcSummaries"][0]
        self.assertEqual(len(summary["mainline"]), 280)
        self.assertEqual(len(summary["turningPoints"]), 3)
        self.assertEqual(len(summary["villainStatus"]), 120)
        self.assertEqual(summary["needsReviewChapters"], [2])
        self.assertEqual([entry["text"] for entry in summary["usedBeats"]], ["实证换岗"])
        self.assertEqual(len(summary["usedHooks"]), 1)
        self.assertTrue(self.store.save_arc_summary(job_id, arcs[0], source1, {**raw, "mainline": "忽略重复请求"}))
        self.assertEqual(self.store.get_canonical_facts(job_id)["arcSummaries"], [summary])
        self.assertTrue(self.store.save_arc_summary(job_id, arcs[1], source2, {"mainline": "第二单元对峙"}))
        rolled = self.store.delete_uncommitted_facts(job_id, 4)
        self.assertEqual(rolled["arcSummaries"], [summary])
        self.assertTrue(self.store.save_arc_summary(job_id, arcs[1], source2, {"mainline": "第二单元对峙"}))
        self.store.save_chapter(job_id, 2, {"content": "已被作者修改。"})
        self.assertFalse(self.store.save_arc_summary(job_id, arcs[0], source1, raw))
        self.assertEqual([row["arc"] for row in self.store.get_canonical_facts(job_id)["arcSummaries"]], [2])
        self.assertEqual(self.store.get_job_record(job_id)["arcPlans"], arcs)

        self.store.update_job(job_id, {"status": "completed"})
        chapters = self.store.get_chapters(job_id)
        self.store.apply_manuscript_revision(job_id, {
            "revisionId": "edit-archived-chapter", "sourceVersion": manuscript_fingerprint(chapters),
            "changes": [{"chapterNumber": 5, "title": "新章名", "content": "重写归档章节。"}],
        })
        self.assertEqual(self.store.get_canonical_facts(job_id)["arcSummaries"], [])

    def test_arc_plan_transaction_never_keeps_partial_outlines_or_stale_context(self):
        job_id = self.store.create_job({"totalChapters": 5})["id"]
        self.store.save_chapter(job_id, 1, {"title": "旧稿开场", "content": SOURCE})
        self.store.update_job(job_id, {"outlines": [{"chapterNumber": 1, "title": "旧稿开场"}]})
        arc = {"arcIndex": 1, "startChapter": 2, "endChapter": 5, "arcTitle": "港口", "arcGoal": "揭示"}
        outlines = [{"chapterNumber": n, "title": f"归档第{n}章"} for n in range(2, 6)]
        before = self.store.get_job_record(job_id)
        source = arc_context_version(self.store.get_canonical_facts(job_id), self.store.get_chapters(job_id), 2)
        checkpoint = {"stage": "chapter_writing", "nextChapter": 2, "nextOutlineChapter": 6}
        with closing(sqlite3.connect(self.store.path)) as db:
            db.execute("""CREATE TRIGGER fixture_arc_abort BEFORE UPDATE ON jobs
                          BEGIN SELECT RAISE(ABORT, 'arc save failed'); END""")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "arc save failed"):
            self.store.save_arc_plan(job_id, arc, outlines, total=5, expected_context=source, checkpoint=checkpoint)
        self.assertEqual(self.store.get_job_record(job_id), before)
        with closing(sqlite3.connect(self.store.path)) as db:
            db.execute("DROP TRIGGER fixture_arc_abort")
        self.store.save_chapter(job_id, 1, {"title": "旧稿开场", "content": SOURCE + "现在正文有变。"})
        self.assertFalse(self.store.save_arc_plan(job_id, arc, outlines, total=5, expected_context=source, checkpoint=checkpoint))
        source = arc_context_version(self.store.get_canonical_facts(job_id), self.store.get_chapters(job_id), 2)
        self.assertTrue(self.store.save_arc_plan(job_id, arc, outlines, total=5, expected_context=source, checkpoint=checkpoint))
        saved = self.store.get_job_record(job_id)
        self.assertEqual(saved["arcPlans"], [arc])
        self.assertEqual([row["chapterNumber"] for row in saved["outlines"]], [1, 2, 3, 4, 5])
        self.assertEqual(saved["checkpoint"]["nextOutlineChapter"], 6)
        self.assertFalse(self.store.save_arc_plan(job_id, arc, outlines, total=5, expected_context=source, checkpoint=checkpoint))

    def test_word_count_and_existing_config_formats(self):
        self.assertEqual(count_chinese_words(" 甲㐀。ABC_1, xy123 🚢１２ "), 4)
        self.store.config_path.write_text(json.dumps({
            "activeProvider": "openai", "unknown": {"keep": True}, "providers": {
                "openai": {"baseUrl": "https://custom.example/v1", "apiKey": "sk-example-test",
                           "model": "chosen", "manualModels": ["old"], "unknown": [1, 2]},
            },
        }), encoding="utf-8-sig")
        with patch.dict(os.environ, {"OPENAI_API_KEY": "only-use-at-official-endpoint"}):
            public = self.store.get_public_provider_config("openai")
            self.assertTrue(public["hasKey"])
            self.assertNotIn("apiKey", public)
            self.store.save_provider_config({"apiKey": public["maskedKey"], "model": "new"})
            self.assertEqual(self.store.get_provider_config("openai")["apiKey"], "sk-example-test")
            self.store.save_provider_config({"clearApiKey": True})
            self.assertFalse(self.store.get_public_provider_config("openai")["hasKey"])
            self.store.save_provider_config({"baseUrl": "https://api.openai.com/v1"})
            self.assertTrue(self.store.get_public_provider_config("openai")["hasKey"])
        saved = json.loads(self.store.config_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["unknown"], {"keep": True})
        self.assertEqual(saved["providers"]["openai"]["unknown"], [1, 2])
        self.assertEqual(saved["providers"]["openai"]["manualModels"], ["old"])
        self.assertEqual(saved["providers"]["openai"]["apiKey"], "")
        original = self.store.config_path.read_bytes()
        with patch("novel_forge.storage.os.replace", side_effect=OSError("fixture replace failure")):
            with self.assertRaisesRegex(OSError, "fixture replace failure"):
                self.store.save_provider_config({"model": "failed"})
        self.assertEqual(self.store.config_path.read_bytes(), original)
        self.assertEqual(list(self.store.directory.glob("config.json.tmp.*")), [])

    def test_concurrent_writes_share_latest_job_and_config(self):
        job_id = self.store.create_job({"totalChapters": 24})["id"]
        peers = [Store(self.store.directory) for _ in range(4)]

        def write(number):
            peer = peers[number % len(peers)]
            peer.update_job(job_id, lambda job: {"counter": job.get("counter", 0) + 1})
            peer.save_chapter(job_id, number, {"content": "夜航"})
            provider = "openai" if number % 2 else "gemini"
            peer.save_provider_config({"provider": provider, "model": f"model-{number}"})

        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(write, range(1, 25)))
        job = self.store.get_job_record(job_id)
        self.assertEqual((job["counter"], job["completedChapters"], job["totalWords"]), (24, 24, 48))
        self.assertEqual([chapter["chapterNumber"] for chapter in self.store.get_chapters(job_id)], list(range(1, 25)))
        self.assertTrue(self.store.get_provider_config("openai")["model"].startswith("model-"))
        self.assertTrue(self.store.get_provider_config("gemini")["model"].startswith("model-"))

    def test_verified_write_rolls_back_both_payloads_and_needs_review_preserves_facts(self):
        job_id = self.store.create_job({"totalChapters": 2})["id"]
        with closing(sqlite3.connect(self.store.path)) as db:
            db.execute("""CREATE TRIGGER fixture_abort BEFORE UPDATE ON jobs
                          BEGIN SELECT RAISE(ABORT, 'fixture abort'); END""")
        before = self.store.get_job_record(job_id)
        with self.assertRaisesRegex(sqlite3.IntegrityError, "fixture abort"):
            self.store.save_verified_chapter(job_id, 1, {"content": SOURCE}, INITIAL_MEMORY, long_memory=True,
                                             meme_title="同一梗")
        self.assertIsNone(self.store.get_chapter(job_id, 1))
        self.assertEqual(self.store.get_job_record(job_id), before)
        with closing(sqlite3.connect(self.store.path)) as db:
            db.execute("DROP TRIGGER fixture_abort")

        self.store.save_verified_chapter(job_id, 1, {"content": SOURCE}, INITIAL_MEMORY, long_memory=True,
                                         meme_title="同一梗")
        before = self.store.get_canonical_facts(job_id)
        self.store.save_verified_chapter(job_id, 2, {"content": SOURCE, "status": "needs_review"},
                                         INITIAL_MEMORY, long_memory=True, meme_title="另一梗")
        self.store.save_verified_chapter(job_id, 2, {"content": SOURCE, "status": "needs_review"},
                                         INITIAL_MEMORY, long_memory=True, meme_title="另一梗")
        self.assertEqual(self.store.get_job_record(job_id)["completedChapters"], 2)
        self.assertEqual(self.store.get_job_record(job_id)["usedMemeTitles"], ["同一梗", "另一梗"])
        self.assertEqual(self.store.get_canonical_facts(job_id), before)
        self.assertEqual(self.store.save_checkpoint(job_id, {"stage": "writing"})["stage"], "writing")
        self.store.append_job_log(job_id, {"message": "已落库"})
        self.store.append_repair_log(job_id, {"chapterNumber": 2, "issues": ["待审"]})
        job = self.store.get_job_record(job_id)
        self.assertIn("savedAt", job["checkpoint"])
        self.assertEqual(job["logs"][-1]["message"], "已落库")
        self.assertEqual(job["repairLogs"][-1]["chapterNumber"], 2)

    def test_active_injury_merges_by_side_and_rollback_restores_legacy_baseline(self):
        job_id = self.store.create_job({"totalChapters": 20})["id"]
        left = {"id": "legacy-left", "character": "姚青", "description": "左前臂骨折", "active": True,
                "severity": "high", "sourceChapter": 3}
        later = {"id": "legacy-later", "character": "船医", "description": "胸口旧伤", "sourceChapter": 14}
        self.store.update_job(job_id, {"canonicalFacts": {"injuries": [left, later]}})

        def commit(number, *changes):
            self.store.save_verified_chapter(job_id, number, {"content": SOURCE}, {"injuries": list(changes)})
            return {entry["id"]: entry for entry in self.store.get_canonical_facts(job_id)["injuries"]}

        changed = commit(8, {"character": "姚青", "bodyPart": "左臂", "description": "左臂骨折已固定"},
                         {"id": "new-right", "character": "姚青", "description": "右臂重伤", "severity": "medium"})
        self.assertEqual(set(changed), {"legacy-left", "legacy-later", "new-right"})
        self.assertEqual((changed["legacy-left"]["sourceChapter"], changed["legacy-left"]["updatedAtChapter"]), (3, 8))
        self.assertEqual(changed["legacy-left"]["severity"], "high")
        healed = commit(12, {"character": "姚青", "bodyPart": "左臂", "active": False})
        self.assertFalse(healed["legacy-left"]["active"])
        self.assertTrue(healed["new-right"]["active"])
        self.assertEqual(healed["legacy-left"]["healedChapter"], 12)

        reopened = Store(self.store.directory)
        restored = reopened.delete_uncommitted_facts(job_id, 12)["injuries"]
        self.assertEqual({entry["id"]: entry for entry in restored},
                         {key: changed[key] for key in ("legacy-left", "new-right")})
        self.assertEqual(reopened.delete_uncommitted_facts(job_id, 8)["injuries"], [left])

    def test_replacing_a_chapter_delta_removes_its_previous_injury_update(self):
        job_id = self.store.create_job({"totalChapters": 4})["id"]
        chapter = {"content": SOURCE}
        first = {"character": "姚青", "description": "左肩脱臼", "severity": "medium"}
        original = self.store.save_verified_chapter(job_id, 1, chapter, {"injuries": [first]})["canonicalFacts"]["injuries"]
        self.store.save_verified_chapter(job_id, 3, chapter, {"injuries": [
            {"id": original[0]["id"], "character": "姚青", "active": False},
        ]})
        self.store.save_chapter(job_id, 3, {"content": SOURCE + "潮水再次涌来。"})
        self.assertFalse(self.store.get_canonical_facts(job_id)["injuries"][0]["active"])
        current = self.store.save_verified_chapter(job_id, 3, chapter, {})["canonicalFacts"]["injuries"]
        self.assertEqual(current, original)

    def test_memory_replay_and_archive_invalidation(self):
        job_id = self.store.create_job({"totalChapters": 20})["id"]
        self.store.update_job(job_id, lambda job: {"canonicalFacts": {
            key: value for key, value in job["canonicalFacts"].items()
            if key not in ("characterStates", "constraints", "commitments", "subplots", "volumeSummaries")
        }})
        self.assertEqual(self.store.get_canonical_facts(job_id)["characterStates"], {})
        for number in range(1, 21):
            self.store.save_chapter(job_id, number, {"content": SOURCE})
        raw = {"memory": {**INITIAL_MEMORY["memory"], "constraints": [
            *INITIAL_MEMORY["memory"]["constraints"], {"text": "虚构约束", "quote": "不存在"}, None,
        ]}}
        first = self.store.save_verified_chapter(job_id, 3, {"content": SOURCE}, raw, long_memory=True)["canonicalFacts"]
        self.assertEqual(len(first["constraints"]), 1)
        self.assertEqual(first["constraints"][0]["sourceChapter"], 3)
        self.store.commit_chapter_facts(job_id, 11, {"memory": {
            "characterStates": {"姚青": {"seen": True, "currentGoal": "准备离港", "quote": SOURCE}},
            "commitments": [{"id": first["commitments"][0]["id"], "status": "fulfilled", "quote": SOURCE}],
            "subplots": [{"id": first["subplots"][0]["id"], "status": "resolved", "quote": SOURCE}],
        }}, long_memory=True, chapter_text=SOURCE)
        chapters = self.store.get_chapters(job_id)
        original_hash = volume_source_hash(chapters[:10])
        self.assertTrue(self.store.save_volume_summary(job_id, 10, original_hash, {"mainline": "打开潮门"}))
        self.assertTrue(self.store.save_volume_summary(job_id, 20, volume_source_hash(chapters[10:]), {"mainline": "离港"}))
        earlier = self.store.get_canonical_facts(job_id)
        self.store.save_chapter(job_id, 3, {"content": SOURCE + "大雾升起。"})
        edited = self.store.get_canonical_facts(job_id)
        self.assertEqual(edited["factHistory"], earlier["factHistory"])
        self.assertEqual([volume["volume"] for volume in edited["volumeSummaries"]], [2])
        self.assertFalse(self.store.save_volume_summary(job_id, 10, original_hash, {"mainline": "过期"}))
        latest = volume_source_hash(self.store.get_chapters(job_id)[:10])
        self.assertTrue(self.store.save_volume_summary(job_id, 10, latest, {"mainline": "雾中打开潮门"}))
        rolled_back = Store(self.store.directory).delete_uncommitted_facts(job_id, 11)
        self.assertEqual([entry["chapter"] for entry in rolled_back["factHistory"]], [3])
        self.assertEqual(rolled_back["characterStates"]["姚青"]["currentGoal"], "找回船灯")
        self.assertEqual(rolled_back["commitments"][0]["status"], "active")
        self.assertEqual(rolled_back["subplots"][0]["status"], "active")
        self.assertEqual([volume["volume"] for volume in rolled_back["volumeSummaries"]], [1])
        replaced = self.store.save_verified_chapter(job_id, 3, {"content": SOURCE},
                                                    {"memory": "invalid"}, long_memory=True)["canonicalFacts"]
        self.assertEqual(replaced["characterStates"], {})
        self.assertEqual(replaced["constraints"], [])

    def test_apply_revision_validates_fingerprint_and_is_atomic_and_idempotent(self):
        job_id = self.store.create_job({"totalChapters": 2})["id"]
        self.store.save_verified_chapter(job_id, 2, {"content": SOURCE}, INITIAL_MEMORY, long_memory=True)
        self.store.save_chapter(job_id, 1, {"content": SOURCE, "status": "needs_review", "verification": {"passed": False}})
        job = self.store.update_job(job_id, {"status": "completed", "evaluationReport": {"test": True}})
        before = self.store.get_chapters(job_id)
        self.assertEqual(revision_manuscript_state(job, before), "ready")
        self.assertEqual(revision_manuscript_state(job, before[1:]), "incomplete")
        patch_ = {"chapterNumber": 1, "title": "新标题", "content": "正文已润色。"}
        candidate = {"revisionId": "candidate", "sourceVersion": manuscript_fingerprint(before), "changes": [patch_]}
        for value, busy, error in [
            ({**candidate, "changes": [patch_, patch_]}, False, "REVISION_INVALID_CHAPTER"),
            ({**candidate, "sourceVersion": "stale"}, False, "REVISION_SOURCE_CHANGED"),
            (candidate, lambda _: True, "REVISION_JOB_BUSY"),
        ]:
            with self.assertRaises(StorageError) as caught:
                self.store.apply_manuscript_revision(job_id, value, busy)
            self.assertEqual(caught.exception.code, error)
            self.assertEqual(self.store.get_chapters(job_id), before)
        history = self.store.get_canonical_facts(job_id)["factHistory"]
        self.store.apply_manuscript_revision(job_id, candidate)
        chapter = self.store.get_chapter(job_id, 1)
        self.assertEqual((chapter["content"], chapter["status"], chapter["verification"]),
                         ("正文已润色。", "needs_review", {"passed": False}))
        applied = self.store.get_job_record(job_id)
        self.assertIsNone(applied["evaluationReport"])
        self.assertEqual(applied["canonicalFacts"]["factHistory"], history)
        self.store.apply_manuscript_revision(job_id, candidate)
        self.assertEqual(self.store.get_job_record(job_id), applied)

    def test_legacy_json_import_preserves_camel_case_and_runs_once(self):
        legacy = self.store.directory / "legacy"
        directory = legacy / "jobs" / "existing" / "chapters"
        directory.mkdir(parents=True)
        job = {"id": "existing", "title": "原稿", "updatedAt": "2026-01-01T00:00:00.000Z",
               "llmConfig": {"provider": "openai", "apiKey": "sk-private-legacy"}, "unknown": {"k": True}}
        chapter = {"chapterNumber": 1, "title": "雾港", "content": SOURCE, "status": "completed"}
        job_file = legacy / "jobs" / "existing.json"
        job_file.write_text(json.dumps(job), encoding="utf-8")
        (directory / "1.json").write_text(json.dumps(chapter), encoding="utf-8")
        store = Store(legacy)
        self.assertEqual(store.get_job_record("existing"), job)
        self.assertEqual(store.get_chapter("existing", 1), chapter)
        self.assertEqual(store.get_job("existing")["llmConfig"]["apiKey"], "sk-p****gacy")
        job_file.write_text(json.dumps({**job, "title": "重复旧档"}), encoding="utf-8")
        self.assertEqual(Store(legacy).get_job_record("existing"), job)

    def test_opens_legacy_database_and_preserves_fingerprints(self):
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        job = fixture["job"]
        legacy_id = job["id"]
        with closing(sqlite3.connect(self.store.path)) as db, db:
            db.execute("INSERT INTO jobs VALUES (?, ?)", (legacy_id, json.dumps(job)))
            db.executemany("INSERT INTO chapters VALUES (?, ?, ?)", [
                (legacy_id, row["chapterNumber"], json.dumps(row)) for row in fixture["chapters"]
            ])
        reopened = Store(self.store.directory)
        rows = reopened.get_chapters(legacy_id)
        self.assertEqual(reopened.get_job_record(legacy_id)["title"], fixture["title"])
        self.assertEqual(reopened.get_job_record(legacy_id)["completedChapters"], 2)
        self.assertEqual(rows, fixture["chapters"])
        self.assertEqual(rows[1]["status"], "needs_review")
        self.assertEqual(manuscript_fingerprint(rows[:1]), fixture["fingerprint"])
        self.assertEqual(volume_source_hash(rows[:1]), fixture["archiveHash"])
        facts = reopened.get_canonical_facts(legacy_id)
        self.assertEqual([entry["chapter"] for entry in facts["factHistory"]], [1])

        reopened.save_verified_chapter(legacy_id, 3, {**rows[0], "status": "needs_review"}, INITIAL_MEMORY, long_memory=True)
        self.assertEqual(reopened.get_job_record(legacy_id)["completedChapters"], 3)
        self.assertEqual(reopened.get_canonical_facts(legacy_id), facts)
