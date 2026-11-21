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

    async def test_story_ledger_name_collision_requires_a_distinct_full_name_in_the_source(self):
        book_plan = {"mainCharacters": [{"name": "赵明", "role": "反派"}]}
        cases = (
            ("新来的船医也叫赵明。他和管账的赵明不是同一人。", "赵明", True),
            ("赵明的妹妹赵兰是新来的船医。", "赵兰", False),
            ("管账的赵明改行做船医。", "赵明", False),
            ("真巧，新来的小赵明和船医同名。", "赵明", False),
            ("新来的船医用化名赵明，声称与管账的赵明同名。", "赵明", False),
            ("新来的船医名叫赵明，大家问他来做什么。", "赵明", False),
        )
        for body, name, blocked in cases:
            with self.subTest(body=body):
                verdict = {"passed": True, "issues": [], "aiIssues": [], "factsDelta": {},
                           "newCharacterCandidates": [{"name": name, "identity": "新船医", "quote": body}]}
                client = await self.client(lambda _: _reply(verdict))
                opts = dict(chapter_number=3, chapter_text=body, min_words=0, llm_config=_MODEL, client=client,
                            book_plan=book_plan, max_retries=0, degrade_on_verify_exhausted=True)
                actual = await verifier.verify_and_repair_chapter(**opts, story_ledger=True)
                self.assertEqual(actual["passed"], not blocked)
                self.assertFalse(actual["canCommitForReview"])
                if blocked:
                    self.assertEqual(actual["verification"]["issues"][0]["type"], "character_name_collision")
                    self.assertIsNone(actual["factsDelta"])
                    self.assertTrue((await verifier.verify_and_repair_chapter(**opts))["passed"])

        identical_names = {"mainCharacters": [*book_plan["mainCharacters"], {"name": "赵明", "role": "船医"}]}
        body = cases[0][0]
        client = await self.client(lambda _: _reply({"passed": True, "issues": [], "aiIssues": [], "factsDelta": {},
                                                    "newCharacterCandidates": [{"name": "赵明", "identity": "船医", "quote": body}]}))
        result = await verifier.verify_chapter(chapter_number=3, chapter_text=body, min_words=0, llm_config=_MODEL, client=client,
                                               book_plan=identical_names, story_ledger=True)
        self.assertTrue(result["passed"])

    async def test_story_ledger_errors_need_current_quote_and_a_real_prior_reference(self):
        body = "赵明在渡口说，自己从没有见过铜铃。"
        facts = {"storyClock": {"day": "第三天", "fieldChapters": {"day": 2}},
                 "characterStates": {"赵明": {"stanceTier": "wary", "stanceTierChapter": 2,
                                            "knownFacts": ["亲眼见铜铃"], "knownFactChapters": {"亲眼见铜铃": 2}}}}
        conflict = {"type": "character_ooc", "severity": "warning", "message": "与此前认知冲突。",
                    "quote": body, "ledgerRef": {"kind": "character", "key": "赵明"}}
        verdict = {"passed": True, "issues": [conflict], "aiIssues": [], "factsDelta": {}}
        client = await self.client(lambda _: _reply(verdict))
        options = dict(chapter_number=3, chapter_text=body, canonical_facts=facts, llm_config=_MODEL, min_words=0,
                       client=client, max_retries=0, degrade_on_verify_exhausted=True, story_ledger=True)
        result = await verifier.verify_and_repair_chapter(**options)
        self.assertFalse(result["passed"])
        self.assertFalse(result["canCommitForReview"])
        self.assertEqual(result["verification"]["issues"][0]["sourceChapter"], 2)
        verdict["issues"] = [{**conflict, "severity": "error"}]
        self.assertTrue((await verifier.verify_and_repair_chapter(**{**options, "story_ledger": False}))["passed"])

        for replacement in ({"quote": "这是没有出现过的引文。"}, {"ledgerRef": {"kind": "character", "key": "无记录"}}):
            verdict["issues"] = [{**conflict, **replacement, "severity": "error"}]
            self.assertTrue((await verifier.verify_and_repair_chapter(**options))["passed"])
        verdict["issues"] = []
        explicit = "林渊救下了赵明的女儿，赵明终于答应结盟。"
        self.assertTrue((await verifier.verify_and_repair_chapter(**{**options, "chapter_text": explicit}))["passed"])

    async def test_story_ledger_blocks_only_grounded_exact_net_arithmetic(self):
        balances = {"ledgerBalances": [{"label": "码头备用金", "value": "600.10元", "updatedAtChapter": 1,
                                        "recentChanges": []}]}
        base = {"label": "码头备用金", "value": "801.20元", "change": "+200.10元"}
        tests = (
            ("码头备用金本章净变动：+200.10元，结算余额801.20元。", {}, False),
            ("码头备用金本章净变动：+200.10元，结算余额800.20元。", {"value": "800.20元"}, True),
            ("码头备用金本次进账+200.10元，随后又给了保安一笔，结算余额801.20元。", {}, True),
            ("码头备用金本章净变动：+200.10积分，结算余额801.20元。", {"change": "+200.10积分"}, True),
        )
        for quote, patch, passed in tests:
            with self.subTest(quote=quote):
                verdict = {"passed": True, "issues": [], "aiIssues": [], "factsDelta": {"memory": {
                    "ledgerBalances": [{**base, **patch, "quote": quote}],
                }}}
                client = await self.client(lambda _: _reply(verdict))
                result = await verifier.verify_chapter(chapter_number=2, chapter_text=quote, min_words=0, llm_config=_MODEL,
                                                       client=client, canonical_facts=balances, story_ledger=True)
                self.assertEqual(result["passed"], passed)
                if not passed:
                    self.assertEqual(result["issues"][0]["type"], "hard_value_conflict")
                    self.assertIsNone(result["factsDelta"])

    async def test_story_ledger_fact_extraction_fallback_cannot_commit_a_known_balance_conflict(self):
        quote = "码头备用金本章净变动：+200元，结算余额801元。"
        calls = []

        async def extract(body):
            calls.append(body)
            return {"memory": {"ledgerBalances": [{"label": "码头备用金", "value": "801元", "change": "+200元", "quote": body}]}}

        client = await self.client(lambda _: _reply({"passed": True, "issues": [], "aiIssues": []}))
        verification = await verifier.verify_and_repair_chapter(
            chapter_number=2, chapter_text=quote, min_words=0, llm_config=_MODEL, client=client, max_retries=0,
            canonical_facts={"ledgerBalances": [{"label": "码头备用金", "value": "600元", "updatedAtChapter": 1}]},
            story_ledger=True, extract_missing_facts=extract, degrade_on_verify_exhausted=True,
        )
        self.assertEqual(calls, [quote])
        self.assertFalse(verification["passed"])
        self.assertFalse(verification["canCommitForReview"])
        self.assertEqual(verification["verification"]["issues"][0]["type"], "hard_value_conflict")

    async def test_phase2_local_rules_degrade_only_without_fatal_issues(self):
        client = await self.client(lambda _: self.fail("the local gate should not call the reviewer"))
        cases = (
            ("cliche_filler_density", "潮声在瞬间变响。" * 6, None),
            ("mouth_arc_cliche", "掌柜嘴角慢慢泛起冷意。师傅唇角扬起轻蔑的笑。", None),
            ("explanatory_narration", "这番话看似在劝他回家，实则已经把退路封死。", None),
            ("repetitive_ending_hook", "【警报：电梯失控】", ["【系统提示：封闭出口】", "【警报：信号中断】"]),
        )
        for rule, body, history in cases:
            with self.subTest(rule=rule):
                options = {"chapter_number": 4, "chapter_text": body, "min_words": 0, "client": client,
                           "chapter_ai_gate": True, "recent_chapters": history, "max_retries": 0, "llm_config": _MODEL}
                strict = await verifier.verify_and_repair_chapter(**options)
                self.assertFalse(strict["canCommitForReview"])
                self.assertIn(rule, [issue.get("rule") for issue in strict["verification"]["issues"]])
                degraded = await verifier.verify_and_repair_chapter(**options, degrade_on_verify_exhausted=True)
                self.assertTrue(degraded["canCommitForReview"])
                self.assertTrue(degraded["exceededMaxRetries"])
                self.assertIsNone(degraded["factsDelta"])
                hard = await verifier.verify_and_repair_chapter(
                    **{**options, "chapter_text": "TBD。\n" + body}, degrade_on_verify_exhausted=True,
                )
                self.assertFalse(hard["canCommitForReview"])
                short = await verifier.verify_and_repair_chapter(
                    **{**options, "min_words": 999}, degrade_on_verify_exhausted=True,
                )
                self.assertFalse(short["canCommitForReview"])

    async def test_phase2_repair_receives_the_ending_quote_and_uses_the_same_history_on_recheck(self):
        history = ["更早一章。", "【系统提示：通道关闭】", "【警报：信号中断】"]
        tail = "【警报：电梯失控】"
        calls = []

        def handle(request):
            system, prompt = [msg["content"] for msg in json.loads(request.content)["messages"]]
            calls.append(system.splitlines()[0])
            self.assertEqual(calls[-1], "[TASK:REPAIR]")
            self.assertIn(tail, prompt)
            self.assertIn("改用本章独有的行动后果", prompt)
            # Keep the same bad hook to prove it is caught again with the original window.
            return _reply(_PROSE + "\n" + tail)

        client = await self.client(handle)
        result = await verifier.verify_and_repair_chapter(
            chapter_number=4, chapter_text=_PROSE + "\n" + tail, recent_chapters=history, llm_config=_MODEL,
            chapter_ai_gate=True, max_retries=1, min_words=0, client=client,
        )
        self.assertEqual(calls, ["[TASK:REPAIR]"])
        self.assertEqual(result["repairCount"], 1)
        self.assertTrue(result["exceededMaxRetries"])
        self.assertEqual(result["verification"]["issues"][0]["quote"], tail)

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
            prompt = body["messages"][1]["content"]
            calls.append((body["model"], system.split("\n")[0]))
            self.assertIn("钟楼只能于退潮时进入", prompt)
            self.assertIn("昨日船灯被取走", prompt)
            if system.startswith("[TASK:REPAIR]"):
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
            book_plan={"worldview": {"coreRules": ["钟楼只能于退潮时进入"]}},
            canonical_facts={"constraints": [{"id": "lamp", "text": "昨日船灯被取走", "status": "active", "sourceChapter": 1}]},
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

    async def test_only_typed_grounded_hard_conflicts_are_promoted(self):
        quote = "钟楼的影子越过了长街。"
        ordinary = {
            "passed": True,
            "issues": [
                {"type": kind, "severity": "warning", "message": "后续继续交代", "quote": quote}
                for kind in ("timeline", "secret_leak", "pov_violation", "word_count", "inconsistent_details")
            ],
            "aiIssues": [
                {"category": category, "priority": "P2", "quote": quote}
                for category in ("over_explaining", "formulaic_reaction", "shared_voice", "repetitive_hooks", "inconsistent_details")
            ],
            "factsDelta": {"items": {}},
        }
        verdict = ordinary
        client = await self.client(lambda _: _reply(verdict))
        options = {"chapter_number": 2, "chapter_text": _PROSE, "llm_config": _MODEL, "client": client,
                   "min_words": 100, "max_retries": 0, "degrade_on_verify_exhausted": True}
        for gate in (False, True):
            verdict = ordinary
            result = await verifier.verify_and_repair_chapter(**options, chapter_ai_gate=gate)
            self.assertTrue(result["passed"])
            verdict = {**ordinary, "issues": [*ordinary["issues"], {
                "type": "inconsistent_details", "hardConflictType": "hard_value_conflict",
                "severity": "warning", "message": "金额对不上", "quote": quote,
            }, {"type": "world_rule_conflict", "severity": "warning", "message": "违反世界观", "quote": quote,
            }, {"type": "world_rule_conflict", "severity": "warning", "message": "疑似违反世界观", "quote": "幻觉引文"}],
                "aiIssues": [*ordinary["aiIssues"], {"category": "core_scene_repetition", "priority": "P2", "quote": quote},
                             {"category": "world_rule_conflict", "priority": "P2", "quote": "幻觉引文"}]}
            result = await verifier.verify_and_repair_chapter(**options, chapter_ai_gate=gate)
            self.assertFalse(result["passed"])
            self.assertFalse(result["canCommitForReview"])
            self.assertIsNone(result["factsDelta"])
            errors = [row for row in result["verification"]["issues"] if row["severity"] == "error"]
            self.assertEqual([row["type"] for row in errors], ["hard_value_conflict", "world_rule_conflict", "core_scene_repetition"])
            self.assertTrue(all(row["quote"] == quote for row in errors))
            self.assertEqual(result["aiIssues"][-2]["priority"], "P1")

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
