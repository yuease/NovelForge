import json
import os
import re
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import AsyncMock, patch

from novel_forge.arc_planning import compact_arc_summaries, normalize_canon_locks
from novel_forge.book_plan import (
    CANON_LOCKS_V2_BUDGET, PLAN_V2_MARKER, BookPlanValidationError, canon_locks_text,
    collect_boundary_state, validate_book_plan, volume_card_text, volume_for_chapter, volume_ladder_text,
    volumes_for_range,
)
from novel_forge.llm import LLMError, STYLE_BIBLE_MARKER, call_mock_llm, estimate_tokens
from novel_forge.long_memory import normalize_arc_summary
from novel_forge.pipeline import PipelineRuntime, _plan_audit_evidence
from novel_forge.prompts import (
    build_arc_outlines_prompt, build_arc_summary_prompt, build_book_plan_prompt, build_chapter_outlines_prompt,
    build_chapter_write_prompt, build_full_book_audit_prompt, build_repair_prompt, build_verify_prompt,
)
from novel_forge.storage import Store, volume_source_hash
from novel_forge.verifier import verify_and_repair_chapter, verify_chapter


MEMBERS = ["灯塔铜钥", "蓝封证词", "旧船草图", "白釉墨瓶", "断弦徽记", "海盐箱单"]
MUTABLE = "canonLocks.boundaries[0]"
IMMUTABLE = "canonLocks.boundaries[1]"
QUOTE = "守门人当面递给她通行文书，准她带一位证人入内。"
NEW_RULE = "获准带一位证人进入灯塔密室"


def _raw_plan(total=200):
    middle = (total + 1) // 2
    volumes = [
        {"volume": 1, "title": "海岸查案", "chapters": [1, middle], "summary": "找齐海船证据",
         "escalation": {"pressure": "封港令即将落地", "costAtRisk": "证人面临停职", "retiredTactics": [],
                        "stageTransition": None}},
        {"volume": 2, "title": "港务院公审", "chapters": [middle + 1, total], "summary": "在公审上揭出旧案",
         "escalation": {"pressure": "港务院冻结调查并要求当天交出全部材料", "costAtRisk": "公审失败会失去保护证人的资格",
                        "stageTransition": "持盖章文书和证人签名后方可进入公审厅",
                        "retiredTactics": [
                            {"tactic": "夜里私访库房", "whyFails": "港外警戒升级并统一收回夜间通行证"},
                            {"tactic": "靠旧船绕开检查", "whyFails": "调证令要求记录出入港的每一艘船"},
                            {"tactic": "借旧编号查阅验货单", "whyFails": "船号已经改为双人核对的新登记簿"},
                        ]}},
    ]
    return {
        "title": "潮汐档案", "logline": "港口调查员沿弃船留下的证据为旧案翻案",
        "worldview": {"setting": "一座以航运为生的架空港城", "coreRules": ["跨港调证须有盖章文书"]},
        "mainCharacters": [{"name": "周青", "role": "主角"}],
        "volumes": [row for row in volumes if row["chapters"][0] <= row["chapters"][1]],
        "canonLocks": {
            "powerScale": "潮汐术只限预报水位",
            "currencyAndEra": "架空近代港城以元结算",
            "worldTaboos": ["潮汐术不得改变海流", "法庭休庭后才准外人调证"],
            "keyAssets": ["验货单一律以蓝墨钤印", "灯塔铜钥背面刻有海燕号"],
            "identityAliases": [{"character": "周青", "scope": "港务院", "alias": "海燕"}],
            "boundaries": [{"subject": "灯塔密室", "rule": "仅守门人可进入", "changeWhen": "守门人当面递交通行文书"},
                           {"subject": "潮汐塔", "rule": "无权开启底层水闸", "changeWhen": None}],
            "assetSets": [{"name": "海船案卷", "members": list(MEMBERS)}],
        },
        "customMeta": {"source": "fixture"},
    }


def _plan(total=200):
    plan, _, issues = validate_book_plan(_raw_plan(total), total_chapters=total)
    if issues:
        raise AssertionError(issues)
    return plan


def _reply(value, finish="stop"):
    return {"text": value if isinstance(value, str) else json.dumps(value, ensure_ascii=False), "finishReason": finish}


def _chapter(number, *, content=QUOTE, changes=(), status="verified", passed=True):
    return {"chapterNumber": number, "status": status, "content": content,
            "verification": {"passed": passed, "boundaryChanges": list(changes)}}


def _change(ref=MUTABLE, *, result=NEW_RULE, quote=QUOTE):
    return {"planRef": ref, "result": result, "quote": quote}


class BookPlanTests(unittest.TestCase):
    def test_valid_full_plan_and_normalization(self):
        raw = _raw_plan()
        raw["logline"] = "主" * 160
        raw["worldview"].update(setting="设" * 260, coreRules=["规" * 160] * 9, extra=True)
        raw["mainCharacters"] = [{"name": ""}] * 12 + [{"name": f"人物{i}"} for i in range(13)]
        raw["foreshadowingPlan"] = [{"id": str(i)} for i in range(17)]
        raw["volumes"][0]["extra"] = 42
        plan, warnings, issues = validate_book_plan(raw, total_chapters=200, rolling_arc_outlines=True)
        self.assertEqual((warnings, issues), ([], []))
        self.assertEqual(plan["planVersion"], 2)
        self.assertEqual(plan["canonLocks"], raw["canonLocks"])
        self.assertEqual(len(plan["logline"]), 120)
        self.assertEqual(len(plan["worldview"]["setting"]), 220)
        self.assertEqual(list(map(len, plan["worldview"]["coreRules"])), [120] * 8)
        self.assertEqual(len(plan["mainCharacters"]), 12)
        self.assertEqual(plan["mainCharacters"][0]["name"], "人物0")
        self.assertEqual(len(plan["foreshadowingPlan"]), 16)
        self.assertEqual(plan["customMeta"], raw["customMeta"])
        self.assertTrue(plan["worldview"]["extra"])
        self.assertEqual(plan["volumes"][0]["extra"], 42)
        self.assertNotIn("planVersion", raw)

    def test_fatal_paths_and_closed_sets(self):
        def fail(raw, path, message=""):
            with self.assertRaises(BookPlanValidationError) as raised:
                validate_book_plan(raw, total_chapters=200)
            self.assertTrue(any(error.startswith(path + ":") for error in raised.exception.field_errors),
                            raised.exception.field_errors)
            self.assertIn(message, str(raised.exception))

        for raw in (None, "坏JSON", [], {}):
            fail(raw, "bookPlan")
        for field, value, path in (("title", " ", "title"), ("worldview", {}, "worldview.setting"),
                                   ("mainCharacters", [{"name": ""}], "mainCharacters"), ("volumes", [], "volumes")):
            fail({**_raw_plan(), field: value}, path)
        for first, second, path in (([1, 100], [102, 200], "volumes[1].chapters[0]"),
                                    ([1, 100], [100, 200], "volumes[1].chapters[0]"),
                                    ([True, 100], [101, 200], "volumes[0].chapters[0]"),
                                    ([1, 100], [101, 199], "volumes[1].chapters[1]"),
                                    ([1, 100], [101, 100], "volumes[1].chapters[1]")):
            raw = _raw_plan()
            raw["volumes"][0]["chapters"], raw["volumes"][1]["chapters"] = first, second
            fail(raw, path)
        raw = _raw_plan()
        raw["volumes"][0]["volume"] = True
        fail(raw, "volumes[0].volume")
        fail({**_raw_plan(), "canonLocks": "错"}, "canonLocks")
        for alias in ("海燕", "白鹭"):
            raw = _raw_plan()
            raw["canonLocks"]["identityAliases"].append({"character": "周青", "scope": "港务院", "alias": alias})
            fail(raw, "canonLocks.identityAliases[1]")
        for value in (MEMBERS[0], "", "甲" * 21, None):
            raw = _raw_plan()
            raw["canonLocks"]["assetSets"][0]["members"].append(value)
            fail(raw, "canonLocks.assetSets[0].members[6]")
            self.assertEqual(raw["canonLocks"]["assetSets"][0]["members"][-1], value)
        raw = _raw_plan()
        raw["canonLocks"]["assetSets"][0]["members"] = []
        fail(raw, "canonLocks.assetSets[0].members")
        raw = _raw_plan()
        raw["canonLocks"]["boundaries"][0]["rule"] = " "
        fail(raw, "canonLocks.boundaries[0].rule")
        del raw["canonLocks"]["boundaries"][0]["changeWhen"]
        fail(raw, "canonLocks.boundaries[0].changeWhen")
        raw = _raw_plan()
        raw["canonLocks"]["powerScale"] = "力" * 101
        fail(raw, "canonLocks.powerScale")
        raw["canonLocks"].update(powerScale="力" * 100, currencyAndEra="元" * 120,
                                 worldTaboos=["禁" * 65] * 4, keyAssets=["物" * 65] * 6)
        fail(raw, "canonLocks", "请压缩")

    def test_soft_ladder_mode_and_tactic_retention(self):
        raw = _raw_plan(40)
        raw["volumes"][1]["escalation"] = dict(raw["volumes"][0]["escalation"])
        _, _, issues = validate_book_plan(raw, total_chapters=40)
        self.assertEqual(len(issues), 2)
        self.assertIn("volumes[1].escalation.retiredTactics", issues[0])
        self.assertIn("与上一卷相同", issues[1])
        self.assertEqual(validate_book_plan(raw, total_chapters=40, strict_ladder=False)[2], [])
        short = _raw_plan(29)
        del short["volumes"][0]["escalation"]
        self.assertEqual(validate_book_plan(short, total_chapters=29)[2], [])
        self.assertIn("volumes[0].escalation.pressure", validate_book_plan(short, total_chapters=29, strict_ladder=True)[2][0])
        raw = _raw_plan()
        tactics = raw["volumes"][1]["escalation"]["retiredTactics"]
        raw["volumes"][0]["escalation"]["retiredTactics"] = [tactics[0]]
        tactics.insert(1, tactics[0])
        plan, _, issues = validate_book_plan(raw, total_chapters=200)
        self.assertEqual(issues, [])
        self.assertEqual(len(plan["volumes"][1]["escalation"]["retiredTactics"]), 3)
        self.assertEqual(plan["volumes"][0]["escalation"]["retiredTactics"][0],
                         plan["volumes"][1]["escalation"]["retiredTactics"][0])

    def test_temporal_state_and_plain_text(self):
        plan = _plan()
        history = [_chapter(100, changes=[_change(result="未经确认的状态")], passed=False),
                   _chapter(80, changes=[_change(), _change(IMMUTABLE)]),
                   _chapter(81, changes=[_change(result="另一个错误状态", quote="无从定位的引文")]),
                   _chapter(90, changes=[_change(result="目录草稿")], status="draft"),
                   _chapter(91, changes=[_change(result="待复核")], status="needs_review")]
        state = collect_boundary_state(history, before_chapter=160, book_plan=plan)
        self.assertEqual(state, [{**_change(), "chapter": 80}])
        for before in (50, 80):
            self.assertEqual(collect_boundary_state(history, before_chapter=before, book_plan=plan), [])
        latest = _chapter(110, changes=[_change(result="扩至两位证人")])
        self.assertEqual(collect_boundary_state([latest, *history], book_plan=plan),
                         [{**_change(result="扩至两位证人"), "chapter": 110}])
        self.assertEqual(collect_boundary_state(history, book_plan={**plan, "planVersion": 1}), [])
        locks = canon_locks_text(plan["canonLocks"], boundary_state=state)
        self.assertLessEqual(len(locks), CANON_LOCKS_V2_BUDGET)
        self.assertTrue(locks.startswith("不可变核心口径锁（初始值仅为历史基线）："))
        self.assertIn("（第80章起已变更：" + NEW_RULE + "）", locks)
        self.assertIn("全员：" + "、".join(MEMBERS), locks)
        self.assertIn("周青在港务院称「海燕」", locks)
        self.assertNotIn("第80章起", canon_locks_text(plan["canonLocks"]))

    def test_volume_projections_and_v1_quoted_clause(self):
        plan = _plan()
        self.assertEqual([row["volume"] for row in volumes_for_range(plan, 98, 104)], [1, 2])
        self.assertEqual(volumes_for_range(plan, 99, 100), [plan["volumes"][0]])
        self.assertEqual(volumes_for_range(plan, 102, 101), [])
        self.assertEqual(volume_for_chapter(plan, 101), plan["volumes"][1])
        self.assertIsNone(volume_for_chapter(plan, 201))
        card = volume_card_text(plan, 101)
        self.assertLessEqual(len(card), 300)
        for text in ("新增压迫", "真实代价", "退役手段", "跃迁条件", "夜里私访库房"):
            self.assertIn(text, card)
        self.assertLessEqual(len(volume_ladder_text(plan)), 1600)
        self.assertIn("第1卷", volume_ladder_text(plan))
        self.assertIn("第2卷", volume_ladder_text(plan))
        v1 = {**plan, "planVersion": 1}
        self.assertEqual(volumes_for_range(v1, 1, 200), [])
        self.assertIsNone(volume_for_chapter(v1, 1))
        self.assertEqual(volume_card_text(v1, 1), "")
        self.assertEqual(volume_ladder_text(v1), "")
        bulky = _raw_plan()
        ladder = bulky["volumes"][1]["escalation"]
        ladder.update(pressure="压" * 64, costAtRisk="险" * 64, stageTransition="阶" * 60)
        ladder["retiredTactics"] = [{"tactic": char * 32, "whyFails": "因" * 40} for char in "甲乙丙"]
        max_card = volume_card_text(validate_book_plan(bulky, total_chapters=200)[0], 101)
        self.assertLessEqual(len(max_card), 300)
        for label in ("压迫", "代价", ladder["stageTransition"], *(row["tactic"] for row in ladder["retiredTactics"])):
            self.assertIn(label, max_card)
        first = "已知。"
        text = first + "甲" * 96 + "。」其余规则。"
        self.assertEqual(normalize_canon_locks({"powerScale": text}), {"powerScale": first})

    def test_prompt_projections_across_volume_boundary(self):
        plan = _plan()
        state = [{**_change(), "chapter": 80}]
        rendered = canon_locks_text(plan["canonLocks"], boundary_state=state)
        for builder, kwargs in (
            (build_chapter_outlines_prompt, {"start_chapter": 98, "end_chapter": 103}),
            (build_arc_outlines_prompt, {"start_chapter": 98, "total_chapters": 200, "canonical_facts": {}, "arc_index": 3}),
            (build_arc_summary_prompt, {"arc": {"arcIndex": 3, "startChapter": 98, "endChapter": 103},
                                        "chapters": [_chapter(98)]}),
        ):
            prompt = builder(book_plan=plan, boundary_state=state, **kwargs)["prompt"]
            self.assertIn(rendered, prompt)
            self.assertIn("封港令即将落地", prompt)
            self.assertIn("港务院冻结调查", prompt)
            self.assertIn("夜里私访库房", prompt)
        verify = build_verify_prompt(chapter_number=101, chapter_text=QUOTE, book_plan=plan, boundary_state=state)
        repair = build_repair_prompt(chapter_number=101, chapter_text=QUOTE, book_plan=plan, boundary_state=state)
        self.assertIn("boundaryChanges", verify["system"])
        for reply in (verify, repair):
            self.assertIn(rendered, reply["prompt"])
            self.assertIn(volume_card_text(plan, 101), reply["prompt"])
        first = build_chapter_write_prompt(outline={"chapterNumber": 100}, book_plan=plan, boundary_state=state)["prompt"]
        second = build_chapter_write_prompt(outline={"chapterNumber": 101}, book_plan=plan, boundary_state=state)["prompt"]
        first_prefix, first_body = first.split("\n请撰写第")
        second_prefix, second_body = second.split("\n请撰写第")
        self.assertEqual(first_prefix, second_prefix)
        self.assertIn(rendered, first_prefix)
        self.assertIn(volume_card_text(plan, 100), first_body)
        self.assertIn(volume_card_text(plan, 101), second_body)
        plan_prompt = build_book_plan_prompt(plan_version=2, draft_quality=True)
        self.assertLess(plan_prompt["system"].index(PLAN_V2_MARKER), plan_prompt["system"].index(STYLE_BIBLE_MARKER))
        self.assertTrue(plan_prompt["system"].endswith(STYLE_BIBLE_MARKER))

    def test_audit_preserves_opening_evidence_with_bounded_snippets(self):
        plan = _plan()
        body = "漫长的叙述" * 50 + "港务院叫她海燕，她把灯塔铜钥收入海船案卷，随后赶往灯塔密室。" + "离开之后" * 30
        chapters = [_chapter(number, content=body) for number in (1, 10, 89, 101, 120, 180)]
        evidence = _plan_audit_evidence(plan, chapters)
        self.assertEqual(set(Counter((row["volume"], row["category"]) for row in evidence).values()), {2})
        self.assertTrue(all(len(row["quote"]) <= 120 and row["quote"] in body for row in evidence))
        self.assertEqual({row["chapter"] for row in evidence}, {1, 89, 101, 180})
        facts = {"items": {"灯塔铜钥": {"description": "早期物件"}, **{str(i): {} for i in range(643)}}}
        prompt = build_full_book_audit_prompt(book_plan=plan, canonical_facts=facts, plan_evidence=evidence,
                                             boundary_state=[{**_change(), "chapter": 80}])["prompt"]
        self.assertIn("总纲口径对应的正文证据", prompt)
        self.assertIn("港务院叫她海燕", prompt)
        self.assertIn("全书分卷阶梯", prompt)
        self.assertIn("第80章起已变更", prompt)

    def test_large_and_tight_chapter_budgets(self):
        for total in (200, 300):
            raw = _raw_plan(total)
            locks = raw["canonLocks"]
            locks.update(powerScale="力" * 100, currencyAndEra="元" * 120,
                         worldTaboos=[char * 65 for char in "甲乙丙丁"], keyAssets=[char * 48 for char in "戊己庚辛"])
            plan, _, _ = validate_book_plan(raw, total_chapters=total)
            lock_text = canon_locks_text(plan["canonLocks"])
            self.assertGreater(len(lock_text), 800)
            facts = {"items": {f"案卷物件{i}": {"description": "旁支案卷物证原属码头仓库" * 20, "sourceChapter": 40}
                               for i in range(643)}}
            card = volume_card_text(plan, 160)
            for budget in (16_384, 4_400):
                with self.subTest(total=total, budget=budget):
                    result = build_chapter_write_prompt(
                        book_plan=plan, outline={"chapterNumber": 160, "title": "进入港务院", "keyEvents": ["追查海船案卷"]},
                        canonical_facts=facts, total_chapters=total, input_budget=budget, long_memory=True,
                        draft_quality=True, chapter_ai_gate=True, rolling_arc_outlines=True, story_ledger=True,
                        prev_chapter_ending="周青逐项整理手里的物件，并将证词和纸页重新按日期收拢。" * 100,
                    )
                    self.assertLessEqual(estimate_tokens(result["system"] + "\n" + result["prompt"]), budget - 1024)
                    self.assertIn(lock_text, result["prompt"])
                    for member in MEMBERS:
                        self.assertIn(member, result["prompt"])
                    if budget == 16_384:
                        self.assertIn(card, result["prompt"])
                    else:
                        self.assertNotIn("【本卷升级阶梯", result["prompt"])

    def test_v2_arc_summary_normalizes_progress_and_canon_only(self):
        chapters = [_chapter(80)]
        drift = {"chapter": 80, "kind": "canon", "issue": "通行人数异常", "fixHint": "核对文书", "quote": QUOTE}
        raw = {"mainline": "通行文书已下达", "driftChecks": [drift, {**drift, "kind": "clock"}],
               "volumeProgress": {"escalation": "压" * 41, "cost": "代" * 31, "retiredReused": [42, "手" * 25, "偷看", "旁听"],
                                  "extra": True}}
        archive = normalize_arc_summary(raw, chapters, plan_canon=True)
        self.assertEqual(archive["driftChecks"], [drift])
        expected = {"escalation": "压" * 40, "cost": "代" * 30, "retiredReused": ["手" * 24, "偷看"]}
        self.assertEqual(archive["volumeProgress"], expected)
        self.assertNotIn("driftChecks", normalize_arc_summary(raw, chapters))
        archives = [{**archive, "startChapter": i, "endChapter": i + 4} for i in (11, 6, 1)]
        compact = compact_arc_summaries(archives)
        self.assertEqual(len(compact), 2)
        self.assertEqual(compact[0]["volumeProgress"], expected)


class BookPlanRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store = Store(self.directory)
        self.runtime = PipelineRuntime(self.store)
        self.addAsyncCleanup(self.runtime.close)
        self.enterContext(patch.dict(os.environ, {"NOVEL_FORGE_BOOK_PLAN_DEGRADED_OK": "0",
                                                  "NOVEL_FORGE_ROLLING_ARC_OUTLINES": "0"}))

    def _job(self, total=40, *, provider="deepseek"):
        job = self.store.create_job({"title": "潮汐档案", "premise": "港城旧案", "llmConfig": {"provider": provider},
                                     "topicPackage": {"title": "潮汐档案"}, "totalChapters": total, "wordsPerChapter": 850,
                                     "targetWords": total * 850})
        self.store.save_checkpoint(job["id"], {"stage": "topic_locked", "nextChapter": 1})
        return self.store.get_job_record(job["id"])

    async def test_retry_with_exact_feedback_and_partial_ladder_warning(self):
        job = self._job()
        partial = _raw_plan(40)
        del partial["volumes"][1]["escalation"]
        with patch.object(self.runtime, "_llm", side_effect=[_reply(partial), _reply(partial)]) as llm:
            saved = await self.runtime._book_plan(job, job["llmConfig"], "none", [], True)
        self.assertEqual(saved["bookPlan"]["volumeLadderStatus"], "partial")
        self.assertEqual(llm.await_count, 2)
        follow_up = llm.call_args.args[3]["prompt"]
        self.assertIn("volumes[1].escalation.pressure", follow_up)
        self.assertIn("volumes[1].escalation.retiredTactics", follow_up)
        self.assertIn("partial", str(self.store.get_job_record(job["id"])["logs"]))
        job = self._job()
        invalid = {**_raw_plan(40), "title": ""}
        with patch.object(self.runtime, "_llm", side_effect=[_reply(invalid), _reply(_raw_plan(40))]) as llm:
            saved = await self.runtime._book_plan(job, job["llmConfig"], "none", [], False)
        self.assertIn("title:", llm.call_args.args[3]["prompt"])
        self.assertNotIn("volumeLadderStatus", saved["bookPlan"])
        self.assertEqual(self.store.get_job_record(job["id"])["checkpoint"]["stage"], "chapter_outlines")

    async def test_p0_halts_and_manual_resume_recovers_from_original_checkpoint(self):
        job = self._job(1, provider="mock")
        with patch.object(self.runtime, "_llm", side_effect=[_reply(_raw_plan(1), "length"), _reply("{broken")]) as llm:
            await self.runtime.start_job(job["id"])
            stopped = await self.runtime.wait_for_job(job["id"])
            self.assertIn("输出被截断", llm.call_args.args[3]["prompt"])
        self.assertEqual(stopped["status"], "interrupted")
        self.assertEqual(stopped["error"]["code"], "BOOK_PLAN_INVALID")
        self.assertTrue(stopped["error"]["retryable"])
        self.assertIsNone(stopped["bookPlan"])
        self.assertEqual(stopped["checkpoint"], job["checkpoint"])
        restarted = PipelineRuntime(Store(self.directory))
        self.addAsyncCleanup(restarted.close)
        with patch.object(restarted, "_launch", new_callable=AsyncMock) as launch:
            await restarted.recover_interrupted_jobs()
            launch.assert_not_awaited()
        await restarted.resume_job(job["id"])
        finished = await restarted.wait_for_job(job["id"])
        self.assertEqual(finished["status"], "completed", finished.get("error"))
        self.assertEqual(finished["bookPlan"]["planVersion"], 2)
        self.assertEqual(len(finished["bookPlan"]["volumes"]), 1)

    async def test_explicit_degraded_escape_hatch_and_truncated_exception(self):
        job = self._job()
        truncated = LLMError("truncated", code="OUTPUT_TRUNCATED", finish_reason="length")
        with patch.dict(os.environ, {"NOVEL_FORGE_BOOK_PLAN_DEGRADED_OK": "1"}):
            with patch.object(self.runtime, "_llm", side_effect=[truncated, _reply("{}")]) as llm:
                saved = await self.runtime._book_plan(job, job["llmConfig"], "none", [], False)
        self.assertIn("输出被截断", llm.call_args.args[3]["prompt"])
        self.assertTrue(saved["bookPlan"]["degraded"])
        self.assertEqual(saved["bookPlan"]["volumeLadderStatus"], "partial")
        self.assertIn("保存临时总纲", str(self.store.get_job_record(job["id"])["logs"]))

    async def test_extend_preserves_v2_and_builds_ladder_with_or_without_direction(self):
        for direction in ("", "赶往海事法院与证人会面"):
            with self.subTest(direction=direction):
                job = self._job(40, provider="mock")
                plan = _plan(40)
                self.store.update_job(job["id"], {"bookPlan": plan, "status": "completed"})
                with patch.object(self.runtime, "_launch", new_callable=AsyncMock):
                    await self.runtime.extend_job(job["id"], add_chapters=3, direction=direction)
                saved = self.store.get_job_record(job["id"])["bookPlan"]
                self.assertEqual(saved["planVersion"], 2)
                self.assertEqual(saved["canonLocks"], plan["canonLocks"])
                self.assertEqual(saved["volumes"][:2], plan["volumes"])
                self.assertEqual(saved["volumes"][2]["chapters"], [41, 43])
                self.assertEqual(validate_book_plan(saved, total_chapters=43)[2], [])
                if direction:
                    self.assertIn(direction, saved["volumes"][2]["escalation"]["pressure"])

    async def test_mock_one_chapter_and_large_plan(self):
        for total, volumes in ((1, 1), (300, 2)):
            prompt = build_book_plan_prompt(plan_version=2, total_chapters=total, draft_quality=True)
            reply = await call_mock_llm(**prompt)
            plan, _, issues = validate_book_plan(json.loads(reply["text"]), total_chapters=total, strict_ladder=True)
            self.assertEqual(issues, [])
            self.assertEqual(len(plan["volumes"]), volumes)
            self.assertIn("styleBible", plan)
            self.assertIn("assetSets", plan["canonLocks"])
        v1 = await call_mock_llm(**build_book_plan_prompt(total_chapters=300))
        self.assertNotIn("planVersion", json.loads(v1["text"]))

    async def test_arc_archive_persists_v2_fields_with_story_ledger_off(self):
        job = self._job(1, provider="mock")
        arc = {"arcIndex": 1, "startChapter": 1, "endChapter": 1, "arcTitle": "进门"}
        plan = _plan(1)
        self.store.update_job(job["id"], {"bookPlan": plan, "arcPlans": [arc]})
        self.store.save_chapter(job["id"], 1, {**_chapter(1), "title": "递文书"})
        summary = {"mainline": "守门人递交通行文书", "volumeProgress": {"escalation": "取得通行证"},
                   "driftChecks": [{"chapter": 1, "kind": "canon", "issue": "口径冲突", "fixHint": "核对文书", "quote": QUOTE}]}
        with patch.object(self.runtime, "_llm", return_value=_reply(summary)) as llm:
            await self.runtime._archive_arc(job["id"], arc, job["llmConfig"], book_plan=plan)
        self.assertIn("volumeProgress", llm.call_args.args[3]["prompt"])
        record = self.store.get_canonical_facts(job["id"])["arcSummaries"][0]
        self.assertEqual(record["volumeProgress"], summary["volumeProgress"])
        self.assertEqual(record["driftChecks"], summary["driftChecks"])
        self.assertEqual(record["sourceHash"], volume_source_hash(self.store.get_chapters(job["id"])))

    async def test_verified_change_flows_from_committed_chapter_to_later_stages(self):
        job = self._job(8, provider="mock")
        seen = {}

        async def llm(*, system, prompt, **_):
            task = re.search(r"\[TASK:([A-Z_]+)\]", system)[1]
            if task == "BOOK_PLAN":
                return _reply(_raw_plan(8))
            seen.setdefault(task, []).append(prompt)
            reply = await call_mock_llm(system=system, prompt=prompt)
            if task == "CHAPTER_WRITE" and "\n请撰写第1/8章" in prompt:
                reply["text"] = f"港务院叫周青海燕，灯塔铜钥入了海船案卷。{QUOTE}\n\n" + reply["text"]
            if task == "VERIFY" and prompt.startswith("请一次性完成第1章"):
                result = json.loads(reply["text"])
                result["boundaryChanges"] = [_change()]
                reply = _reply(result)
            return reply

        with patch.dict(os.environ, {"NOVEL_FORGE_ROLLING_ARC_OUTLINES": "1", "NOVEL_FORGE_STORY_LEDGER": "0"}):
            with patch("novel_forge.pipeline.call_llm", side_effect=llm), patch("novel_forge.verifier.call_llm", side_effect=llm):
                await self.runtime.start_job(job["id"])
                done = await self.runtime.wait_for_job(job["id"])
        self.assertEqual(done["status"], "completed", done.get("error"))
        first = self.store.get_chapter(job["id"], 1)
        self.assertEqual(first["verification"]["boundaryChanges"], [_change()])
        transition = "第1章起已变更：" + NEW_RULE
        for task in ("CHAPTER_WRITE", "VERIFY", "ARC_OUTLINES"):
            self.assertNotIn(transition, seen[task][0])
            self.assertTrue(all(transition in prompt for prompt in seen[task][1:]))
        self.assertNotIn(transition, seen["ARC_SUMMARY"][0])
        audit = seen["FULL_AUDIT"][0]
        self.assertIn(transition, audit)
        plan_evidence = audit.split("【总纲口径对应的正文证据】")[1].split("按证据章号")[0]
        self.assertIn(QUOTE, plan_evidence)
        self.assertIn("港务院叫周青海燕", plan_evidence)


class BookPlanVerifierTests(unittest.IsolatedAsyncioTestCase):
    async def _verify(self, remote, *, book_plan):
        with patch("novel_forge.verifier.call_llm", return_value=_reply(remote)):
            return await verify_chapter(chapter_number=80, chapter_text=QUOTE, min_words=0, book_plan=book_plan)

    async def test_only_verified_mutable_changes_are_saved_and_v1_ignores_them(self):
        remote = {"passed": True, "issues": [], "aiIssues": [], "boundaryChanges": [
            _change(), _change(IMMUTABLE), _change("canonLocks.boundaries[7]"), _change(quote="无从定位的伪造引文"),
            _change(result="甲" * 49), _change(quote="守门"), _change(result=""), _change(quote="引" * 121),
        ]}
        v2 = await self._verify(remote, book_plan=_plan())
        self.assertTrue(v2["passed"], v2["issues"])
        self.assertEqual(v2["boundaryChanges"], [_change()])
        v1 = await self._verify(remote, book_plan={**_plan(), "planVersion": 1})
        self.assertTrue(v1["passed"])
        self.assertNotIn("boundaryChanges", v1)

    async def test_plan_conflict_ref_and_quote_grounding(self):
        plan = _plan()
        issue = {"type": "world_rule_conflict", "severity": "error", "message": "进入范围超出约定",
                 "planRef": MUTABLE, "quote": QUOTE}
        for ref in (MUTABLE, "canonLocks.assetSets[0].members[5]", "volumes[1].escalation.retiredTactics[0]"):
            result = await self._verify({"passed": False, "issues": [{**issue, "planRef": ref}], "aiIssues": [],
                                         "boundaryChanges": [_change()]}, book_plan=plan)
            self.assertFalse(result["passed"])
            self.assertEqual(result["issues"][0]["planRef"], ref)
            self.assertEqual(result["issues"][0]["quote"], QUOTE)
            self.assertNotIn("boundaryChanges", result)
        for fields in ({"planRef": "canonLocks.boundaries[99]"}, {"planRef": "title"},
                       {"planRef": "volumes[1].escalation.retiredTactics[3]"}, {"quote": "正文不存在的引文"}):
            result = await self._verify({"passed": False, "issues": [{**issue, **fields}], "aiIssues": []}, book_plan=plan)
            self.assertTrue(result["passed"], result["issues"])
            self.assertEqual(result["issues"][0]["severity"], "warning")
        v1_issue = {key: value for key, value in issue.items() if key != "planRef"}
        v1 = await self._verify({"passed": False, "issues": [v1_issue], "aiIssues": []}, book_plan=_raw_plan())
        self.assertFalse(v1["passed"])
        self.assertNotIn("planRef", v1["issues"][0])

    async def test_review_and_repair_stage_receive_boundary_state(self):
        state = [{**_change(), "chapter": 80}]
        body = QUOTE * 30
        issue = {"type": "world_rule_conflict", "severity": "error", "message": "范围扩大",
                 "planRef": MUTABLE, "quote": QUOTE}
        with patch("novel_forge.verifier.call_llm", side_effect=[
            _reply({"passed": False, "issues": [issue], "aiIssues": []}), _reply(body),
            _reply({"passed": True, "issues": [], "aiIssues": [], "boundaryChanges": [_change()]}),
        ]) as llm:
            result = await verify_and_repair_chapter(chapter_number=160, chapter_text=body, min_words=0, max_words=2000,
                                                     book_plan=_plan(), boundary_state=state, max_retries=1)
        self.assertTrue(result["passed"], result["verification"])
        self.assertEqual(result["verification"]["boundaryChanges"], [_change()])
        for call in llm.call_args_list:
            self.assertIn("第80章起已变更", call.kwargs["prompt"])

    async def test_manual_chapter_reverify_route_passes_boundary_state(self):
        from fastapi.testclient import TestClient
        from novel_forge.app import create_app

        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(data_dir=Path(tmp))
            with TestClient(app) as client:
                store = app.state.store
                job = store.create_job({"title": "潮汐档案", "premise": "港口旧案", "totalChapters": 4, "wordsPerChapter": 800})
                store.update_job(job["id"], {"bookPlan": _plan(4)})
                store.save_chapter(job["id"], 1, {"title": "第一章", "content": QUOTE, "status": "verified",
                                                  "verification": {"passed": True, "boundaryChanges": [_change()]}})
                store.save_chapter(job["id"], 2, {"title": "第二章", "content": QUOTE * 15, "status": "verified",
                                                  "verification": {"passed": True}})
                captured = {}

                async def fake_verify(**kwargs):
                    captured.update(kwargs)
                    return {"passed": True, "issues": [], "aiIssues": [], "summary": "ok"}

                with patch("novel_forge.app.verify_chapter", side_effect=fake_verify):
                    res = client.put(f"/api/jobs/{job['id']}/chapters/2", json={"content": QUOTE * 15, "reverify": True})
                self.assertEqual(res.status_code, 200, res.text)
                self.assertEqual(captured["boundary_state"], [{**_change(), "chapter": 1}])
