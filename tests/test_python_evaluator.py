import asyncio
import json
import re
import tempfile
import unittest
from pathlib import Path

import httpx

from novel_forge import evaluator
from novel_forge.llm import DEFAULT_MAX_TOKENS, reset_provider_circuit_state
from novel_forge.storage import Store


FIXTURE = Path(__file__).with_name("fixtures") / "test_python_evaluator.json"
_JOB_MODEL = {"provider": "openai", "baseUrl": "http://127.0.0.1:7139/v1", "model": "fixture", "maxRetries": 0}
_CHAPTER = "傍晚时分，钟楼投下逆光。\n海风掠过铜钥匙，许吟从台阶边抬起头。\n"


def reply(data, finish="stop"):
    text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    return httpx.Response(200, json={"choices": [{"message": {"content": text}, "finish_reason": finish}]})


def _verdict(issue=None):
    return {"overallSummary": "投稿前先收束两个悬念。", "aiFlavorIssues": [issue] if issue else [],
            "sixDimensions": {entry["id"]: {"score": 4.5} for entry in evaluator.SIX_DIMENSION_DEFS},
            "rewritePlan": ["检查第3章的重复旁白"]}


def _request(request):
    body = json.loads(request.content)
    messages = body["messages"]
    system, prompt = [message["content"] for message in messages]
    found = re.search(r"请审读第\s+(\d+)~(\d+)\s+章全文", prompt)
    span = f"{found[1]}-{found[2]}" if found else "pro"
    return body["model"], body["max_tokens"], span, system, prompt


def _batch(span, issues=None):
    return {"batchRange": span, "timelineSummary": f"通过的批段 {span}", "aiFlavorIssues": issues or []}


class EvaluationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.store = Store(Path(directory))
        reset_provider_circuit_state()
        self.addCleanup(reset_provider_circuit_state)

    def book(self, count=6, *, provider="openai"):
        job = self.store.create_job({"title": "夜航测试", "totalChapters": count, "llmConfig": {**_JOB_MODEL, "provider": provider}})
        for number in range(1, count + 1):
            self.store.save_chapter(job["id"], number, {"title": f"第{number}章 甲板", "content": _CHAPTER * 12,
                                                        "summary": f"进展{number}", "status": "needs_review" if number == 2 else "completed"})
        return job["id"]

    async def client(self, handler):
        return await self.enterAsyncContext(httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    async def test_two_stage_evaluation_only_expands_the_truncated_segment(self):
        job_id = self.book()
        requests, progress = [], []
        initial_budget = 32_768
        issue = {"chapterNumber": 4, "category": "formulaic_reaction", "quote": "钟楼投下逆光", "priority": "P1"}

        def handle(request):
            model, budget, span, system, prompt = _request(request)
            requests.append((model, budget, span))
            if span == "pro":
                self.assertIn("通过的批段 1-3", prompt)
                self.assertIn("通过的批段 4-6", prompt)
                return reply(_verdict({**issue, "priority": "P0"}))
            if span == "4-6" and budget == initial_budget:
                return reply('{"timelineSummary":', "length")
            return reply({**_batch(span, [issue] if span == "4-6" else []), "batchRange": span.replace("-", "–")})

        result = await evaluator.evaluate_book(self.store, job_id, batch_size=3, flash_model="a", pro_model="b",
                                               on_progress=progress.append, client=await self.client(handle),
                                               flash_max_tokens=initial_budget)
        self.assertEqual(requests, [
            ("a", initial_budget, "1-3"), ("a", initial_budget, "4-6"),
            ("a", DEFAULT_MAX_TOKENS, "4-6"), ("b", DEFAULT_MAX_TOKENS, "pro"),
        ])
        self.assertTrue(any("正在自动重跑当前段" in entry for entry in progress))
        report = result["evaluationReport"]
        self.assertEqual(report["totalChapters"], 6)
        self.assertEqual([row["batchRange"] for row in report["batchSummaries"]], ["1-3", "4-6"])
        self.assertEqual([issue["priority"] for issue in report["aiFlavorIssues"]], ["P0"])
        self.assertEqual(report["models"], {"flash": "openai/a", "pro": "openai/b"})
        self.assertIsNone(self.store.get_job_record(job_id)["evaluationCheckpoint"])

    async def test_batch_failure_resume_reuses_durable_batches_and_allows_new_model(self):
        job_id = self.book(9)
        requests = []
        fail = True

        def handle(request):
            model, budget, span, system, _ = _request(request)
            self.assertEqual(budget, DEFAULT_MAX_TOKENS)
            requests.append((model, span))
            if span == "4-6" and fail:
                return reply('{"batchRange":"1-3", "timelineSummary":"错误章号", "aiFlavorIssues":[]}')
            return reply(_batch(span))

        client = await self.client(handle)
        with self.assertRaises(evaluator.EvaluationError) as caught:
            await evaluator.evaluate_book(self.store, job_id, mode="flash_only", batch_size=3, flash_model="first", client=client)
        self.assertEqual(caught.exception.code, "EVALUATION_INVALID_RESULT")
        saved = self.store.get_job_record(job_id)
        self.assertEqual(saved["evaluationStatus"], "error")
        self.assertEqual([entry["batchRange"] for entry in saved["evaluationCheckpoint"]["batchResults"]], ["1-3"])
        self.assertFalse(evaluator.is_book_evaluation_running(job_id, store=self.store))
        fail = False
        reopened = Store(self.store.directory)
        result = await evaluator.evaluate_book(reopened, job_id, resume_evaluation=True, flash_model="next", client=client)
        self.assertEqual(requests, [("first", "1-3"), ("first", "4-6"), ("first", "4-6"), ("next", "4-6"), ("next", "7-9")])
        self.assertEqual(result["evaluationReport"]["models"], {"flash": "openai/first、openai/next", "pro": None})

    async def test_restart_recovery_and_source_hash_mismatch(self):
        job_id = self.book(3)
        self.store.update_job(job_id, {"evaluationStatus": "running", "evaluationCheckpoint": {
            "version": 1, "sourceHash": "stale", "mode": "full", "totalBatches": 1, "batchResults": [],
        }})
        await evaluator.recover_interrupted_evaluations(self.store)
        stalled = self.store.get_job_record(job_id)
        self.assertEqual(stalled["evaluationStatus"], "error")
        self.assertIn("已保存", stalled["evaluationProgress"])
        with self.assertRaises(evaluator.EvaluationError) as caught:
            await evaluator.evaluate_book(self.store, job_id, resume_evaluation=True)
        self.assertEqual(caught.exception.code, "EVALUATION_CHECKPOINT_MISMATCH")
        self.assertEqual(self.store.get_job_record(job_id)["evaluationCheckpoint"], stalled["evaluationCheckpoint"])

    async def test_candidate_resume_emits_progress_without_changing_job_or_chapters(self):
        job_id = self.book(1)
        before = self.store.get_job_record(job_id), self.store.get_chapters(job_id)
        candidate = [{"chapterNumber": number, "title": f"修订第{number}章", "content": _CHAPTER * 2}
                     for number in range(1, 13)]
        fail = True
        calls, patches = [], []

        def handle(request):
            _, _, span, _, prompt = _request(request)
            self.assertIn("修订第", prompt)
            calls.append(span)
            if span == "11-12" and fail:
                return reply("无效格式")
            return reply(_verdict() if span == "pro" else _batch(span))

        client = await self.client(handle)
        options = {"chapters": candidate, "flash": {"provider": "openai", "model": "flash"},
                   "pro": {"provider": "openai", "model": "pro"}, "progress": patches.append, "client": client}
        with self.assertRaises(evaluator.EvaluationError) as caught:
            await evaluator.evaluate_candidate(self.store, job_id, **options)
        self.assertEqual(caught.exception.code, "EVALUATION_INVALID_RESULT")
        checkpoint = [patch["evaluationCheckpoint"] for patch in patches if patch.get("evaluationCheckpoint")][-1]
        self.assertEqual(evaluator.checkpoint_status(checkpoint)["completedBatches"], 1)
        fail = False
        report = await evaluator.evaluate_candidate(self.store, job_id, **options, checkpoint=checkpoint)
        self.assertEqual(calls, ["1-10", "11-12", "11-12", "11-12", "pro"])
        self.assertEqual(report["totalChapters"], 12)
        self.assertEqual(before, (self.store.get_job_record(job_id), self.store.get_chapters(job_id)))
        with self.assertRaises(evaluator.EvaluationError) as caught:
            await evaluator.evaluate_candidate(self.store, job_id, **{**options, "chapters": candidate[1:]})
        self.assertEqual(caught.exception.code, "EVALUATION_CANDIDATE")
        candidate[0]["content"] += "修订稿又有更新。"
        with self.assertRaises(evaluator.EvaluationError) as caught:
            await evaluator.evaluate_candidate(self.store, job_id, **options, checkpoint=checkpoint)
        self.assertEqual(caught.exception.code, "EVALUATION_CHECKPOINT_MISMATCH")

    async def test_legacy_failed_batch_repair_then_pro_only(self):
        job_id = self.book()
        job = self.store.get_job_record(job_id)
        chapters = self.store.get_chapters(job_id)
        self.store.update_job(job_id, {"evaluationReport": {
            "sourceHash": evaluator.evaluation_source(job, chapters), "mode": "flash_only", "batchSize": 3,
            "batchSummaries": [{"batchRange": "1-3", "timelineSummary": "已完成的旧初审"},
                               {"batchRange": "4-6", "timelineSummary": "JSON 解析失败（已保留原始片段）"}],
            "aiFlavorIssues": [], "models": {"flash": "openai/old"},
        }})
        calls = []

        def handle(request):
            _, _, span, _, prompt = _request(request)
            calls.append(span)
            if span == "pro":
                self.assertIn("已完成的旧初审", prompt)
                return reply(_verdict())
            return reply(_batch(span))

        client = await self.client(handle)
        repaired = await evaluator.evaluate_book(self.store, job_id, repair_failed_batches=True, flash_model="new", client=client)
        self.assertEqual(repaired["evaluationReport"]["models"]["flash"], "openai/old、openai/new")
        pro = await evaluator.evaluate_book(self.store, job_id, mode="pro_only", client=client)
        self.assertEqual(len(pro["evaluationReport"]["batchSummaries"]), 2)
        self.assertIsNone(pro["evaluationReport"]["models"]["flash"])
        self.assertEqual(calls, ["4-6", "pro"])

    async def test_concurrent_start_and_cancellation_keep_recoverable_checkpoint(self):
        job_id = self.book(3)
        entered = asyncio.Event()

        async def handle(_):
            entered.set()
            await asyncio.Future()

        client = await self.client(handle)
        started = await evaluator.start_book_evaluation(self.store, job_id, mode="flash_only", client=client)
        await asyncio.wait_for(entered.wait(), 3)
        self.assertTrue(evaluator.is_book_evaluation_running(job_id))
        with self.assertRaises(evaluator.EvaluationError) as caught:
            await evaluator.start_book_evaluation(Store(self.store.directory), job_id, client=client)
        self.assertEqual(caught.exception.code, "EVALUATION_RUNNING")
        job = self.store.get_job_record(job_id)
        self.assertEqual(job["evaluationCheckpoint"]["stage"], "flash")
        task = started["completion"]
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(evaluator.is_book_evaluation_running(job_id))
        self.assertEqual(self.store.get_job_record(job_id)["evaluationStatus"], "error")
        self.assertTrue(self.store.get_job_record(job_id)["evaluationCheckpoint"])

    async def test_reuses_chapter_review_evidence_without_model_scan(self):
        job_id = self.book(3)
        sample = {"category": "over_explaining", "quote": "钟楼投下逆光", "priority": "P1"}
        chapter = self.store.get_chapter(job_id, 2)
        # Existing Node databases can hold chapter-level findings in the JSON payload.
        with self.store._write() as db:
            self.store._write_chapter(db, job_id, {**chapter, "aiIssues": [sample]})
        client = await self.client(lambda _: self.fail("the saved chapter review should be reused"))
        result = await evaluator.evaluate_book(self.store, job_id, mode="flash_only", client=client)
        report = result["evaluationReport"]
        self.assertEqual(report["models"]["flash"], "章节审校记录")
        self.assertEqual([(row["chapterNumber"], row["quote"]) for row in report["aiFlavorIssues"]], [(2, sample["quote"])])

    async def test_mock_and_surgical_rewrite_protects_short_output_and_facts(self):
        job_id = self.book(2, provider="mock")
        result = await evaluator.evaluate_book(self.store, job_id)
        self.assertIn("Mock", result["evaluationReport"]["overallSummary"])
        before = "不仅如此，" + _CHAPTER * 15
        self.store.save_chapter(job_id, 2, {"title": "第2章 另一处", "content": before, "status": "needs_review"})
        report = result["evaluationReport"]
        self.store.update_job(job_id, {"evaluationReport": {**report, "aiFlavorIssues": [{
            "id": "p1", "chapterNumber": 2, "priority": "P1", "category": "formulaic_reaction", "quote": "不仅如此，", "resolved": False,
        }]}})
        rewrite = await evaluator.de_ai_rewrite_chapters(self.store, job_id)
        self.assertEqual(rewrite["updatedChapters"][0]["chapterNumber"], 2)
        self.assertTrue(rewrite["evaluationReport"]["aiFlavorIssues"][0]["resolved"])
        self.assertEqual(self.store.get_chapter(job_id, 2)["status"], "needs_review")
        self.assertNotIn("不仅如此", self.store.get_chapter(job_id, 2)["content"])
        self.assertEqual(self.store.get_canonical_facts(job_id)["factHistory"], [])

        clean_before = self.store.get_chapter(job_id, 2)
        self.store.save_provider_config({"provider": "openai", "baseUrl": _JOB_MODEL["baseUrl"]})
        short = await self.client(lambda _: reply("过短的稿件。"))
        with self.assertRaises(evaluator.EvaluationError) as caught:
            await evaluator.de_ai_rewrite_chapters(self.store, job_id, chapter_numbers=[2], provider="openai", client=short)
        self.assertEqual(caught.exception.code, "DE_AI_TOO_SHORT")
        self.assertEqual(self.store.get_chapter(job_id, 2), clean_before)

    async def test_surgical_partial_failure_preserves_prior_completed_issue_resolution(self):
        job_id = self.book(2)
        self.store.update_job(job_id, {"evaluationReport": {"evaluatedAt": "2026-11-21T00:00:00Z", "mode": "full",
            "aiFlavorIssues": [{"id": f"i{number}", "chapterNumber": number, "priority": "P1", "quote": "钟楼"}
                               for number in range(1, 3)]}})
        original = self.store.get_chapter(job_id, 2)
        calls = []

        def handle(request):
            body = json.loads(request.content)
            self.assertEqual(body["max_tokens"], DEFAULT_MAX_TOKENS)
            self.assertTrue(body["messages"][0]["content"].startswith("[TASK:SURGICAL_DE_AI]"))
            prompt = body["messages"][1]["content"]
            calls.append(prompt)
            return reply(_CHAPTER.replace("钟楼", "灯塔") * 12 if len(calls) == 1 else "短稿。")

        with self.assertRaises(evaluator.EvaluationError):
            await evaluator.de_ai_rewrite_chapters(self.store, job_id, client=await self.client(handle))
        self.assertIn("灯塔", self.store.get_chapter(job_id, 1)["content"])
        self.assertEqual(self.store.get_chapter(job_id, 2), original)
        findings = self.store.get_job_record(job_id)["evaluationReport"]["aiFlavorIssues"]
        self.assertEqual([finding.get("resolved", False) for finding in findings], [True, False])


class EvaluationCompatibilityTests(unittest.TestCase):
    def test_json_fingerprint_and_signals_match_legacy_reports(self):
        job = {"title": "夜航🚢", "genre": "悬疑", "pov": "第一人称",
               "bookPlan": {"worldview": {"3": "三", "1": "一", "small": 1e-7, "cost": 25.0}}}
        chapters = [{"chapterNumber": 2, "title": "第二章", "content": "指节发白，瞳孔一缩。\n对讲机响了。"},
                    {"chapterNumber": 1, "title": "第一章", "content": "指节发白，指节绷得发白。\n还有一件事。\n"}]
        source, signals = json.loads(FIXTURE.read_text(encoding="utf-8"))["test_json_fingerprint_and_signals_match_legacy_reports"]
        self.assertEqual(evaluator.evaluation_source(job, chapters), source)
        self.assertEqual(evaluator.collect_book_signals(chapters), signals)
        self.assertNotEqual(evaluator.evaluation_source(job, chapters[::-1]), source)
