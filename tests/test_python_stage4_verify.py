import json
import os
import unittest
from decimal import Decimal
from unittest.mock import AsyncMock, patch

from novel_forge import prompts, verifier
from novel_forge.chapter_style import (
    can_save_unverified_draft, find_meta_text, reaction_counts, reaction_window_issues, run_deterministic_checks,
)
from novel_forge.ledger_checks import assess_ledger_delta, ledger_label_aliases, numeric_claims, parse_amount, unit_conversions
from novel_forge.llm import estimate_tokens
from novel_forge.pipeline import read_writing_options
from novel_forge.prompt_budget import DEFAULT_REPAIR_INPUT_BUDGET, DEFAULT_VERIFY_INPUT_BUDGET, read_review_input_budget


_BODY = "白砚推开仓门，把湿漉漉的账册摆在长桌上。风吹灭了门边的灯，船工搁下扳手跑去取油。"
_LEDGER = {"ledgerBalances": [{"label": "盐铺账户", "value": "300两", "updatedAtChapter": 6}]}


def _delta(body, *, value="600两", change="+200两"):
    row = {"label": "盐铺账户", "value": value, "quote": body}
    if change is not None:
        row["change"] = change
    return {"items": {"盐票": {"owner": "白砚"}}, "memory": {"ledgerBalances": [row]}}


def _reply(**fields):
    return {"text": json.dumps({"passed": True, "issues": [], "aiIssues": [], **fields}, ensure_ascii=False)}


def _review_json(result):
    marker = "【相关规范事实库（含 sourceChapter 溯源）】\n" if result["system"].startswith("[TASK:VERIFY]") else "【相关规范事实库】\n"
    return json.loads(result["prompt"].split(marker, 1)[1].splitlines()[0])


class PromptBudgetTests(unittest.TestCase):
    def test_huge_history_fits_reserved_budgets_and_keeps_chapter_three_character(self):
        detail = "走水那一晚客船误入盐船航道，守岸人循着两盏青灯摸到接应他们的船工。" * 4
        arcs = [
            {"arc": index, "startChapter": index * 3 - 2, "endChapter": index * 3, "mainline": detail,
             "usedBeats": [{"chapter": index * 3, "text": f"守岸人取出信号旗{beat}，{detail}", "quote": "归档原文"}
                           for beat in range(8)]}
            for index in range(1, 61)
        ]
        volumes = [{"volume": index, "startChapter": index * 10 - 9, "endChapter": index * 10,
                    "mainline": detail * 2} for index in range(1, 21)]
        characters = {
            f"船户甲{index:03d}": {"role": "码头经办人", "location": detail, "traits": [detail] * 2,
                                     "sourceChapter": min(179, index + 4)} for index in range(212)
        }
        characters["白砚"] = {"role": "亡港旧账房", "location": "松门盐栈", "sourceChapter": 3}
        items = {f"通行证乙{index:03d}": {"owner": "账房", "attributes": [detail] * 3, "sourceChapter": 13}
                 for index in range(643)}
        facts = {
            "characters": characters, "items": items, "arcSummaries": arcs, "volumeSummaries": volumes,
            "subplots": [{"id": f"plot-{index}", "text": f"白砚追查失船{index}，{detail}", "characters": ["白砚"],
                          "status": "active", "updatedAtChapter": 160} for index in range(123)],
            "constraints": [{"id": "sealed", "text": "潮门只在退潮时开启", "status": "active", "sourceChapter": 4}],
            "foreshadowing": [{"id": "lantern", "clue": "两盏青灯", "status": "planted", "sourceChapter": 4}],
            **_LEDGER,
        }
        plan = {"planVersion": 2, "canonLocks": {"currencyAndEra": "盐货用两计价"},
                "mainCharacters": [{"name": "白砚", "role": "主角"}], "volumes": [
            {**row, "chapters": [row["startChapter"], row["endChapter"]],
             "escalation": {"pressure": f"潮汛{row['volume']}封住船道"}}
            for row in volumes
        ], "worldview": {"coreRules": ["潮门只在退潮时开启"]}}
        names = "、".join(list(characters)[:24] + list(items)[:20])
        body = _BODY + "他点过名册和物件：" + names + "。账上现余300两。\n\n" + detail * 14
        outline = {"chapterNumber": 180, "title": "交账", "summary": "白砚去取名册", "unrelated": "不应出现在章纲"}
        inputs = dict(chapter_number=180, chapter_text=body, outline=outline, canonical_facts=facts, book_plan=plan,
                      long_memory=True, story_ledger=True, rolling_arc_outlines=True)
        review = prompts.build_verify_prompt(**inputs, input_budget=DEFAULT_VERIFY_INPUT_BUDGET)
        repair = prompts.build_repair_prompt(**inputs, input_budget=DEFAULT_REPAIR_INPUT_BUDGET,
                                             issues=[{"type": "world_rule_conflict", "message": "白砚提前开闸"}])
        for result, ceiling in ((review, 13_312), (repair, 11_264)):
            self.assertNotIn("budgetMeta", result)
            self.assertLessEqual(estimate_tokens(result["system"] + "\n" + result["prompt"]), ceiling)
            self.assertIn(body, result["prompt"])
            self.assertNotIn("不应出现在章纲", result["prompt"])
            self.assertIn("盐货用两计价", result["prompt"])
            self.assertIn("潮汛18", result["prompt"])
            self.assertIn("亡港旧账房", result["prompt"])
        review_data, repair_data = _review_json(review), _review_json(repair)
        self.assertLessEqual(len(review_data["textEntities"]["characters"]), 24)
        self.assertLessEqual(len(review_data["textEntities"]["items"]), 20)
        self.assertEqual(repair_data["textEntities"]["characters"][0]["name"], "白砚")
        self.assertNotIn("repeatRecall", repair_data)
        self.assertEqual([row["id"] for row in review_data["hardMemory"]["constraints"]], ["sealed"])
        self.assertNotIn("已有活跃条目", review["prompt"])
        self.assertIn("numericClaims", review["prompt"])
        self.assertIn("storyLedger", repair["prompt"])
        self.assertNotIn("usedBeats", review_data["arcNear"]["arcSummaries"][0])
        self.assertEqual(len(review_data["horizon"]["volumeSummaries"]), 1)
        self.assertLessEqual(len(review_data["horizon"]["volumeSummaries"][0]["mainline"]), 140)

    def test_protected_overflow_keeps_body_and_clears_optional_slots(self):
        chapter = _BODY * 350
        kwargs = {"chapter_number": 11, "chapter_text": chapter,
                  "canonical_facts": {"characters": {"白砚": {"role": "弹性槽专用角色信息"}}}}
        for builder, budget in ((prompts.build_verify_prompt, DEFAULT_VERIFY_INPUT_BUDGET),
                                (prompts.build_repair_prompt, DEFAULT_REPAIR_INPUT_BUDGET)):
            with self.subTest(builder=builder.__name__):
                result = builder(**kwargs, input_budget=budget)
                self.assertIn(chapter, result["prompt"])
                self.assertNotIn("弹性槽专用角色信息", result["prompt"])
                used = estimate_tokens(result["system"] + "\n" + result["prompt"])
                self.assertEqual(result["budgetMeta"], {"budget": budget, "used": used, "expanded": True})
                self.assertGreater(used, budget)

    def test_zero_env_and_explicit_none_preserve_unbudgeted_prompts(self):
        env = {"NOVEL_FORGE_REVIEW_INPUT_BUDGET": "0", "NOVEL_FORGE_REPAIR_INPUT_BUDGET": "0"}
        with patch.dict(os.environ, env):
            options = read_writing_options()
        self.assertIsNone(read_review_input_budget("verify", 0))
        self.assertIsNone(options["verify_input_budget"])
        self.assertIsNone(options["repair_input_budget"])
        for builder, budget in ((prompts.build_verify_prompt, options["verify_input_budget"]),
                                (prompts.build_repair_prompt, options["repair_input_budget"])):
            kwargs = dict(chapter_number=2, chapter_text=_BODY, outline={"legacyField": "保留字段"})
            legacy = builder(**kwargs)
            self.assertEqual(legacy, builder(**kwargs, input_budget=budget))
            self.assertIn("legacyField", legacy["prompt"])
            self.assertNotIn("budgetMeta", legacy)
        with patch.dict(os.environ, {key: "" for key in env}):
            self.assertEqual(read_review_input_budget(), DEFAULT_VERIFY_INPUT_BUDGET)
            self.assertEqual(read_review_input_budget("repair"), DEFAULT_REPAIR_INPUT_BUDGET)

    def test_fact_extraction_compacts_input_and_output_schema(self):
        inputs = dict(chapter_number=7, chapter_text=_BODY, story_ledger=True, long_memory=True)
        for result in (prompts.build_fact_extraction_prompt(**inputs, input_budget=2000),
                       prompts.build_fact_extraction_prompt(**inputs, compact=True)):
            context = result["prompt"].split("【已有事实库摘要】\n")[1].splitlines()[0]
            self.assertIn("characters", json.loads(context))
            self.assertIn("ledgerBalances", json.loads(result["prompt"].splitlines()[-1])["memory"])
        self.assertIn('\n  "characters":', prompts.build_fact_extraction_prompt(**inputs)["prompt"])

    def test_recall_uses_older_arcs_and_repair_requires_core_repeat_issue(self):
        def archive(start, end, beats):
            return {"startChapter": start, "endChapter": end, "mainline": "轮流看守七星灯",
                    "usedBeats": [{"chapter": start, "text": text, "quote": "不回传的原文"} for text in beats]}

        archives = [archive(45, 50, [f"题签交接{i}" for i in range(8)]),
                    archive(39, 44, [f"查验铁牌{i}" for i in range(8)]),
                    archive(33, 38, ["别院移植百年海棠"]),
                    archive(30, 32, ["长绒棉保暖内衣/高脚椅"]),
                    archive(22, 29, ["夜袭水牢并打翻祭坛"])]
        body = "白砚换上长绒棉保暖内衣，坐上高脚椅。"
        expected = [{"chapter": 30, "text": "长绒棉保暖内衣/高脚椅"}]
        self.assertEqual(prompts.recall_used_beats(archives, body, exclude_near=((45, 50), (39, 44))), expected)
        for number in (51, 52):
            kwargs = dict(chapter_number=number, chapter_text=body, canonical_facts={"arcSummaries": archives},
                          rolling_arc_outlines=True, input_budget=DEFAULT_VERIFY_INPUT_BUDGET)
            verify = _review_json(prompts.build_verify_prompt(**kwargs))["repeatRecall"]
            self.assertEqual(verify["recalledBeats"], expected)
            self.assertEqual(len(verify["usedBeats"]), 12)
            self.assertTrue(all(beat["chapter"] >= 39 for beat in verify["usedBeats"]))
            for kind in ("placeholder", "core_scene_repetition"):
                sections = _review_json(prompts.build_repair_prompt(**kwargs, issues=[{"type": kind}]))
                if kind == "core_scene_repetition":
                    self.assertEqual(sections["repeatRecall"]["recalledBeats"], expected)
                else:
                    self.assertNotIn("repeatRecall", sections)


class LedgerTests(unittest.TestCase):
    def test_amounts_aliases_and_conversion_declarations(self):
        plan = {
            "canonLocks": {"currencyAndEra": "1斤=16两；一贯=一千文", "identityAliases": [
                {"character": "白砚", "scope": "盐行", "alias": "白掌柜"},
            ]},
            "worldview": {"coreRules": ["1两银=1000文"]},
            "styleBible": {"domainTaboos": ["1银圆=10角"]},
        }
        conversions = unit_conversions(plan)
        for text, expected in (
            ("12,345.50元", ("12345.50", "元")), ("十二万三千两", ("123000", "两")), ("两两", ("2", "两")),
            ("半两", ("0.5", "两")), ("1.5万元", ("15000", "元")), ("1.5亿元", ("150000000", "元")),
            ("1.5万", ("15000", "")), ("十二万三千", ("123000", "")), ("1.5亿", ("150000000", "")),
            ("1.5亿积分", ("150000000", "积分")), ("三块钱", ("3", "元")), ("2斤", ("32", "两")),
            ("一贯", ("1000", "文")), ("一两银", ("1000", "文")), ("两银圆", ("20", "角")),
            ("三人民币", ("3", "元")), ("2金铢", ("2", "金铢")),
        ):
            with self.subTest(text=text):
                self.assertEqual(parse_amount(text, conversions), (Decimal(expected[0]), expected[1]))
        self.assertIsNone(parse_amount("多少银圆"))
        self.assertEqual(parse_amount("2斤"), (Decimal(2), "斤"))
        self.assertEqual(ledger_label_aliases("盐铺账户"), ["盐铺账户", "盐铺"])
        self.assertIn("白掌柜", ledger_label_aliases("白砚账户", plan))

    def test_natural_prose_and_panel_ground_closing_balance_conflicts(self):
        for body in ("盐铺卖盐进账 200两，账上现余 600两。", "【盐铺账户：入账200两，余额600两】"):
            with self.subTest(body=body):
                delta = _delta(body)
                result = assess_ledger_delta(delta, body, _LEDGER)
                self.assertEqual([(row["type"], row["rule"]) for row in result["issues"]],
                                 [("hard_value_conflict", "balance_arithmetic")])
                self.assertEqual(result["issues"][0]["sourceChapter"], 6)
                self.assertEqual(result["issues"][0]["quote"], body)
                self.assertEqual(result["cleanedDelta"]["memory"]["ledgerBalances"], [])
                self.assertEqual(len(delta["memory"]["ledgerBalances"]), 1)
                self.assertEqual(result["cleanedDelta"]["items"], delta["items"])

    def test_continuity_uses_prior_account_and_actual_transactions(self):
        for body in ("账上只剩 0两。", "盐铺账户摊在案上。账上只剩 0两。", "【盐铺账户：余额0两】"):
            with self.subTest(body=body):
                result = assess_ledger_delta(None, body, _LEDGER)
                self.assertEqual(result["issues"][0]["rule"], "balance_continuity")
                self.assertEqual(result["issues"][0]["ledgerRef"], {"kind": "balance", "key": "盐铺账户"})
                self.assertIn(result["issues"][0]["quote"], body)
        for body in ("盐铺缴了罚款，账上只剩0两。", "盐铺期初余额为0两。", "盐铺余额大约0两。"):
            self.assertEqual(assess_ledger_delta({}, body, _LEDGER)["issues"], [])
        body = "账上现余32两。"
        view = {"ledgerBalances": [{"label": "盐铺账户", "value": "2斤", "updatedAtChapter": 6}]}
        plan = {"canonLocks": {"currencyAndEra": "1斤=16两"}}
        self.assertEqual(assess_ledger_delta(None, body, view, book_plan=plan)["issues"], [])
        self.assertEqual(assess_ledger_delta(None, body, view)["issues"], [])
        body = "账上现余33两。"
        self.assertEqual(assess_ledger_delta(None, body, view)["issues"], [])
        self.assertEqual(assess_ledger_delta(None, body, view, book_plan=plan)["issues"][0]["rule"], "balance_continuity")

    def test_clean_prose_keeps_warnings_and_discards_bad_extraction(self):
        cases = (
            ("盐铺花费200两，余额100两。", "100两", "+200两", "change_direction"),
            ("盐铺卖盐进账200两，现余500两。", "600两", "+200两", "balance_arithmetic"),
            ("盐铺本次进账200两，随后又给了伙计一笔，余额600两。", "600两", "+200两", "balance_arithmetic"),
            ("盐铺支付200两，账册交还掌柜。", "600两", "-200两", "balance_arithmetic"),
        )
        for body, value, change, rule in cases:
            with self.subTest(body=body):
                checked = assess_ledger_delta(_delta(body, value=value, change=change), body, _LEDGER)
                self.assertEqual(checked["issues"], [])
                self.assertEqual(checked["warnings"][0]["rule"], rule)
                self.assertEqual(checked["cleanedDelta"]["memory"]["ledgerBalances"], [])
        body = "盐铺卖盐进账200两，账上现余500两。"
        delta = _delta(body, value="500两")
        self.assertEqual(assess_ledger_delta(delta, body, _LEDGER)["cleanedDelta"], delta)

    def test_referenced_balances_stay_with_their_accounts(self):
        view = {"ledgerBalances": [*_LEDGER["ledgerBalances"], {"label": "布行账户", "value": "600两"}]}
        body = "盐铺进账200两，布行账户余额600两。"
        result = assess_ledger_delta(_delta(body), body, view)
        self.assertEqual(result["issues"], [])
        self.assertEqual(result["cleanedDelta"]["memory"]["ledgerBalances"], [])
        body = "盐铺进账200两，余额500两。"
        delta = _delta(body, value="500两")
        delta["memory"]["ledgerBalances"][0]["label"] = "盐铺"
        cleaned = assess_ledger_delta(delta, body, view)["cleanedDelta"]
        self.assertEqual(cleaned["memory"]["ledgerBalances"][0]["label"], "盐铺账户")

    def test_dialogue_and_additive_total_warnings(self):
        for marks in ("“”", "「」"):
            body = f"掌柜说：{marks[0]}盐铺卖盐进账200两，现余600两。{marks[1]}"
            result = assess_ledger_delta(_delta(body), body, _LEDGER)
            self.assertFalse(result["issues"])
            self.assertEqual(result["warnings"][0]["severity"], "warning")
            self.assertEqual(result["cleanedDelta"]["memory"]["ledgerBalances"], [])
        for body in ("货款三两、运费二两，共六两。", "货款三两、运费二两。合计六两。"):
            self.assertEqual(assess_ledger_delta({}, body, {})["warnings"][0]["rule"], "additive_total")
        for body in ("三匹，每匹二两，共六两。", "货款三两、运费二两，共约六两。", "“货款三两、运费二两，共六两。”"):
            self.assertEqual(assess_ledger_delta({}, body, {})["warnings"], [])

    def test_numeric_claims_are_compact_relevant_and_limited(self):
        body = "码头有500枚铜钱。掌柜吹嘘：“盐铺余额900两。”盐铺账户已摊开。"
        body += "【盐铺现余300两】。\n盐铺账上现余301两。"
        lines = numeric_claims(body, _LEDGER, limit=1)
        self.assertEqual(len(lines), 1)
        self.assertIn("章前前值 300两", lines[0])
        self.assertIn("【盐铺现余300两】", lines[0])
        self.assertNotIn("500枚", lines[0])
        self.assertEqual(numeric_claims("“盐铺余额900两。”", _LEDGER), [])


class LocalGateTests(unittest.TestCase):
    def test_real_meta_text_is_fatal_in_both_dialogue_and_narration(self):
        for number, body in ((80, "“把第78章大秦五原关樟木箱里……”"),
                             (200, "随着第199章因果闭环达成，他合上箱盖。"),
                             (80, "她问：“上一章的读者们会怎么看？”")):
            issues = find_meta_text(body, chapter_number=number)
            self.assertTrue(issues)
            self.assertTrue(all(row["type"] == "meta_text" and row["severity"] == "error" for row in issues))
            self.assertFalse(can_save_unverified_draft(body, {"passed": False, "issues": issues},
                                                       chapter_number=number, min_words=0))

    def test_meta_whitelist_is_local_and_exemptions_are_explicit(self):
        body = "# 第80章 潮落\n翻开《大明律》第三章。他们约法三章，在序章签下名字。\n“把第78章大秦五原关樟木箱里。”"
        issues = find_meta_text(body, chapter_number=80)
        self.assertEqual(len(issues), 1)
        self.assertIn("第78章", issues[0]["quote"])
        for body, plan in (("【第七十九章任务】", None), ("她说：“这一切都写在上一章。”", {"genre": "穿书"}),
                           ("传说第999章会解封。", None)):
            with self.subTest(body=body):
                self.assertEqual([row["severity"] for row in find_meta_text(body, chapter_number=80, book_plan=plan)],
                                 ["warning"])
        self.assertEqual(find_meta_text("法典第四章早已修订。", chapter_number=80), [])
        self.assertEqual(find_meta_text("《大明律》。他回到第78章。", chapter_number=80)[0]["severity"], "error")
        self.assertEqual(find_meta_text("第79章发生的事", chapter_number=80,
                                        book_plan={"logline": "故事并非穿书"})[0]["severity"], "error")
        self.assertEqual(find_meta_text("“从第一百九十九章起，便没有消息。”", chapter_number=200)[0]["severity"], "error")

    def test_title_mismatch_is_warning_with_body_evidence(self):
        issues = run_deterministic_checks(chapter_number=14, chapter_text="拍卖师敲槌，1吨起拍。",
                                          title="十吨起拍", min_words=0)["issues"]
        self.assertEqual([(issue["type"], issue["severity"]) for issue in issues], [("title_mismatch", "warning")])
        self.assertEqual(issues[0]["bodyValues"], ["1"])

    def test_reaction_families_dialogue_and_thresholds(self):
        body = ("瞳孔骤缩。倒吸一口凉气。唇角扬起。指节泛白。喉结上下滚动。冷汗把后背湿透。"
                "被人像拖死狗一样带走。双膝一软。脸色瞬间铁青。落针可闻。")
        self.assertEqual(reaction_counts(body), dict.fromkeys(
            ("pupil", "gasp", "mouth", "knuckle", "throat", "soaked", "drag", "collapse", "pallor", "silence"), 1,
        ))
        self.assertEqual(reaction_counts("“瞳孔骤缩。”「脸色惨白。」雨淋湿了后背。"), {})
        recent = {number: reaction_counts("瞳孔骤缩。") for number in (1, 3, 4, 6, 7, 8)}
        for gate, severity in ((True, "error"), (False, "warning")):
            issues = reaction_window_issues("瞳孔骤缩。", recent, chapter_number=9, ai_gate=gate)
            self.assertEqual([(row["rule"], row["reactionFamily"], row["severity"]) for row in issues],
                             [("formulaic_reaction_window", "pupil", severity)])
        self.assertEqual(reaction_window_issues("瞳孔骤缩。", recent, chapter_number=5, ai_gate=True), [])
        self.assertEqual(reaction_window_issues("“瞳孔骤缩。”", recent, chapter_number=9, ai_gate=True), [])
        self.assertEqual(reaction_window_issues("瞳孔骤缩。" * 4, {}, chapter_number=1, ai_gate=True)[0]["severity"], "error")
        self.assertEqual(reaction_window_issues("瞳孔骤缩。" * 3, {}, chapter_number=1, ai_gate=True)[0]["severity"], "warning")
        issue = reaction_window_issues("瞳孔骤缩。", {8: {"pupil": 3}, 10: {"pupil": 2}},
                                       chapter_number=15, ai_gate=True)[0]
        self.assertEqual(issue["severity"], "warning")
        self.assertIn("近十章", issue["message"])


class ReviewIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_repairs_reuse_context_and_reshuffle_current_entities(self):
        calls = []

        async def model(**request):
            calls.append((request["system"].splitlines()[0], request["prompt"]))
            if calls[-1][0] == "[TASK:REPAIR]":
                return {"text": ("杜掌柜" if len(calls) == 2 else "老船工") + "披上蓑衣。" + _BODY * 4}
            if len(calls) < 5:
                return _reply(passed=False, issues=[{"type": "timeline", "severity": "error", "message": "时刻矛盾"}])
            return _reply(factsDelta={})

        facts = {"characters": {"杜掌柜": {"role": "镇上识货人"}, "老船工": {"role": "曾经的引航员"}}}
        with (patch.object(verifier, "build_review_context", wraps=prompts.build_review_context) as context,
              patch.object(verifier, "call_llm", side_effect=model)):
            result = await verifier.verify_and_repair_chapter(
                chapter_number=9, chapter_text=_BODY, min_words=0, canonical_facts=facts, max_retries=2,
                verify_input_budget=DEFAULT_VERIFY_INPUT_BUDGET, repair_input_budget=DEFAULT_REPAIR_INPUT_BUDGET,
            )
        self.assertTrue(result["passed"])
        self.assertEqual(result["repairCount"], 2)
        context.assert_called_once()
        self.assertEqual([task for task, _ in calls], ["[TASK:VERIFY]", "[TASK:REPAIR]"] * 2 + ["[TASK:VERIFY]"])
        self.assertNotIn("镇上识货人", calls[0][1])
        self.assertIn("镇上识货人", calls[2][1])
        self.assertIn("曾经的引航员", calls[4][1])
        self.assertNotIn("镇上识货人", calls[4][1])

    async def test_reaction_never_masks_remote_review_and_can_degrade(self):
        history = {n: {"pupil": 1} for n in (5, 6, 8)}
        body = "白砚的瞳孔骤缩。" + _BODY
        with patch.object(verifier, "call_llm", new_callable=AsyncMock, return_value=_reply(factsDelta={"items": {}})) as model:
            for gate in (True, False):
                result = await verifier.verify_and_repair_chapter(
                    chapter_number=9, chapter_text=body, chapter_ai_gate=gate, reaction_history=history, min_words=0,
                    max_retries=0, degrade_on_verify_exhausted=True,
                )
                self.assertEqual(result["passed"], not gate)
                self.assertEqual(result["canCommitForReview"], gate)
                self.assertEqual(result["verification"]["issues"][0]["rule"], "formulaic_reaction_window")
                self.assertEqual(result["factsDelta"], None if gate else {"items": {}})
            self.assertEqual(model.await_count, 2)

    async def test_reaction_keeps_remote_hard_evidence_in_the_verdict(self):
        history = {n: {"pupil": 1} for n in (5, 6, 8)}
        body = "白砚的瞳孔骤缩。" + _BODY
        verdict = _reply(issues=[{"type": "world_rule_conflict", "severity": "warning",
                                 "quote": "白砚推开仓门", "message": "仓门在满潮时封闭"}])
        with patch.object(verifier, "call_llm", new_callable=AsyncMock, return_value=verdict) as model:
            result = await verifier.verify_and_repair_chapter(
                chapter_number=9, chapter_text=body, chapter_ai_gate=True, reaction_history=history, min_words=0,
                max_retries=0, degrade_on_verify_exhausted=True,
            )
        model.assert_awaited_once()
        self.assertFalse(result["canCommitForReview"])
        self.assertEqual([issue["type"] for issue in result["verification"]["issues"]], ["ai_flavor", "world_rule_conflict"])

    async def test_delta_cleaned_during_verify_and_fallback(self):
        body = "盐铺花费200两，余额100两。"
        extracted = _delta(body, value="100两", change="+200两")
        for fallback in (False, True):
            with self.subTest(fallback=fallback):
                callback = AsyncMock(return_value=extracted)
                reply = _reply() if fallback else _reply(factsDelta=extracted)
                with patch.object(verifier, "call_llm", new_callable=AsyncMock, return_value=reply):
                    result = await verifier.verify_and_repair_chapter(
                        chapter_number=10, chapter_text=body, canonical_facts=_LEDGER, story_ledger=True, min_words=0,
                        extract_missing_facts=callback, max_retries=0,
                    )
                self.assertEqual(callback.await_count, int(fallback))
                self.assertTrue(result["passed"])
                self.assertEqual(result["factsDelta"]["memory"]["ledgerBalances"], [])
                self.assertEqual(result["verification"]["issues"][0]["severity"], "warning")

    async def test_fallback_hard_conflict_clears_accepted_boundary_change(self):
        body = "门卫当面放行。盐铺卖盐进账200两，账上现余600两。"
        plan = {"planVersion": 2, "canonLocks": {"boundaries": [
            {"subject": "仓门", "rule": "禁止入内", "changeWhen": "门卫当面放行"},
        ]}}
        change = {"planRef": "canonLocks.boundaries[0]", "result": "仓门可通行", "quote": "门卫当面放行"}
        with patch.object(verifier, "call_llm", new_callable=AsyncMock, return_value=_reply(boundaryChanges=[change])):
            result = await verifier.verify_and_repair_chapter(
                chapter_number=10, chapter_text=body, canonical_facts=_LEDGER, story_ledger=True, min_words=0,
                book_plan=plan, extract_missing_facts=AsyncMock(return_value=_delta(body)), max_retries=0,
            )
        self.assertFalse(result["passed"])
        self.assertIsNone(result["factsDelta"])
        self.assertNotIn("boundaryChanges", result["verification"])
        self.assertEqual(result["verification"]["issues"][0]["type"], "hard_value_conflict")

    async def test_verify_expands_without_sending_metadata_to_model(self):
        with patch.object(verifier, "call_llm", new_callable=AsyncMock, return_value=_reply()) as model:
            result = await verifier.verify_chapter(chapter_number=8, chapter_text=_BODY * 90, min_words=0,
                                                    input_budget=1500)
        self.assertTrue(result["passed"])
        self.assertTrue(result["budgetMeta"]["expanded"])
        self.assertNotIn("budgetMeta", model.await_args.kwargs)
