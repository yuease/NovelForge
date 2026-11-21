import json
import tempfile
import unittest
from pathlib import Path

from novel_forge.llm import STORY_LEDGER_MARKER, call_mock_llm
from novel_forge.long_memory import normalize_arc_summary
from novel_forge.storage import Store, volume_source_hash
from novel_forge.text_evidence import locate_quote, normalize_quote


FIRST_CHAPTER = "姚青看着码头备用金ＡＢ，确认凭证之后才款到账。门房随即关门。"
SECOND_CHAPTER = "账房却把尚未到账的款项计入可用余额。"
CHAPTERS = [
    {"chapterNumber": 1, "content": FIRST_CHAPTER},
    {"chapterNumber": 2, "content": SECOND_CHAPTER},
]


def drift(chapter, quote, *, kind="value", issue="余额口径发生变化", fix_hint="按凭证核对可用余额"):
    return {"chapter": chapter, "kind": kind, "issue": issue, "fixHint": fix_hint, "quote": quote}


class ArcDriftNormalizationTests(unittest.TestCase):
    def test_invalid_drift_is_fail_soft_and_valid_entries_stay_bounded(self):
        exact = drift(2, SECOND_CHAPTER)
        flexible = drift(1, "姚青看着码头备用金 AB ，确认凭证……门房随即关门。", kind="canon")
        gross = dict(exact, quote="原文中没有明说这一笔钱")
        raw = {
            "mainline": "姚青进入码头核对账本。",
            "usedBeats": [{"chapter": 1, "text": "调取凭证", "quote": FIRST_CHAPTER}],
            "driftChecks": [
                None, gross, dict(exact, chapter=3), dict(exact, kind="bad"),
                dict(exact, kind=[]), dict(exact, issue="错" * 61), dict(exact, fixHint="错" * 81),
                dict(exact, quote=""), dict(exact, quote="长" * 121), flexible, exact, dict(exact),
                drift(2, SECOND_CHAPTER, kind="clock", issue="两段陈述时点不同"),
                drift(2, SECOND_CHAPTER, kind="character_ooc", issue="账房对票据的判断前后不一"),
                drift(1, FIRST_CHAPTER, issue="超过容量的发现"),
            ],
        }
        result = normalize_arc_summary(raw, CHAPTERS, story_ledger=True)
        self.assertEqual(result["mainline"], raw["mainline"])
        self.assertEqual(result["usedBeats"], raw["usedBeats"])
        self.assertEqual([row["kind"] for row in result["driftChecks"]], ["canon", "value", "clock", "character_ooc"])
        self.assertEqual(result["driftChecks"][:2], [flexible, exact])
        self.assertNotIn("driftChecks", normalize_arc_summary(raw, CHAPTERS))
        self.assertEqual(normalize_arc_summary({**raw, "driftChecks": [gross]}, CHAPTERS, story_ledger=True)["driftChecks"], [])
        self.assertIsNone(normalize_arc_summary({"driftChecks": [exact]}, CHAPTERS, story_ledger=True))

    def test_persisted_archive_respects_switch_and_current_source_hash(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        store = Store(root)
        job_id = store.create_job({"totalChapters": 2})["id"]
        arc = {"arcIndex": 1, "startChapter": 1, "endChapter": 2, "arcTitle": "核对凭证", "arcGoal": "统一账目"}
        store.update_job(job_id, {"arcPlans": [arc]})
        for chapter in CHAPTERS:
            store.save_chapter(job_id, chapter["chapterNumber"], {"content": chapter["content"], "status": "verified"})

        before = volume_source_hash(store.get_chapters(job_id))
        summary = {"mainline": "核对台账", "driftChecks": [drift(1, FIRST_CHAPTER), drift(2, "伪造原句")]}
        self.assertTrue(store.save_arc_summary(job_id, arc, before, summary))
        self.assertTrue(store.save_arc_summary(job_id, arc, before, summary, story_ledger=True))
        old_arc, = store.get_canonical_facts(job_id)["arcSummaries"]
        self.assertNotIn("driftChecks", old_arc)

        store.save_chapter(job_id, 2, {"content": "改写后的真正正文。", "status": "verified"})
        self.assertFalse(store.save_arc_summary(job_id, arc, before, summary, story_ledger=True))
        self.assertEqual(store.get_canonical_facts(job_id)["arcSummaries"], [])
        after = volume_source_hash(store.get_chapters(job_id))
        self.assertTrue(store.save_arc_summary(job_id, arc, after, summary, story_ledger=True))
        new_arc, = store.get_canonical_facts(job_id)["arcSummaries"]
        self.assertEqual(new_arc["sourceHash"], after)
        self.assertEqual(new_arc["driftChecks"], summary["driftChecks"][:1])


class ArcDriftMockTests(unittest.IsolatedAsyncioTestCase):
    async def test_mock_phase4_fields_use_grounded_text_in_both_extraction_paths(self):
        marker = STORY_LEDGER_MARKER
        scenario = {"mode": "story-ledger-drift"}
        chapter = await call_mock_llm(
            system=f"[TASK:CHAPTER_WRITE]\n{marker}", prompt="撰写第2章正文\n目标字数：1400", test_scenario=scenario,
        )
        body = chapter["text"]
        review_prompt = f"第2章\n【待审校正文】\n{body}\n输出 JSON"
        reviewed = json.loads((await call_mock_llm(system=f"[TASK:VERIFY]\n{marker}", prompt=review_prompt))["text"])
        extracted = json.loads((await call_mock_llm(
            system=f"[TASK:FACT_EXTRACTION]\n{marker}", prompt=f"第2章\n【第2章正文】\n{body}\n请按以下 JSON 格式输出",
        ))["text"])
        self.assertTrue(reviewed["passed"])
        self.assertEqual(extracted, reviewed["factsDelta"])
        memory = extracted["memory"]
        clock = memory["storyClock"]
        self.assertEqual((clock["day"], clock["timeOfDay"], clock["season"], clock["countdown"]), ("第2天", "清晨", "初冬", "28天"))
        account, = memory["ledgerBalances"]
        self.assertEqual((account["value"], account["change"]), ("110元", "+10元"))
        actor = memory["characterStates"]["苏清雪"]
        self.assertEqual(actor["stanceTier"], "ally")
        self.assertEqual(actor["knownFacts"], ["亲眼看见钟楼暗门"])
        self.assertTrue(all(locate_quote(row["quote"], normalize_quote(body)) for row in (clock, account, actor)))
        old = json.loads((await call_mock_llm(system="[TASK:VERIFY]", prompt=review_prompt))["text"])
        self.assertNotIn("memory", old["factsDelta"])

        chapters = [{"chapterNumber": 2, "title": "核对备用金", "content": body}]
        prompt = f"【正文依据】\n{json.dumps(chapters, ensure_ascii=False)}\n【输出结构】\n{{}}"
        summary = json.loads((await call_mock_llm(system=f"[TASK:ARC_SUMMARY]\n{marker}", prompt=prompt))["text"])
        normalized = normalize_arc_summary(summary, chapters, story_ledger=True)
        one, = normalized["driftChecks"]
        self.assertEqual(one["kind"], "value")
        self.assertEqual(one["quote"], SECOND_CHAPTER)
        old_summary = json.loads((await call_mock_llm(system="[TASK:ARC_SUMMARY]", prompt=prompt))["text"])
        self.assertNotIn("driftChecks", old_summary)
