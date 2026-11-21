import tempfile
import unittest

from novel_forge.long_memory import normalize_memory_delta, story_ledger_events, story_ledger_view
from novel_forge.storage import Store


class StoryMemoryTests(unittest.TestCase):
    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.store = Store(self.directory)
        self.job = self.store.create_job({"totalChapters": 20})

    def save(self, number, body, memory, *, enabled=True, status="verified"):
        return self.store.save_verified_chapter(
            self.job["id"], number, {"content": body, "status": status},
            {"memory": memory}, long_memory=False, story_ledger=enabled,
        )["canonicalFacts"]

    def test_clock_verified_incremental_and_as_of_rollback(self):
        initial = "第三天，初冬，天刚发亮，距离开门还剩两夜。"
        facts = self.save(1, initial, {"storyClock": {
            "day": "第三天", "season": "初冬", "timeOfDay": "黎明", "countdown": "还剩两夜",
            "quote": "第三天,初冬……还剩两夜。",
        }, "constraints": [{"text": "旧记忆开关独立", "quote": initial}]})
        self.assertEqual(facts["constraints"], [])
        evening = "天边见晚，林渊决定在门外等候。"
        facts = self.save(2, evening, {"storyClock": {"timeOfDay": "黄昏", "quote": evening}})
        self.assertEqual(facts["storyClock"], {"day": "第三天", "season": "初冬", "timeOfDay": "黄昏",
                                               "countdown": "还剩两夜", "fieldChapters": {
                                                   "day": 1, "season": 1, "timeOfDay": 2, "countdown": 1,
                                               }})
        self.assertEqual(story_ledger_view(facts, chapter_number=2)["storyClock"]["timeOfDay"], "黎明")
        self.assertEqual(story_ledger_events(facts, 2, 2), [{"chapter": 2, "storyClock": {"timeOfDay": "黄昏"}}])
        facts = self.save(2, evening, {"storyClock": {"timeOfDay": "正午", "quote": "完全不存在的原文"}})
        self.assertEqual(facts["storyClock"]["timeOfDay"], "黎明")
        facts = self.save(2, evening, {"storyClock": {"timeOfDay": "黄昏", "quote": evening}}, status="needs_review")
        self.assertEqual(facts["storyClock"]["timeOfDay"], "黎明")
        self.assertEqual(self.store.delete_uncommitted_facts(self.job["id"], 2)["storyClock"]["season"], "初冬")
        self.assertNotIn("storyClock", self.store.delete_uncommitted_facts(self.job["id"], 1))

    def test_balance_cap_normalized_units_and_recent_changes_replay(self):
        first = "现金100元，另有账户甲1元、乙2元、丙3元、丁4元、戊5元。"
        rows = [{"label": label, "value": amount, "quote": first} for label, amount in (
            ("ＶＩＰ 可用 现金", "100元"), ("甲", "1元"), ("乙", "2元"), ("丙", "3元"), ("丁", "4元"), ("戊", "5元"),
        )]
        facts = self.save(1, first, {"ledgerBalances": rows})
        self.assertEqual(len(facts["ledgerBalances"]), 6)
        self.assertEqual(facts["ledgerBalances"][0]["recentChanges"], [])
        for number, value in ((2, 110), (3, 120), (4, 130), (5, 140)):
            body = f"现金新增10元，当前余额{value}元，冻结款还有9元。"
            facts = self.save(number, body, {"ledgerBalances": [
                {"label": "vip可用现金", "value": f"{value}元", "change": "+10元", "quote": body},
                {"label": "第七个账户冻结款", "value": "9元", "quote": body},
            ]})
        balance = facts["ledgerBalances"][0]
        self.assertEqual(balance["label"], "ＶＩＰ 可用 现金")
        self.assertEqual(balance["value"], "140元")
        self.assertEqual([item["chapter"] for item in balance["recentChanges"]], [3, 4, 5])
        self.assertEqual(len(facts["ledgerBalances"]), 6)
        repeat = "现金仍为140.00元，余额没有变化。"
        facts = self.save(6, repeat, {"ledgerBalances": [{"label": "VIP 可用现金", "value": "140.00元", "quote": repeat}]})
        self.assertEqual(facts["ledgerBalances"][0], balance)
        self.assertEqual(story_ledger_events(facts, 6, 6), [])
        revised = self.store.delete_uncommitted_facts(self.job["id"], 5)["ledgerBalances"][0]
        self.assertEqual(revised["value"], "130元")
        self.assertEqual([item["chapter"] for item in revised["recentChanges"]], [2, 3, 4])

    def test_character_lock_scope_replay_and_verified_recurrence(self):
        plan = {"mainCharacters": [{"name": "林渊", "role": "主角"}, {"name": "苏灵", "role": "盟友"}]}
        self.store.update_job(self.job["id"], {"bookPlan": plan})
        extras = [f"配角{index:02d}" for index in range(13)]
        first = "苏灵亲眼确认铜铃能打开密道。一次性守卫交出了钥匙。"
        body = first + "。".join(f"{name}来队伍报名" for name in extras)
        states = {"苏灵": {"stanceTier": "wary", "knownFacts": ["铜铃能打开密道"], "quote": first},
                  "一次性守卫": {"seen": True, "stanceTier": "hostile", "quote": "一次性守卫交出了钥匙"}}
        for name in extras:
            states[name] = {"seen": True, "stanceTier": "hostile", "quote": f"{name}来队伍报名"}
        facts = self.save(1, body, {"characterStates": states})
        self.assertEqual(set(facts["characterStates"]), {"苏灵"})

        second = "苏灵加入林渊的队伍，亲手确认银锁并未损坏。林渊答应护送她。"
        body = second + "。".join(f"{name}领取出行证件" for name in extras)
        states = {name: {"seen": True, "stanceTier": "wary", "quote": f"{name}领取出行证件"} for name in extras}
        states["林渊"] = {"seen": True, "stanceTier": "ally", "quote": "林渊答应护送她"}
        states["苏灵"] = {"seen": True, "stanceTier": "ally", "knownFacts": ["铜铃能 打开密道。", "银锁并未损坏"],
                           "relationships": {"林渊": "结伴进山"}, "quote": second}
        facts = self.save(2, body, {"characterStates": states})
        self.assertEqual(len(facts["characterStates"]), 12)
        self.assertIn("林渊", facts["characterStates"])
        self.assertNotIn("一次性守卫", facts["characterStates"])
        self.assertEqual(facts["characterStates"]["苏灵"]["knownFacts"], ["铜铃能打开密道", "银锁并未损坏"])
        projected = story_ledger_view(facts, chapter_number=3, book_plan=plan, context_text="苏灵开始登山")
        self.assertEqual(set(projected["characterStates"]), {"苏灵"})
        state = projected["characterStates"]["苏灵"]
        self.assertEqual(state["stanceTier"], "ally")
        self.assertEqual(state["stanceTierChapter"], 2)
        self.assertEqual(state["knownFactChapters"], {"铜铃能打开密道": 1, "银锁并未损坏": 2})
        self.assertEqual(state["relationships"]["林渊"]["updatedAtChapter"], 2)
        self.assertEqual(story_ledger_view(facts, chapter_number=2, book_plan=plan)["characterStates"]["苏灵"]["stanceTier"], "wary")
        old = self.store.delete_uncommitted_facts(self.job["id"], 2)
        self.assertEqual(set(old["characterStates"]), {"苏灵"})
        self.assertEqual(old["characterStates"]["苏灵"]["knownFacts"], ["铜铃能打开密道"])

    def test_legacy_shape_and_story_gate_off_keep_contract(self):
        body = "林渊确认了港口地址和钥匙的颜色。"
        ordinary = {"characterStates": {"林渊": {"seen": True, "currentGoal": "进港口", "quote": body}}}
        richer = {**ordinary, "storyClock": {"day": "第二日", "quote": body}, "ledgerBalances": [
            {"label": "积分", "value": 5, "quote": body},
        ]}
        self.assertEqual(normalize_memory_delta(ordinary, 1, body), normalize_memory_delta(richer, 1, body))
        facts = self.save(1, body, richer, enabled=False)
        self.assertNotIn("storyClock", facts)
        self.assertNotIn("ledgerBalances", facts)
        self.assertEqual(story_ledger_view(facts, chapter_number=2), {})
