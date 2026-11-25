import json
import os
import unittest
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

from novel_forge import chapter_style, draft_quality, long_memory, prompt_budget, prompts
from novel_forge.arc_planning import arc_chapter_span, normalize_canon_locks, validate_arc_plan
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
    def phase2_issue(self, rule, body, *, history=None, words=None):
        issues = chapter_style.find_chapter_style_issues(
            body, {"characters": {"钟楼调查员林默": {}, "林默": {}}},
            count_chinese_words(body) if words is None else words, recent_chapters=history,
        )
        matches = [issue for issue in issues if issue["rule"] == rule]
        self.assertLessEqual(len(matches), 1)
        if matches:
            self.assertIn(matches[0]["quote"], body)
            self.assertTrue(matches[0]["quote"].strip())
            self.assertTrue(matches[0]["suggestion"])
            return matches[0]
        return None

    def test_filler_rules_respect_thresholds_idioms_and_quoted_dialogue(self):
        rule = "cliche_filler_density"
        self.assertIsNotNone(self.phase2_issue(rule, "潮声在瞬间变响。" * 6, words=4000))
        self.assertIsNone(self.phase2_issue(rule, "潮声在瞬间变响。" * 5))
        fillers = "瞬间安静，骤然回响，死寂散开。仿佛听见一丝细声，窗上有一抹光。"
        self.assertIsNotNone(self.phase2_issue(rule, fillers * 2, words=2666))
        self.assertIsNone(self.phase2_issue(rule, fillers * 2, words=2667))
        self.assertIsNone(self.phase2_issue(rule, "风沿石阶上行。" * 570 + "\n" + fillers * 2))
        for opening, closing in (("“", "”"), ("「", "」"), ("『", "』"), ('"', '"'), ("‘", "’"), ("＂", "＂")):
            self.assertIsNone(self.phase2_issue(rule, opening + "就是这个瞬间。" * 8 + closing))
        self.assertIsNone(self.phase2_issue(rule, "他做事一丝不苟，从无一丝一毫含糊，忙到两眼一抹黑。" * 7))

    def test_mouth_arc_variants_are_local_or_use_no_more_than_three_earlier_chapters(self):
        rule = "mouth_arc_cliche"
        draft = "掌柜嘴角微颤，扯出一点笑意。"
        old_a, old_b = "唇角慢慢泛起冷意。", "嘴角扬起一线嘲弄。"
        self.assertIsNotNone(self.phase2_issue(rule, draft + old_a))
        self.assertIsNotNone(self.phase2_issue(rule, draft, history=[old_a, "散场。", old_b]))
        self.assertIsNone(self.phase2_issue(rule, draft))
        self.assertIsNone(self.phase2_issue(rule, draft, history=[old_a + old_b]))
        self.assertIsNone(self.phase2_issue(rule, draft, history=[old_a, "散场。", "远处下雪。", old_b]))
        self.assertIsNone(self.phase2_issue(rule, '“嘴角泛起冷意。”他说。嘴角“传说”勾起风声。', history=[old_a, old_b]))
        self.assertIsNone(self.phase2_issue(rule, "嘴角痉挛. 扬起旗帜。", history=[old_a, old_b]))

    def test_explanation_and_accounting_detection_has_narrow_scope(self):
        rule = "explanatory_narration"
        strong = "门锁上了。这番话看似在劝他回家，实则已经把退路封死。"
        weak = "这不仅是救人，更是守信。"
        self.assertIsNotNone(self.phase2_issue(rule, strong))
        self.assertIsNone(self.phase2_issue(rule, f"“{strong}”她打断了值班人。"))
        self.assertIsNone(self.phase2_issue(rule, weak))
        self.assertIsNotNone(self.phase2_issue(rule, weak + "掌柜打的正是两头收钱的算盘。"))
        account = ("她翻到纸背（首笔实际到账８万，后来扣除２万，纸背还附有每次交割的时间和对应的收款人，"
                   "余额与昨日登记时两张单据上的数字核对一致，另外两项也已审核）。")
        self.assertIsNotNone(self.phase2_issue(rule, account))
        stated = ("她翻到纸背（前一本单一共写了８万元，另一页写了2万元，里头另有3万元，"
                  "她把留存下来的三张纸挨个拿到灯下重看，纸上的签名与门房留下的对得上）。")
        self.assertIsNotNone(self.phase2_issue(rule, stated))
        self.assertIsNone(self.phase2_issue(rule, "他说（到账8万元，扣除2万元，余额6万元）。"))
        self.assertIsNone(self.phase2_issue(rule, "她在窗边等候（这扇南窗曾经悬过一道麻绳，木框和窗沿都被来往的人擦得发亮，"
                                                "雨天可以看清水迹从屋檐一直流到铺开的旧报纸上）。"))
        self.assertIsNone(self.phase2_issue(rule, "系统显示（第一回合加40分，第二回合减20分，"
                                                "累计影响后面所有判断，走廊另一端的五人刚刚完成训练还有挑战赛）。"))

    def test_ending_windows_detect_three_patterns_and_keep_natural_closures(self):
        rule = "repetitive_ending_hook"
        turn = "然而，就在脚步声消失的瞬间，电梯底部传上来半句人声。"
        old_turn = "还没等潮水彻底平息，船只就离开了。"
        other_turn = "就在钟声响起的刹那，无人的楼上亮起了灯。"
        plain = "浪打在远处的礁石上。"
        self.assertIsNotNone(self.phase2_issue(rule, turn, history=[old_turn]))
        self.assertIsNotNone(self.phase2_issue(rule, turn, history=[old_turn, other_turn, plain]))
        self.assertIsNone(self.phase2_issue(rule, turn, history=[old_turn, "", plain]))
        self.assertIsNone(self.phase2_issue(rule, turn))
        self.assertIsNone(self.phase2_issue(rule, turn + "一切归于平静。", history=[old_turn]))
        self.assertIsNone(self.phase2_issue(rule, turn + "灯灭了。" * 110, history=[old_turn]))
        self.assertIsNone(self.phase2_issue(rule, "“" + "安静些。" * 120 + turn + "”", history=[old_turn]))

        alarms = ["【系统提示：仓门锁定】", "【警报：失去信号】"]
        self.assertIsNotNone(self.phase2_issue(rule, "灯灭了。\n【系统警报：通道即将关闭】", history=alarms))
        self.assertIsNotNone(self.phase2_issue(rule, "【倒计时00:15】", history=alarms))
        self.assertIsNone(self.phase2_issue(rule, "“【系统警报：通道即将关闭】”", history=alarms))
        self.assertIsNone(self.phase2_issue(rule, "灯灭了。\n【计划】", history=alarms))
        self.assertIsNone(self.phase2_issue(rule, "【警报：通道即将关闭】他出去寻找出口。", history=alarms))
        self.assertIsNone(self.phase2_issue(rule, "【警报：通道即将关闭】", history=alarms[-1:]))

        motif = "他看向冷库铁门。黑色封条翘起一角。"
        earlier = ["脚步停在冷库铁门。夜色吞没黑色封条。", "他们背靠冷库铁门，等人揭开黑色封条。"]
        self.assertIsNotNone(self.phase2_issue(rule, motif, history=earlier))
        self.assertIsNone(self.phase2_issue(rule, motif, history=[earlier[0], plain]))
        self.assertIsNotNone(self.phase2_issue(rule, "冷库铁门内侧，咚咚。", history=["拳头落在冷库铁门，咚咚。", "咚、咚，响自冷库铁门。"]))
        self.assertIsNone(self.phase2_issue(rule, "冷库铁门外有人。", history=["他听见冷库铁门合拢。", "去冷库铁门旁等我。"]))
        self.assertIsNone(self.phase2_issue(rule, "钟楼调查员林默，回家了。", history=["先等钟楼调查员林默。", "钟楼调查员林默、住手。"]))
        self.assertIsNone(self.phase2_issue(rule, "咚咚。", history=["咚咚。", "咚、咚。"]))

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
    def test_commitment_deadline_only_upserts_and_legacy_aliases_replay(self):
        quote = "姚青说天亮前必须赶回港口。"
        entries = [
            {"text": "返 港倒计时Ａ。", "deadline": "天亮", "characters": ["姚青"]},
            {"text": "返港倒计时A", "deadline": "日落", "characters": ["姚青"]},
            {"text": "返港倒计时A", "deadline": "午时", "characters": ["船医"]},
            {"id": "commitments-2-1", "status": "fulfilled"},
            {"text": "返港倒计时A", "deadline": "下次天亮", "characters": ["姚青"]},
        ]
        history = [{"chapter": chapter, "delta": {"memory": long_memory.normalize_memory_delta(
            {"commitments": [{**entry, "quote": quote}]}, chapter, quote,
        )}} for chapter, entry in enumerate(entries, 1)]
        updated = long_memory.replay_long_memory(history[:2])["commitments"]
        self.assertEqual(len(updated), 1)
        self.assertEqual((updated[0]["id"], updated[0]["sourceChapter"], updated[0]["updatedAtChapter"], updated[0]["deadline"]),
                         ("commitments-1-1", 1, 2, "日落"))
        complete = long_memory.replay_long_memory(list(reversed(history)))["commitments"]
        self.assertEqual([(entry["id"], entry["status"], entry["deadline"]) for entry in complete], [
            ("commitments-1-1", "fulfilled", "日落"), ("commitments-3-1", "active", "午时"),
            ("commitments-5-1", "active", "下次天亮"),
        ])

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
    def test_review_repair_and_extraction_pick_old_facts_from_body_and_outline(self):
        plan, outline, facts = story_fixture()
        facts["items"] = {
            "唯一铜章": {"sourceChapter": 1, "location": "壁炉夹层"},
            "独门暗号": {"sourceChapter": 2, "location": "背面刻记"},
            **{f"柜中零件{i}": {"sourceChapter": i} for i in range(3, 38)},
        }
        outline = {**outline, "goal": "核对独门暗号"}
        common = {"chapter_number": 40, "chapter_text": "他捡起了唯一铜章。", "outline": outline, "canonical_facts": facts}
        for builder in (prompts.build_verify_prompt, prompts.build_repair_prompt, prompts.build_fact_extraction_prompt):
            with self.subTest(builder=builder.__name__):
                rendered = builder(**common)["prompt"]
                self.assertIn('"唯一铜章"', rendered)
                self.assertIn('"独门暗号"', rendered)
        for builder in (prompts.build_verify_prompt, prompts.build_repair_prompt):
            active = builder(**common, book_plan=plan, long_memory=True)["prompt"]
            self.assertIn("舞台世界观：雾中海港", active)
            self.assertIn("世界观硬规则：潮门只能在退潮前打开", active)
            self.assertIn("林默(主角)", active)
            self.assertIn("黎明前返港", active)
            self.assertIn("第3卷前情", active)
            self.assertIn("第2卷前情", active)
            for absent in ("第1卷前情", "第4卷前情", "旧承诺已兑现", "journalSentinel", "sourceHash"):
                self.assertNotIn(absent, active)
            no_memory = builder(**common, book_plan=plan)["prompt"]
            self.assertNotIn("当前约束与前卷摘要", no_memory)
            self.assertIn("舞台世界观：雾中海港", no_memory)

    def test_injury_age_filter_is_optional_and_applies_to_both_write_paths(self):
        plan, outline, _ = story_fixture()
        facts = {"injuries": [
            {"id": key, "character": name, "description": key, "updatedAtChapter": updated, "severity": severity}
            for key, name, updated, severity in (
                ("旧无关轻伤", "陌生人", 29, "low"), ("旧相关中伤", "林默", 5, "medium"),
                ("旧无关重伤", "旁观者", 4, "high"), ("十章边界", "第三人", 30, "medium"),
            )
        ]}
        self.assertEqual(len(prompts.extract_relevant_facts(facts)["activeInjuries"]), 4)
        trimmed = prompts.extract_relevant_facts(facts, context_text="林默开门", current_chapter=40)
        self.assertEqual([row["id"] for row in trimmed["activeInjuries"]], ["旧相关中伤", "旧无关重伤", "十章边界"])
        for input_budget in (None, 3350):
            result = prompts.build_chapter_write_prompt(book_plan=plan, outline=outline, canonical_facts=facts,
                                                        input_budget=input_budget)
            self.assertEqual(set(result), {"system", "prompt"})
            self.assertNotIn("旧无关轻伤", result["prompt"])
            self.assertIn("旧相关中伤", result["prompt"])
            self.assertIn("旧无关重伤", result["prompt"])

    def test_story_ledger_old_injuries_need_a_part_or_description_reference(self):
        plan, outline, _ = story_fixture()
        facts = {"injuries": [
            {"id": "名字命中", "character": "林默", "bodyPart": "左膝", "description": "旧擦伤",
             "sourceChapter": 5, "severity": "low"},
            {"id": "部位命中", "character": "林默", "bodyPart": "右臂", "description": "旧撞伤",
             "sourceChapter": 5, "severity": "medium"},
            {"id": "重伤保留", "character": "船长", "bodyPart": "左臂", "description": "重伤未愈",
             "sourceChapter": 5, "severity": "high"},
        ]}
        outline = {**outline, "summary": "林默托住右臂，再去查潮门。"}
        for budget in (None, 8000):
            args = {"book_plan": plan, "outline": outline, "canonical_facts": facts, "input_budget": budget}
            active = prompts.build_chapter_write_prompt(**args, story_ledger=True)["prompt"]
            self.assertNotIn("名字命中", active)
            self.assertIn("部位命中", active)
            self.assertIn("重伤保留", active)
            legacy = prompts.build_chapter_write_prompt(**args)["prompt"]
            self.assertIn("名字命中", legacy)

    def story_ledger_fixture(self):
        plan, outline, _ = story_fixture()
        plan["canonLocks"] = {"currencyAndEra": "民国初年，账目统一以元记。"}
        plan["mainCharacters"].append({"name": "方弦", "role": "对手"})
        setup = [
            (1, "第一天初冬傍晚，林默亲眼看见铜碑从水面浮出。苏红亲手收好海图。港口可用余额80元。",
             {"storyClock": {"day": "第一天", "season": "初冬", "timeOfDay": "傍晚"},
              "ledgerBalances": [{"label": "港口可用余额", "value": "80元"}],
              "characterStates": {"林默": {"seen": True, "stanceTier": "ally", "knownFacts": ["铜碑曾从水面浮出"]},
                                  "苏红": {"seen": True, "stanceTier": "wary", "knownFacts": ["海图暗藏取潮手诀"]}}}),
            (2, "第二天清晨，港口可用余额增加20元，扣除前项后明确记为100元。林默向方弦出示借据。",
             {"storyClock": {"day": "第二天", "timeOfDay": "清晨"},
              "ledgerBalances": [{"label": "港口可用余额", "value": "100元", "change": "+20元"}],
              "characterStates": {"方弦": {"seen": True, "stanceTier": "hostile"}}}),
            (4, "第四天深夜，港口可用余额已经变成900元，苏红看到账簿被偷走。",
             {"storyClock": {"day": "第四天", "timeOfDay": "深夜"},
              "ledgerBalances": [{"label": "港口可用余额", "value": "900元"}],
              "characterStates": {"苏红": {"seen": True, "knownFacts": ["看到后院有人偷走账簿"]}}}),
        ]
        history = []
        for number, quote, event in setup:
            raw = {
                "storyClock": {**event["storyClock"], "quote": quote},
                "ledgerBalances": [{**row, "quote": quote} for row in event["ledgerBalances"]],
                "characterStates": {name: {**state, "quote": quote} for name, state in event["characterStates"].items()},
            }
            delta = long_memory.normalize_memory_delta(raw, number, quote, story_ledger=True, long_memory=False,
                                                        book_plan=plan, history=history)
            history.append({"chapter": number, "delta": {"memory": delta}})
        facts = {**long_memory.replay_long_memory(history, book_plan=plan), "factHistory": history}
        outline = {**outline, "chapterNumber": 2, "title": "港口来信", "summary": "林默查看账房收据",
                   "keyEvents": [], "goal": "核对借据", "hook": "掌柜传信"}
        return plan, outline, facts

    def test_story_ledger_builder_projection_limits_locks_and_respects_chapter_cutoff(self):
        plan, outline, facts = self.story_ledger_fixture()
        for budget in (None, 8000):
            options = {"book_plan": plan, "outline": outline, "canonical_facts": facts,
                       "input_budget": budget, "prev_chapter_ending": "苏红刚刚走出档案室。"}
            active = prompts.build_chapter_write_prompt(**options, story_ledger=True)["prompt"]
            self.assertIn("铜碑曾从水面浮出", active)
            self.assertIn("80元", active)
            self.assertNotIn("100元", active)
            self.assertNotIn("900元", active)
            self.assertNotIn("海图暗藏取潮手诀", active)
            legacy = prompts.build_chapter_write_prompt(**options, long_memory=True)["prompt"]
            self.assertNotIn("knownFacts", legacy)
            self.assertNotIn("stanceTier", legacy)
            self.assertNotIn("ledgerBalances", legacy)
        options = {"chapter_number": 2, "chapter_text": "林默看到潮水已退。", "canonical_facts": facts,
                   "outline": outline, "book_plan": plan}
        for build in (prompts.build_verify_prompt, prompts.build_repair_prompt, prompts.build_fact_extraction_prompt):
            rendered = build(**options, story_ledger=True)
            self.assertIn("铜碑曾从水面浮出", rendered["prompt"])
            self.assertIn("80元", rendered["prompt"])
            self.assertNotIn("100元", rendered["prompt"])
            self.assertNotIn("海图暗藏取潮手诀", rendered["prompt"])
            self.assertIn("[STORY_LEDGER]", rendered["system"])
        review = prompts.build_verify_prompt(**options, story_ledger=True, rolling_arc_outlines=True)
        self.assertLess(review["prompt"].index("不可变核心口径锁"), review["prompt"].index("本章前随章台账"))
        self.assertIn("newCharacterCandidates", review["prompt"])
        self.assertIn("ledgerRef", review["system"])
        self.assertNotIn("newCharacterCandidates", prompts.build_verify_prompt(**options)["prompt"])

    def test_arc_story_ledger_review_uses_opening_baseline_and_chronological_changes(self):
        plan, _, facts = self.story_ledger_fixture()
        arc = {"arcIndex": 1, "startChapter": 2, "endChapter": 4}
        chapters = [{"chapterNumber": n, "status": ("needs_review" if n == 3 else "verified"),
                     "content": f"第{n}章的真实正文。"} for n in (2, 3, 4)]
        archive = prompts.build_arc_summary_prompt(arc, chapters, story_ledger=True, canonical_facts=facts, book_plan=plan)
        blocks = archive["prompt"].split("【单元开始前的台账基线与按章变化】\n")[1].split("\n【正文依据】\n")
        material = json.loads(blocks[0])
        self.assertEqual(material["baselineBeforeChapter"], 2)
        self.assertEqual(material["openingLedger"]["storyClock"]["day"], "第一天")
        self.assertEqual(material["openingLedger"]["ledgerBalances"][0]["value"], "80元")
        self.assertEqual([row["chapter"] for row in material["changesByChapter"]], [2, 4])
        self.assertEqual(material["changesByChapter"][0]["storyClock"], {"day": "第二天", "timeOfDay": "清晨"})
        self.assertEqual(material["changesByChapter"][0]["ledgerBalances"][0]["change"], "+20元")
        self.assertEqual(material["changesByChapter"][1]["ledgerBalances"][0]["value"], "900元")
        self.assertIn("driftChecks", archive["prompt"])
        self.assertIn("民国初年", blocks[0])
        self.assertNotIn("driftChecks", prompts.build_arc_summary_prompt(arc, chapters)["prompt"])

        facts["arcSummaries"] = [
            {"arc": 0, "startChapter": 1, "endChapter": 1, "driftChecks": [{"issue": "更早的问题"}]},
            {"arc": 1, "startChapter": 2, "endChapter": 4,
             "driftChecks": [{"chapter": 4, "kind": "value", "issue": "把总收入当成可用余额",
                              "fixHint": "下单元区分总额与可用", "quote": "原文引句仅留存档"}]},
        ]
        opts = {"start_chapter": 5, "book_plan": plan, "canonical_facts": facts, "total_chapters": 9, "arc_index": 2}
        active = prompts.build_arc_outlines_prompt(**opts, story_ledger=True)["prompt"]
        self.assertIn("900元", active)
        self.assertIn("coreCharacterNames", active)
        self.assertIn("下单元区分总额与可用", active)
        self.assertNotIn("更早的问题", active)
        self.assertNotIn("原文引句仅留存档", active)
        legacy = prompts.build_arc_outlines_prompt(**opts)["prompt"]
        self.assertNotIn("storyLedger", legacy)
        self.assertNotIn("下单元区分总额与可用", legacy)

    def test_independent_options_keep_node_default_precedence(self):
        env = {name: "" for name in (
            "NOVEL_FORGE_CHAPTER_INPUT_BUDGET", "NOVEL_FORGE_DRAFT_QUALITY", "NOVEL_FORGE_LONG_MEMORY",
            "NOVEL_FORGE_WRITE_AI_GATE", "NOVEL_FORGE_DEGRADE_ON_VERIFY_EXHAUSTED", "NOVEL_FORGE_ROLLING_ARC_OUTLINES",
            "NOVEL_FORGE_STORY_LEDGER",
        )}
        with patch.dict(os.environ, env):
            default = prompts.read_writing_options()
            self.assertEqual(default, {"input_budget": 16384, "draft_quality": True, "long_memory": True,
                                       "chapter_ai_gate": True, "degrade_on_verify_exhausted": False,
                                       "rolling_arc_outlines": True, "story_ledger": True})
            for name, option, expected in (
                ("CHAPTER_INPUT_BUDGET", "input_budget", None), ("DRAFT_QUALITY", "draft_quality", False),
                ("LONG_MEMORY", "long_memory", False), ("WRITE_AI_GATE", "chapter_ai_gate", False),
                ("ROLLING_ARC_OUTLINES", "rolling_arc_outlines", False),
                ("STORY_LEDGER", "story_ledger", False),
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
            ("build_chapter_write_prompt", {**write, "input_budget": 3350, "draft_quality": True,
                                                                        "long_memory": True, "chapter_ai_gate": True}),
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

    def test_arc_bounds_have_no_orphan_tail_for_every_legal_choice(self):
        def all_paths(start, total):
            low, high = arc_chapter_span(start, total)
            if total - start + 1 <= 6:
                self.assertEqual((low, high), (total - start + 1,) * 2)
                return
            for length in range(low, high + 1):
                self.assertGreaterEqual(length, 3)
                self.assertLessEqual(length, 6)
                self.assertGreaterEqual(total - start - length + 1, 3)
                all_paths(start + length, total)

        for total in (2, 5, 7, 8, 9, 10, 20):
            with self.subTest(total=total):
                all_paths(1, total)
        self.assertEqual(arc_chapter_span(1, 7), (3, 4))
        self.assertEqual(arc_chapter_span(1, 8), (3, 5))
        self.assertEqual(arc_chapter_span(1, 9), (3, 6))

    def test_arc_validation_rejects_gaps_orphans_empty_entries_and_duplicate_titles(self):
        rows = [{"chapterNumber": number, "title": f"第{number}章 取证路线{number}", "goal": "找到证人",
                 "summary": "仓库保管员交出待交割的收据", "keyEvents": ["收据送入档案室"], "hook": "有人推开暗门"}
                for number in range(1, 6)]
        payload = {"arcTitle": "档案室交割", "arcGoal": "证人确认收据", "villainChips": [], "chapters": rows[:4]}
        plan, batch = validate_arc_plan(payload, arc_index=1, start=1, total=7)
        self.assertEqual((plan["arcIndex"], plan["startChapter"], plan["endChapter"]), (1, 1, 4))
        self.assertEqual(batch, rows[:4])
        cases = (
            ({**payload, "chapters": rows}, "3–4 章"),
            ({**payload, "chapters": [rows[0], rows[2], rows[3]]}, "整数章号 2"),
            ({**payload, "chapters": [rows[0], {**rows[1], "summary": ""}, rows[2]]}, "实质|剧情目标"),
            ({**payload, "chapters": [rows[0], {**rows[1], "title": "第2章 取证路线1"}, rows[2]]}, "标题.*重复"),
            ({**payload, "villainChips": [{"villain": "守卫", "newChips": [], "exitPlan": "illegal"}]}, "exitPlan"),
        )
        for raw, message in cases:
            with self.assertRaisesRegex(ValueError, message):
                validate_arc_plan(raw, arc_index=1, start=1, total=7)
        with self.assertRaisesRegex(ValueError, "标题.*重复"):
            validate_arc_plan(payload, arc_index=2, start=1, total=7, recent_titles=["第99章 取证路线1"])
        long_chip = "掌控兵部职方司边镇舆图与木沙盘制作权，用陈年核桃油蜡混塞北黄沙伪造旧沙盘包浆，并将饮马河与百眼井水源坐标向西篡改八十里指向盐碱干涸死地，企图截断五万北伐先锋铁骑补给线"
        normalized_plan, _ = validate_arc_plan({
            **payload,
            "villainChips": [
                {"villain": "守卫甲（" + "头衔" * 30 + "）", "newChips": [long_chip, "", "第二条筹码", "第三条筹码", "第四条溢出"], "exitPlan": "退场"},
                {"villain": "守卫乙", "newChips": None, "exitPlan": " BACKGROUND "},
            ],
        }, arc_index=2, start=1, total=7)
        self.assertEqual(len(normalized_plan["villainChips"]), 2)
        self.assertLessEqual(len(normalized_plan["villainChips"][0]["villain"]), 50)
        self.assertEqual(len(normalized_plan["villainChips"][0]["newChips"]), 3)
        self.assertGreater(len(normalized_plan["villainChips"][0]["newChips"][0]), 80)
        self.assertEqual(normalized_plan["villainChips"][0]["exitPlan"], "exit")
        self.assertEqual(normalized_plan["villainChips"][1]["newChips"], [])
        self.assertEqual(normalized_plan["villainChips"][1]["exitPlan"], "background")

    def test_canon_locks_use_420_character_budget_without_cutting_rules(self):
        first = "等级上限100级，主角初始46级。"
        taboo = "现代都市不存在穿书入口，也没有会凭空发任务和领取游戏奖励的系统机制。"
        asset = "核心银铃终身戴在主角右手腕内侧，铃身正面刻有完整的玄天七号，避开左手腕和挂绳口径。"
        locks = normalize_canon_locks({
            "powerScale": first + "不得擅自改变数值，主角起点不能超出约定值" * 6,
            "currencyAndEra": {"amount": 100},
            "worldTaboos": [None, taboo, taboo, "禁用银两"],
            "keyAssets": [asset, "固定金额 1000" + "千" * 60],
        })
        self.assertEqual(locks, {"powerScale": first, "worldTaboos": [taboo, "禁用银两"], "keyAssets": [asset]})
        bounded = normalize_canon_locks({
            "powerScale": "甲" * 100, "currencyAndEra": "乙" * 100,
            "worldTaboos": ["丙" * 45, "丁" * 45, "戊" * 45, "额外规则"],
            "keyAssets": ["己" * 50, "庚" * 50, "辛" * 50, "壬" * 50],
        })
        self.assertEqual(sum(len(value) if isinstance(value, str) else sum(map(len, value)) for value in bounded.values()), 385)
        self.assertEqual(len(bounded["worldTaboos"]), 3)
        self.assertEqual(len(bounded["keyAssets"]), 1)
        self.assertIsNone(normalize_canon_locks({"powerScale": 100, "keyAssets": "错误"}))

    def test_arc_context_projections_keep_locks_and_recent_cumulative_beats_with_long_memory_off(self):
        plan, outline, facts = story_fixture()
        plan["canonLocks"] = {"powerScale": "满级100，主角开局46。", "keyAssets": ["银铃只系右手腕。"]}
        facts["arcSummaries"] = [{
            "arc": index, "startChapter": index * 5 - 4, "endChapter": index * 5,
            "mainline": f"单元{index}剧情", "sourceHash": f"hash-{index}",
            "usedBeats": [{"chapter": index * 5, "text": f"机制{index}甲", "quote": "不可泄漏的原文引句"},
                          {"chapter": index * 5, "text": f"独有机制{index}乙", "quote": "不可泄漏的原文引句"}],
            "usedHooks": [{"chapter": index * 5, "text": f"钩子{index}", "quote": "不可泄漏的原文引句"}],
            "villainStatus": f"反派筹码{index}",
        } for index in range(1, 9)]
        facts["arcSummaries"][2]["usedBeats"].append({"chapter": 15, "text": "机制６ 甲。", "quote": "不可泄漏的原文引句"})
        facts["foreshadowing"].append({"id": "terminal-clue", "clue": "船底没有开启的暗格", "status": "planted",
                                     "sourceChapter": 35, "payoffChapter": 40})
        plan_prompt = prompts.build_book_plan_prompt(draft_quality=True, rolling_arc_outlines=True)
        self.assertIn("canonLocks", plan_prompt["prompt"])
        self.assertIn("styleBible", plan_prompt["prompt"])
        self.assertNotIn("canonLocks", prompts.build_book_plan_prompt(draft_quality=True)["prompt"])

        roll = prompts.build_arc_outlines_prompt(book_plan=plan, canonical_facts=facts, start_chapter=40,
                                                total_chapters=40, arc_index=8, finalize_book=True)
        self.assertIn("满级100", roll["prompt"])
        self.assertIn("终局收网单元", roll["prompt"])
        self.assertIn("船底没有开启的暗格", roll["prompt"])
        self.assertIn("独有机制1乙", roll["prompt"])
        self.assertIn("反派筹码7", roll["prompt"])
        self.assertNotIn("单元8剧情", roll["prompt"])

        write = {"book_plan": plan, "outline": outline, "canonical_facts": facts}
        wide = prompts.build_chapter_write_prompt(**write, rolling_arc_outlines=True)["prompt"]
        self.assertIn("单元7剧情", wide)
        self.assertIn("机制6甲", wide)
        tight = prompts.build_chapter_write_prompt(**write, rolling_arc_outlines=True, input_budget=2250)
        self.assertIn("满级100", tight["prompt"])
        self.assertNotIn("单元7剧情", tight["prompt"])
        self.assertNotIn("不可泄漏的原文引句", wide)
        self.assertNotIn("满级100", prompts.build_chapter_write_prompt(**write)["prompt"])
        self.assertNotIn("满级100", prompts.build_chapter_outlines_prompt(book_plan=plan)["prompt"])
        self.assertNotIn("满级100", prompts.build_full_book_audit_prompt(book_plan=plan)["prompt"])

        for builder in (prompts.build_verify_prompt, prompts.build_repair_prompt):
            args = {"chapter_number": 40, "chapter_text": "林默发现铜钥匙。", "book_plan": plan, "canonical_facts": facts}
            text = builder(**args, rolling_arc_outlines=True, long_memory=False)["prompt"]
            self.assertIn("银铃只系右手腕", text)
            self.assertIn("单元6剧情", text)
            self.assertNotIn("单元5剧情", text)
            block = json.JSONDecoder().raw_decode(text.split("【前两个单元与累计已演桥段（正文复演需拦截）】\n")[1])[0]
            self.assertEqual(len(block["usedBeats"]), 12)
            self.assertIn({"chapter": 10, "text": "独有机制2乙"}, block["usedBeats"])
            self.assertNotIn("不可泄漏的原文引句", text)
            self.assertNotIn("单元7剧情", builder(**args)["prompt"])

    def test_phase2_red_lines_only_affect_enabled_writing_and_review(self):
        scenes = "核心情感高潮禁止出戏玩梗"
        args = {"meme_level": "light", "meme_samples": [{"title": "打哑谜", "summary": "用信息差制造反差。"}]}
        default = prompts.build_chapter_write_prompt(**args)
        disabled = prompts.build_chapter_write_prompt(**args, chapter_ai_gate=False)
        enabled = prompts.build_chapter_write_prompt(**args, chapter_ai_gate=True)
        self.assertEqual(default, disabled)
        self.assertEqual(set(enabled), {"system", "prompt"})
        self.assertNotIn(scenes, disabled["system"])
        self.assertIn(scenes, enabled["system"])
        self.assertIn("禁止说明书式对白和背供词", enabled["system"])
        self.assertIn("禁止旁白抽象定性", enabled["system"])
        self.assertIn("【打哑谜】", enabled["system"])
        plan = prompts.build_book_plan_prompt(**args)
        self.assertNotIn(scenes, plan["system"] + plan["prompt"])

        review = {"chapter_number": 2, "chapter_text": "她放下戒指。"}
        enabled = prompts.build_verify_prompt(**review, chapter_ai_gate=True)["system"]
        for marker in ("大段括号", "背供词", "连续用「然而就在……瞬间」", "以上病灶"):
            self.assertIn(marker, enabled)
            self.assertNotIn(marker, prompts.build_verify_prompt(**review, chapter_ai_gate=False)["system"])
            self.assertNotIn(marker, prompts.build_verify_prompt(**review, chapter_ai_gate=True, check_ai_flavor=False)["system"])

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

    def test_static_prefix_precedes_chapter_variables_for_prompt_cache(self):
        plan, outline, facts = story_fixture()
        ch5 = prompts.build_chapter_write_prompt(
            book_plan=plan, outline={**outline, "chapterNumber": 5, "title": "第5章 起风"},
            canonical_facts=facts, draft_quality=True, total_chapters=40,
        )
        ch6 = prompts.build_chapter_write_prompt(
            book_plan=plan, outline={**outline, "chapterNumber": 6, "title": "第6章 靠岸"},
            canonical_facts=facts, draft_quality=True, total_chapters=40,
        )
        self.assertEqual(ch5["system"], ch6["system"])
        marker = "\n请撰写第"
        self.assertIn(marker, ch5["prompt"])
        prefix5 = ch5["prompt"].split(marker)[0]
        prefix6 = ch6["prompt"].split(marker)[0]
        self.assertEqual(prefix5, prefix6)
        self.assertIn("全书总纲核心锚点", prefix5)
        self.assertIn("本书风格指南", prefix5)

        o1 = prompts.build_chapter_outlines_prompt(book_plan=plan, start_chapter=11, end_chapter=20)
        o2 = prompts.build_chapter_outlines_prompt(book_plan=plan, start_chapter=21, end_chapter=30)
        self.assertEqual(o1["system"], o2["system"])
        self.assertEqual(o1["prompt"].split("\n请生成连续章纲：")[0], o2["prompt"].split("\n请生成连续章纲：")[0])
        self.assertIn("【全书总纲】", o1["prompt"].split("\n请生成连续章纲：")[0])

        r5 = prompts.build_repair_prompt(chapter_number=5, chapter_text="甲", outline=outline, issues=[])
        r6 = prompts.build_repair_prompt(chapter_number=6, chapter_text="乙", outline=outline, issues=[])
        self.assertEqual(r5["system"], r6["system"])
