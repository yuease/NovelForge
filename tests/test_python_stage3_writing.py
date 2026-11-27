import io
import json
import math
import os
import re
import tempfile
import unittest
import zipfile
from copy import deepcopy
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from novel_forge import llm
from novel_forge.chapter_title import local_retitle, strip_conflicting_number, title_body_conflicts
from novel_forge.entity_salience import active_subplots_view, entity_index, entity_tier, retired_roster, salience_score
from novel_forge.long_memory import normalize_arc_summary, relevant_memory_characters
from novel_forge.pipeline import PipelineRuntime
from novel_forge.prompts import (
    build_arc_outlines_prompt, build_arc_summary_prompt, build_chapter_write_prompt, build_fact_extraction_prompt,
    build_retitle_prompt, extract_relevant_facts,
)
from novel_forge.providers import thinking_capability
from novel_forge.storage import Store, count_chinese_words, volume_source_hash
from novel_forge.thinking_policy import resolve_writing_thinking, writing_profile


FACTS_MARKER = "【本章相关规范事实 Relevant Facts（必须严格遵守）】\n"
ROSTER = "已退场/身故人物（未经铺垫不得复活或重登舞台）："
LOCAL = "http://127.0.0.1:7142"
HISTORICAL = {"genre": "历史考据与工业测评", "premise": "按宋代度量衡换算货款，追查商会账目"}
ARCHIVE_QUOTE = "守门人交还了密室钥匙"


def large_manuscript():
    assets = [f"核心设备{i:02}" for i in range(10)]
    items = {name: {"sourceChapter": 3, "location": "工坊", "status": "在库"} for name in assets}
    items.update({f"临时材料{i:03}": {"sourceChapter": 1 + i % 190, "location": "旧仓库"}
                  for i in range(590)})
    items["临时材料001"]["status"] = "已消耗"
    items["核心设备00"].update(updatedAtChapter=185, location="未来设备间", status="已消耗")
    characters = {f"闲角{i:03}": {"sourceChapter": 1 + i % 170} for i in range(209)}
    characters.update({
        "林舟": {"sourceChapter": 1, "role": "主角"},
        "顾局长": {"sourceChapter": 3, "updatedAtChapter": 185, "status": "存活", "location": "未来监狱"},
        "沈四海": {"sourceChapter": 6, "status": "身亡"},
        "后章路人": {"sourceChapter": 190},
    })
    history = [
        {"chapter": n, "delta": {"characters": {"顾局长": {"location": "旧港"}},
                                 "items": {"核心设备00": {"location": "工坊", "status": "在库"}} if n == 3 else {}}}
        for n in range(3, 13)
    ]
    history += [
        {"chapter": 12, "delta": {"characters": {"顾局长": {"role": "核心反派"}}}},
        {"chapter": 185, "delta": {"characters": {"顾局长": {"location": "未来监狱"}},
                                   "items": {"核心设备00": {"location": "未来设备间"}}}},
        {"chapter": 190, "delta": {"characters": {"后章路人": {}}}},
    ]
    subplots = [{"id": f"subplot-{i:03}", "text": f"追查码头密函{i:03}", "status": "active",
                 "updatedAtChapter": 15 if i < 121 else 178} for i in range(123)]
    plan = {"title": "港口交割", "mainCharacters": [{"name": "林舟", "role": "主角"},
            {"name": "顾局长", "role": "核心反派"}],
            "canonLocks": {"assetSets": [{"name": "工坊", "members": assets}]}}
    return plan, assets, {"characters": characters, "items": items, "factHistory": history, "subplots": subplots}


class EntitySalienceTests(unittest.TestCase):
    def test_large_chapter_prompt_retains_core_and_isolates_future(self):
        plan, assets, facts = large_manuscript()
        original = deepcopy(facts)
        self.assertEqual(tuple(len(facts[key]) for key in ("items", "characters", "subplots")), (600, 213, 123))
        index = entity_index(facts, before_chapter=180)
        villain = index["characters"]["顾局长"]
        self.assertEqual(villain["chapters"], set(range(3, 13)))
        self.assertEqual((villain["firstChapter"], villain["lastChapter"]), (3, 12))
        self.assertEqual(index["items"]["核心设备00"]["entry"]["location"], "工坊")
        self.assertNotIn("后章路人", index["characters"])

        options = {"book_plan": plan, "canonical_facts": facts, "input_budget": 16_384, "total_chapters": 200,
                   "rolling_arc_outlines": True, "long_memory": True}
        outline = {"chapterNumber": 180, "title": "打开旧港", "summary": "林舟调回工坊设备"}
        request = build_chapter_write_prompt(**options, outline=outline)
        rendered = json.loads(request["prompt"].split(FACTS_MARKER)[1])
        self.assertLessEqual(llm.estimate_tokens(request["system"] + "\n" + request["prompt"]), 15_360)
        self.assertTrue(set(assets) <= rendered["items"].keys())
        self.assertIn("顾局长", rendered["characters"])
        self.assertEqual(rendered["characters"]["顾局长"]["location"], "旧港")
        self.assertIn(ROSTER + "沈四海", request["prompt"])
        self.assertEqual(request["prompt"].count("沈四海"), 1)
        self.assertNotIn("未来", request["prompt"])
        self.assertNotIn("后章路人", request["prompt"])
        self.assertNotIn("临时材料002", rendered["items"])
        self.assertEqual(len(rendered["longMemory"]["subplots"]), 2)
        self.assertIn("正文以事实为准", request["system"])
        self.assertIn('status: "已消耗"', build_fact_extraction_prompt(chapter_number=180, chapter_text="货已发走。",
                                                                         story_ledger=True)["system"])

        request = build_chapter_write_prompt(**options, outline={**outline, "summary": "核对临时材料001、临时材料002与沈四海旧案"})
        rendered = json.loads(request["prompt"].split(FACTS_MARKER)[1])
        self.assertEqual(rendered["items"]["临时材料001"]["status"], "已消耗")
        self.assertIn("临时材料002", rendered["items"])
        self.assertEqual(rendered["characters"]["沈四海"]["status"], "身亡")
        self.assertEqual(facts, original)
        self.assertEqual(len(extract_relevant_facts(facts)["characters"]), 213)
        self.assertEqual(len(extract_relevant_facts(facts, max_characters=1, current_chapter=180)["characters"]), 1)

    def test_tiers_frequency_age_clue_links_and_ledger_anchors(self):
        facts = {
            "characters": {"闻立": {"sourceChapter": 1}, "贺余": {"sourceChapter": 2}, "李河": {"sourceChapter": 40}},
            "characterStates": {"陈掌柜": {"lastSeenChapter": 2, "stanceTier": "ally"},
                                "未来证人": {"sourceChapter": 1, "lastSeenChapter": 50}},
            "ledgerBalances": [{"subject": "贺余", "updatedAtChapter": 2}],
            "items": {"账牌": {"sourceChapter": 1}, "火漆": {"sourceChapter": 45},
                      "水钟": {"sourceChapter": 1, "updatedAtChapter": 3},
                      "暗格钥匙": {"sourceChapter": 1}, "白绫": {"sourceChapter": 1, "location": "已交付"}},
            "foreshadowing": [{"clue": "暗格钥匙是取证关键", "status": "planted", "sourceChapter": 2}],
        }
        index = entity_index(facts, before_chapter=50)
        self.assertNotIn("未来证人", index["characters"])
        plan = {"canonLocks": {"identityAliases": [{"character": "闻立", "alias": "临江客"}],
                               "keyAssets": ["账牌是旧坊的凭证"]}}
        arcs = [{"endChapter": 49, "villainChips": [{"villain": "李河", "exitPlan": "exit"}]},
                {"endChapter": 50, "villainChips": [{"villain": "闻立", "exitPlan": "exit"}]}]
        expected = {"characters": {"闻立": "core", "陈掌柜": "core", "贺余": "core", "李河": "retired"},
                    "items": {"账牌": "core", "火漆": "ephemeral", "水钟": "dormant",
                              "暗格钥匙": "active", "白绫": "retired"}}
        for kind, rows in expected.items():
            for name, tier in rows.items():
                with self.subTest(name=name):
                    self.assertEqual(entity_tier(name, kind, index[kind][name]["entry"], index, book_plan=plan,
                                                 current_chapter=50, arc_plans=arcs), tier)
        self.assertTrue(index["items"]["暗格钥匙"]["clueLinked"])
        self.assertEqual(retired_roster(facts, index, arc_plans=arcs, current_chapter=50, limit=1), ["李河"])
        selected = extract_relevant_facts(facts, current_chapter=50, context_text="查问李河此前的行踪", book_plan=plan, arc_plans=arcs)
        self.assertEqual(selected["characters"]["李河"]["status"], "退场")
        memory = relevant_memory_characters(facts, facts["characters"], "闻立", "贺余来了",
                                             salience_index=index, core_names={"陈掌柜", "贺余"}, retired_names={"李河"})
        self.assertEqual([row["key"] for row in memory], ["闻立", "贺余", "陈掌柜"])
        context = "取回暗格钥匙"
        base = 4 + 2 * math.log2(2) + 3 * math.exp(-49 / 20) + 1
        for tier, multiplier in (("active", 0.5), ("core", 1)):
            actual = salience_score("暗格钥匙", "items", {}, tier, index, current_chapter=50, context_text=context,
                                    resolved_theme_ranges=[{"range": "1-8", "tags": ["取证"]}])
            self.assertAlmostEqual(actual, (base + (8 if tier == "core" else 0)) * multiplier)
        self.assertAlmostEqual(salience_score("暗格钥匙", "items", {}, "active", index, current_chapter=50,
                                             context_text=context, resolved_theme_ranges=["1-50"]), base)

    def test_theme_ledger_decay_affects_item_ranking(self):
        facts = {
            "items": {"旧券": {"sourceChapter": 1, "updatedAtChapter": 9},
                      "新券": {"sourceChapter": 2, "updatedAtChapter": 8}},
            "arcSummaries": [{"startChapter": 1, "endChapter": 1, "themes": ["旧券交割"]}],
        }
        self.assertEqual(list(extract_relevant_facts(facts, book_plan={}, current_chapter=12)["items"]), ["新券", "旧券"])

    def test_subplot_closures_are_evidenced_and_disappear_on_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            job = store.create_job({"totalChapters": 80})
            key = job["id"]
            arc = {"arcIndex": 1, "startChapter": 48, "endChapter": 49, "arcTitle": "密室", "arcGoal": "搜查"}
            store.update_job(key, {"arcPlans": [arc]})
            store.save_verified_chapter(key, 1, {"content": ARCHIVE_QUOTE, "status": "verified"}, {
                "memory": {"subplots": [{"text": f"试图取回文书{i}", "quote": ARCHIVE_QUOTE} for i in range(6)]},
            }, long_memory=True)
            for chapter in (48, 49):
                store.save_verified_chapter(key, chapter, {"content": ARCHIVE_QUOTE, "status": "verified"})
            chapters = store.get_chapters(key, start_chapter=48, end_chapter=49)
            facts = store.get_canonical_facts(key)
            ids = [row["id"] for row in facts["subplots"]]
            archive_prompt = build_arc_summary_prompt(arc, chapters, canonical_facts=facts)
            self.assertIn("subplotClosures", archive_prompt["system"])
            self.assertIn(ids[5], archive_prompt["prompt"])
            raw = {"mainline": "密室钥匙交割", "subplotClosures": [
                {"id": ids[0], "status": "resolved", "quote": ARCHIVE_QUOTE},
                {"id": ids[1], "status": "dropped"},
                {"id": ids[2], "status": "resolved", "quote": "伪造证据引用"},
                {"id": "  ", "status": "dropped"},
                {"id": "a" * 81, "status": "dropped"},
                {"id": ids[3], "status": "active"},
            ]}
            self.assertEqual([row["id"] for row in normalize_arc_summary(raw, chapters)["subplotClosures"]], ids[:2])
            self.assertNotIn("subplotClosures", normalize_arc_summary({"mainline": raw["mainline"]}, chapters))
            self.assertTrue(store.save_arc_summary(key, arc, volume_source_hash(chapters), raw))
            facts = store.get_canonical_facts(key)
            self.assertEqual(len(facts["subplots"]), 6)
            visible, stale = active_subplots_view(facts, current_chapter=50)
            self.assertEqual(visible, [])
            self.assertEqual([row["id"] for row in stale], ids[2:])
            # Closures from the current/future arc have no effect at a gap or before rollback.
            self.assertEqual(len(active_subplots_view(facts, current_chapter=49)[1]), 6)
            recall, stale = active_subplots_view(facts, current_chapter=50, context_text=f"重访{ids[2]}")
            self.assertEqual([row["id"] for row in recall], [ids[2]])
            self.assertEqual(len(stale), 3)
            options = {"book_plan": {}, "start_chapter": 50, "total_chapters": 80, "arc_index": 2}
            self.assertIn('"staleSubplots":[{"id":"' + ids[2], build_arc_outlines_prompt(
                **options, canonical_facts=facts)["prompt"])

            store.delete_uncommitted_facts(key, 48)
            rolled_back = store.get_canonical_facts(key)
            self.assertEqual(rolled_back["arcSummaries"], [])
            recalled, stale = active_subplots_view({**rolled_back, "foreshadowing": [
                {"id": "linked", "subplotId": ids[0], "status": "planted"},
            ]}, current_chapter=50, context_text=f"再次查阅{ids[1]}")
            self.assertEqual({row["id"] for row in recalled}, set(ids[:2]))
            self.assertEqual(len(stale), 4)
            prompt = build_arc_outlines_prompt(**options, canonical_facts=rolled_back)["prompt"]
            material = json.JSONDecoder().raw_decode(prompt[prompt.index("【已发生事实与全书路标】\n")
                                                              + len("【已发生事实与全书路标】\n"):])[0]
            self.assertEqual(len(material["staleSubplots"]), 4)


class TitleLogicTests(unittest.TestCase):
    def test_numerals_units_and_anchor_precision(self):
        cases = [
            ("十吨起拍", "司仪宣布，1吨起拍。", "1吨起拍", "10", "1"),
            ("百支精梳棉", "这批材料全部是80支精梳棉。", "80支精梳棉", "100", "80"),
            ("百吨级重工通道", "承重标识写着50吨级。", "50吨级重工通道", "100", "50"),
            ("十吨起拍", "旧仓储备十吨库存，一吨起拍。", "一吨起拍", "10", "1"),
            ("一点五亿估值", "新估值为1.2亿。", "1.2亿估值", "150000000", "120000000"),
            ("半斤口粮", "这包口粮称了一斤。", "一斤口粮", "0.5", "1"),
            ("两百两银赔款", "对方只肯付150两银赔款。", "150两银赔款", "200", "150"),
            ("九千零五元成交", "落槌成交价8,000元。", "8,000元成交", "9005", "8000"),
            ("误差百分比8％", "误差百分比为1.5%。", "误差百分比1.5％", "8", "1.5"),
            ("十吨库存，十吨起拍", "库房里有十吨库存，商会最终一吨起拍。", "十吨库存，一吨起拍", "10", "1"),
        ]
        for title, body, candidate, expected_title, expected_body in cases:
            with self.subTest(title=title, body=body):
                conflicts = title_body_conflicts(title, body)
                self.assertEqual(len(conflicts), 1)
                self.assertEqual((conflicts[0]["titleValue"], conflicts[0]["bodyValues"]), (expected_title, [expected_body]))
                self.assertEqual(local_retitle(title, conflicts, body), candidate)
                self.assertEqual(title_body_conflicts(candidate, body), [])

    def test_no_conflicts_without_comparable_evidence(self):
        for title, body in (
            ("千钧一发、百战百胜、十拿九稳", "场内堆着三吨货和十丈长的吊绳。"),
            ("一石二鸟", "门口堆了九石谷子。"),
            ("百年大计", "实际进度还要十二年。"),
            ("十吨起拍", "价格定在500元。"),
            ("十吨起拍", "主持人确认10吨起拍。"),
            ("百吨级重工通道", "新门允许100吨通行。"),
            ("1万件入库", "库存清点为10,000件。"),
            ("1万", "成交一共是10000。"),
            ("1百台", "机器总共100台。"),
            ("两公里行程", "路标标明2公里行程。"),
            ("十吨旧案", "十吨库存之外，现场停着一吨样品。"),
        ):
            with self.subTest(title=title):
                self.assertEqual(title_body_conflicts(title, body), [])

    def test_retitle_prompt_is_short_and_strip_removes_exact_spans(self):
        title, body = "十吨起拍", "一吨起拍销往内海，两吨起拍运往外港。"
        conflicts = title_body_conflicts(title, body)
        self.assertEqual(conflicts[0]["bodyValues"], ["1", "2"])
        self.assertIsNone(local_retitle(title, conflicts, body))
        self.assertEqual(strip_conflicting_number(title, conflicts), "起拍")
        self.assertIsNone(strip_conflicting_number("十吨", conflicts))
        prompt = build_retitle_prompt(planned_title=title, conflicts=conflicts,
                                      chapter_text="一吨起拍。" + "开场灯影。" * 200 + "两吨起拍。",
                                      recent_titles=["灯火交割", "外港再审"])
        self.assertLessEqual(llm.estimate_tokens(prompt["system"] + "\n" + prompt["prompt"]), 1000)
        material = json.loads(prompt["prompt"])
        self.assertTrue(material["beginning"].startswith("一吨起拍"))
        self.assertTrue(material["ending"].endswith("两吨起拍。"))
        self.assertEqual(material["recentTitles"], ["灯火交割", "外港再审"])


class ThinkingPolicyTests(unittest.TestCase):
    def test_profile_signals_and_recent_window(self):
        plan = {"styleBible": {"domainTaboos": ["旧制不使用现代公制"]},
                "canonLocks": {"currencyAndEra": "银一两折1000文", "powerScale": "只能使用手工量具"}}
        profile = writing_profile({**HISTORICAL, "story_ledger": True}, plan)
        self.assertTrue(profile["reasoningHeavy"])
        self.assertEqual(profile["score"], 9)
        self.assertEqual(profile["signals"][:3], ["domainTaboos", "quantitativeCanon", "ledger"])
        romance = {"genre": "都市恋爱日常", "premise": "青梅竹马开一家面包店"}
        self.assertEqual(writing_profile(romance), {"reasoningHeavy": False, "score": 0, "signals": []})
        types = ("hard_value_conflict", "world_rule_conflict", "timeline_conflict", "timeline_contradiction")
        chapter = {"verification": {"issues": [{"type": value} for value in types]}}
        self.assertEqual(writing_profile(romance, recent_chapters=[chapter, *[{}] * 10])["score"], 0)
        adaptive = writing_profile({**romance, "genre": "悬疑"}, recent_chapters=[*[{}] * 9, chapter])
        self.assertEqual(adaptive, {"reasoningHeavy": True, "score": 4, "signals": ["悬疑", "recentVerificationConflicts"]})
        self.assertEqual(writing_profile({"premise": "核对账本", "canonicalFacts": {"ledgerBalances": [{}]}})["score"], 2)

    def test_capability_and_thinking_source(self):
        cases = [
            ("deepseek", "deepseek-chat", "", "toggle"), ("deepseek", "deepseek-reasoner", "", "always"),
            ("openai", "o3-mini", "", "always"), ("openai", "gpt-5-mini", "", "always"),
            ("openai", "gpt-4.1", "", "none"), ("openai-compatible", "gpt-5-mini", LOCAL, "none"),
            ("vertex", "gemini-4-argon-search", "", "toggle"), ("vertex-new", "gemini-3.8-flash", "", "none"),
            ("vertex-new", "gemini-2.5-flash", "https://us-central1-aiplatform.googleapis.com/models", "toggle"),
            ("vertex", "gemini-4-argon", LOCAL, "none"), ("mock", "mock-novel-v1", "", "toggle"),
            ("anthropic", "claude-sonnet", "", "none"), ("gemini", "gemini-4-argon", "", "none"),
        ]
        for provider, model, base, capability in cases:
            with self.subTest(provider=provider, model=model, base=base):
                self.assertEqual(thinking_capability(provider, model, base), capability)
        heavy = {"reasoningHeavy": True}
        legacy = resolve_writing_thinking({"thinkingMode": "disabled"}, heavy, "toggle")
        self.assertEqual((legacy["source"], legacy["effective"], legacy["autoEnabled"]), ("user", "disabled", False))
        self.assertIn("手动关闭", legacy["warning"])
        automatic = resolve_writing_thinking({"thinkingMode": "disabled", "thinkingModeSource": "default"}, heavy, "toggle")
        self.assertEqual((automatic["effective"], automatic["autoEnabled"], automatic["warning"]), ("enabled", True, None))
        always = resolve_writing_thinking({"thinkingMode": "auto"}, heavy, "always")
        self.assertEqual((always["effective"], always["autoEnabled"]), ("enabled", False))
        unsupported = resolve_writing_thinking({"thinkingMode": "auto"}, heavy, "none")
        self.assertEqual(unsupported["effective"], "disabled")
        self.assertIn("不支持", unsupported["warning"])
        self.assertEqual(resolve_writing_thinking({"thinkingMode": "auto"}, {}, "toggle")["effective"], "disabled")
        self.assertEqual(resolve_writing_thinking({"thinkingMode": "enabled"}, {}, "toggle")["effective"], "enabled")
        self.assertIsNotNone(resolve_writing_thinking({"thinkingMode": "enabled"}, {}, "none")["warning"])


def chat(content):
    return httpx.Response(200, json={"choices": [{"message": {"content": content}, "finish_reason": "stop"}]})


class WritingIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = Store(Path(self.enterContext(tempfile.TemporaryDirectory())))
        self.enterContext(patch.dict(os.environ, {
            "NOVEL_FORGE_ROLLING_ARC_OUTLINES": "0", "NOVEL_FORGE_STORY_LEDGER": "1",
            "NOVEL_FORGE_LONG_MEMORY": "1", "NOVEL_FORGE_DRAFT_QUALITY": "0", "NOVEL_FORGE_WRITE_AI_GATE": "0",
            "NOVEL_FORGE_DEGRADE_ON_VERIFY_EXHAUSTED": "1", "NOVEL_FORGE_CHAPTER_INPUT_BUDGET": "16384",
            "NOVEL_FORGE_OUTPUT_GUARD": "1", "NOVEL_FORGE_MIN_OUTPUT_TOKENS": "0",
        }))
        llm.reset_thinking_fallback_cache()
        llm.reset_provider_circuit_state()
        self.addCleanup(llm.reset_thinking_fallback_cache)
        self.addCleanup(llm.reset_provider_circuit_state)
        self.runtime = PipelineRuntime(self.store)

    async def test_reconcile_routes_are_local_then_auxiliary_then_strip(self):
        job_id = self.store.create_job()["id"]
        auxiliary = {"provider": "mock", "model": "light"}
        with patch.object(self.runtime, "_llm", new_callable=AsyncMock) as ask:
            for title, body, expected in (
                ("十吨起拍", "十吨库存已清点，一吨起拍。", "一吨起拍"),
                ("百支精梳棉", "实际只有80支精梳棉。", "80支精梳棉"),
                ("百吨级重工通道", "许可上限为50吨级。", "50吨级重工通道"),
            ):
                final, check = await self.runtime._reconcile_title(job_id, 180, title, body, [], auxiliary)
                self.assertEqual(final, expected)
                self.assertEqual((check["planned"], check["final"], check["method"]), (title, expected, "local"))
            for title, body in (
                ("十拿九稳", "运来一吨铜锭。"), ("十吨起拍", "价格是五千元。"), ("十吨起拍", "10吨起拍。"),
            ):
                self.assertEqual(await self.runtime._reconcile_title(job_id, 180, title, body, [], auxiliary), (title, None))
            ask.assert_not_awaited()

            body = "一吨起拍销往内海，两吨起拍运往外港。"
            ask.return_value = {"text": "```markdown\n## 第180章：港口换约\n```"}
            title, check = await self.runtime._reconcile_title(job_id, 180, "十吨起拍", body, [], auxiliary)
            self.assertEqual((title, check["method"]), ("港口换约", "llm"))
            ask.assert_awaited_once()
            _, stage, config, prompt, temperature = ask.call_args.args
            self.assertEqual((stage, config, temperature), ("chapter_retitle", auxiliary, 0.2))
            self.assertEqual(ask.call_args.kwargs["max_tokens"], 128)
            self.assertTrue(prompt["system"].startswith("[TASK:RETITLE]"))
            for invalid in ("港口换约", "## 第180章 十吨起拍", "给定章名\n需要解释"):
                ask.reset_mock(return_value=True)
                ask.return_value = {"text": invalid}
                title, check = await self.runtime._reconcile_title(job_id, 180, "十吨起拍", body, ["第179章 港口换约"], auxiliary)
                self.assertEqual((title, check["method"]), ("起拍", "strip"))
                ask.assert_awaited_once()
            ask.side_effect = llm.LLMError("网关拒绝标题任务")
            self.assertEqual(await self.runtime._reconcile_title(job_id, 180, "十吨", "库存有一吨。", [], auxiliary),
                             ("一吨", {"planned": "十吨", "final": "一吨", "method": "local",
                                       "conflicts": title_body_conflicts("十吨", "库存有一吨。")}))
            title, check = await self.runtime._reconcile_title(job_id, 180, "十吨起拍", body, [], auxiliary)
            self.assertEqual((title, check["method"]), ("起拍", "strip"))
        mock_reply = await llm.call_mock_llm(**build_retitle_prompt(
            planned_title="十吨起拍", conflicts=title_body_conflicts("十吨起拍", body), chapter_text=body,
        ))
        self.assertFalse(title_body_conflicts(mock_reply["text"], body))

    async def _run_writing_job(self, mode=None, *, repair=False, needs_review=False):
        options = {"provider": "deepseek", "baseUrl": LOCAL, "model": "writer", "lightModel": "reviewer",
                   "apiKey": "fixture-only", "maxRetries": 0}
        if mode is not None:
            options["thinkingMode"] = mode
        tail = "\n\n".join([
            "林渊翻过契纸，顺着长案走到窗边。他把木牌递给商会执事，等对方敲响铜钟。",
            "木梯响了好几声，楼下的伙计挑起帘布，挽着袖口把靠墙的空箱一只只搬到院里。",
            "有人提着茶壶上台阶，手背刚一松，壶盖便掉了。旁边的小孩蹲下去，捡起来递回去。",
            "窗边离桌太远，铺在案上的契纸看不大清楚。执事换过油盏，把染了浆糊的那面翻下来。",
            "码头雇来的脚夫靠着廊柱等候叫号，靴面沾着水草。轮到他时，账房给他留了门边的位置。",
            "案边的船主捏着印章，先在一小片废纸上试了墨。印泥太干，边缘的字只留下浅浅一圈。",
            "林渊把窗推开，让凉风吹干印迹。对岸吊起一面蓝布旗，水面上渐渐聚来几条来接货的小船。",
            "执事对着登记册念出每人的姓氏，确认各家都派了人。散席之前，先到的人还得留下作证。",
            "杂役提来灯笼挂到门旁，照着收尾的队伍。台阶外的雨已经停了，巷口清出一条车道。",
        ])
        final = "执事揭开红布，当众宣布1吨起拍。\n\n" + tail
        draft = final.replace("当众宣布", "记错账目，仍宣布")
        words = count_chinese_words(final)
        job = self.store.create_job({**HISTORICAL, "title": "港口交割", "wordsPerChapter": words,
                                     "totalChapters": 2, "targetWords": 2 * words, "llmConfig": options,
                                     "maxRepairRetries": 0 if needs_review else 1, "degradeOnVerifyExhausted": needs_review})
        self.store.update_job(job["id"], {"llmConfig": {**job["llmConfig"], "maxTokens": 2048}})
        calls = []

        async def handler(request):
            wire = json.loads(request.content)
            system, prompt = (message["content"] for message in wire["messages"])
            task = re.match(r"\[TASK:(\w+)\]", system)[1]
            calls.append((task, wire))
            if task == "CHAPTER_OUTLINES":
                outlines = json.loads((await llm.call_mock_llm(system=system, prompt=prompt))["text"])
                outlines[0]["title"] = "十吨起拍"
                return chat(json.dumps(outlines, ensure_ascii=False))
            if task == "CHAPTER_WRITE":
                if re.search(r"^请撰写第1(?:/2)?章正文", prompt, re.M) and (repair or needs_review):
                    return chat(draft)
                return chat(final)
            if task == "VERIFY" and "记错账目，仍宣布" in prompt.split("【待审校正文】")[-1]:
                if needs_review:
                    return chat(json.dumps({"passed": False, "issues": [], "aiIssues": [{
                        "category": "over_explaining", "priority": "P1",
                        "quote": "记错账目，仍宣布1吨起拍", "impact": "账目变化后的解释可以留待人工精简",
                    }]}, ensure_ascii=False))
                issue = {
                    "type": "hard_value_conflict", "severity": "error", "message": "需要复核账目",
                    "quote": "记错账目，仍宣布1吨起拍",
                }
                return chat(json.dumps({"passed": False, "issues": [issue], "aiIssues": []}, ensure_ascii=False))
            if task == "REPAIR":
                return chat(final)
            return chat((await llm.call_mock_llm(system=system, prompt=prompt))["text"])

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            runtime = PipelineRuntime(self.store, client=client)
            with patch.dict(os.environ, {"NOVEL_FORGE_WRITE_AI_GATE": "1" if needs_review else "0"}):
                await runtime._consume(job["id"])
            saved = self.store.get_job_record(job["id"])
            self.assertEqual(saved["status"], "completed", saved.get("error"))
            txt = await runtime.export_novel(job["id"], "txt")
            docx = await runtime.export_novel(job["id"], "docx")
        return job["id"], saved, draft if needs_review else final, calls, txt, docx

    async def test_default_and_auto_enable_writing_and_repair_only_with_reconciled_exports(self):
        for mode, source in ((None, "default"), ("auto", "auto")):
            with self.subTest(mode=mode):
                job_id, saved, content, calls, txt, docx = await self._run_writing_job(mode, repair=True)
                chapter = self.store.get_chapter(job_id, 1)
                self.assertEqual((chapter["title"], chapter["content"], chapter["status"]), ("1吨起拍", content, "repaired"))
                self.assertEqual(chapter["titleCheck"]["method"], "local")
                self.assertNotIn("titleCheck", self.store.get_chapter(job_id, 2))
                self.assertTrue(saved["writingProfile"]["reasoningHeavy"])
                self.assertEqual(saved["llmConfig"]["thinkingModeSource"], source)
                self.assertEqual(saved["llmConfig"]["thinkingMode"], mode or "disabled")
                self.assertIn("\n1吨起拍\n\n" + content, txt["content"])
                with zipfile.ZipFile(io.BytesIO(docx["content"])) as archive:
                    document = archive.read("word/document.xml").decode("utf-8")
                self.assertIn("第1章 1吨起拍", document)
                for paragraph in content.split("\n\n"):
                    self.assertIn(paragraph, document)
                self.assertIn("REPAIR", [task for task, _ in calls])
                self.assertNotIn("RETITLE", [task for task, _ in calls])
                for task, payload in calls:
                    self.assertEqual(payload["thinking"]["type"], "enabled" if task in ("CHAPTER_WRITE", "REPAIR") else "disabled")
                    if task == "VERIFY":
                        self.assertEqual(payload["model"], "reviewer")
                    if task == "CHAPTER_WRITE":
                        self.assertGreaterEqual(payload["max_tokens"], 16_384)

    async def test_explicit_disable_warns_once_and_needs_review_is_retitled(self):
        job_id, saved, content, calls, _, _ = await self._run_writing_job("disabled", needs_review=True)
        self.assertEqual(saved["llmConfig"]["thinkingModeSource"], "user")
        self.assertEqual([log["level"] for log in saved["logs"] if "已手动关闭思考模式" in log["message"]], ["warn"])
        self.assertTrue(all(wire["thinking"]["type"] == "disabled" for _, wire in calls))
        chapter = self.store.get_chapter(job_id, 1)
        self.assertEqual((chapter["status"], chapter["title"], chapter["content"]), ("needs_review", "1吨起拍", content))
        self.assertEqual(chapter["titleCheck"]["method"], "local")

    async def test_rewrites_reconcile_and_protect_author_edits(self):
        job = self.store.create_job({"wordsPerChapter": 1000, "targetWords": 1000})
        job_id = job["id"]
        chapter = self.store.save_verified_chapter(job_id, 1, {
            "title": "十吨起拍", "content": "旧市场仍按十吨起拍。", "status": "verified",
        })["chapter"]
        chapters = {1: chapter}
        revised = "新市场改成一吨起拍。"
        with patch.object(self.runtime, "_adjust_words", return_value=revised):
            await self.runtime._calibrate(job, {}, chapters, 10, (900, 1180), {})
        self.assertEqual(chapters[1]["title"], "一吨起拍")
        self.assertEqual(chapters[1]["titleCheck"]["method"], "local")
        self.assertNotIn("authorEdited", chapters[1])

        repaired = {"passed": True, "chapterText": "商会决定从两吨起拍。", "verification": {"passed": True},
                    "repairCount": 1, "factsDelta": {}}
        for edited, expected in ((False, "两吨起拍"), (True, "作者保留十吨起拍")):
            if edited:
                chapters[1] = self.store.save_chapter(job_id, 1, {**chapters[1], "title": expected, "content": "作者改稿。"})
                self.assertTrue(chapters[1]["authorEdited"])
                self.assertNotIn("titleCheck", chapters[1])
            rows = list(chapters.values())
            with patch("novel_forge.pipeline.verify_and_repair_chapter", return_value=repaired):
                await self.runtime._repair_audit_chapters(job, {}, rows, {1}, {}, {}, False)
            chapters[1] = rows[0]
            self.assertEqual(rows[0]["title"], expected)
            if not edited:
                self.assertEqual(rows[0]["titleCheck"]["planned"], "十吨起拍")

    async def test_rolling_arc_gets_committed_title_instead_of_outline(self):
        with patch.dict(os.environ, {"NOVEL_FORGE_ROLLING_ARC_OUTLINES": "1"}):
            job = self.store.create_job({"totalChapters": 4, "targetWords": 1600, "wordsPerChapter": 400})
            self.store.update_job(job["id"], {"bookPlan": {}})
            self.store.save_verified_chapter(job["id"], 1, {"title": "一吨起拍", "content": "一吨起拍。", "status": "verified"})
            job = self.store.get_job_record(job["id"])
            with patch.object(self.runtime, "_llm", side_effect=llm.LLMError("捕获滚纲请求")) as ask:
                with self.assertRaises(llm.LLMError):
                    await self.runtime._plan_arc(job, [], {1: {"title": "十吨起拍"}}, 2, {}, {})
            self.assertIn('"recentTitles":["一吨起拍"]', ask.call_args.args[3]["prompt"])
            self.assertNotIn("十吨起拍", ask.call_args.args[3]["prompt"])

    async def test_job_creation_and_connection_patch_preserve_provenance(self):
        job = self.store.create_job({"thinkingMode": "auto"})
        self.assertEqual(job["llmConfig"]["thinkingModeSource"], "auto")
        job_id = job["id"]
        for mode, source in (("disabled", "user"), ("enabled", "user"), ("auto", "auto")):
            await self.runtime.update_job_llm_config(job_id, {"thinkingMode": mode})
            config = self.store.get_job_record(job_id)["llmConfig"]
            self.assertEqual((config["thinkingMode"], config["thinkingModeSource"]), (mode, source))
        await self.runtime.update_job_llm_config(job_id, {"model": "alternative"})
        self.assertEqual(self.store.get_job_record(job_id)["llmConfig"]["thinkingModeSource"], "auto")
        legacy = {"provider": "mock", "thinkingMode": "disabled"}
        self.store.update_job(job_id, {"llmConfig": legacy})
        await self.runtime.update_job_llm_config(job_id, {"model": "alternative"})
        self.assertNotIn("thinkingModeSource", self.store.get_job_record(job_id)["llmConfig"])

    async def test_transports_omit_illegal_thinking_and_cache_only_rejections(self):
        calls = []

        def handler(request):
            body = json.loads(request.content)
            calls.append(body)
            if body.get("model") == "bad-model":
                return httpx.Response(400, json={"error": {"message": "Invalid model name"}})
            if body.get("model") == "old-deepseek" and "thinking" in body:
                return httpx.Response(400, json={"error": {"message": "Unknown field: thinking"}})
            if body.get("generationConfig") is not None:
                if "thinkingConfig" in body["generationConfig"]:
                    return httpx.Response(400, text="Requested reasoning configuration unsupported")
                return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "正文"}]}}]})
            if request.url.path.endswith("/messages"):
                return httpx.Response(200, json={"content": [{"type": "text", "text": "正文"}]})
            return chat("正文")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            common = {"base_url": LOCAL, "api_key": "fixture-only", "client": client, "retries": 0, "prompt": "写章"}
            for provider, model in (("anthropic", "claude"), ("openai", "gpt-4.1"), ("openai-compatible", "gpt-5-mini")):
                await llm.call_llm(**common, provider=provider, model=model, thinking_mode="enabled")
                self.assertNotIn("thinking", calls[-1])
            await llm.call_llm(**common, provider="deepseek", model="modern-deepseek", thinking_mode="auto")
            self.assertEqual(calls[-1]["thinking"], {"type": "disabled"})

            before = len(calls)
            deepseek = {**common, "provider": "deepseek", "model": "old-deepseek", "thinking_mode": "enabled"}
            self.assertEqual((await llm.call_llm(**deepseek))["text"], "正文")
            self.assertEqual((await llm.call_llm(**deepseek))["text"], "正文")
            self.assertEqual(len(calls) - before, 3)
            self.assertEqual(calls[-3]["thinking"], {"type": "enabled"})
            self.assertNotIn("thinking", calls[-2])
            self.assertNotIn("thinking", calls[-1])
            self.assertIn(("deepseek", LOCAL, "old-deepseek"), llm._UNSUPPORTED_THINKING_KEYS)

            before = len(calls)
            with self.assertRaises(llm.LLMError) as caught:
                await llm.call_llm(**common, provider="deepseek", model="bad-model", thinking_mode="enabled")
            self.assertEqual((caught.exception.code, len(calls) - before), ("HTTP_400", 1))
            self.assertNotIn(("deepseek", LOCAL, "bad-model"), llm._UNSUPPORTED_THINKING_KEYS)

            vertex = {**common, "provider": "vertex-new", "model": "gemini-4-argon", "thinking_mode": "enabled"}
            before = len(calls)
            await llm.call_llm(**vertex)
            await llm.call_llm(**vertex)
            self.assertEqual(len(calls) - before, 3)
            self.assertIn("thinkingConfig", calls[-3]["generationConfig"])
            self.assertNotIn("thinkingConfig", calls[-2]["generationConfig"])
            self.assertNotIn("thinkingConfig", calls[-1]["generationConfig"])
            llm.reset_thinking_fallback_cache()
            self.assertEqual(llm._UNSUPPORTED_THINKING_KEYS, set())


if __name__ == "__main__":
    unittest.main()
