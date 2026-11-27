import hashlib
import json
import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from novel_forge.app import create_app
from novel_forge.drift_repair import apply_text_patches, classify_drifts, downstream_light_check
from novel_forge.llm import LLMError, _MOCK_UNPOSTED_DRIFT, call_mock_llm
from novel_forge.long_memory import drift_id, drift_ledger, drift_stats, merge_drift_entries, unresolved_drifts
from novel_forge.pipeline import PipelineRuntime
from novel_forge.prompts import build_arc_outlines_prompt, build_drift_patch_prompt
from novel_forge.storage import Store, count_chinese_words, manuscript_fingerprint, rebuild_canonical_facts, volume_source_hash


WRONG = "@贞观老李"
RIGHT = "@天策上将"
HOOK = "门外第三次响起敲击声，信封背面的印记终于显露。"
FILLER = "姚青沿着石阶下到码头，提灯绕过仓门，把封蜡按在收据上。" * 42
PLAN = {"planVersion": 2, "logline": "姚青调查码头账房", "canonLocks": {"identityAliases": [
    {"character": "李世民", "scope": "直播间", "alias": RIGHT},
]}}
CONFIG = {"provider": "mock", "model": "mock-novel-v1"}
ARC = {"arcIndex": 3, "startChapter": 9, "endChapter": 10, "arcTitle": "码头交割"}


def prose(line):
    return "封面上的印章还没有干。\n\n" + line + "\n\n" + FILLER + HOOK


def finding(number, quote, *, kind="canon", issue="李世民直播间网名与锁定口径不符", hint=f"统一为{RIGHT}"):
    return {"chapter": number, "kind": kind, "issue": issue, "fixHint": hint, "quote": quote}


def patch_for(drift, new, **overrides):
    return {"driftId": drift_id(drift), "confirmed": True, "find": drift["quote"], "replace": new,
            "reason": "依据口径锁纠偏", **overrides}


def reply(**value):
    return {"text": json.dumps(value, ensure_ascii=False)}


def account(value, quote, change=None):
    return {"memory": {"ledgerBalances": [{"label": "码头备用金", "value": value, "quote": quote,
                                           **({"change": change} if change is not None else {})}]}}


class DriftLogicTests(unittest.TestCase):
    def test_stable_ids_deduplication_priority_and_stats(self):
        original = finding(9, WRONG, issue="ＡＢ ， 旧名")
        identical = {**original, "issue": "AB,旧名"}
        expected = "drift-" + hashlib.sha1("9|canon|AB,旧名".encode()).hexdigest()[:12]
        self.assertEqual(drift_id(original), drift_id(identical))
        self.assertEqual(drift_id(original), expected)
        drifts = [original, identical, finding(6, "次日抵达城门", kind="clock"),
                  finding(2, "码头备用金", kind="value"), finding(11, "姚青摇了摇头", kind="character_ooc")]
        ledger = merge_drift_entries([], arc_index=1, drifts=drifts, source_hash="a", timestamp="then")
        self.assertEqual(len(ledger), 4)
        ledger[0].update(status="deferred", autoRepairAttempts=1)
        refreshed = merge_drift_entries(ledger, arc_index=1, drifts=[identical], source_hash="b", timestamp="now")
        self.assertEqual(refreshed[0]["autoRepairAttempts"], 1)
        self.assertEqual(refreshed[0]["detectedAt"], "then")
        self.assertEqual(refreshed[0]["detectedSourceHash"], "b")
        self.assertEqual(ledger[0]["detectedSourceHash"], "a")
        self.assertEqual([row["chapter"] for row in unresolved_drifts({"driftLedger": refreshed}, before_chapter=11)], [9, 2, 6])
        refreshed[0]["status"] = "repaired"
        stable = merge_drift_entries(refreshed, arc_index=2, drifts=[identical], source_hash="c", timestamp="later")
        self.assertEqual(stable[0], refreshed[0])
        self.assertEqual(drift_stats({"driftLedger": stable}), {"total": 4, "open": 3, "repaired": 1, "deferred": 0,
                         "dismissed": 0, "byKind": {"canon": 1, "clock": 1, "value": 1, "character_ooc": 1}})

    def test_classifier_enforces_gates_per_chapter_quota_and_book_cap(self):
        text = prose(WRONG)
        chapters = [{"chapterNumber": n, "content": text, "status": "verified"} for n in range(6, 11)]
        chapters[0]["content"] = text + WRONG
        chapters[1]["status"] = "needs_review"
        drifts = [finding(6, WRONG), finding(7, WRONG), finding(8, WRONG, kind="character_ooc"),
                  finding(9, WRONG, kind="clock"), *(finding(10, WRONG, issue=f"不同问题{i}") for i in range(4))]
        summary = {"endChapter": 10, "driftChecks": drifts}
        ledger = [{"id": str(i), "chapter": n, "autoRepairAttempts": 1} for i, n in enumerate((1, 1, 2, 3))]
        queue, deferred = classify_drifts(summary, chapters, facts={}, ledger=ledger, total_chapters=25)
        self.assertEqual([(row["chapter"], len(row["drifts"])) for row in queue], [(10, 3)])
        self.assertEqual([row["reason"] for row in deferred], ["quote_not_unique", "needs_review", "ooc_deferred", "quota", "quota"])
        queue, deferred = classify_drifts(summary, chapters, facts={}, ledger=[*ledger, {"id": "new", "chapter": 10,
                                         "autoRepairAttempts": 1}], total_chapters=25)
        self.assertEqual(queue, [])
        self.assertEqual([row["reason"] for row in deferred].count("attempt_cap"), 4)
        _, stale = classify_drifts(summary, chapters, facts={}, latest_committed=11)
        self.assertEqual({row["reason"] for row in stale}, {"stale_arc"})

    def test_patcher_checks_each_local_guardrail(self):
        drift = finding(9, WRONG)
        drifts = {drift_id(drift): drift}
        text = prose(WRONG)
        cases = [
            (text, patch_for(drift, RIGHT, confirmed=1), "unconfirmed"),
            (text + WRONG, patch_for(drift, RIGHT), "quote_not_unique"),
            (text, patch_for(drift, RIGHT, find="封面上的印章"), "quote_not_unique"),
            (text, patch_for(drift, RIGHT, find=text[:201]), "invalid_patch"),
            (text, patch_for(drift, "字" * 48), "patch_length"),
            (text, patch_for(drift, "字" * 45), "word_delta"),
            (text, patch_for(drift, "正如第八章所说"), "patch_regressed"),
            (text, patch_for(drift, "【待补充】"), "patch_regressed"),
            (text, patch_for(drift, "正确\n名字"), "paragraph_changed"),
        ]
        tail_case = "甲" * 500 + WRONG + "乙" * 379
        # Put the quote immediately before the tail and make the proposed find cross it.
        tail_case += "丙"
        cases.append((tail_case, patch_for(drift, RIGHT + "丁", find=WRONG + "乙"), "tail_changed"))
        for body, proposed, reason in cases:
            with self.subTest(reason=reason):
                after, applied, rejected = apply_text_patches(body, [proposed], drifts, chapter_number=9)
                self.assertEqual(after, body)
                self.assertEqual(applied, [])
                self.assertEqual(rejected[0]["reason"], reason)
        near_tail = text + "尾声：楼上的新网名是" + WRONG
        hook_only = {**drift, "quote": "楼上的新网名是" + WRONG}
        body, applied, _ = apply_text_patches(near_tail, [patch_for(hook_only, "楼上的新网名是" + RIGHT)],
                                             {drift_id(hook_only): hook_only}, chapter_number=9)
        self.assertEqual(len(applied), 1)
        self.assertTrue(body.endswith(RIGHT))

    def test_normalized_matches_and_cumulative_span(self):
        drift = finding(9, "开门 AB，入内")
        text = prose("开门　ＡＢ，入内")
        body, applied, rejected = apply_text_patches(text, [patch_for(drift, "开门ＣＤ，入内")],
                                                   {drift_id(drift): drift}, chapter_number=9)
        self.assertIn("开门ＣＤ，入内", body)
        self.assertEqual((len(applied), rejected), (1, []))
        body, applied, rejected = apply_text_patches(text, [patch_for(drift, "开门 AB，入内")],
                                                   {drift_id(drift): drift}, chapter_number=9)
        self.assertIn("开门 AB，入内", body)
        self.assertEqual((len(applied), rejected), (1, []))
        first, second = "第一处" + "甲" * 68, "第二处" + "乙" * 68
        drifts = [finding(9, quote, issue=quote[:3]) for quote in (first, second)]
        text = prose(first + "。" + second)
        _, applied, rejected = apply_text_patches(text, [patch_for(row, row["quote"][:-1] + "终") for row in drifts],
                                                  {drift_id(row): row for row in drifts}, chapter_number=9)
        self.assertEqual(len(applied), 1)
        self.assertEqual(rejected[0]["reason"], "edit_span")

    def test_clock_cascade_uses_matching_fields_and_alias_scan_checks_locked_scope(self):
        quote = "次日姚青到了仓门。"
        drift = finding(9, quote, kind="clock", issue="日期漂移", hint="统一日期")
        chapters = [{"chapterNumber": 9, "content": prose(quote)},
                    {"chapterNumber": 10, "content": "直播间里，李世民的网名是@贞观老李。"}]
        facts = {"factHistory": [{"chapter": 10, "delta": {"memory": {"storyClock": {"day": "第三天"}}}}]}
        args = ({"endChapter": 10, "driftChecks": [drift]}, chapters)
        self.assertEqual(classify_drifts(*args, facts=facts)[1][0]["reason"], "cascade_risk")
        facts["factHistory"][0]["delta"]["memory"]["storyClock"] = {"season": "初冬"}
        self.assertEqual(len(classify_drifts(*args, facts=facts)[0]), 1)
        issue, = downstream_light_check(chapters[1:], facts, PLAN)
        self.assertEqual((issue["type"], issue["alias"]), ("canon_conflict", WRONG))
        self.assertEqual(downstream_light_check([{**chapters[1], "content": "毫无相关的另一个地方。"}], facts, PLAN), [])

    def test_rebuild_preserves_unjournaled_facts_when_history_has_a_gap(self):
        legacy = {"name": "旧名册", "sourceChapter": 1}
        facts = {"items": {"旧名册": legacy}, "arcSummaries": [{"arc": 1}], "volumeSummaries": [{"volume": 1}]}
        history = [{"chapter": 9, "delta": {"items": {"新名册": {"location": "账房"}}}}]
        rebuilt = rebuild_canonical_facts(facts, history)
        self.assertEqual(rebuilt["items"]["旧名册"], legacy)
        self.assertEqual(rebuilt["items"]["新名册"]["sourceChapter"], 9)
        self.assertEqual(facts["items"], {"旧名册": legacy})
        self.assertEqual(rebuilt["arcSummaries"], facts["arcSummaries"])
        self.assertEqual(rebuilt["volumeSummaries"], facts["volumeSummaries"])


class DriftLoopTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {"NOVEL_FORGE_DRIFT_REPAIR": "1", "NOVEL_FORGE_LONG_MEMORY": "1"}))
        self.store = Store(Path(self.enterContext(tempfile.TemporaryDirectory())))
        self.runtime = PipelineRuntime(self.store)
        self.job_id = self.store.create_job({"totalChapters": 25, "llmConfig": CONFIG})["id"]
        self.store.update_job(self.job_id, {"arcPlans": [ARC], "bookPlan": PLAN})
        self.addAsyncCleanup(self.runtime.close)

    def chapter(self, number, line, delta=None):
        return self.store.save_verified_chapter(self.job_id, number, {"content": prose(line), "title": "交割凭证",
                                                "status": "verified"}, delta, story_ledger=True)["chapter"]

    def continuous_prefix(self, through=8):
        for number in range(1, through + 1):
            self.chapter(number, f"姚青收到编号为{number}的回单。", {})

    def summary(self, *drifts):
        return {"mainline": "姚青确认交割凭证", "driftChecks": list(drifts),
                "usedBeats": [{"chapter": 9, "text": "出现错误网名", "quote": WRONG}],
                "usedHooks": [{"chapter": 9, "text": "敲击声引出印记", "quote": HOOK}]}

    def archive(self, *drifts, arc=ARC):
        source = self.source(arc)
        self.assertTrue(self.store.save_arc_summary(self.job_id, arc, source, self.summary(*drifts), story_ledger=True))
        return source

    def source(self, arc=ARC):
        return volume_source_hash(self.store.get_chapters(self.job_id, start_chapter=arc["startChapter"], end_chapter=arc["endChapter"]))

    def facts(self):
        return self.store.get_canonical_facts(self.job_id)

    def drift(self):
        entry, = drift_ledger(self.facts())
        return entry

    def state(self):
        return self.facts()["arcSummaries"][-1]["driftRepair"]

    async def repair(self):
        return await self.runtime._repair_arc_drifts(self.job_id, ARC, CONFIG, story_ledger=True, book_plan=PLAN)

    async def refresh(self):
        return await self.runtime._refresh_arc_archives(self.job_id, [ARC], 11, CONFIG, story_ledger=True, book_plan=PLAN)

    @contextmanager
    def llm_calls(self, llm):
        async def patch_request(job_id, config, prompt):
            return await llm(job_id, "drift_patch", config, prompt)

        with patch.object(self.runtime, "_llm", llm), patch.object(self.runtime, "_request_drift_patch", patch_request):
            yield llm

    async def test_chapter_nine_alias_repair_refreshes_arc_and_overlapping_volume(self):
        self.continuous_prefix()
        before = self.chapter(9, f"直播间的公告写得明白，李世民的网名是{WRONG}。")
        after_chapter = self.chapter(10, "姚青收好了那封信。")
        self.assertTrue(self.store.save_volume_summary(self.job_id, 10, self.source({"startChapter": 1, "endChapter": 10}),
                                                       {"mainline": "旧卷摘要"}))
        old_volume = self.facts()["volumeSummaries"][0]["sourceHash"]
        source = self.source()
        drift = finding(9, WRONG)
        committed = {9: before, 10: after_chapter}
        llm = AsyncMock(side_effect=[reply(**self.summary(drift)), reply(patches=[patch_for(drift, RIGHT)])])
        with self.llm_calls(llm):
            changed, = await self.runtime._archive_arc(self.job_id, ARC, CONFIG, story_ledger=True, book_plan=PLAN,
                                                       committed=committed)
            self.assertEqual([call.args[1] for call in llm.await_args_list], ["arc_archive", "drift_patch"])
            self.assertEqual(await self.refresh(), [])
            self.assertEqual(llm.await_count, 2)
        body = changed["content"]
        self.assertEqual(body, before["content"].replace(WRONG, RIGHT))
        self.assertEqual(body[-380:].encode(), before["content"][-380:].encode())
        self.assertLessEqual(abs(changed["wordCount"] - before["wordCount"]), before["wordCount"] * 0.03)
        self.assertEqual(committed[9], changed)
        archive, = self.facts()["arcSummaries"]
        self.assertEqual((archive["sourceHash"], archive["usedBeats"], archive["driftChecks"]), (self.source(), [], []))
        self.assertNotEqual(archive["sourceHash"], source)
        self.assertEqual(archive["usedHooks"][0]["quote"], HOOK)
        self.assertEqual((self.state()["state"], self.state()["round"], self.state()["chapters"]), ("done", 1, [9]))
        self.assertEqual((self.drift()["status"], self.drift()["autoRepairAttempts"]), ("repaired", 1))
        self.assertNotEqual(self.facts()["volumeSummaries"][0]["sourceHash"], old_volume)
        self.assertEqual(self.facts()["volumeSummaries"][0]["sourceHash"], self.source({"startChapter": 1, "endChapter": 10}))
        job = self.store.get_job_record(self.job_id)
        self.assertEqual(job["totalWords"], sum(row["wordCount"] for row in self.store.get_chapters(self.job_id)))
        self.assertEqual(job["chapterIndex"]["9"]["wordCount"], count_chinese_words(body))
        self.assertNotIn("driftRepairDraft", job["checkpoint"])

    async def test_meta_regression_is_terminal_and_manual_rearchive_is_stale(self):
        before = self.chapter(9, WRONG)
        self.chapter(10, "姚青把回单藏进衣袖。")
        drift = finding(9, WRONG)
        self.archive(drift)
        llm = AsyncMock(return_value=reply(patches=[patch_for(drift, "正如第八章所说")]))
        with self.llm_calls(llm):
            self.assertEqual(await self.repair(), [])
            self.assertEqual(await self.refresh(), [])
        llm.assert_awaited_once()
        self.assertEqual(self.store.get_chapter(self.job_id, 9), before)
        self.assertEqual((self.drift()["status"], self.drift()["reason"], self.state()["state"]),
                         ("deferred", "patch_regressed", "done"))
        edited = self.store.save_chapter(self.job_id, 9, {**before, "content": before["content"] + "门外有人应声。"})
        llm = AsyncMock(return_value=reply(**self.summary(drift)))
        with self.llm_calls(llm):
            self.assertEqual(await self.refresh(), [])
        self.assertEqual([call.args[1] for call in llm.await_args_list], ["arc_archive"])
        self.assertEqual(self.store.get_chapter(self.job_id, 9), edited)
        self.assertEqual((self.drift()["reason"], self.drift()["autoRepairAttempts"]), ("stale_arc", 1))

    async def test_same_account_cascade_defers_without_any_call(self):
        nine = "码头备用金支出20元，余额为80元。"
        ten = "码头备用金进账20元，余额为100元。"
        self.chapter(9, nine, account("80元", nine, "-20元"))
        self.chapter(10, ten, account("100元", ten, "+20元"))
        self.archive(finding(9, nine, kind="value", issue="码头备用金余额错误", hint="重算备用金"))
        with self.llm_calls(AsyncMock()) as llm:
            self.assertEqual(await self.repair(), [])
        llm.assert_not_awaited()
        self.assertEqual((self.drift()["reason"], self.drift()["autoRepairAttempts"], self.state()["state"]),
                         ("cascade_risk", 0, "done"))

    async def test_downstream_conflict_rolls_back_patch_and_new_facts(self):
        self.continuous_prefix(7)
        opening = "码头备用金余额为100元。"
        self.chapter(8, opening, account("100元", opening))
        nine = "码头备用金支出20元，余额为80元。"
        corrected = "码头备用金支出10元，余额为90元。"
        before = self.chapter(9, nine, account("80元", nine, "-20元"))
        self.store.save_verified_chapter(self.job_id, 10, {"status": "verified", "title": "封存账簿",
                                         "content": "姚青确认码头备用金余额为80元。随即封存账簿。"}, {})
        drift = finding(9, nine, issue="备用金的扣款与凭证不符", hint="按真实扣款重算")
        source = self.archive(drift)
        fake = AsyncMock(side_effect=[reply(patches=[patch_for(drift, corrected)]), reply(**account("90元", corrected, "-10元"))])
        with self.llm_calls(fake):
            self.assertEqual(await self.repair(), [])
        self.assertEqual([call.args[1] for call in fake.await_args_list], ["drift_patch", "fact_extraction"])
        self.assertEqual(self.store.get_chapter(self.job_id, 9), before)
        self.assertEqual(self.facts()["ledgerBalances"][0]["value"], "80元")
        self.assertEqual(self.facts()["arcSummaries"][0]["sourceHash"], source)
        self.assertEqual((self.drift()["status"], self.drift()["reason"]), ("deferred", "cascade"))

    async def test_resume_reuses_patch_draft_and_rebuilds_replaced_facts(self):
        self.continuous_prefix()
        old_delta = {"characters": {WRONG: {"quote": WRONG, "role": "网名"}}, "items": {"旧印章": {"quote": WRONG}}}
        self.chapter(9, WRONG, old_delta)
        self.chapter(10, "新署名尚未公布。", {})
        drift = finding(9, WRONG)
        source = self.archive(drift)
        llm = AsyncMock(side_effect=[reply(patches=[patch_for(drift, RIGHT)]), InterruptedError("停在提取事实前")])
        with self.llm_calls(llm), self.assertRaises(InterruptedError):
            await self.repair()
        checkpoint = self.store.get_job_record(self.job_id)["checkpoint"]
        self.assertEqual(checkpoint["driftRepairDraft"]["3"][source]["9"]["patches"], [patch_for(drift, RIGHT)])
        self.assertEqual(self.state()["state"], "pending")
        self.store.save_checkpoint(self.job_id, {"stage": "chapter_outlines", "nextChapter": 11})
        extraction = AsyncMock(return_value=reply(characters={RIGHT: {"quote": RIGHT, "role": "网名"}}))
        with self.llm_calls(extraction):
            changed, = await self.refresh()
        self.assertEqual([call.args[1] for call in extraction.await_args_list], ["fact_extraction"])
        self.assertIn(RIGHT, changed["content"])
        facts = self.facts()
        self.assertEqual(set(facts["characters"]), {RIGHT})
        self.assertNotIn("旧印章", facts["items"])
        self.assertEqual(next(row for row in facts["factHistory"] if row["chapter"] == 9)["delta"]["characters"][RIGHT]["quote"], RIGHT)
        self.assertEqual(self.state()["state"], "done")

    async def test_hash_mismatch_leaves_concurrent_author_edit_and_no_stale_drafts(self):
        before = self.chapter(9, WRONG)
        self.chapter(10, "姚青收好了那封信。")
        drift = finding(9, WRONG)
        source = self.archive(drift)
        snapshot = self.store.job_detail(self.job_id)
        self.assertIsNone(self.store.apply_arc_drift_repairs(self.job_id, ARC, expected_source_hash="old", patched_chapters=[],
                          updated_facts_deltas={}, ledger_updates=[], drift_repair_state={"state": "done"}))
        self.assertEqual(snapshot, self.store.job_detail(self.job_id))

        async def concurrent(*_args, **_kwargs):
            self.store.save_chapter(self.job_id, 9, {**before, "content": before["content"].replace(WRONG, "作者改稿")})
            return reply(patches=[patch_for(drift, RIGHT)])

        with self.llm_calls(AsyncMock(side_effect=concurrent)):
            self.assertEqual(await self.repair(), [])
        self.assertNotEqual(self.source(), source)
        self.assertIn("作者改稿", self.store.get_chapter(self.job_id, 9)["content"])
        self.assertEqual((self.drift()["status"], self.drift()["reason"], self.drift()["autoRepairAttempts"]),
                         ("dismissed", "source_changed", 0))
        job = self.store.get_job_record(self.job_id)
        self.assertEqual(job["logs"][-1]["meta"]["reason"], "source_changed")
        self.assertFalse(job.get("checkpoint"))

    async def test_cached_draft_only_resume_makes_zero_calls(self):
        before = self.chapter(9, WRONG)
        self.chapter(10, "姚青收好了那封信。")
        drift = finding(9, WRONG)
        source = self.archive(drift)
        self.store.save_drift_repair_draft(self.job_id, ARC, source, 9, {"patches": [patch_for(drift, RIGHT)]})
        with self.llm_calls(AsyncMock()) as llm:
            changed, = await self.refresh()
        llm.assert_not_awaited()
        self.assertEqual(changed["content"], before["content"].replace(WRONG, RIGHT))

    async def test_persisted_book_quota_defers_without_llm_calls(self):
        self.chapter(9, WRONG)
        self.chapter(10, "姚青收好了那封信。")
        entries = merge_drift_entries([], arc_index=1, drifts=[finding(n, WRONG) for n in range(1, 5)],
                                      source_hash="old", timestamp="then")
        for row in entries:
            row.update(status="deferred", autoRepairAttempts=1)
        self.store.update_job(self.job_id, {"canonicalFacts": {**self.facts(), "driftLedger": entries}})
        latest = finding(9, WRONG)
        self.archive(latest)
        with self.llm_calls(AsyncMock()) as llm:
            self.assertEqual(await self.repair(), [])
        llm.assert_not_awaited()
        row = next(row for row in drift_ledger(self.facts()) if row["id"] == drift_id(latest))
        self.assertEqual((row["status"], row["reason"], row["autoRepairAttempts"]), ("deferred", "quota", 0))

    async def test_retryable_error_records_each_try_and_gives_up_without_blocking(self):
        before = self.chapter(9, WRONG)
        self.chapter(10, "姚青收好了那封信。")
        self.archive(finding(9, WRONG))
        seen = []

        async def timed_out(*_args, **_kwargs):
            seen.append(self.state()["tries"])
            raise LLMError("timed out", code="TIMEOUT", status=408, retryable=True)

        with self.llm_calls(AsyncMock(side_effect=timed_out)):
            self.assertEqual(await self.repair(), [])
            self.assertEqual(await self.refresh(), [])
        self.assertEqual(seen, [0, 1])
        self.assertEqual((self.state()["state"], self.state()["tries"]), ("done", 2))
        self.assertEqual((self.drift()["reason"], self.drift()["autoRepairAttempts"]), ("repair_unavailable", 1))
        self.assertEqual(self.store.get_chapter(self.job_id, 9), before)

    async def test_disabled_repair_records_ledger_and_legacy_archives_never_auto_repair(self):
        self.chapter(9, WRONG)
        self.chapter(10, "姚青收好了那封信。")
        drift = finding(9, WRONG)
        self.archive(drift)
        llm = AsyncMock()
        with self.llm_calls(llm), patch.dict(os.environ, {"NOVEL_FORGE_DRIFT_REPAIR": "0"}):
            self.assertEqual(await self.repair(), [])
            self.assertEqual(await self.refresh(), [])
        self.assertEqual(self.drift()["status"], "open")
        facts = self.facts()
        facts.pop("driftLedger")
        facts["arcSummaries"][0].pop("driftRepair")
        self.store.update_job(self.job_id, {"canonicalFacts": facts})
        self.assertEqual((self.drift()["status"], self.drift()["reason"]), ("deferred", "stale_arc"))
        self.assertEqual(self.store.job_detail(self.job_id)["job"]["canonicalFacts"]["driftLedger"], [self.drift()])
        with self.llm_calls(llm):
            self.assertEqual(await self.refresh(), [])
        llm.assert_not_awaited()
        self.assertNotIn("driftLedger", self.store.get_job_record(self.job_id)["canonicalFacts"])

    async def test_cross_arc_prompt_rollback_and_manual_edit(self):
        first = finding(2, "老码头使用橙色回单", issue="旧凭证口径冲突", hint="橙色回单留待作者确认")
        repaired = finding(3, "更正过的署名", issue="已修复署名")
        late = finding(9, WRONG)
        self.chapter(2, first["quote"])
        self.chapter(9, WRONG)
        self.chapter(10, "姚青收好了那封信。")
        prior = merge_drift_entries([], arc_index=1, drifts=[first, repaired], source_hash="early", timestamp="then")
        prior[0].update(status="deferred", reason="ooc_deferred")
        prior[1].update(status="repaired", autoRepairAttempts=1)
        self.store.update_job(self.job_id, {"canonicalFacts": {**self.facts(), "driftLedger": prior}})
        self.archive(late)
        facts = self.facts()
        self.assertIn(drift_id(first), {row["id"] for row in unresolved_drifts(facts)})
        prompt = build_arc_outlines_prompt(book_plan=PLAN, canonical_facts=facts, start_chapter=17, total_chapters=25, arc_index=5)
        self.assertIn("previousArcDriftChecks", prompt["prompt"])
        self.assertIn(first["fixHint"], prompt["prompt"])
        self.assertNotIn(repaired["issue"], prompt["prompt"])
        self.store.save_chapter(self.job_id, 2, {"content": "替换后的稿件仍然保有证据。", "status": "verified"})
        row = next(row for row in drift_ledger(self.facts()) if row["id"] == drift_id(first))
        self.assertEqual((row["status"], row["reason"]), ("dismissed", "source_changed"))
        after = self.store.delete_uncommitted_facts(self.job_id, 9)
        self.assertEqual([row["chapter"] for row in drift_ledger(after)], [2, 3])

    async def test_mock_patch_uses_only_local_windows(self):
        line = _MOCK_UNPOSTED_DRIFT
        drift = finding(9, line, kind="value")
        text = "章首私有标记" + "开" * 900 + line + "末" * 900 + "章尾私有标记"
        request = build_drift_patch_prompt(chapter={"chapterNumber": 9, "content": text},
                                           drifts=[{**drift, "id": drift_id(drift)}], book_plan=PLAN)
        self.assertNotIn("章首私有标记", request["prompt"])
        self.assertNotIn("章尾私有标记", request["prompt"])
        self.assertIn(RIGHT, request["prompt"])
        response = json.loads((await call_mock_llm(**request))["text"])
        after, applied, rejected = apply_text_patches(text, response["patches"], {drift_id(drift): drift}, chapter_number=9)
        self.assertEqual((len(applied), rejected), (1, []))
        self.assertNotIn(line, after)


class DriftApiTests(unittest.TestCase):
    def test_manual_status_endpoint_and_revision_ledger_refresh(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, {"NOVEL_FORGE_BACKGROUND_FEEDS": "0", "NOVEL_FORGE_AUTO_RECOVER": "0"}))
        app = create_app(root)
        with TestClient(app) as client:
            store = app.state.store
            job_id = store.create_job({"totalChapters": 1})["id"]
            arc = {"arcIndex": 1, "startChapter": 1, "endChapter": 1}
            store.update_job(job_id, {"status": "completed", "arcPlans": [arc]})
            store.save_chapter(job_id, 1, {"title": "码头", "content": prose(WRONG), "status": "verified"})
            source = store.get_chapters(job_id)
            drift = finding(1, WRONG)
            store.save_arc_summary(job_id, arc, volume_source_hash(source), {"mainline": "码头交割", "driftChecks": [drift]},
                                   plan_canon=True)
            endpoint = f"/api/jobs/{job_id}/drifts/{drift_id(drift)}"
            self.assertEqual(client.patch(endpoint, json={"status": "repaired"}).status_code, 400)
            self.assertEqual(client.patch(endpoint + "-missing", json={"status": "deferred"}).status_code, 404)
            response = client.patch(endpoint, json={"status": "dismissed"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual((response.json()["status"], response.json()["reason"]), ("dismissed", "human"))
            response = client.patch(endpoint, json={"status": "deferred", "reason": "等作者复查"})
            self.assertEqual(response.json()["reason"], "等作者复查")
            store.apply_manuscript_revision(job_id, {"revisionId": "rev-1", "sourceVersion": manuscript_fingerprint(source),
                                           "changes": [{"chapterNumber": 1, "title": "码头", "content": prose(RIGHT)}]})
            response = client.get(f"/api/jobs/{job_id}").json()
            row, = response["job"]["canonicalFacts"]["driftLedger"]
            self.assertEqual((row["status"], row["reason"]), ("dismissed", "source_changed"))
            self.assertEqual(response["job"]["canonicalFacts"]["arcSummaries"], [])
