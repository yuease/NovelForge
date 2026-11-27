import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from novel_forge.arc_planning import (
    SKELETON_CODES, arc_rhythm, derive_arc_themes, find_theme_collisions, horizon_memory, normalize_skeleton,
    skeleton_similarity, validate_arc_plan,
)
from novel_forge.llm import LLMError, call_mock_llm, estimate_tokens
from novel_forge.long_memory import normalize_arc_summary
from novel_forge.pipeline import PipelineRuntime, read_writing_options
from novel_forge.prompt_budget import ARC_PROMPT_BUDGET_EXCEEDED, fit_prompt, read_arc_input_budget
from novel_forge.prompts import build_arc_outlines_prompt, build_chapter_write_prompt
from novel_forge.storage import Store, volume_source_hash


REPEATED_SKELETON = ["investigation", "confrontation"]
OLD_THEMES = ["长绒棉保暖内衣", "跪坐改高脚椅"]
QUOTE = "掌柜提起木箱，露出背面的封印。"
TOKEN_LIMIT = 16_384 - 1024


def chapter(number):
    return {"chapterNumber": number, "title": f"灯下第{number}份卷宗", "content": QUOTE,
            "summary": "验明封印后封存卷宗", "status": "verified"}


def candidate(start, count, *, skeleton=None):
    raw = {
        "arcTitle": "巡堤人的账本",
        "arcGoal": "确认账本残页的流向",
        "chapters": [
            {"chapterNumber": n, "title": f"值夜簿第{n}页", "goal": "找出抄录者", "summary": "根据脚印找到抄录者",
             "hook": "抄录者递出残页", "keyEvents": ["辨认堤上的脚印"]}
            for n in range(start, start + count)
        ],
    }
    if skeleton is not None:
        raw["skeleton"] = skeleton
    return raw


def arc_history(lengths, *, skeleton=None):
    rows, start = [], 1
    for index, length in enumerate(lengths, 1):
        rows.append({"arc": index, "startChapter": start, "endChapter": start + length - 1,
                     "mainline": f"第{index}单元完成封印交割",
                     **({"skeleton": skeleton} if skeleton is not None else {})})
        start += length
    return rows


def large_history():
    rows = arc_history([3] * 8 + [5] + [3] * 44 + [4] * 7)
    plans = [{"arcIndex": row["arc"], "startChapter": row["startChapter"], "endChapter": row["endChapter"],
              "arcTitle": f"案卷交割{row['arc']}"} for row in rows]
    for row in rows:
        index = row["arc"]
        row.update(
            themes=[f"封印案卷{index}"], shape="调查卷宗之后完成交割。",
            skeleton=REPEATED_SKELETON,
            usedBeats=[{"chapter": row["startChapter"] + n % 3, "text": f"独有机制{index}号{n}",
                        "quote": "原文只留归档"} for n in range(7 if index == 1 else 4)],
            volumeProgress={"escalation": f"港口传令范围升级{index}", "retiredReused": ["旧库房取证"]},
        )
    rows[9]["themes"] = [*OLD_THEMES]
    rows[8]["themes"] = [OLD_THEMES[1]]
    rows[-1]["driftChecks"] = [
        {"chapter": 189, "kind": "canon", "issue": "误用旧通行令", "fixHint": "改核蓝印通行令", "quote": "只留存档"}
    ]
    volumes = [{"volume": end // 10, "startChapter": end - 9, "endChapter": end, "sourceHash": str(end),
                "mainline": f"{end}章以前的港口卷摘要" + "甲" * 220,
                "turningPoints": [{"chapter": end - n, "text": "蓝印通行令"} for n in range(4)]}
               for end in range(10, 201, 10)]
    facts = {
        "arcSummaries": rows,
        "volumeSummaries": volumes,
        "characters": {f"旧名册{i}": {"role": "港口巡查处临时守夜人" * 36, "sourceChapter": 9} for i in range(75)},
        "foreshadowing": [{"id": f"clue-{i}", "clue": f"收网暗号{i}", "status": "planted", "sourceChapter": i + 1}
                          for i in range(26)],
    }
    return facts, plans


def plan_v2():
    return {
        "planVersion": 2, "title": "港口问案", "logline": "巡堤人调查港口旧案", "canonLocks": {"powerScale": "潮汐术只限预报水位"},
        "volumes": [
            {"volume": 1, "chapters": [1, 189], "escalation": {"threat": "旧卷路标"}},
            {"volume": 2, "chapters": [190, 200], "escalation": {"threat": "海关加封", "cost": "交出旧印",
             "stageTransition": "当面验蓝印后才能入港", "retiredTactics": [{"tactic": "夜访库房", "whyFails": "撤销夜间许可"}]}},
        ],
    }


def outline_material(prompt):
    block = prompt["prompt"].split("【已发生事实与全书路标】\n")[1].split("\n【输出结构】\n")[0]
    return json.loads(block.splitlines()[-1])


def prompt_tokens(prompt):
    return estimate_tokens(f"{prompt['system']}\n{prompt['prompt']}")


class Stage2MemoryTests(unittest.TestCase):
    def test_archive_round_robin_evidence_caps_and_optional_fields(self):
        rows = [chapter(n) for n in range(29, 35)]
        beats = [{"chapter": 29, "text": f"动作{i}", "quote": QUOTE} for i in range(4)]
        beats += [{"chapter": 31, "text": "退还印信", "quote": "从未写过的台词"},
                  {"chapter": 31, "text": "归还木箱", "quote": QUOTE}]
        raw = {"mainline": "封存木箱", "usedBeats": beats, "themes": [" 高脚椅 ", "", "高脚椅", "案" * 26, 5, "封印", "多余"],
               "skeleton": ["investigation", "bad", "investigation", {}, "trial", "investigation", "reward", "scheme", "reveal"],
               "shape": "甲" * 45}
        result = normalize_arc_summary(raw, rows[:4])
        self.assertEqual([(row["chapter"], row["text"]) for row in result["usedBeats"]],
                         [(29, "动作0"), (31, "归还木箱"), (29, "动作1")])
        self.assertEqual(result["themes"], ["高脚椅", "案" * 24, "封印"])
        self.assertEqual(result["skeleton"], ["investigation", "trial", "investigation", "reward", "scheme"])
        self.assertEqual(result["shape"], "甲" * 40)
        self.assertEqual(len(SKELETON_CODES), 16)
        self.assertEqual(normalize_skeleton("investigation"), [])
        raw["usedBeats"] += [{"chapter": n, "text": f"交割{n}", "quote": QUOTE} for n in (30, 32, 33, 34)]
        self.assertEqual([row["chapter"] for row in normalize_arc_summary(raw, rows)["usedBeats"]], list(range(29, 35)))
        legacy = normalize_arc_summary({"mainline": "封存木箱", "themes": [0], "shape": " ", "skeleton": ["bad"]}, rows)
        self.assertEqual(legacy, {"mainline": "封存木箱", "turningPoints": [], "usedBeats": [], "usedHooks": [], "villainStatus": ""})

    def test_horizon_ranges_bounded_recent_beats_and_full_book_themes(self):
        facts, plans = large_history()
        self.assertEqual(sum(len(row["usedBeats"]) for row in facts["arcSummaries"]), 243)
        memory = horizon_memory(facts, plans, 190)
        layers = []
        for layer in ("near", "capsules", "volumes"):
            covered = set()
            for row in memory[layer]:
                start, end = (map(int, row["range"].split("-")) if "range" in row
                              else (row["startChapter"], row["endChapter"]))
                self.assertLess(end, 190)
                covered.update(range(start, end + 1))
            self.assertTrue(covered)
            self.assertFalse(any(covered & past for past in layers))
            layers.append(covered)
        self.assertEqual([arc["endChapter"] for arc in memory["near"]], [189, 185])
        self.assertTrue(all(len(row["mainline"]) <= 90 for row in memory["capsules"]))
        self.assertTrue(all(len(row["turningPoints"]) == 2 and len(row["mainline"]) <= 160 for row in memory["volumes"][:3]))
        self.assertTrue(all("turningPoints" not in row and len(row["mainline"]) <= 60 for row in memory["volumes"][3:]))
        args = {"book_plan": {}, "canonical_facts": facts, "arc_plans": plans, "total_chapters": 200, "arc_index": 61}
        rendered = outline_material(build_arc_outlines_prompt(**args, start_chapter=190))
        self.assertEqual(rendered["recentBeats"], memory["recentBeats"])
        self.assertLessEqual(len(rendered["recentBeats"]), 12)
        self.assertTrue(all(182 <= row["chapter"] <= 189 for row in rendered["recentBeats"]))
        self.assertTrue(all("usedBeats" not in row for row in rendered["nearArcs"]))
        self.assertNotIn("usedBeats", rendered)
        for start in (49, 150, 190):
            material = outline_material(build_arc_outlines_prompt(**args, start_chapter=start))
            self.assertIn("30-32:" + "｜".join(OLD_THEMES), material["themeLedger"])
            self.assertEqual(sum(OLD_THEMES[1] in row for row in material["themeLedger"]), 1)

    def test_horizon_handles_straddling_volume_sparse_arcs_and_legacy_fallback(self):
        arc = {"arc": 1, "startChapter": 9, "endChapter": 13,
               "usedBeats": [{"chapter": 11, "text": "凭蓝印领取额外通行凭证" * 3}]}
        plan = {"arcIndex": 1, "startChapter": 9, "endChapter": 13, "arcTitle": "蓝印核验"}
        volumes = [{"volume": 1, "startChapter": 1, "endChapter": 10, "mainline": "最早卷"},
                   {"volume": 2, "startChapter": 11, "endChapter": 20, "mainline": "第二卷"},
                   {"volume": 3, "startChapter": 21, "endChapter": 30, "mainline": "第三卷"}]
        memory = horizon_memory({"arcSummaries": [arc], "volumeSummaries": volumes}, [plan], 56)
        self.assertEqual([row["range"] for row in memory["volumes"]], ["21-30"])
        self.assertEqual(memory["themes"], [{"range": "9-13", "tags": derive_arc_themes(arc, plan)}])
        self.assertEqual(memory["themes"][0]["tags"][0], "蓝印核验")
        self.assertEqual(len(memory["themes"][0]["tags"][1]), 20)

    def test_rolling_write_shows_theme_and_only_nonoverlapping_volumes(self):
        facts, _ = large_history()
        outline = {"chapterNumber": 150, "title": "换掉旧高脚椅", "goal": "照料来客", "keyEvents": ["把高脚椅移到天井"]}
        result = build_chapter_write_prompt(book_plan=plan_v2(), outline=outline, canonical_facts=facts,
                                            input_budget=16_384, rolling_arc_outlines=True, long_memory=True)
        block = result["prompt"].split("【本章相关规范事实 Relevant Facts（必须严格遵守）】\n")[1]
        rendered, _ = json.JSONDecoder().raw_decode(block)
        self.assertLessEqual(prompt_tokens(result), TOKEN_LIMIT)
        self.assertIn("30-32:跪坐改高脚椅", rendered["horizon"]["themeHits"])
        earlier = rendered["horizon"]["earlierVolume"]
        self.assertEqual(earlier["endChapter"], 140)
        self.assertLessEqual(len(earlier["mainline"]), 140)
        self.assertEqual([row["endChapter"] for row in rendered["longMemory"]["volumeSummaries"]], [140, 130])
        self.assertTrue(all(earlier["endChapter"] < row["startChapter"] for row in rendered["arcSummaries"]))

    def test_rhythm_hard_lengths_and_terminal_exemption(self):
        rows = arc_history([3, 3, 3])
        rhythm = arc_rhythm(rows, rows, start_chapter=10, total_chapters=40)
        self.assertEqual(rhythm["recentLengths"], [3, 3, 3])
        self.assertEqual(rhythm["allowedLengths"], [4, 5, 6])
        params = {"arc_index": 4, "start_chapter": 10, "total_chapters": 40,
                  "allowed_lengths": rhythm["allowedLengths"]}
        for soft in (True, False):
            with self.assertRaisesRegex(ValueError, "4、5、6"):
                validate_arc_plan(candidate(10, 3), **params, soft=soft)
        self.assertEqual(len(validate_arc_plan(candidate(10, 4), **params)[1]), 4)
        for remaining in range(1, 7):
            self.assertEqual(arc_rhythm(rows, rows, start_chapter=10, total_chapters=9 + remaining)["allowedLengths"],
                             [remaining])
        rows = arc_history([3, 3, 3, 3, 4, 3])
        self.assertEqual(arc_rhythm([], rows, start_chapter=20, total_chapters=26)["allowedLengths"], [4])
        rows = arc_history([4] * 6)
        for remaining in range(1, 13):
            self.assertTrue(arc_rhythm(rows, [], start_chapter=25, total_chapters=24 + remaining)["allowedLengths"])

    def test_skeleton_and_theme_checks_are_soft_and_legacy_archives_stay_compatible(self):
        rows = arc_history([3] * 6, skeleton=REPEATED_SKELETON)
        rhythm = arc_rhythm([], rows, start_chapter=19, total_chapters=40)
        self.assertEqual(skeleton_similarity(REPEATED_SKELETON, REPEATED_SKELETON[::-1]), 0.75)
        self.assertEqual(skeleton_similarity([], REPEATED_SKELETON), 0)
        self.assertEqual(rhythm["codeCounts"], {"investigation": 6, "confrontation": 6})
        self.assertEqual(rhythm["cooldownCodes"], REPEATED_SKELETON)
        self.assertTrue(rhythm["repeatSkeletonActive"])
        args = {"start": 19, "total": 40, "arc_index": 7, "allowed_lengths": rhythm["allowedLengths"]}
        raw = candidate(19, 4, skeleton=REPEATED_SKELETON)
        with self.assertRaisesRegex(ValueError, "叙事骨架"):
            validate_arc_plan(raw, **args, rhythm=rhythm)
        self.assertEqual(validate_arc_plan(raw, **args, rhythm=rhythm, soft=False)[0]["skeleton"], REPEATED_SKELETON)
        self.assertTrue(validate_arc_plan(candidate(19, 4, skeleton=["loss", "trial"]), **args, rhythm=rhythm))
        alternate = {**rhythm, "recent4Codes": [*REPEATED_SKELETON, "loss", "trial"]}
        with self.assertRaisesRegex(ValueError, "近期未用过"):
            validate_arc_plan(candidate(19, 4, skeleton=["loss", "trial"]), **args, rhythm=alternate)
        rows[-1].pop("skeleton")
        legacy_rhythm = arc_rhythm([], rows, start_chapter=19, total_chapters=40)
        self.assertFalse(legacy_rhythm["repeatSkeletonActive"])
        self.assertTrue(validate_arc_plan(raw, **args, rhythm=legacy_rhythm))
        self.assertNotIn("skeleton", validate_arc_plan(candidate(19, 4), **args, rhythm=rhythm)[0])
        themes = [{"range": "30-32", "tags": [*OLD_THEMES, "揭开真相"]}]
        raw["chapters"][0]["goal"] = "重新兜售保暖内衣"
        self.assertEqual(find_theme_collisions(raw, themes), [])
        raw["chapters"][1]["title"] = "用高脚椅"  # Four contiguous characters including 改 are needed for this tag.
        self.assertEqual(find_theme_collisions(raw, themes), [])
        raw["chapters"][1]["title"] = "改高脚椅的生意"
        raw["chapters"][2]["summary"] = "揭开真相以后散场"
        self.assertEqual(len(find_theme_collisions(raw, themes)), 2)
        with self.assertRaisesRegex(ValueError, "30-32:长绒棉保暖内衣.*30-32:跪坐改高脚椅"):
            validate_arc_plan(raw, **args, themes=themes)
        self.assertTrue(validate_arc_plan(raw, **args, themes=themes, soft=False))

    def test_outline_budget_protects_v2_material_retry_and_terminal_clues(self):
        facts, plans = large_history()
        preceding = [chapter(n) for n in range(185, 190)]
        preceding[-1]["content"] = "略" * 500 + "门卫举起蓝印通行令。"
        params = {
            "book_plan": plan_v2(), "canonical_facts": facts, "arc_plans": plans, "start_chapter": 190,
            "total_chapters": 200, "arc_index": 61, "preceding_chapters": preceding,
            "continuation_direction": "调查接下来由巡堤人主持",
        }
        unbounded = build_arc_outlines_prompt(**params)
        self.assertGreater(prompt_tokens(unbounded), TOKEN_LIMIT)
        for note in ("", "历史 30-32：旧议题重复。" * 30):
            result = build_arc_outlines_prompt(**params, input_budget=16_384, retry_note=note)
            material = outline_material(result)
            self.assertLessEqual(prompt_tokens(result), TOKEN_LIMIT)
            for value in ("潮汐术只限预报水位", "retiredTactics", "夜访库房", "当面验蓝印后才能入港", "allowedLengths", note):
                self.assertIn(value, result["prompt"])
            self.assertTrue(material["lastEnding"].endswith("门卫举起蓝印通行令。"))
            self.assertEqual(len(material["lastEnding"]), 350)
            self.assertEqual(material["continuationDirection"], params["continuation_direction"])
            self.assertEqual(material["previousArcDriftChecks"][0]["fixHint"], "改核蓝印通行令")
            self.assertEqual(material["nearArcs"][0]["volumeProgress"]["retiredReused"], ["旧库房取证"])
            self.assertEqual(material["patternStats"]["allowedLengths"], [3, 5, 6])
            self.assertEqual(len(material["bookPlan"]["volumes"]), 1)
            self.assertNotIn("原文只留归档", result["prompt"])
            self.assertLessEqual(len(material["canonicalFacts"].get("openForeshadowing", [])), 22)
        terminal = build_arc_outlines_prompt(**{**params, "start_chapter": 195}, input_budget=4_500, finalize_book=True)
        self.assertIn("终局收网单元", terminal["prompt"])
        self.assertIn("禁止开放性续作悬念", terminal["prompt"])
        terminal_material = outline_material(terminal)
        self.assertEqual(len(terminal_material["uncollectedForeshadowing"]), 26)
        self.assertEqual(terminal_material["patternStats"]["allowedLengths"], [6])
        self.assertLessEqual(prompt_tokens(terminal), 4_500 - 1024)

    def test_arc_budget_options_disable_and_report_protected_overflow(self):
        self.assertEqual(read_arc_input_budget(None), 16_384)
        for value in (0, "0"):
            self.assertIsNone(read_arc_input_budget(value))
        for value in ("-1", "invalid", 1024):
            with self.assertRaises(LLMError) as caught:
                read_arc_input_budget(value)
            self.assertEqual((caught.exception.code, caught.exception.status), ("ARC_PROMPT_BUDGET_INVALID", 400))
        options = {"book_plan": {}, "canonical_facts": {}, "start_chapter": 1, "total_chapters": 30, "arc_index": 1}
        with self.assertRaises(LLMError) as caught:
            build_arc_outlines_prompt(**options, input_budget=1025)
        error = caught.exception
        self.assertEqual((error.code, error.status, error.retryable), (ARC_PROMPT_BUDGET_EXCEEDED, 422, False))
        self.assertIn("滚纲", str(error))
        with patch.dict(os.environ, {"NOVEL_FORGE_ARC_INPUT_BUDGET": "0"}):
            budget = read_writing_options()["arc_input_budget"]
            self.assertIsNone(budget)
            self.assertEqual(build_arc_outlines_prompt(**options, input_budget=budget), build_arc_outlines_prompt(**options))
        with self.assertRaises(LLMError) as caught:
            fit_prompt(render=lambda _: {"system": "硬规则", "prompt": ""}, slots=[], input_budget=3, reserve=2)
        self.assertEqual(caught.exception.status, 422)


class Stage2PipelineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store = Store(self.directory)
        self.runtime = PipelineRuntime(self.store)
        self.addAsyncCleanup(self.runtime.close)
        self.enterContext(patch.dict(os.environ, {
            "NOVEL_FORGE_ARC_INPUT_BUDGET": "", "NOVEL_FORGE_CHAPTER_INPUT_BUDGET": "",
            "NOVEL_FORGE_ROLLING_ARC_OUTLINES": "1", "NOVEL_FORGE_LONG_MEMORY": "1", "NOVEL_FORGE_STORY_LEDGER": "0",
            "NOVEL_FORGE_DRAFT_QUALITY": "1", "NOVEL_FORGE_WRITE_AI_GATE": "1", "NOVEL_FORGE_ARC_REVIEW_MODEL": "",
        }))

    def book(self, total=20):
        return self.store.create_job({
            "title": "铜钥夜航", "premise": "林渊沿钟楼线索查出旧案", "wordsPerChapter": 850,
            "totalChapters": total, "targetWords": total * 850, "autoResumeOnRateLimit": False,
            "llmConfig": {"provider": "mock", "maxRetries": 0},
        })["id"]

    async def test_rolling_volume_archives_include_final_block_and_switch_off_cleanly(self):
        for enabled in ("1", "0"):
            with self.subTest(long_memory=enabled), patch.dict(os.environ, {"NOVEL_FORGE_LONG_MEMORY": enabled}):
                job_id = self.book()
                await self.runtime.start_job(job_id)
                finished = await self.runtime.wait_for_job(job_id)
                self.assertEqual(finished["status"], "completed", finished.get("error"))
                facts = self.store.get_canonical_facts(job_id)
                volumes = facts["volumeSummaries"]
                self.assertEqual([row["endChapter"] for row in volumes], [10, 20] if enabled == "1" else [])
                self.assertEqual(finished["stageStats"].get("volume_archive", {}).get("calls", 0), len(volumes))
                for row in volumes:
                    start, end = row["startChapter"], row["endChapter"]
                    self.assertEqual(start, end - 9)
                    self.assertEqual(row["sourceHash"], volume_source_hash(self.store.get_chapters(
                        job_id, start_chapter=start, end_chapter=end)))
                self.assertTrue(all(row["skeleton"] for row in finished["arcPlans"]))
                for row in facts["arcSummaries"]:
                    self.assertTrue(row["themes"] and row["skeleton"] and row["shape"])
                    self.assertEqual({beat["chapter"] for beat in row["usedBeats"]},
                                     set(range(row["startChapter"], row["endChapter"] + 1)))

    async def test_resume_after_arc_archive_and_at_final_commit_without_duplicate_archives(self):
        job_id = self.book()
        archive = self.runtime._archive
        interrupted, generated = set(), []

        async def interrupt(job_id, start, end, chapters, config):
            if end not in interrupted:
                interrupted.add(end)
                raise LLMError("模拟归档前中断", code="TIMEOUT", status=408, retryable=True)
            generated.append(end)
            await archive(job_id, start, end, chapters, config)

        with patch.object(self.runtime, "_archive", side_effect=interrupt):
            await self.runtime.start_job(job_id)
            first = await self.runtime.wait_for_job(job_id)
            self.assertEqual(first["status"], "interrupted", first.get("error"))
            self.assertEqual(first["completedChapters"], 10)
            facts = self.store.get_canonical_facts(job_id)
            self.assertEqual([row["endChapter"] for row in facts["arcSummaries"]], [5, 10])
            self.assertEqual(facts["volumeSummaries"], [])
            prior_arcs = facts["arcSummaries"]
            await self.runtime.resume_job(job_id)
            second = await self.runtime.wait_for_job(job_id)
            self.assertEqual(second["status"], "interrupted", second.get("error"))
            self.assertEqual(second["completedChapters"], 20)
            volume10 = self.store.get_canonical_facts(job_id)["volumeSummaries"][0]
            await self.runtime.resume_job(job_id)
            finished = await self.runtime.wait_for_job(job_id)
        self.assertEqual(finished["status"], "completed", finished.get("error"))
        self.assertEqual(generated, [10, 20])
        self.assertEqual(finished["stageStats"]["volume_archive"]["calls"], 2)
        self.assertEqual(finished["stageStats"]["arc_archive"]["calls"], 3)
        facts = self.store.get_canonical_facts(job_id)
        self.assertEqual(facts["arcSummaries"][:2], prior_arcs)
        self.assertEqual(facts["volumeSummaries"][0], volume10)
        self.assertEqual([row["endChapter"] for row in facts["volumeSummaries"]], [10, 20])

    async def test_direct_planning_hashes_volume_catchup_and_retries_soft_feedback(self):
        job_id = self.book(40)
        rows = arc_history([5, 5], skeleton=REPEATED_SKELETON)
        for row in rows:
            row["sourceHash"] = f"prior-{row['arc']}"
        arcs = [{"arcIndex": row["arc"], "startChapter": row["startChapter"], "endChapter": row["endChapter"]} for row in rows]
        for n in range(1, 11):
            self.store.save_chapter(job_id, n, chapter(n))
        job = self.store.update_job(job_id, {"bookPlan": {"title": "前版总纲"}, "arcPlans": arcs,
                                            "canonicalFacts": {"arcSummaries": rows}})
        reply = {"text": json.dumps(candidate(11, 5, skeleton=REPEATED_SKELETON))}
        with patch.object(self.runtime, "_llm", new=AsyncMock(return_value=reply)) as llm:
            await self.runtime._plan_arc(job, arcs, {}, 11, {"provider": "mock"}, {"provider": "mock"}, long_memory=True)
        self.assertEqual(llm.await_count, 2)
        first, retry = [call.args[3] for call in llm.await_args_list]
        self.assertNotIn("上次校验未通过", first["prompt"])
        self.assertIn("上次校验未通过：叙事骨架", retry["prompt"])
        self.assertTrue(all(prompt_tokens(prompt) <= TOKEN_LIMIT for prompt in (first, retry)))
        saved = self.store.get_job_record(job_id)
        self.assertEqual(saved["arcPlans"][-1]["skeleton"], REPEATED_SKELETON)
        self.assertEqual(saved["canonicalFacts"]["volumeSummaries"][0]["endChapter"], 10)
        self.assertTrue(any(row["level"] == "warn" and "重试后保留" in row["message"] for row in saved["logs"]))

    async def test_volume_catchup_skips_gaps_and_replaces_stale_hashes(self):
        job_id = self.book(30)
        config = {"provider": "mock"}
        committed = {n: self.store.save_chapter(job_id, n, chapter(n)) for n in range(1, 31) if n != 7}
        good_block = [committed[n] for n in range(11, 21)]
        self.assertTrue(self.store.save_volume_summary(job_id, 20, volume_source_hash(good_block), {"mainline": "既有卷摘要"}))
        facts = self.store.get_canonical_facts(job_id)
        facts["volumeSummaries"].append({
            "volume": 3, "startChapter": 21, "endChapter": 30, "sourceHash": "stale", "mainline": "过期摘要",
        })
        self.store.update_job(job_id, {"canonicalFacts": facts})
        await self.runtime._catch_up_volumes(job_id, 30, committed, config)
        committed[7] = self.store.save_chapter(job_id, 7, chapter(7))
        await self.runtime._catch_up_volumes(job_id, 30, committed, config)
        await self.runtime._catch_up_volumes(job_id, 30, committed, config)
        summaries = self.store.get_canonical_facts(job_id)["volumeSummaries"]
        self.assertEqual([row["endChapter"] for row in summaries], [10, 20, 30])
        self.assertEqual(summaries[1]["mainline"], "既有卷摘要")
        self.assertEqual(summaries[2]["sourceHash"], volume_source_hash([committed[n] for n in range(21, 31)]))
        self.assertEqual(self.store.get_job_record(job_id)["stageStats"]["volume_archive"]["calls"], 2)

    async def test_budget_failure_waits_for_manual_resume_and_mock_uses_allowed_lengths(self):
        job_id = self.book(3)
        with patch.dict(os.environ, {"NOVEL_FORGE_ARC_INPUT_BUDGET": "1025"}):
            await self.runtime.start_job(job_id)
            stopped = await self.runtime.wait_for_job(job_id)
            self.assertEqual(stopped["error"]["code"], ARC_PROMPT_BUDGET_EXCEEDED)
            self.assertEqual(stopped["status"], "interrupted")
            self.assertEqual(stopped["checkpoint"]["nextChapter"], 1)
            await self.runtime.recover_interrupted_jobs()
            self.assertFalse(self.runtime.is_job_running(job_id))
        with patch.dict(os.environ, {"NOVEL_FORGE_ARC_INPUT_BUDGET": "0"}):
            await self.runtime.resume_job(job_id)
            finished = await self.runtime.wait_for_job(job_id)
            self.assertEqual(finished["status"], "completed", finished.get("error"))
        prompt = build_arc_outlines_prompt(book_plan={}, canonical_facts={"arcSummaries": arc_history([5] * 3)},
                                           start_chapter=16, total_chapters=40, arc_index=4)
        reply = await call_mock_llm(**prompt)
        planned = json.loads(reply["text"])
        self.assertIn(len(planned["chapters"]), [3, 4, 6])
        self.assertEqual(planned["skeleton"], normalize_skeleton(planned["skeleton"]))
