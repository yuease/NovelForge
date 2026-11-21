import asyncio
import io
import json
import os
import re
import tempfile
import threading
import unittest
import zipfile
from collections import Counter
from contextlib import aclosing
from pathlib import Path
from unittest.mock import patch

import httpx

from novel_forge.llm import LLMError, call_mock_llm, reset_mock_state, reset_provider_circuit_state
from novel_forge.pipeline import PipelineRuntime, compute_dynamic_chapter_target, review_action
from novel_forge.storage import Store, count_chinese_words
from novel_forge.workflow import SUCCESSORS


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store = Store(self.directory)
        self.runtime = PipelineRuntime(self.store)
        self.enterContext(patch.dict(os.environ, {
            "NOVEL_FORGE_DRAFT_QUALITY": "1", "NOVEL_FORGE_LONG_MEMORY": "1",
            "NOVEL_FORGE_WRITE_AI_GATE": "1", "NOVEL_FORGE_DEGRADE_ON_VERIFY_EXHAUSTED": "0",
            "NOVEL_FORGE_CHAPTER_INPUT_BUDGET": "", "NOVEL_FORGE_PROVIDER_CIRCUIT": "1",
        }))
        reset_mock_state()
        reset_provider_circuit_state()
        self.addCleanup(reset_mock_state)
        self.addCleanup(reset_provider_circuit_state)

    def book(self, chapters, *, words=850, provider="mock", **fields):
        config = {"provider": provider, "maxRetries": 0}
        if provider != "mock":
            config.update(baseUrl="http://127.0.0.1:8471/v1", model="writer", lightModel="reviewer",
                          apiKey="fixture-private-key")
        return self.store.create_job({
            "title": "铜钥夜航", "premise": "林渊沿钟楼线索查出旧案", "wordsPerChapter": words,
            "totalChapters": chapters, "targetWords": chapters * words, "llmConfig": config, **fields,
        })["id"]

    async def transport(self, override):
        calls = []

        async def handler(request):
            body = json.loads(request.content)
            system, prompt = [message["content"] for message in body["messages"]]
            task = re.search(r"\[TASK:([^\]]+)\]", system + "\n" + prompt)
            chapter = re.search(r"第\s*(\d+)\s*章", prompt) or re.search(r"第\s*(\d+)\s*章", system)
            label, number = task[1].lower() if task else "", int(chapter[1]) if chapter else 1
            calls.append((label, number, body["model"], prompt))
            result = await override(label, number, system, prompt)
            if isinstance(result, httpx.Response):
                return result
            if result is None:
                result = (await call_mock_llm(system=system, prompt=prompt))["text"]
            content = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
            return httpx.Response(200, json={"choices": [{"message": {"content": content}, "finish_reason": "stop"}]})

        client = await self.enterAsyncContext(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        return PipelineRuntime(self.store, client=client), calls

    async def walk(self, job_id, runtime=None):
        phases = []
        current_phase = None
        action = None
        async with aclosing((runtime or self.runtime).run_pipeline(job_id)) as runner:
            while True:
                try:
                    phase = await runner.asend(action)
                except StopAsyncIteration:
                    break
                name = phase["phase"]
                phases.append((name, phase.get("chapter")))
                if name == "review":
                    self.assertEqual(current_phase, "verify")
                    action = review_action(phase)
                else:
                    if current_phase is None:
                        self.assertEqual(name, "prepare")
                    elif name != "retry":
                        self.assertIn(name, SUCCESSORS[current_phase])
                    current_phase, action = name, None
        return phases

    def saved_job(self, job_id):
        return self.store.get_job_record(job_id)

    async def test_twenty_chapters_protocol_long_range_facts_evaluation_and_export(self):
        job_id = self.book(20, premise="", enableEvaluation=True,
                           testScenario={"mode": "foreshadowing", "runId": "twenty"})
        self.store.update_job(job_id, {"preferFreeform": True})
        async with aclosing(self.runtime.subscribe_job_events(job_id, max_pending=256)) as subscriber:
            self.assertEqual((await anext(subscriber))["event"], "snapshot")
            with patch.object(self.runtime.meme_store, "get_active_memes", return_value=[]):
                phases = await self.walk(job_id)
            events = []
            while not events or events[-1]["event"] not in ("job:completed", "job:error"):
                events.append(await asyncio.wait_for(anext(subscriber), 1))
        self.assertEqual(phases[:4], [(name, None) for name in ("prepare", "topic", "plan", "outlines")])
        self.assertEqual(phases[-3:], [(name, None) for name in ("calibrate", "audit", "finish")])
        self.assertEqual([number for phase, number in phases if phase == "commit"], list(range(1, 21)))
        self.assertEqual(sum(event["event"] == "chapter:completed" for event in events), 20)

        job = self.saved_job(job_id)
        self.assertEqual(job["status"], "completed", job.get("error"))
        self.assertEqual(job["topicPackage"]["inspirationMode"], "model_freeform")
        self.assertIn("styleBible", job["bookPlan"])
        self.assertEqual(job["evaluationStatus"], "completed", job.get("evaluationProgress"))
        self.assertEqual(job["evaluationReport"]["totalChapters"], 20)
        self.assertEqual(job["checkpoint"]["nextChapter"], 21)
        self.assertEqual(job["stageStats"]["volume_archive"]["calls"], 2)
        facts = self.store.get_canonical_facts(job_id)
        self.assertEqual([record["endChapter"] for record in facts["volumeSummaries"]], [10, 20])
        payoff = next(entry for entry in facts["foreshadowing"] if entry["id"] == "fs_bronze_key")
        self.assertEqual((payoff["status"], payoff["payoffChapter"]), ("paid_off", 18))

        txt = await self.runtime.export_novel(job_id)
        md = await self.runtime.export_novel(job_id, "md")
        docx = await self.runtime.export_novel(job_id, "fanqie")
        self.assertTrue(txt["filename"].endswith(".txt"))
        self.assertIn("## ", md["content"])
        self.assertIn(self.store.get_chapter(job_id, 1)["title"], txt["content"])
        with zipfile.ZipFile(io.BytesIO(docx["content"])) as package:
            self.assertIsNone(package.testzip())
            document = package.read("word/document.xml").decode("utf-8")
        self.assertEqual(document.count('w:val="Heading1"'), 20)
        self.assertNotIn("生成信息", document)

    async def test_fault_injection_repairs_or_rejects_with_the_same_checkpoint_contract(self):
        repaired = self.book(3, testScenario={"mode": "fault-injection", "runId": "repair"})
        phases = await self.walk(repaired)
        self.assertIn(("repair", 2), phases)
        self.assertEqual(self.saved_job(repaired)["status"], "completed")
        self.assertEqual(self.store.get_chapter(repaired, 2)["status"], "repaired")
        self.assertEqual(self.saved_job(repaired)["repairLogs"][0]["chapterNumber"], 2)

        rejected = self.book(3, maxRepairRetries=0, degradeOnVerifyExhausted=True,
                             testScenario={"mode": "fault-injection", "runId": "reject"})
        with patch.dict(os.environ, {"NOVEL_FORGE_DEGRADE_ON_VERIFY_EXHAUSTED": "1"}):
            phases = await self.walk(rejected)
        self.assertEqual(phases[-1], ("reject", 2))
        job = self.saved_job(rejected)
        self.assertEqual(job["error"]["code"], "VERIFICATION_FAILED")
        self.assertEqual(job["checkpoint"]["nextChapter"], 2)
        self.assertEqual(job["completedChapters"], 1)
        self.assertIsNone(self.store.get_chapter(rejected, 2))

    async def test_optional_degrade_saves_needs_review_without_leaking_facts(self):
        async def warn(label, number, _system, _prompt):
            if label == "verify" and number == 2:
                return {
                    "passed": False, "issues": [],
                    "aiIssues": [{"category": "over_explaining", "priority": "P1",
                                  "quote": "客厅内的气氛骤然紧绷", "impact": "动作后的反应重复"}],
                    "factsDelta": {"items": {"未核实道具": {"owner": "无人"}}},
                }
            return None

        runtime, calls = await self.transport(warn)
        job_id = self.book(3, provider="deepseek", maxRepairRetries=0, degradeOnVerifyExhausted=True)
        with patch.dict(os.environ, {"NOVEL_FORGE_DEGRADE_ON_VERIFY_EXHAUSTED": ""}):
            phases = await self.walk(job_id, runtime)
        self.assertIn(("commit", 2), phases)
        job = self.saved_job(job_id)
        self.assertEqual(job["status"], "completed", job.get("error"))
        self.assertEqual(job["completedChapters"], 3)
        self.assertEqual(job["auditGate"]["needsReviewChapters"], [2])
        self.assertTrue(job["auditGate"]["chaptersComplete"])
        self.assertEqual(self.store.get_chapter(job_id, 2)["status"], "needs_review")
        facts = self.store.get_canonical_facts(job_id)
        self.assertNotIn("未核实道具", facts["items"])
        self.assertNotIn("2", facts["factHistory"])
        self.assertTrue(any(label == "verify" and model == "reviewer" for label, _, model, _ in calls))
        self.assertIn(self.store.get_chapter(job_id, 2)["title"], (await runtime.export_novel(job_id))["content"])

    async def test_independent_switches_and_missing_outline_fallback(self):
        async def respond(label, _number, _system, _prompt):
            if label == "chapter_outlines":
                return '[{"chapterNumber":1,"title":"第1章 海风","summary":"潜入钟楼"}, {"chapterNumber":'
            return None

        runtime, calls = await self.transport(respond)
        job_id = self.book(3, provider="deepseek")
        with patch.dict(os.environ, {"NOVEL_FORGE_DRAFT_QUALITY": "0", "NOVEL_FORGE_WRITE_AI_GATE": "0",
                                      "NOVEL_FORGE_LONG_MEMORY": "0", "NOVEL_FORGE_CHAPTER_INPUT_BUDGET": "0"}):
            await self.walk(job_id, runtime)
        job = self.saved_job(job_id)
        self.assertEqual(job["status"], "completed", job.get("error"))
        self.assertNotIn("styleBible", job["bookPlan"])
        self.assertEqual([outline["chapterNumber"] for outline in job["outlines"]], [1, 2, 3])
        self.assertEqual(job["outlines"][0]["title"], "第1章 海风")
        self.assertEqual(job["outlines"][2]["title"], "第3章 危机递进")
        self.assertEqual(sum(label == "chapter_outlines" for label, _, _, _ in calls), 2)

    async def test_prompt_budget_interrupts_before_first_draft_model_call(self):
        async def respond(*_):
            return None

        runtime, calls = await self.transport(respond)
        job_id = self.book(1, provider="deepseek")
        with patch.dict(os.environ, {"NOVEL_FORGE_CHAPTER_INPUT_BUDGET": "1025"}):
            phases = await self.walk(job_id, runtime)
        self.assertEqual(phases[-1], ("chapter", 1))
        self.assertEqual(self.saved_job(job_id)["error"]["code"], "CHAPTER_PROMPT_BUDGET_EXCEEDED")
        self.assertNotIn("chapter_write", {label for label, _, _, _ in calls})
        self.assertEqual(self.saved_job(job_id)["checkpoint"]["nextChapter"], 1)

    async def test_volume_archive_retries_from_saved_tenth_chapter_and_feeds_chapter_twelve(self):
        attempts = 0
        constraint = "午夜之前铜铃不得离开钟楼"

        async def respond(label, number, system, prompt):
            nonlocal attempts
            if label == "volume_summary":
                attempts += 1
                if attempts == 1:
                    return {"malformed": True}
                return {"mainline": "第一卷主线：找到钟楼旧案入口", "turningPoints": [{"chapter": 3, "text": constraint}]}
            if label == "chapter_write" and number == 3:
                prose = (await call_mock_llm(system=system, prompt=prompt))["text"]
                return prose + "\n\n" + constraint + "。"
            if label == "verify" and number == 3:
                verdict = json.loads((await call_mock_llm(system=system, prompt=prompt))["text"])
                verdict["factsDelta"]["memory"] = {"constraints": [{"id": "bell-rule", "text": constraint,
                                                                    "status": "active", "quote": constraint}]}
                return verdict
            return None

        runtime, calls = await self.transport(respond)
        job_id = self.book(12, provider="deepseek")
        await runtime.start_job(job_id)
        failed = await runtime.wait_for_job(job_id)
        self.assertEqual(failed["error"]["code"], "VOLUME_SUMMARY_INVALID", failed)
        self.assertEqual(failed["checkpoint"]["nextChapter"], 11)
        chapter10 = self.store.get_chapter(job_id, 10)
        self.assertEqual(failed["completedChapters"], 10)

        self.store = Store(self.directory)
        runtime = PipelineRuntime(self.store, client=runtime.client)
        await runtime.resume_job(job_id)
        finished = await runtime.wait_for_job(job_id)
        self.assertEqual(finished["status"], "completed", finished.get("error"))
        self.assertEqual(self.store.get_chapter(job_id, 10), chapter10)
        self.assertEqual(attempts, 2)
        self.assertEqual(finished["stageStats"]["volume_archive"]["calls"], 2)
        self.assertEqual(finished["stageStats"]["chapter_verify"]["calls"], 12)
        prompts = [prompt for task, number, _, prompt in calls if task == "chapter_write" and number == 12]
        self.assertEqual(len(prompts), 1)
        self.assertIn(constraint, prompts[0])
        self.assertIn("第一卷主线：找到钟楼旧案入口", prompts[0])
        self.assertTrue(all(model == "reviewer" for task, _, model, _ in calls if task == "volume_summary"))

    async def test_forty_chapters_apply_dynamic_targets_and_all_four_gates(self):
        self.assertGreater(compute_dynamic_chapter_target({"targetWords": 100_000, "wordsPerChapter": 2500}, 94_000, 2), 2500)
        job_id = self.book(40, words=2500, testScenario={"mode": "audit-fail-once", "runId": "forty"})
        await self.runtime.start_job(job_id)
        finished = await self.runtime.wait_for_job(job_id)
        self.assertEqual(finished["status"], "completed", finished.get("error"))
        gate = finished["auditGate"]
        self.assertTrue(all(gate[key] for key in ("chaptersComplete", "wordCountInRange", "auditPassed", "exportValid")))
        self.assertEqual(gate["allowedRange"], [90_000, 110_000])
        self.assertEqual(gate["totalWords"], sum(count_chinese_words(ch["content"]) for ch in self.store.get_chapters(job_id)))
        self.assertEqual(finished["stageStats"]["full_audit"]["calls"], 2)

    async def test_exhausted_audit_blocks_finish_and_keeps_document(self):
        async def respond(label, _number, _system, _prompt):
            if label == "full_audit":
                return {"passed": True, "blockingIssues": [{"chapterNumber": 2, "message": "终章缺少回应"}]}
            return None

        runtime, _calls = await self.transport(respond)
        job_id = self.book(3, provider="deepseek")
        self.store.update_job(job_id, {"maxAuditRetries": 0})
        phases = await self.walk(job_id, runtime)
        self.assertNotIn(("finish", None), phases)
        job = self.saved_job(job_id)
        self.assertEqual(job["error"]["code"], "AUDIT_GATE_FAILED")
        self.assertTrue(job["auditGate"]["chaptersComplete"])
        self.assertFalse(job["auditGate"]["auditPassed"])
        self.assertEqual(job["checkpoint"]["stage"], "full_audit")
        self.assertTrue((await runtime.export_novel(job_id))["content"])

    async def test_checkpoint_lag_after_commit_recovers_without_rewriting_saved_chapter(self):
        base_checkpoint = self.store.save_checkpoint
        job_id = self.book(3, autoResumeOnRateLimit=False)

        def interrupt_after_commit(current_id, checkpoint):
            if checkpoint["stage"] == "chapter_writing" and checkpoint.get("nextChapter") == 2:
                raise LLMError("模拟提交后重启", code="TIMEOUT", retryable=True)
            return base_checkpoint(current_id, checkpoint)

        with patch.object(self.store, "save_checkpoint", side_effect=interrupt_after_commit):
            await self.runtime.start_job(job_id)
            result = await self.runtime.wait_for_job(job_id)
        self.assertEqual(result["status"], "interrupted", result.get("error"))
        self.assertEqual(result["checkpoint"]["nextChapter"], 1)
        original = self.store.get_chapter(job_id, 1)
        self.assertIsNotNone(original)

        self.store = Store(self.directory)
        runtime = PipelineRuntime(self.store)
        recovered = await runtime.recover_interrupted_jobs(auto_resume=True)
        self.assertEqual([item["id"] for item in recovered], [job_id])
        finished = await runtime.wait_for_job(job_id)
        self.assertEqual(finished["status"], "completed", finished.get("error"))
        self.assertEqual(self.store.get_chapter(job_id, 1), original)
        self.assertEqual(finished["stageStats"]["chapter_writing"]["calls"], 3)

    async def test_pause_waits_for_atomic_chapter_write_and_restarts_from_saved_text(self):
        job_id = self.book(3)
        entered, release = threading.Event(), threading.Event()
        save = self.store.save_verified_chapter

        def block_first_chapter(current_id, number, data, delta, **options):
            if number == 1:
                entered.set()
                if not release.wait(4):
                    raise RuntimeError("test failed to release transaction")
            return save(current_id, number, data, delta, **options)

        with patch.object(self.store, "save_verified_chapter", side_effect=block_first_chapter):
            await self.runtime.start_job(job_id)
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                waiter = asyncio.create_task(self.runtime.wait_for_job(job_id))
                pause = asyncio.create_task(self.runtime.pause_job(job_id))
                await asyncio.sleep(0.04)
                self.assertFalse(pause.done())
                self.assertTrue(self.runtime.is_job_running(job_id))
            finally:
                release.set()
            self.assertEqual((await pause)["status"], "paused")
            self.assertEqual((await waiter)["status"], "paused")
        committed = self.store.get_chapter(job_id, 1)
        self.assertIsNotNone(committed)
        self.assertFalse(self.runtime.is_job_running(job_id))
        restarted = PipelineRuntime(Store(self.directory))
        self.assertEqual(await restarted.recover_interrupted_jobs(), [])
        await restarted.resume_job(job_id)
        self.assertEqual((await restarted.wait_for_job(job_id))["status"], "completed")
        self.assertEqual(self.store.get_chapter(job_id, 1), committed)

    async def test_extend_preserves_old_chapters_and_appends_outlines(self):
        job_id = self.book(3)
        await self.runtime.start_job(job_id)
        self.assertEqual((await self.runtime.wait_for_job(job_id))["status"], "completed")
        old = self.store.get_chapters(job_id)
        started = await self.runtime.extend_job(job_id, add_chapters=2, direction="去灯塔追查暗线", finalize_book=True)
        self.assertEqual(started["totalChapters"], 5)
        finished = await self.runtime.wait_for_job(job_id)
        self.assertEqual(finished["status"], "completed", finished.get("error"))
        self.assertEqual(self.store.get_chapters(job_id)[:3], old)
        self.assertEqual(finished["bookPlan"]["volumes"][-1]["chapters"], [4, 5])
        self.assertIn("去灯塔追查暗线", finished["bookPlan"]["continuationNotes"][-1])
        self.assertEqual(finished["auditGate"]["targetWords"], started["targetWords"])

    async def test_429_cooldown_retries_only_missing_work(self):
        failures = 0

        async def respond(label, number, _system, _prompt):
            nonlocal failures
            if label == "chapter_write" and number == 2 and failures == 0:
                failures += 1
                return httpx.Response(429, json={"error": {"message": "rate limit"}})
            return None

        runtime, calls = await self.transport(respond)
        job_id = self.book(3, provider="deepseek", autoResumeCooldownMs=20)
        phases = await asyncio.wait_for(self.walk(job_id, runtime), 6)
        self.assertIn(("retry", None), phases)
        self.assertEqual(self.saved_job(job_id)["status"], "completed")
        writes = Counter(number for task, number, _, _ in calls if task == "chapter_write")
        self.assertEqual(writes, {1: 1, 2: 2, 3: 1})
        self.assertIsNone(self.saved_job(job_id)["autoRetryState"])

    async def test_pause_cancels_rate_limit_sleep_and_masks_event_credentials(self):
        limited = True

        async def respond(label, number, _system, _prompt):
            if label == "chapter_write" and number == 2 and limited:
                return httpx.Response(429, json={"error": {"message": "rate limit"}})
            return None

        runtime, calls = await self.transport(respond)
        job_id = self.book(3, provider="deepseek", autoResumeCooldownMs=60_000)
        async with aclosing(runtime.subscribe_job_events(job_id)) as subscriber:
            first = await anext(subscriber)
            self.assertNotIn("fixture-private-key", json.dumps(first))
            await runtime.start_job(job_id)
            while True:
                event = await asyncio.wait_for(anext(subscriber), 3)
                if event.get("autoRetryState"):
                    break
            self.assertEqual(event["autoRetryState"]["code"], "HTTP_429")
            paused = await asyncio.wait_for(runtime.pause_job(job_id), 2)
            self.assertEqual(paused["status"], "paused")
            self.assertIsNone(paused["autoRetryState"])
            self.assertEqual(paused["checkpoint"]["nextChapter"], 2)
        previous_count = len(calls)
        self.assertEqual(await runtime.recover_interrupted_jobs(), [])
        self.assertEqual(len(calls), previous_count)
        limited = False
        await runtime.resume_job(job_id)
        self.assertEqual((await runtime.wait_for_job(job_id))["status"], "completed")

    async def test_official_openai_429_waits_for_manual_resume(self):
        async def limit(*_):
            return httpx.Response(429, json={"error": {"message": "rate limit"}})

        runtime, calls = await self.transport(limit)
        job_id = self.book(1, provider="openai", llmConfig={"provider": "openai", "apiKey": "fixture", "maxRetries": 5})
        await runtime.start_job(job_id)
        result = await asyncio.wait_for(runtime.wait_for_job(job_id), 3)
        self.assertEqual(result["status"], "interrupted", result.get("error"))
        self.assertEqual(result["error"]["code"], "OPENAI_RATE_LIMIT")
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
