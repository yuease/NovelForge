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

from novel_forge.arc_planning import CANON_LOCKS_MARKER
from novel_forge.llm import LLMError, call_mock_llm, reset_mock_state, reset_provider_circuit_state
from novel_forge.pipeline import PipelineRuntime, compute_dynamic_chapter_target, review_action
from novel_forge.prompts import build_chapter_write_prompt
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
            "NOVEL_FORGE_ROLLING_ARC_OUTLINES": "0",
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

    async def test_story_ledger_arc_drift_and_model_override_are_independent_from_long_memory(self):
        clock_line = "第二天清晨，正值初冬，距离交割还有两天。"
        check = "交割倒计时缺少衔接"
        note = "下一单元沿用剩余两天"

        async def respond(label, number, system, prompt):
            if label == "chapter_write" and number == 1:
                body = (await call_mock_llm(system=system, prompt=prompt))["text"]
                return body + "\n\n" + clock_line
            if label == "verify" and number == 1:
                return {"passed": True, "issues": [], "aiIssues": [], "factsDelta": {"memory": {
                    "storyClock": {"day": "第二天", "season": "初冬", "countdown": "还有两天", "quote": clock_line},
                }}}
            if label == "arc_summary":
                return {"mainline": "码头交割开始", "driftChecks": [
                    {"chapter": 1, "kind": "clock", "issue": check, "fixHint": note, "quote": clock_line},
                    {"chapter": 1, "kind": "clock", "issue": "伪造错误", "fixHint": "略过", "quote": "这句话从未存在。"},
                ]}
            return None

        runtime, calls = await self.transport(respond)
        for enabled in ("1", "0"):
            with self.subTest(story_ledger=enabled):
                calls.clear()
                job_id = self.book(8, provider="deepseek")
                env = {"NOVEL_FORGE_ROLLING_ARC_OUTLINES": "1", "NOVEL_FORGE_LONG_MEMORY": "0",
                       "NOVEL_FORGE_STORY_LEDGER": enabled, "NOVEL_FORGE_ARC_REVIEW_MODEL": "  strong-arc-model  "}
                with patch.dict(os.environ, env):
                    await self.walk(job_id, runtime)
                job = self.saved_job(job_id)
                self.assertEqual(job["status"], "completed", job.get("error"))
                archives = [(model, prompt) for task, _, model, prompt in calls if task == "arc_summary"]
                self.assertEqual(len(archives), 1)
                next_arc = [prompt for task, _, _, prompt in calls if task == "arc_outlines"][-1]
                facts = self.store.get_canonical_facts(job_id)
                stored = facts["arcSummaries"][0]
                if enabled == "1":
                    self.assertEqual(archives[0][0], "strong-arc-model")
                    self.assertTrue(all(model != "strong-arc-model" for task, _, model, _ in calls if task != "arc_summary"))
                    self.assertEqual(facts["storyClock"]["day"], "第二天")
                    self.assertEqual([row["issue"] for row in stored["driftChecks"]], [check])
                    self.assertIn(note, next_arc)
                    self.assertNotIn("伪造错误", next_arc)
                else:
                    self.assertEqual(archives[0][0], "reviewer")
                    self.assertFalse(facts.get("storyClock"))
                    self.assertNotIn("driftChecks", stored)
                    self.assertNotIn(note, next_arc)

    async def test_rolling_arcs_schedule_only_after_commit_and_archive_without_long_memory(self):
        scheduled = []
        first_written = []

        async def respond(label, number, _system, prompt):
            if label == "arc_outlines":
                start = int(re.search(r"起始章节：(\d+)", prompt)[1])
                job = self.saved_job(job_id)
                scheduled.append((start, len(job["outlines"]), job["completedChapters"], prompt))
            if label == "chapter_write" and number == 1:
                first_written.extend(row["chapterNumber"] for row in self.saved_job(job_id)["outlines"])
            return None

        runtime, calls = await self.transport(respond)
        job_id = self.book(20, provider="deepseek")
        with patch.dict(os.environ, {"NOVEL_FORGE_ROLLING_ARC_OUTLINES": "1", "NOVEL_FORGE_LONG_MEMORY": "0"}):
            phases = await self.walk(job_id, runtime)
        job = self.saved_job(job_id)
        self.assertEqual(job["status"], "completed", job.get("error"))
        self.assertEqual(job["totalChapters"], 20)
        self.assertEqual(phases.count(("outlines", None)), 1)
        self.assertEqual([chapter for phase, chapter in phases if phase == "outlines"], [None, 6, 11, 16])
        self.assertEqual([chapter for phase, chapter in phases if phase == "archive"], [5, 10, 15])
        self.assertEqual(first_written, [1, 2, 3, 4, 5])
        self.assertEqual([row[:3] for row in scheduled], [(1, 0, 0), (6, 5, 5), (11, 10, 10), (16, 15, 15)])
        self.assertIn("第1章的证据交接与当面拆招", scheduled[1][3])
        self.assertIn("第1章的证据交接与当面拆招", scheduled[3][3])
        self.assertIn("终局收网单元", scheduled[3][3])
        self.assertIn("canonLocks", job["bookPlan"])
        self.assertEqual([(arc["startChapter"], arc["endChapter"]) for arc in job["arcPlans"]],
                         [(1, 5), (6, 10), (11, 15), (16, 20)])
        facts = self.store.get_canonical_facts(job_id)
        self.assertEqual([row["endChapter"] for row in facts["arcSummaries"]], [5, 10, 15])
        self.assertEqual(facts["volumeSummaries"], [])
        self.assertEqual(job["stageStats"]["arc_archive"]["calls"], 3)
        self.assertTrue(all(model == "reviewer" for task, _, model, _ in calls if task == "arc_summary"))
        for label in ("chapter_write", "verify"):
            prompt = next(prompt for task, number, _, prompt in calls if task == label and number == 6)
            self.assertIn("第1章的证据交接与当面拆招", prompt)
            self.assertIn("不可变核心口径锁", prompt)
        self.assertNotIn("volume_archive", job["stageStats"])

    async def test_rolling_resume_reuses_plan_and_archive_at_three_boundaries(self):
        schedules, rewrites, archives = [], [], []

        async def respond(label, number, _system, prompt):
            if label == "arc_outlines":
                start = int(re.search(r"起始章节：(\d+)", prompt)[1])
                schedules.append(start)
                if start == 6 and schedules.count(6) <= 2:
                    return {"arcTitle": "无章节"}
            if label == "chapter_write":
                rewrites.append(number)
                if number == 3 and rewrites.count(3) == 1:
                    return httpx.Response(502, text="测试写章断流")
            if label == "arc_summary":
                archives.append(prompt)
                if len(archives) == 1:
                    return {"mainline": None, "usedBeats": []}
            return None

        runtime, calls = await self.transport(respond)
        job_id = self.book(8, provider="deepseek", autoResumeOnRateLimit=False)
        with patch.dict(os.environ, {"NOVEL_FORGE_ROLLING_ARC_OUTLINES": "1"}):
            errors = [("HTTP_502", 3, 6, 0), ("ARC_SUMMARY_INVALID", 6, 6, 0), ("OUTLINE_PARSE_FAILED", 6, 6, 1)]
            for code, chapter, outline, archive_count in errors:
                await self.walk(job_id, runtime)
                job = self.saved_job(job_id)
                self.assertEqual(job["status"], "interrupted")
                self.assertEqual(job["error"]["code"], code)
                self.assertEqual((job["checkpoint"]["nextChapter"], job["checkpoint"]["nextOutlineChapter"]), (chapter, outline))
                self.assertEqual(len(job["arcPlans"]), 1)
                self.assertEqual(len(self.store.get_canonical_facts(job_id)["arcSummaries"]), archive_count)
                runtime = PipelineRuntime(Store(self.directory), client=runtime.client)
            self.assertFalse(runtime.is_job_running(job_id))
            await runtime.recover_interrupted_jobs()
            self.assertFalse(runtime.is_job_running(job_id))
            await runtime.resume_job(job_id)
            await runtime.wait_for_job(job_id)
        self.assertEqual(self.saved_job(job_id)["status"], "completed")
        self.assertEqual(schedules, [1, 6, 6, 6])
        self.assertEqual(rewrites, [1, 2, 3, 3, 4, 5, 6, 7, 8])
        self.assertEqual(len(archives), 2)
        self.assertEqual(self.saved_job(job_id)["stageStats"]["arc_archive"]["calls"], 2)
        self.assertTrue(any("上次校验未通过" in prompt for task, _, _, prompt in calls if task == "arc_outlines"))

    async def test_rolling_extend_archives_native_tail_and_preserves_legacy_work(self):
        async def respond(*_):
            return None

        runtime, calls = await self.transport(respond)
        for initial_mode in ("0", "1"):
            with self.subTest(initial_mode=initial_mode):
                calls.clear()
                job_id = self.book(5, provider="deepseek")
                with patch.dict(os.environ, {"NOVEL_FORGE_ROLLING_ARC_OUTLINES": initial_mode}):
                    await self.walk(job_id, runtime)
                old = self.store.get_chapters(job_id)
                original = self.saved_job(job_id)
                self.assertEqual(original["status"], "completed")
                self.assertEqual(self.store.get_canonical_facts(job_id)["arcSummaries"], [])
                with patch.dict(os.environ, {"NOVEL_FORGE_ROLLING_ARC_OUTLINES": "1"}):
                    await runtime.extend_job(job_id, add_chapters=2, direction="去灯塔核对执勤簿", finalize_book=True)
                    finished = await runtime.wait_for_job(job_id)
                self.assertEqual(finished["status"], "completed", finished.get("error"))
                stored = self.saved_job(job_id)
                self.assertEqual(self.store.get_chapters(job_id)[:5], old)
                self.assertEqual(stored["outlines"][:5], original["outlines"])
                self.assertEqual(stored["bookPlan"].get("canonLocks"), original["bookPlan"].get("canonLocks"))
                self.assertEqual(stored["totalChapters"], 7)
                self.assertEqual((stored["arcPlans"][-1]["startChapter"], stored["arcPlans"][-1]["endChapter"]), (6, 7))
                extension = next(prompt for task, _, _, prompt in calls if task == "arc_outlines" and "起始章节：6\n" in prompt)
                self.assertIn("去灯塔核对执勤簿", extension)
                self.assertIn("彻底完结", extension)
                actual = [row["endChapter"] for row in self.store.get_canonical_facts(job_id)["arcSummaries"]]
                self.assertEqual(actual, [5] if initial_mode == "1" else [])

    async def test_rolling_plan_rejects_a_source_edited_while_the_model_is_planning(self):
        changed = False

        async def respond(label, _number, _system, prompt):
            nonlocal changed
            if label == "arc_outlines" and "起始章节：6\n" in prompt and not changed:
                chapter = self.store.get_chapter(job_id, 1)
                self.store.save_chapter(job_id, 1, {**chapter, "content": chapter["content"] + "\n\n作者补充一条线索。"})
                changed = True
            return None

        runtime, calls = await self.transport(respond)
        job_id = self.book(8, provider="deepseek")
        with patch.dict(os.environ, {"NOVEL_FORGE_ROLLING_ARC_OUTLINES": "1"}):
            await self.walk(job_id, runtime)
            stopped = self.saved_job(job_id)
            self.assertTrue(changed)
            self.assertEqual(stopped["error"]["code"], "ARC_SOURCE_CHANGED")
            self.assertEqual(len(stopped["arcPlans"]), 1)
            self.assertEqual(len(stopped["outlines"]), 5)
            self.assertEqual(self.store.get_canonical_facts(job_id)["arcSummaries"], [])
            await self.walk(job_id, runtime)
        self.assertEqual(self.saved_job(job_id)["status"], "completed")
        self.assertEqual(sum(task == "arc_summary" for task, *_ in calls), 2)
        self.assertEqual([number for task, number, *_ in calls if task == "chapter_write"], list(range(1, 9)))

    async def test_switch_off_reuses_saved_rolling_outlines_and_restores_ten_chapter_archives(self):
        stop = True

        async def respond(label, number, _system, _prompt):
            nonlocal stop
            if label == "chapter_write" and number == 6 and stop:
                stop = False
                return httpx.Response(502, text="测试切换前断流")
            return None

        runtime, calls = await self.transport(respond)
        job_id = self.book(12, provider="deepseek", autoResumeOnRateLimit=False)
        legacy = await call_mock_llm(system=f"[TASK:BOOK_PLAN]\n{CANON_LOCKS_MARKER}", prompt="总章数：12")
        self.store.update_job(job_id, {"bookPlan": json.loads(legacy["text"])})
        with patch.dict(os.environ, {"NOVEL_FORGE_ROLLING_ARC_OUTLINES": "1"}):
            await self.walk(job_id, runtime)
        before = self.saved_job(job_id)
        archived = self.store.get_canonical_facts(job_id)["arcSummaries"]
        self.assertEqual(before["status"], "interrupted")
        self.assertEqual(len(before["outlines"]), 9)
        self.assertEqual([row["endChapter"] for row in archived], [5])
        calls.clear()
        await self.walk(job_id, runtime)
        done = self.saved_job(job_id)
        self.assertEqual(done["status"], "completed", done.get("error"))
        self.assertEqual(done["outlines"][:9], before["outlines"])
        self.assertEqual(done["arcPlans"], before["arcPlans"])
        self.assertEqual(self.store.get_canonical_facts(job_id)["arcSummaries"], archived)
        self.assertEqual([row["endChapter"] for row in self.store.get_canonical_facts(job_id)["volumeSummaries"]], [10])
        self.assertEqual([task for task, *_ in calls if task in ("arc_outlines", "arc_summary", "chapter_outlines", "volume_summary")],
                         ["chapter_outlines", "volume_summary"])
        self.assertTrue(all("不可变核心口径锁" not in prompt and "已演桥段" not in prompt for _, _, _, prompt in calls))

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

    async def test_phase2_gate_reuses_committed_chapter_tails_after_resume_and_can_be_disabled(self):
        interrupted = False
        system_by_chapter = {}

        async def respond(label, number, system, prompt):
            nonlocal interrupted
            if label != "chapter_write":
                return None
            system_by_chapter[number] = system
            if number == 5 and not interrupted:
                interrupted = True
                return httpx.Response(502, text="fixture")
            body = (await call_mock_llm(system=system, prompt=prompt))["text"]
            alarms = {3: "【警报：通道已经关闭】", 4: "【系统提示：信号中断】", 5: "【警报：电梯失控】"}
            return f"{body}\n\n{alarms[number]}" if number in alarms else body

        runtime, calls = await self.transport(respond)
        fields = {"autoResumeOnRateLimit": False, "maxRepairRetries": 0, "degradeOnVerifyExhausted": True,
                  "memeLevel": "light"}
        job_id = self.book(5, provider="deepseek", **fields)
        with patch.dict(os.environ, {"NOVEL_FORGE_DEGRADE_ON_VERIFY_EXHAUSTED": ""}):
            with patch.object(runtime.meme_store, "get_meme_inspiration_samples", return_value=[]):
                await self.walk(job_id, runtime)
            self.assertEqual(self.saved_job(job_id)["status"], "interrupted")
            self.assertEqual(self.saved_job(job_id)["checkpoint"]["nextChapter"], 5)
            self.assertEqual(len(self.store.get_chapters(job_id)), 4)
            reopened = PipelineRuntime(Store(self.directory), client=runtime.client)
            with patch.object(reopened.meme_store, "get_meme_inspiration_samples", return_value=[]):
                phases = await self.walk(job_id, reopened)

        self.assertEqual(self.saved_job(job_id)["status"], "completed")
        self.assertEqual(self.saved_job(job_id)["auditGate"]["needsReviewChapters"], [5])
        self.assertIn(("commit", 5), phases)
        self.assertIn("核心情感高潮禁止出戏玩梗", system_by_chapter[5])
        saved = self.store.get_chapter(job_id, 5)
        self.assertEqual(saved["status"], "needs_review")
        self.assertEqual(saved["verification"]["issues"][0]["rule"], "repetitive_ending_hook")
        facts = self.store.get_canonical_facts(job_id)
        self.assertNotIn(5, [entry["chapter"] for entry in facts["factHistory"]])
        self.assertFalse(any(task == "verify" and number == 5 for task, number, *_ in calls))

        disabled_job = self.book(5, provider="deepseek", **fields)
        calls.clear()
        with patch.dict(os.environ, {"NOVEL_FORGE_WRITE_AI_GATE": "0", "NOVEL_FORGE_DEGRADE_ON_VERIFY_EXHAUSTED": ""}):
            with patch.object(runtime.meme_store, "get_meme_inspiration_samples", return_value=[]):
                await self.walk(disabled_job, runtime)
        self.assertEqual(self.saved_job(disabled_job)["status"], "completed")
        self.assertEqual(self.store.get_chapter(disabled_job, 5)["status"], "verified")
        self.assertNotIn("核心情感高潮禁止出戏玩梗", system_by_chapter[5])
        self.assertTrue(any(task == "verify" and number == 5 for task, number, *_ in calls))

    async def test_independent_switches_and_missing_outline_resume(self):
        ranges = []

        async def respond(label, _number, _system, prompt):
            if label == "chapter_outlines":
                start, end = (int(number) for number in re.findall(r"(?:起始|结束)章节：(\d+)", prompt))
                ranges.append((start, end))
                if len(ranges) == 1:
                    return ('[{"chapterNumber":1,"title":"第1章 海风","summary":"潜入钟楼"}, '
                            '{"chapterNumber":3,"title":"第3章 启航","summary":"船驶出港口"}, {"chapterNumber":')
                if len(ranges) == 2:
                    return [{"chapterNumber": 2}]
                return [{"chapterNumber": 2, "title": "第2章 查证", "summary": "找到失踪的值班簿"}]
            return None

        runtime, calls = await self.transport(respond)
        job_id = self.book(3, provider="deepseek")
        with patch.dict(os.environ, {"NOVEL_FORGE_DRAFT_QUALITY": "0", "NOVEL_FORGE_WRITE_AI_GATE": "0",
                                      "NOVEL_FORGE_LONG_MEMORY": "0", "NOVEL_FORGE_CHAPTER_INPUT_BUDGET": "0"}):
            phases = await self.walk(job_id, runtime)
            job = self.saved_job(job_id)
            self.assertEqual(job["status"], "interrupted")
            self.assertEqual(job["error"]["code"], "OUTLINE_PARSE_FAILED")
            self.assertTrue(job["error"]["retryable"])
            self.assertEqual(job["checkpoint"]["nextOutlineChapter"], 2)
            self.assertEqual([entry["chapterNumber"] for entry in job["outlines"]], [1, 3])
            self.assertEqual(phases[-1], ("outlines", None))
            self.assertFalse(any(label == "chapter_write" for label, *_ in calls))
            recovered = await runtime.recover_interrupted_jobs()
            self.assertEqual(len(recovered), 1)
            self.assertFalse(runtime.is_job_running(job_id))
            await runtime.resume_job(job_id)
            await runtime.wait_for_job(job_id)
        job = self.saved_job(job_id)
        self.assertEqual(job["status"], "completed", job.get("error"))
        self.assertNotIn("styleBible", job["bookPlan"])
        self.assertEqual([outline["chapterNumber"] for outline in job["outlines"]], [1, 2, 3])
        self.assertEqual(job["outlines"][0]["title"], "第1章 海风")
        self.assertEqual(job["outlines"][2]["title"], "第3章 启航")
        self.assertEqual(ranges, [(1, 3), (2, 2), (2, 2)])

    async def test_outline_batch_persists_before_network_failure_and_only_replaces_unwritten_placeholders(self):
        requests = []

        async def respond(label, _number, _system, prompt):
            self.assertEqual(label, "chapter_outlines")
            start, end = (int(number) for number in re.findall(r"(?:起始|结束)章节：(\d+)", prompt))
            requests.append((start, end))
            if requests == [(2, 2), (4, 4)]:
                return httpx.Response(502, text="fixture")
            return [{"chapterNumber": start, "title": "真实标题", "summary": "在钟楼找到值班簿"}]

        def placeholder(number):
            return {"chapterNumber": number, "title": f"第{number}章 危机递进",
                    "summary": f"第{number}章承接前文规则线索展开冲突并留下章末悬念。",
                    "keyEvents": [f"第{number}章关键事件"]}

        runtime, _ = await self.transport(respond)
        job_id = self.book(4, provider="deepseek")
        self.store.save_verified_chapter(job_id, 1, {"content": "已完成旧正文"})
        self.store.update_job(job_id, {"bookPlan": {"title": "铜钥夜航"}, "outlines": [
            placeholder(1), placeholder(2), {"chapterNumber": 3, "title": "原有章名", "summary": "沿河逃脱"},
        ]})
        job = self.saved_job(job_id)
        with self.assertRaises(LLMError) as caught:
            await runtime._outlines(job, job["llmConfig"])
        self.assertEqual(caught.exception.code, "HTTP_502")
        stopped = self.saved_job(job_id)
        self.assertEqual(stopped["checkpoint"]["nextOutlineChapter"], 4)
        self.assertEqual([item["title"] for item in stopped["outlines"]], ["第1章 危机递进", "真实标题", "原有章名"])
        await runtime._outlines(stopped, stopped["llmConfig"])
        self.assertEqual(requests, [(2, 2), (4, 4), (4, 4)])
        self.assertEqual(self.store.get_chapter(job_id, 1)["content"], "已完成旧正文")

    async def test_meme_pool_is_consumed_only_by_committed_drafts_that_received_a_sample(self):
        picks = [{"title": f"样本{number}", "summary": "借用反差结构"} for number in range(1, 5)]
        seen = {}

        async def respond(label, number, system, _prompt):
            if label == "chapter_write":
                sent = re.findall(r"^- 【(样本\d+)】", system, re.M)
                seen.setdefault(number, []).append(sent)
                self.assertIn("网感与玩梗风格要求", system)
                if number == 3 and len(seen[number]) == 1:
                    return httpx.Response(502, text="fixture")
            return None

        def render(**options):
            if options["outline"]["chapterNumber"] == 1:
                # Simulate the budget dropping the sample without changing the prompt return format.
                options["meme_samples"] = []
            return build_chapter_write_prompt(**options)

        runtime, _ = await self.transport(respond)
        job_id = self.book(7, provider="deepseek", memeLevel="light", autoResumeOnRateLimit=False)
        self.store.update_job(job_id, {
            "bookPlan": {"title": "铜钥夜航"},
            "outlines": [{"chapterNumber": number, "title": f"第{number}章", "goal": "吐槽",
                          "summary": "沿潮水追查钟楼线索"} for number in range(1, 8)],
        })
        with patch("novel_forge.pipeline.build_chapter_write_prompt", side_effect=render):
            with patch.object(runtime.meme_store, "get_meme_inspiration_samples", return_value=picks) as sample:
                await self.walk(job_id, runtime)
                sample.assert_awaited_once()
                self.assertEqual(sample.call_args.kwargs["limit"], 4)
            self.assertEqual(self.saved_job(job_id)["usedMemeTitles"], ["样本1"])
            self.assertEqual(self.saved_job(job_id)["status"], "interrupted")
            restarted = PipelineRuntime(Store(self.directory), client=runtime.client)
            with patch.object(restarted.meme_store, "get_meme_inspiration_samples", side_effect=AssertionError):
                await self.walk(job_id, restarted)
        self.assertEqual(self.saved_job(job_id)["status"], "completed")
        self.assertEqual(seen, {1: [[]], 2: [["样本1"]], 3: [["样本2"], ["样本2"]], 4: [["样本3"]],
                                5: [["样本4"]], 6: [[]], 7: [[]]})
        self.assertEqual(self.saved_job(job_id)["usedMemeTitles"], [entry["title"] for entry in picks])

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
        self.assertEqual(gate["allowedRange"], [90_000, 118_000])
        self.assertEqual(gate["totalWords"], sum(count_chinese_words(ch["content"]) for ch in self.store.get_chapters(job_id)))
        self.assertEqual(finished["stageStats"]["full_audit"]["calls"], 2)

    def test_dynamic_targets_protect_actual_finale_chapters_even_with_saved_gaps(self):
        for total, floors in ((3, [1700, 1700, 2000]), (5, [1700, 1700, 1700, 2000, 2000]),
                              (6, [1700, 1700, 1700, 2000, 2000, 2000])):
            job = {"totalChapters": total, "wordsPerChapter": 2000, "targetWords": total * 2000}
            self.assertEqual([
                compute_dynamic_chapter_target(job, total * 2000, 1, chapter_number=number)
                for number in range(1, total + 1)
            ], floors)

    async def test_calibration_shrinks_an_earlier_chapter_and_retains_a_finale_floor(self):
        job_id = self.book(12)
        job = self.saved_job(job_id)
        chapters = {number: self.store.save_chapter(job_id, number, {"content": "雨" * words})
                    for number, words in ((4, 1200), (11, 1600), (12, 1800))}
        with patch.object(self.runtime, "_adjust_words", return_value="风" * 900) as adjust:
            await self.runtime._calibrate(job, {}, chapters, 14_000, (9180, 12036), {})
            self.assertEqual(adjust.call_args.args[1], 4)
            self.assertEqual(self.store.get_chapter(job_id, 4)["wordCount"], 900)
        with patch.object(self.runtime, "_adjust_words", return_value="风" * 700) as adjust:
            await self.runtime._calibrate({**job, "extendedFromChapter": 11}, {}, chapters, 14_000, (9180, 12036), {})
            self.assertEqual(adjust.call_args.args[1], 12)
            self.assertEqual(adjust.call_args.args[4], 850)
            self.assertEqual(self.store.get_chapter(job_id, 12)["wordCount"], 1800)

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

        def interrupt_after_commit(current_id, checkpoint, **options):
            if checkpoint["stage"] == "chapter_writing" and checkpoint.get("nextChapter") == 2:
                raise LLMError("模拟提交后重启", code="TIMEOUT", retryable=True)
            return base_checkpoint(current_id, checkpoint, **options)

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
