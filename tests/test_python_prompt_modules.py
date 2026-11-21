import json
import os
import unittest
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

from novel_forge import chapter_style, draft_quality, long_memory, prompt_budget, prompts
from novel_forge.llm import LLMError, estimate_tokens
from novel_forge.storage import count_chinese_words


FIXTURE = Path(__file__).with_name("fixtures") / "test_python_prompt_modules.json"
FACTS_HEADING = "【本章相关规范事实 Relevant Facts（必须严格遵守）】\n"


def fixture(name):
    return json.loads(FIXTURE.read_text(encoding="utf-8"))[name]


class BudgetTests(unittest.TestCase):
    def test_environment_defaults_disable_and_errors(self):
        for raw, result in (("", 16384), ("\ufeff", 16384), (None, 16384), ("0", None),
                            ("4400", 4400), ("0x4000", 16384), (" 1.6384e4 ", 16384)):
            self.assertEqual(prompt_budget.read_chapter_input_budget(raw), result)
        for raw in (1024, "-1", "1_500", "1024.1", "invalid", "9007199254740992"):
            with self.subTest(raw=raw), self.assertRaises(LLMError) as caught:
                prompt_budget.read_chapter_input_budget(raw)
            self.assertEqual((caught.exception.code, caught.exception.status), ("CHAPTER_PROMPT_BUDGET_INVALID", 400))
        with patch.dict(os.environ, {"NOVEL_FORGE_CHAPTER_INPUT_BUDGET": "4700"}):
            self.assertEqual(prompt_budget.read_chapter_input_budget(), 4700)
        with self.assertRaises(LLMError) as caught:
            prompt_budget.fit_chapter_prompt(render=lambda _: {"system": "约束" * 20, "prompt": "章纲"}, slots=[],
                                             input_budget=1044)
        self.assertEqual(caught.exception.code, "CHAPTER_PROMPT_BUDGET_EXCEEDED")

    def test_clip_quantiles_and_entry_priority(self):
        cases = [
            {"input_budget": 1080, "slots": [
                {"key": "ending", "text": "过时旧事。" * 25 + "\n保留这个完整句子。\n完整结尾🙂。", "from_end": True, "share": 60},
                {"key": "summary", "text": "开头摘要保留。后续不一定。" * 8, "share": 40},
            ]},
            {"input_budget": 1065, "slots": [
                {"key": "head", "text": "无标点的长段文字🙂" * 32, "share": 100},
            ]},
            {"input_budget": 1100, "slots": [
                {"key": "continuity", "entries": [{"setting": "月港的硬约束"}, {"redundant": "文本" * 70}], "share": 30},
                {"key": "cast", "entries": [{"lead": "太长的主角描述" * 30}, {"role": "舵手"}], "allocate_first": True,
                 "first_fallback": {"lead": "林默"}, "share": 30},
                {"key": "ending", "text": "旧情节。" * 60 + "留在原地。", "from_end": True, "share": 30},
                {"key": "samples", "entries": ["梗例", "梗例", "下一条"], "share": 10},
            ]},
        ]
        expected = fixture("test_clip_quantiles_and_entry_priority")

        def render(values):
            return {"system": "规则", "prompt": json.dumps(values, ensure_ascii=False, separators=(",", ":"))}

        for case, ref in zip(cases, expected):
            result = prompt_budget.fit_chapter_prompt(**case, render=render)
            self.assertEqual(result, ref)
            self.assertLessEqual(estimate_tokens(result["system"] + "\n" + result["prompt"]), case["input_budget"] - 1024)
        self.assertNotEqual(json.loads(expected[1]["prompt"])["head"], cases[1]["slots"][0]["text"])
        self.assertEqual(json.loads(expected[2]["prompt"])["cast"][0], {"lead": "林默"})


class StyleAndDraftTests(unittest.TestCase):
    def test_four_style_rules_and_dialogue_break(self):
        facts = {"characters": {"林": {}, "林默": {}}}
        self.assertIn("dash_density", {issue["rule"] for issue in chapter_style.find_chapter_style_issues(
            "灯——亮了！" * 8, facts, 60,
        )})
        self.assertIn("psychological_lead_density", {issue["rule"] for issue in chapter_style.find_chapter_style_issues(
            "他意识到船就要开了。" * 6, facts, 60,
        )})
        self.assertIn("repeated_subject", {issue["rule"] for issue in chapter_style.find_chapter_style_issues(
            "林默收好钥匙。" * 5, facts, 35,
        )})
        self.assertEqual(chapter_style.find_chapter_style_issues("林默靠近。" * 3 + "“停。”" + "林默后退。" * 3, facts, 50), [])
        self.assertEqual(chapter_style.find_chapter_style_issues("风" * 400 + "\n" + "雨" * 400, facts, 800), [])
        self.assertEqual(chapter_style.find_chapter_style_issues("风" * 800, facts, 800)[0]["rule"], "single_paragraph")

    def test_grounded_quotes_and_degradation(self):
        passage = "甲板尽头发现ＡＢ暗记，\r\n确切证明箱中有信。"
        quote = "「甲板尽头发现AB暗记，...确切证明箱中有信。」"
        candidates = [
            {"priority": "P1", "quote": quote, "category": "formulaic_reaction", "suggestion": "改写"},
            {"priority": "P0", "quote": passage.rstrip("。"), "impact": "同一处去重"},
            {"priority": "P1", "quote": "原文没有这个例子"},
            {"priority": "P2", "quote": passage},
        ]
        emoji_text = "同章起点" + "🔑" * 161 + "同章终点"
        local = {"type": "ai_flavor", "severity": "error", "rule": "repeated_subject"}
        allowed = {"passed": False, "issues": [local]}
        check = {"chapter_number": 4, "min_words": 12, "max_words": 60, "pov": "第一人称",
                 "canonical_facts": {"characters": {"林默": {}},
                                     "injuries": [{"sourceChapter": 2, "character": "林默", "description": "左臂骨裂"}],
                                     "secrets": [{"sourceChapter": 1, "secret": "夹层藏有账本", "excludedCharacters": ["林默"]}]}}
        options = [
            (passage, allowed),
            (passage, {"passed": False, "issues": [local], "aiIssues": [{"priority": "P0", "quote": passage}]}),
            (passage, {"passed": False, "issues": [{"type": "ai_flavor", "priority": "P1", "category": "shared_voice"}]}),
            (passage, {"passed": False, "issues": [{"type": "ai_flavor", "priority": "P1", "category": "inconsistent_details"}]}),
            ("正文TBD继续。" + passage, allowed),
            ("林默用完好无损的左臂推门。" + passage, allowed),
            ("林默早已知道夹层藏有账本。", allowed),
            ("林默内心独白忽然浮现。" + passage, allowed),
            ("短句。", allowed),
            ("\ufeff", allowed),
        ]
        expected = fixture("test_grounded_quotes_and_degradation")
        self.assertEqual(chapter_style.verified_ai_blockers(candidates, passage), expected["blocking"])
        self.assertEqual(len(expected["blocking"]), 1)
        self.assertEqual(chapter_style.verified_ai_blockers([{"priority": "P1", "quote": "同章起点……同章终点"}], emoji_text),
                         expected["far"])
        self.assertEqual(expected["far"], [])
        self.assertFalse(chapter_style.can_save_unverified_draft(passage, {"passed": False, "issues": [None]}, **check))
        for (body, verification), reference in zip(options, expected["sections"]):
            self.assertEqual(chapter_style.run_deterministic_checks(chapter_text=body, **check), reference[0])
            self.assertEqual(chapter_style.can_save_unverified_draft(body, verification, **check), reference[1])
            self.assertEqual(chapter_style.find_chapter_style_issues(body, check["canonical_facts"], len(body)), reference[2])

    def test_style_guide_heading_and_seam_cleanup(self):
        opening = "暮色慢慢落在码头最外侧那盏多年未曾换下的旧油灯上。风从沉睡的街道尽头带来潮水即将上涨的气息。"
        tail = "许言拾起漏在门边的钥匙并抬头确认界碑。" * 40
        title = "第2章 雾港回声"
        clean_options = {"title": title, "chapter_number": 2, "previous_ending": "下雨了。" + opening, "min_words": 500}
        body = "# 第２章：雾港回声\r\n\r\n第2章 雾港回声\r\n\r\n" + opening + "\r\n\r\n\r\n" + tail
        variants = [
            (body, clean_options),
            (body, {**clean_options, "min_words": count_chinese_words(tail) + 1}),
            (body.replace("旧油灯", "新油灯"), clean_options),
            (body.replace("风从", "“风从"), clean_options),
            (body, {**clean_options, "previous_ending": opening + "句子未完成"}),
            ("第3章 下一章\n\n" + tail, clean_options),
            (title + "\n" + title + "\n" + title + "\n" + tail, clean_options),
        ]
        guide = {"narrativeVoice": "  紧贴视角\ufeff\t克制🙂  ", "dialogueRules": ["沉稳", "沉稳", "短促", "超过三项"],
                 "domainTaboos": ["错误物件" * 40, "朝代保持一致", 2], "forbiddenCliches": ["空气凝固", "瞳孔骤缩"]}
        expected = fixture("test_style_guide_heading_and_seam_cleanup")
        self.assertEqual(draft_quality.normalize_style_bible(guide), expected[0])
        self.assertEqual(draft_quality.compact_style_bible(guide), expected[1])
        results = [draft_quality.clean_chapter_draft(text, **options) for text, options in variants]
        self.assertEqual(results, expected[2])
        self.assertEqual(results[0], tail)
        self.assertIn(opening, results[1])
        self.assertTrue(results[-1].startswith(title))


class LongMemoryTests(unittest.TestCase):
    def test_normalization_ordered_replay_and_rankings(self):
        source = "潮门规则ＡＢ。\n林默说天亮就回来。🔑🔒"
        rows = [
            [4, {"characterStates": {"林默": {"seen": True, "currentGoal": "找回船灯", "quote": "潮门规则AB。",
                                               "relationships": {"苏红": "愿意结盟"}}},
                 "constraints": [{"text": "潮门规则", "quote": "潮门规则AB。", "characters": ["林默", "林默"]},
                                 {"text": "证据不足", "quote": "完全不存在"},
                                 {"text": "Emoji 也是原文", "quote": "🔑🔒"}],
                 "commitments": [{"text": "天亮前回来", "quote": "林默说天亮就回来", "deadline": "次日天亮"}],
                 "subplots": [{"text": "调查失落的座钟", "quote": "林默说天亮就回来"},
                              {"text": "坏状态", "status": "unknown", "quote": "潮门规则AB。"}]}],
            [8, {"characterStates": {"林默": {"currentGoal": None, "stance": "改为观望", "quote": "潮门规则AB。",
                                               "relationships": {"苏红": "暂不同行"}}},
                 "commitments": [{"id": "commitments-4-1", "status": "fulfilled", "deadline": None, "quote": "潮门规则AB。"},
                                 {"id": "unknown-id", "text": "没有前序记录", "quote": "潮门规则AB。"}],
                 "subplots": [{"id": "subplots-4-1", "status": "resolved", "quote": "潮门规则AB。"}]}],
            [10, {"constraints": [{"text": "不造假", "quote": "伪造"}]}],
        ]
        cast = [{"name": "陈四", "role": "船医"}, {"name": "林默", "role": "主人公"}]
        base = {f"角色{index:02}": {"sourceChapter": index + 1} for index in range(35)}
        base["林默"] = {"sourceChapter": 1}
        volume = {"mainline": " " + "潮" * 350 + "🙂" * 20 + " ",
                  "turningPoints": [None, {"chapter": "1", "text": "非数字"}, {"chapter": 11, "text": "卷外"},
                                    {"chapter": 1, "text": "起点"}, {"chapter": 5, "text": " "}, {"chapter": 10, "text": "转折"},
                                    {"chapter": 2, "text": "甲"}, {"chapter": 3, "text": "乙"}, {"chapter": 4, "text": "丙"}]}
        expected = fixture("test_normalization_ordered_replay_and_rankings")
        deltas = [long_memory.normalize_memory_delta(raw, chapter, source) for chapter, raw in rows]
        history = [{"chapter": chapter, "delta": {"memory": delta}} for (chapter, _), delta in zip(rows, deltas)]
        memory = long_memory.replay_long_memory([*reversed(history), {"chapter": 9, "delta": {"memory": {"version": 2}}}])
        lead = long_memory.protagonist_name(cast)
        self.assertEqual(deltas, expected["deltas"])
        self.assertEqual(memory, expected["memory"])
        self.assertEqual(lead, expected["lead"])
        self.assertEqual(long_memory.active_memory_items(memory, "constraints", "林默找船灯"), expected["active"])
        self.assertEqual(long_memory.normalize_volume_summary(volume, 1, 10), expected["volume"])
        characters = long_memory.relevant_memory_characters(memory, base, lead, "角色00来码头")
        self.assertEqual(characters, expected["characters"])
        self.assertEqual([character["key"] for character in characters[:2]], ["林默", "角色00"])
        self.assertEqual(len(characters), 20)
        self.assertEqual(memory["characterStates"]["林默"]["lastSeenChapter"], 4)
        self.assertIsNone(memory["characterStates"]["林默"]["currentGoal"])
        self.assertEqual(memory["commitments"][0]["status"], "fulfilled")
        self.assertEqual(memory["subplots"][0]["status"], "resolved")
        self.assertEqual(long_memory.replay_long_memory(None), long_memory.empty_long_memory())


def story_fixture():
    plan = {
        "title": "潮声过雾港", "logline": "打捞账本前确认潮门规则",
        "worldview": {"setting": "雾中海港", "coreRules": ["潮门只能在退潮前打开", "每船只能使用一枚钥匙"]},
        "mainCharacters": [{"name": "苏红", "role": "船医"}, {"name": "林默", "role": "主角"}],
        "styleBible": {"narrativeVoice": "用港口的口吻叙述", "dialogueRules": ["主角直截了当"], "forbiddenCliches": ["空气凝固"]},
    }
    outline = {"chapterNumber": 40, "title": "第40章 开潮门", "summary": "林默使用铜钥匙开门",
               "keyEvents": ["苏红知道潮门规则", "铜钥匙换回账本"], "hook": "看见内舱"}
    facts = {
        "characters": {**{f"角色{i:02}": {"sourceChapter": i} for i in range(1, 35)}, "林默": {"role": "主角", "location": "港口"}},
        "characterStates": {"林默": {"lastSeenChapter": 2, "currentGoal": "归还船员的信", "relationships": {}}},
        "injuries": [{"character": "林默", "active": True, "sourceChapter": 3, "description": "左臂受伤"}],
        "items": {"铜钥匙": {"name": "铜钥匙", "owner": "林默", "location": "袖袋", "sourceChapter": 3},
                  **{f"物件{i:02}": {"sourceChapter": i, "location": "船舱里" * 6} for i in range(30)}},
        "secrets": [{"secret": "潮门规则", "sourceChapter": 3, "holders": ["苏红"], "excludedCharacters": ["船长"]}],
        "timeline": [{"event": f"船员在章{i}靠岸", "chapterNumber": i, "characters": ["林默"]} for i in range(31, 40)],
        "foreshadowing": [{"id": "kept", "clue": "回收正好二十章", "payoffChapter": 20, "status": "paid_off"},
                         {"id": "stale", "clue": "上月结清的伏笔", "payoffChapter": 19, "status": "paid_off"},
                         {"id": "still-open", "clue": "谁在内舱", "sourceChapter": 1, "status": "planted"}],
        "constraints": [{"id": "constraints-3-1", "text": "潮门规则", "characters": ["林默"], "status": "active", "updatedAtChapter": 3}],
        "commitments": [{"id": "closed", "text": "旧承诺已兑现", "status": "fulfilled", "updatedAtChapter": 39},
                        {"id": "open", "text": "黎明前返港", "deadline": "黎明", "status": "active", "updatedAtChapter": 3}],
        "subplots": [{"id": "sub-closed", "text": "旧支线已结束", "status": "resolved", "updatedAtChapter": 29},
                     {"id": "sub-open", "text": "查验内舱", "status": "active", "updatedAtChapter": 36}],
        "volumeSummaries": [{"volume": i, "endChapter": i * 10, "mainline": f"第{i}卷前情", "sourceHash": f"digest-{i}"} for i in range(1, 5)],
        "factHistory": [{"delta": {"journalSentinel": "整份流水不可外发"}}],
    }
    return plan, outline, facts


class PromptTests(unittest.TestCase):
    def test_independent_options_keep_node_default_precedence(self):
        env = {name: "" for name in (
            "NOVEL_FORGE_CHAPTER_INPUT_BUDGET", "NOVEL_FORGE_DRAFT_QUALITY", "NOVEL_FORGE_LONG_MEMORY",
            "NOVEL_FORGE_WRITE_AI_GATE", "NOVEL_FORGE_DEGRADE_ON_VERIFY_EXHAUSTED",
        )}
        with patch.dict(os.environ, env):
            default = prompts.read_writing_options()
            self.assertEqual(default, {"input_budget": 16384, "draft_quality": True, "long_memory": True,
                                       "chapter_ai_gate": True, "degrade_on_verify_exhausted": False})
            for name, option, expected in (
                ("CHAPTER_INPUT_BUDGET", "input_budget", None), ("DRAFT_QUALITY", "draft_quality", False),
                ("LONG_MEMORY", "long_memory", False), ("WRITE_AI_GATE", "chapter_ai_gate", False),
            ):
                with patch.dict(os.environ, {"NOVEL_FORGE_" + name: "0"}):
                    self.assertEqual(prompts.read_writing_options(), {**default, option: expected})
            with patch.dict(os.environ, {"NOVEL_FORGE_DEGRADE_ON_VERIFY_EXHAUSTED": "1"}):
                self.assertTrue(prompts.read_writing_options()["degrade_on_verify_exhausted"])
                self.assertFalse(prompts.read_writing_options({"degradeOnVerifyExhausted": False})["degrade_on_verify_exhausted"])
            with patch.dict(os.environ, {"NOVEL_FORGE_DEGRADE_ON_VERIFY_EXHAUSTED": "0"}):
                self.assertFalse(prompts.read_writing_options({"degradeOnVerifyExhausted": True})["degrade_on_verify_exhausted"])

    def test_all_templates_with_legacy_and_extended_options(self):
        plan, outline, facts = story_fixture()
        chapter_text = "林默循着暗流推门，苏红望见抽屉里的账本。"
        topic = {"title": "提案", "premise": "摘要", "coreConflict": "风浪争斗", "protagonistMotivation": "查明真相",
                 "goldenThreeChapters": ["推门", "起航", "推演"], "sourceRefs": [{"id": "hot-001"}]}
        samples = [{"title": "哑谜", "summary": "以信息差制造笑点"}]
        summaries = [{"chapterNumber": i, "summary": f"第{i}章短摘要", "needsReview": i == 38} for i in range(34, 40)]
        write = {"book_plan": plan, "outline": outline, "canonical_facts": facts,
                 "prev_chapter_ending": "未选中旧内容。" * 150 + "铜钥匙在袖袋里。", "recent_summaries": summaries,
                 "meme_level": "light", "meme_samples": samples, "target_words": 2305, "total_chapters": 40}
        chapters = [{"chapterNumber": i, "title": f"第{i}章", "content": chapter_text * 100,
                     "summary": f"第{i}章摘要", "status": "needs_review" if i == 5 else "completed"} for i in range(1, 11)]
        fixtures = [
            ("build_book_plan_prompt", {"topic_package": topic, "pov": "第一人称", "draft_quality": True,
             "hotspot_context": {"platform": "番茄", "title": "海港"}, "meme_level": "rich", "meme_samples": samples}),
            ("build_chapter_outlines_prompt", {"book_plan": plan, "start_chapter": 2, "end_chapter": 6,
             "total_chapters": 6, "existing_outlines": [outline], "topic_package": topic, "relevant_facts": {"items": {}},
             "continuation_direction": "走海路", "finalize_book": True}),
            ("build_chapter_write_prompt", write),
            ("build_chapter_write_prompt", {**write, "draft_quality": True, "long_memory": True}),
            ("build_chapter_write_prompt", {**write, "input_budget": 2700}),
            ("build_chapter_write_prompt", {**write, "input_budget": 3350, "draft_quality": True,
                                                                       "long_memory": True}),
            ("build_chapter_write_prompt", {**write, "input_budget": 16_384, "draft_quality": True,
                                                                       "long_memory": True}),
            ("build_word_count_adjust_prompt", {"chapter_number": 40, "current_words": 1000,
             "target_words": 2000, "outline": outline, "chapter_text": chapter_text}),
            ("build_fact_extraction_prompt", {"chapter_number": 40, "chapter_text": chapter_text,
             "existing_facts": facts, "long_memory": True}),
            ("build_verify_prompt", {"chapter_number": 40, "chapter_text": chapter_text, "outline": outline,
             "canonical_facts": facts, "prev_chapter_ending": "上回挖到了钥匙" * 100, "chapter_ai_gate": True, "long_memory": True}),
            ("build_verify_prompt", {"chapter_number": 40, "chapter_text": chapter_text, "check_ai_flavor": False}),
            ("build_repair_prompt", {"chapter_number": 40, "chapter_text": chapter_text, "outline": outline,
             "issues": [{"type": "placeholder"}], "ai_issues": [{"priority": "P1"}], "canonical_facts": facts,
             "prev_chapter_ending": "前章衔接" * 201, "chapter_ai_gate": True}),
            ("build_de_ai_prompt", {"chapter_text": chapter_text, "meme_level": "light"}),
            ("build_full_book_audit_prompt", {"book_plan": plan, "chapters_summary": summaries,
             "chapter_evidence": [{"chapter": 40, "text": chapter_text}], "word_stats": {"words": 320000}, "canonical_facts": facts}),
            ("build_volume_summary_prompt", chapters),
        ]
        expected = fixture("test_all_templates_with_legacy_and_extended_options")
        results = []
        self.assertEqual(len(fixtures), len(expected))
        for (python, options), reference in zip(fixtures, expected):
            builder = getattr(prompts, python)
            with self.subTest(builder=python, budget=options.get("input_budget") if isinstance(options, dict) else None):
                result = builder(**options) if isinstance(options, dict) else builder(options)
                self.assertEqual({part: sha256(value.encode()).hexdigest() for part, value in result.items()}, reference)
            results.append(result)
        legacy = results[3]
        self.assertIn("本书风格指南", legacy["prompt"])
        self.assertNotIn("longMemory", legacy["prompt"])
        self.assertNotIn("世界观硬规则：", legacy["prompt"])
        budgeted = results[6]
        self.assertIn("世界观硬规则：潮门只能在退潮前打开", budgeted["prompt"])
        self.assertIn("终章完结铁律", budgeted["prompt"])
        data = json.loads(budgeted["prompt"].split(FACTS_HEADING)[1])
        memory = data["longMemory"]
        self.assertEqual(list(data["characters"])[0], "林默")
        self.assertEqual(len(data["characters"]), 20)
        self.assertEqual([entry["volume"] for entry in memory["volumeSummaries"]], [3, 2])
        self.assertEqual([entry["id"] for entry in memory["recentPayoffs"]], ["kept"])
        self.assertEqual([entry["id"] for entry in memory["commitments"]], ["open"])
        self.assertEqual([entry["id"] for entry in memory["subplots"]], ["sub-open"])
        self.assertNotIn("sourceHash", budgeted["prompt"])
        self.assertNotIn("journalSentinel", budgeted["prompt"])
        self.assertIn("第38章（待复核）", budgeted["prompt"])

    def test_fact_selection_prioritizes_matches_before_recency(self):
        _, _, facts = story_fixture()
        options = {"context_text": "主角寻找铜钥匙和潮门规则", "max_items": 3, "max_secrets": 2, "max_foreshadowing": 1}
        expected = fixture("test_fact_selection_prioritizes_matches_before_recency")
        selected = prompts.extract_relevant_facts(facts, **options)
        self.assertEqual(selected, expected)
        self.assertEqual(list(selected["items"]), ["铜钥匙", "物件28", "物件29"])
        self.assertNotIn("sourceChapter", selected["characters"]["林默"])

    def test_mandatory_content_and_complete_unicode_excerpts(self):
        plan = {"worldview": {"coreRules": ["总纲不可裁剪" * 800]}}
        with self.assertRaises(LLMError) as caught:
            prompts.build_chapter_write_prompt(book_plan=plan, input_budget=2150)
        self.assertEqual(caught.exception.code, "CHAPTER_PROMPT_BUDGET_EXCEEDED")
        prior = "🙂" + "甲" * 799
        result = prompts.build_chapter_write_prompt(prev_chapter_ending=prior)
        self.assertIn("【上一章末尾衔接片段（约800字）】\n" + "甲" * 799, result["prompt"])
        result["prompt"].encode("utf-8")
