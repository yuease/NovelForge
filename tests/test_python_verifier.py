import asyncio
import json
import tempfile
import unittest
from contextlib import aclosing
from pathlib import Path

import httpx

from novel_forge import verifier
from novel_forge.llm import LLMError, reset_mock_state, reset_provider_circuit_state
from novel_forge.storage import Store


_MODEL = {"provider": "openai", "baseUrl": "http://127.0.0.1:6815/v1", "maxRetries": 0}
_PROSE = ("傍晚时分，钟楼的影子越过了长街。\n一只麻雀落在栏杆上，船上的灯才亮起来。\n"
          "潮水漫过台阶，旧信封在门板底下露出一角。\n") * 15


def _reply(value):
    return httpx.Response(200, json={"choices": [{"message": {
        "content": value if isinstance(value, str) else json.dumps(value, ensure_ascii=False),
    }, "finish_reason": "stop"}]})


class VerifierParserTests(unittest.TestCase):
    def test_parse_full_fenced_embedded_and_truncated_array(self):
        self.assertEqual(verifier.parse_json_from_llm(' \ufeff {"ok":true} '), {"ok": True})
        fenced = '解释\n```json\n{"array":[1,2]}\n```\n补充说明'
        self.assertEqual(verifier.parse_json_from_llm(fenced), {"array": [1, 2]})
        self.assertEqual(verifier.parse_json_from_llm('前缀 [{"a":1}] 后缀'), [{"a": 1}])
        complete = [
            {"quote": '引号 "、反斜线 \\ 和花括号 }', "nested": [{"x": 12}]},
            {"id": 2},
        ]
        clipped = json.dumps(complete, ensure_ascii=False)[:-1] + ',{"bad":[{"nested":'
        self.assertEqual(verifier.parse_json_from_llm(clipped, []), complete)
        self.assertIsNone(verifier.parse_json_from_llm(clipped))
        self.assertEqual(verifier.parse_json_from_llm('[{"bad":NaN},{"good":1},{"x":', []), [{"good": 1}])
        self.assertEqual(verifier.parse_json_from_llm(12, {"fallback": True}), {"fallback": True})


class VerifierTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        reset_provider_circuit_state()
        reset_mock_state()
        self.addCleanup(reset_provider_circuit_state)
        self.addCleanup(reset_mock_state)

    async def client(self, handler):
        return await self.enterAsyncContext(httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    async def test_local_style_preserves_hard_issues_and_skips_initial_model_call(self):
        notes = {"characters": {"林默": {}}, "injuries": [{"sourceChapter": 1, "character": "林默", "description": "左臂骨折"}]}
        text = ("林默用完好无损的左臂开门。林默迎向潮头。林默熄灭灯。林默关上窗。林默收好钥匙。")
        client = await self.client(lambda _: self.fail("local gate should skip the model"))
        result = await verifier.verify_chapter(chapter_number=2, chapter_text=text, canonical_facts=notes,
                                               llm_config=_MODEL, chapter_ai_gate=True, min_words=200, client=client)
        self.assertEqual([issue["type"] for issue in result["issues"]], ["word_count", "injury_contradiction", "ai_flavor"])
        self.assertEqual(result["issues"][1]["sourceChapter"], 1)
        self.assertIsNone(result["usage"])
        self.assertIsNone(result["factsDelta"])

    async def test_grounded_issues_block_once_and_strict_invalid_json_never_repairs(self):
        options = {"chapter_number": 2, "chapter_text": _PROSE, "llm_config": _MODEL,
                   "min_words": 100, "chapter_ai_gate": True}
        verdict = {
            "passed": False, "issues": [{"type": "ai_flavor", "severity": "error", "message": "同一条重复项"},
                                         {"type": "word_count", "severity": "error", "message": "过度判定字数"}],
            "aiIssues": [{"priority": "P1", "quote": "钟楼的影子越过了长街。", "category": "over_explaining"},
                         {"priority": "P0", "quote": "没有这段引文"}, {"priority": "P2", "quote": "灯才亮起来"}],
            "factsDelta": {"unexpected": "not admitted"},
        }
        calls = []

        def handle(request):
            calls.append(json.loads(request.content)["messages"][0]["content"])
            return _reply(verdict)

        client = await self.client(handle)
        reviewed = await verifier.verify_chapter(**options, client=client)
        self.assertFalse(reviewed["passed"])
        self.assertEqual([(issue["type"], issue["severity"]) for issue in reviewed["issues"]],
                         [("ai_flavor", "error"), ("word_count", "warning")])
        self.assertIsNone(reviewed["factsDelta"])
        verdict = "{malformed"
        with self.assertRaises(LLMError) as caught:
            await verifier.verify_and_repair_chapter(**options, client=client)
        self.assertEqual((caught.exception.code, caught.exception.status), ("CHAPTER_REVIEW_INVALID_JSON", 502))
        self.assertTrue(all(message.startswith("[TASK:VERIFY]") for message in calls))
        self.assertEqual(len(calls), 2)

    async def test_explicit_stages_split_models_log_repair_and_return_final_snapshot(self):
        store = Store(Path(self.enterContext(tempfile.TemporaryDirectory())))
        job_id = store.create_job()["id"]
        calls, events = [], []
        repair = _PROSE.replace("旧信封", "泛黄信封")

        def handle(request):
            body = json.loads(request.content)
            system = body["messages"][0]["content"]
            calls.append((body["model"], system.split("\n")[0]))
            if system.startswith("[TASK:REPAIR]"):
                prompt = body["messages"][1]["content"]
                self.assertNotIn("【一并清理的 AI 味/重复问题】", prompt)
                return _reply(repair)
            check = {"passed": True, "issues": [], "aiIssues": [], "factsDelta": {"items": {"信封": {"owner": "船医"}}}}
            if len(calls) == 1:
                check.update(passed=False, aiIssues=[{"quote": "钟楼的影子越过了长街。", "priority": "P1",
                                                      "category": "over_explaining", "suggestion": "减少反复"}])
            return _reply(check)

        async def on_event(name, values):
            events.append((name, values))

        client = await self.client(handle)
        async with aclosing(verifier.review_chapter_stages(
            chapter_number=2, chapter_text=_PROSE, job_id=job_id, store=store, client=client, min_words=80,
            llm_config={**_MODEL, "model": "writer"}, verify_llm_config={**_MODEL, "model": "reviewer"}, max_retries=2,
            chapter_ai_gate=True, long_memory=True, on_event=on_event,
        )) as stages:
            self.assertEqual((await anext(stages))["phase"], "verify")
            first = await anext(stages)
            self.assertTrue(first["canRepair"])
            self.assertEqual((await stages.asend("repair"))["phase"], "verify")
            last = await anext(stages)
            self.assertFalse(last["canRepair"])
            self.assertTrue(last["passed"])
            self.assertEqual(last["result"]["factsDelta"]["items"]["信封"]["owner"], "船医")
            self.assertEqual(last["result"]["chapterText"], repair.strip())
            self.assertEqual(first["result"]["repairs"], [])
            with self.assertRaises(StopAsyncIteration):
                await anext(stages)
        self.assertEqual(calls, [("reviewer", "[TASK:VERIFY]"), ("writer", "[TASK:REPAIR]"), ("reviewer", "[TASK:VERIFY]")])
        self.assertEqual([name for name, _ in events], ["chapter:repair_started", "chapter:repaired"])
        self.assertTrue(store.get_job_record(job_id)["repairLogs"][0]["resolved"])
        self.assertEqual(store.get_chapters(job_id), [])

    async def test_exhaustion_degrades_only_nonfatal_and_downstream_counts(self):
        def handle(_):
            return _reply({"passed": False, "issues": [], "aiIssues": [{
                "quote": "钟楼的影子越过了长街。", "priority": "P1", "category": "shared_voice",
            }], "factsDelta": {"fake": True}})

        client = await self.client(handle)
        options = {"chapter_number": 2, "chapter_text": _PROSE, "llm_config": _MODEL,
                   "min_words": 100, "chapter_ai_gate": True, "client": client, "max_retries": 0}
        strict = await verifier.verify_and_repair_chapter(**options)
        self.assertFalse(strict["canCommitForReview"])
        degraded = await verifier.verify_and_repair_chapter(**options, degrade_on_verify_exhausted=True)
        self.assertTrue(degraded["canCommitForReview"])
        self.assertTrue(degraded["exceededMaxRetries"])
        self.assertIsNone(degraded["factsDelta"])
        hard = await verifier.verify_and_repair_chapter(**{**options, "chapter_text": "TBD\n" + _PROSE},
                                                        degrade_on_verify_exhausted=True)
        self.assertFalse(hard["canCommitForReview"])
        downstream = await verifier.recheck_downstream_chapters(
            changed_from_chapter=2, chapters=[{"chapterNumber": 4, "content": _PROSE},
                                              {"chapterNumber": 3, "content": "TBD"}, {"chapterNumber": 1, "content": ""}],
            target_words=250,
        )
        self.assertEqual(downstream["checkedCount"], 2)
        self.assertEqual([chapter["chapterNumber"] for chapter in downstream["affectedChapters"]], [3])

    async def test_active_request_cancellation_is_propagated(self):
        entered = asyncio.Event()

        async def blocked(_):
            entered.set()
            await asyncio.Future()

        client = await self.client(blocked)
        task = asyncio.create_task(verifier.verify_and_repair_chapter(
            chapter_number=1, chapter_text=_PROSE, llm_config=_MODEL, min_words=80, client=client,
        ))
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
