import json
import os
import re
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from novel_forge.app import create_app
from novel_forge.audit_judge import adjudicate_audit, ground_blockers, select_audit_repairs
from novel_forge.book_diagnostics import collect_book_diagnostics
from novel_forge.llm import estimate_tokens
from novel_forge.pipeline import PipelineRuntime
from novel_forge.prompt_budget import DEFAULT_AUDIT_INPUT_BUDGET, read_audit_input_budget
from novel_forge.prompts import build_full_book_audit_prompt
from novel_forge.storage import Store, count_chinese_words


VALUE_LINE = "账房核对了备用金，柜中还剩九十枚银币。"
ALIAS_LINE = "码头主事当着买家的面，把林渊称作林七。"
META_LINE = "作者有话说：此处的钟声留到后面解释。"
PASS = {"passed": True, "score": 98, "summary": "全书定稿", "blockingIssues": []}
STYLE_ISSUE = {"type": "ai_flavor", "severity": "error", "rule": "dash_density"}


def chapter(number, body="她推开仓门，沿墙查看旧账册。", **fields):
    return {"chapterNumber": number, "title": f"仓门旧案{number}", "status": "verified", "content": body, **fields}


def drift(number, kind, quote, *, issue=None):
    return {"id": f"{kind}-{number}", "chapter": number, "kind": kind, "issue": issue or f"核对{kind}口径",
            "status": "deferred", "quote": quote, "detectedSourceHash": "archive-a"}


def model_reply(report):
    return {"text": json.dumps(report, ensure_ascii=False)}


def repeating_arcs():
    return [
        {"arcIndex": arc, "startChapter": arc, "endChapter": arc, "skeleton": ["investigation", "reveal"],
         "mainline": f"第{arc}单元揭开铁匣线索。",
         "volumeProgress": {"retiredReused": ["已经用尽的取证手段"]}}
        for arc in range(1, 8)
    ]


class BookDiagnosticsTests(unittest.TestCase):
    def test_hard_drifts_and_meta_override_a_score_of_98(self):
        chapters = [chapter(1, VALUE_LINE), chapter(2, ALIAS_LINE), chapter(3, META_LINE)]
        facts = {"driftLedger": [drift(1, "value", VALUE_LINE), drift(2, "alias", ALIAS_LINE),
                                 drift(3, "clock", "这句话不存在于正文。")]}
        diag = collect_book_diagnostics({"totalChapters": 3}, chapters, facts)
        self.assertEqual([item["code"] for item in diag["hardBlockers"]], ["drift_value", "drift_alias", "meta_text"])
        self.assertEqual(diag["coverage"]["staleDriftsSkipped"], 1)
        self.assertEqual(len(diag["unresolvedDrifts"]), 2)
        report = adjudicate_audit(PASS, diag, chapters)
        self.assertFalse(report["passed"])
        self.assertEqual((report["rawScore"], report["score"], report["verdict"]), (98, 69, "blocked"))
        repairs = select_audit_repairs(report["blockingIssues"], {"2": 1})
        self.assertEqual([target["chapterNumber"] for target in repairs], [3, 1])
        self.assertTrue(all(target["findings"] for target in repairs))

    def test_deduplication_canon_and_hash_inputs(self):
        plan = {"title": "钟楼旧案", "canonLocks": {"identityAliases": [
            {"character": "林渊", "scope": "码头", "alias": "林九"},
        ]}}
        job = {"totalChapters": 2, "bookPlan": plan}
        chapters = [chapter(1, ALIAS_LINE), chapter(2, VALUE_LINE)]
        canon = drift(1, "canon", ALIAS_LINE, issue="林渊的码头身份代号与口径相冲。")
        facts = {"driftLedger": [canon, {**canon, "id": "duplicate"},
                                 drift(2, "canon", VALUE_LINE, issue="物资的常规描述可能有偏差。")]}
        base = collect_book_diagnostics(job, chapters, facts)
        self.assertEqual([row["code"] for row in base["hardBlockers"]], ["drift_canon"])
        self.assertEqual([row["penalty"] for row in base["capItems"]], [2])
        self.assertEqual(base["auditInputHash"], collect_book_diagnostics(job, list(reversed(chapters)), facts)["auditInputHash"])
        for field, value in (("title", "换标题"), ("content", ALIAS_LINE + "她合上账册。"),
                             ("status", "repaired"), ("chapterNumber", 3)):
            with self.subTest(field=field):
                edited = [{**chapters[0], field: value}, chapters[1]]
                self.assertNotEqual(base["auditInputHash"], collect_book_diagnostics(job, edited, facts)["auditInputHash"])
        for field, value in (("status", "dismissed"), ("detectedSourceHash", "archive-b")):
            edited = deepcopy(facts)
            edited["driftLedger"][0][field] = value
            self.assertNotEqual(base["auditInputHash"], collect_book_diagnostics(job, chapters, edited)["auditInputHash"])
        self.assertNotEqual(base["auditInputHash"], collect_book_diagnostics(
            job, chapters, facts, book_plan={**plan, "title": "修订书名"},
        )["auditInputHash"])

    def test_prose_ledger_error_blocks_and_dialogue_does_not(self):
        baseline = "码头备用金的期初余额为100元。"
        contradiction = "码头备用金目前余额为120元。"
        balance = {"label": "码头备用金", "value": "100元", "quote": baseline}
        facts = {"factHistory": [{"chapter": 1, "delta": {"memory": {"version": 1, "ledgerBalances": [balance]}}}]}
        chapters = [chapter(1, baseline), chapter(2, contradiction), chapter(3, f"他试探着问：“{contradiction}”")]
        diag = collect_book_diagnostics({"totalChapters": 3}, chapters, facts)
        self.assertEqual(diag["coverage"]["trackedAccounts"], 1)
        self.assertEqual([(row["code"], row["chapterNumber"]) for row in diag["hardBlockers"]], [("ledger", 2)])

    def test_review_threshold_and_real_factual_errors(self):
        chapters = [chapter(n, status="needs_review", verification={"issues": [STYLE_ISSUE]}) for n in range(1, 5)]
        diag = collect_book_diagnostics({"totalChapters": 100}, chapters, {})
        self.assertEqual(len(diag["hardBlockers"]), 4)
        chapters[0]["verification"]["issues"] = [{"type": "hard_value_conflict", "severity": "error", "message": "余额冲突"}]
        diag = collect_book_diagnostics({"totalChapters": 100}, chapters[:3], {})
        self.assertEqual([row["chapterNumber"] for row in diag["hardBlockers"]], [1])
        self.assertEqual([row["penalty"] for row in diag["capItems"]], [1.5, 1.5])

    def test_retired_character_advisory_ignores_dialogue(self):
        plans = [{"startChapter": 1, "endChapter": 1, "villainChips": [{"villain": "赵天豪", "exitPlan": "exit"}]}]
        chapters = [chapter(1), chapter(2, "他低声说：“赵天豪还会回来。”"), chapter(3, "赵天豪推开了渡口的小门。")]
        diag = collect_book_diagnostics({"totalChapters": 3}, chapters, {}, arc_plans=plans)
        self.assertEqual([(row["code"], row["chapterNumber"], row["exitChapter"]) for row in diag["advisories"]],
                         [("retired_character", 3, 1)])

    def test_grounding_drops_invented_quotes_and_caps_unproven_finale_issues(self):
        chapters = [chapter(1, VALUE_LINE), chapter(2, ALIAS_LINE), chapter(3), chapter(4), chapter(5)]
        report = {**PASS, "auditProtocolVersion": 2, "blockingIssues": [
            {"type": "setting_conflict", "chapterNumber": 1, "quote": "故事里没有这一段。"},
            {"type": "causality", "chapterNumber": 2, "quote": ALIAS_LINE},
            {"type": "ending", "chapterNumber": 5, "message": "最后一场冲突缺少交代"},
            {"type": "ending", "chapterNumber": 999, "quote": VALUE_LINE},
        ]}
        grounded, downgraded = ground_blockers(report, chapters)
        self.assertEqual([row["chapterNumber"] for row in grounded], [2])
        self.assertEqual([(row["chapterNumber"], row["grounding"]) for row in downgraded],
                         [(1, "ungrounded"), (5, "cap"), (999, "ungrounded")])
        diag = collect_book_diagnostics({"totalChapters": 5}, chapters, {})
        self.assertEqual(adjudicate_audit(report, diag, chapters)["issueChapters"], [2])
        macro = adjudicate_audit({**report, "blockingIssues": [report["blockingIssues"][2]]}, diag, chapters)
        self.assertEqual((macro["passed"], macro["score"], macro["blockingIssues"]), (True, 85, []))

    def test_legacy_chapter_only_blockers_can_be_grounded(self):
        chapters = [chapter(1)]
        item = {"chapterNumber": 1, "message": "需要复核本章"}
        self.assertEqual(len(ground_blockers({"blockingIssues": [item]}, chapters)[0]), 1)
        self.assertEqual(len(ground_blockers({"passed": False, "issueChapters": [1]}, chapters)[0]), 1)
        self.assertEqual(ground_blockers({"blockingIssues": [item]}, chapters, strict_quote=True)[0], [])
        self.assertEqual(ground_blockers({"passed": False, "issueChapters": [1],
                                          "blockingIssues": [{**item, "quote": "伪造证据未出现在这里。"}]}, chapters)[0], [])

    def test_large_audit_prompt_respects_reserve_and_protects_opening_finale_and_coverage(self):
        opening = "潮门开篇唯一证据"
        finals = {n: f"终局结尾唯一证据{n}" for n in range(198, 201)}
        chapters = [chapter(n, (opening if n == 1 else "") + ("船工把木板移进仓库。" * 110) + finals.get(n, ""),
                            summary="旧账指认码头守夜人的真名。" * 60) for n in range(1, 201)]
        facts = {
            "characters": {f"角色{n}": {"status": f"配角尾部标记{n}", "sourceChapter": 1} for n in range(213)},
            "items": {f"道具尾部标记{n}": {"location": "仓库"} for n in range(643)},
            "foreshadowing": [{"clue": f"暗号线索{n}" + "回收提示" * 30, "status": "planted", "plantedChapter": n}
                             for n in range(1, 201)],
            "volumeSummaries": [{"startChapter": start, "endChapter": start + 9, "mainline": "仓运调查" * 100}
                                for start in range(1, 191, 10)],
        }
        chapters[9]["content"] += "\n" + META_LINE
        diag = collect_book_diagnostics({"totalChapters": 200}, chapters, facts)
        prompt = build_full_book_audit_prompt(chapters=chapters, canonical_facts=facts, diagnostics=diag,
                                            input_budget=24_576)
        self.assertLessEqual(estimate_tokens(prompt["system"] + "\n" + prompt["prompt"]), 23_552)
        self.assertNotIn("budgetMeta", prompt)
        for term in ('"chaptersScanned":200', '"hardBlockers":[{"chapterNumber":10', opening, *finals.values()):
            self.assertIn(term, prompt["prompt"])
        self.assertNotIn("配角尾部标记212", prompt["prompt"])
        self.assertNotIn("道具尾部标记642", prompt["prompt"])
        self.assertEqual(read_audit_input_budget(None), DEFAULT_AUDIT_INPUT_BUDGET)
        self.assertIsNone(read_audit_input_budget("0"))


class AuditPipelineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.store = Store(self.directory)
        self.runtime = PipelineRuntime(self.store)
        env = patch.dict(os.environ, {"NOVEL_FORGE_LONG_MEMORY": "0", "NOVEL_FORGE_BACKGROUND_FEEDS": "0",
                                      "NOVEL_FORGE_AUTO_RECOVER": "0", "NOVEL_FORGE_DRIFT_REPAIR": "0"})
        env.start()
        self.addCleanup(env.stop)

    def seed(self, chapters, *, total=None, facts=None, plans=(), **fields):
        target = sum(count_chinese_words(row["content"]) for row in chapters)
        job = self.store.create_job({"title": "钟楼旧案", "premise": "主角核对旧账并打开钟楼。",
                                     "llmConfig": {"provider": "mock"}, "totalChapters": total or len(chapters),
                                     "targetWords": target, "wordsPerChapter": target // len(chapters),
                                     "maxAuditRetries": 50, **fields})
        for row in chapters:
            self.store.save_chapter(job["id"], row["chapterNumber"], row)
        data = {"bookPlan": {"title": "钟楼旧案", "logline": "主角核对旧账。"}, "arcPlans": list(plans),
                "outlines": [{"chapterNumber": row["chapterNumber"], "title": row["title"], "summary": "钟楼旧账"}
                             for row in chapters],
                "topicPackage": {"lockedAt": "saved", "title": "钟楼旧案", "premise": "主角核对旧账并打开钟楼。"},
                "checkpoint": {"stage": "full_audit", "nextChapter": len(chapters) + 1}}
        if facts is not None:
            data["canonicalFacts"] = {**self.store.get_canonical_facts(job["id"]), **facts}
        return self.store.update_job(job["id"], data)

    async def test_repair_is_bounded_then_failed_audit_is_cached_and_can_be_accepted(self):
        chapters = [chapter(1, VALUE_LINE), chapter(2, ALIAS_LINE), chapter(3, META_LINE)]
        job = self.seed(chapters, facts={"driftLedger": [drift(1, "value", VALUE_LINE), drift(2, "alias", ALIAS_LINE)]})
        job_id = job["id"]
        with patch.object(self.runtime, "_llm", return_value=model_reply(PASS)) as ask, patch.object(
            self.runtime, "_request_drift_patch", return_value=model_reply({"patches": []})
        ) as patch_call:
            await self.runtime.start_job(job_id)
            await self.runtime.wait_for_job(job_id)
            failed = self.store.get_job_record(job_id)
            self.assertEqual(failed["error"]["code"], "AUDIT_GATE_FAILED")
            self.assertEqual(failed["auditReport"]["verdict"], "blocked")
            self.assertEqual(failed["auditReport"]["rawScore"], 98)
            self.assertEqual(failed["auditGate"]["hardBlockerCount"], 3)
            self.assertEqual(len(failed["auditGate"]["localDiagnostics"]["hardBlockers"]), 3)
            self.assertEqual(failed["auditGate"]["revisionEntry"], {"route": "evaluation", "chapters": [1, 2, 3]})
            self.assertEqual(failed["auditRepairs"], {"1": 1, "2": 1, "3": 1})
            self.assertEqual([re.search(r"第(\d+)章局部纠偏", call.args[2]["prompt"])[1]
                              for call in patch_call.call_args_list], ["3", "2", "1"])
            self.assertEqual([call.args[1] for call in ask.call_args_list], ["full_audit"])
            fingerprint = failed["auditCache"]["auditInputHash"]
            ask.reset_mock()
            patch_call.reset_mock()
            await self.runtime.resume_job(job_id)
            await self.runtime.wait_for_job(job_id)
            ask.assert_not_awaited()
            patch_call.assert_not_awaited()
            self.assertEqual(self.store.get_job_record(job_id)["auditReport"]["auditInputHash"], fingerprint)

            edited = self.store.get_chapter(job_id, 1)
            self.store.save_chapter(job_id, 1, {**edited, "title": "修订后的章名"})
            await self.runtime.resume_job(job_id)
            await self.runtime.wait_for_job(job_id)
            ask.assert_awaited_once()
            patch_call.assert_not_awaited()
            self.assertNotEqual(self.store.get_job_record(job_id)["auditReport"]["auditInputHash"], fingerprint)

        incomplete = self.seed(chapters[:1], total=2)
        # A stale integrity gate must not allow acceptance after the manuscript becomes incomplete.
        self.store.update_job(incomplete["id"], {"status": "failed", "auditGate": failed["auditGate"]})
        with TestClient(create_app(self.directory)) as client:
            accepted = client.post(f"/api/jobs/{job_id}/audit/accept", json={"note": "同意保留现稿" * 50})
            self.assertEqual(accepted.status_code, 200, accepted.text)
            public = accepted.json()["job"]
            self.assertEqual((public["status"], public["error"]), ("completed", None))
            gate = public["auditGate"]
            self.assertFalse(gate["auditPassed"])
            self.assertEqual(gate["verdict"], "blocked")
            self.assertEqual(gate["override"]["hardBlockerCount"], 3)
            self.assertEqual(len(gate["override"]["note"]), 240)
            self.assertTrue(gate["override"]["at"])
            self.assertNotIn("auditCache", public)
            denied = client.post(f"/api/jobs/{incomplete['id']}/audit/accept")
            self.assertEqual(denied.status_code, 409, denied.text)

    async def test_fabricated_quote_never_repairs_and_local_title_is_corrected(self):
        saved = [chapter(1, "商会决定从两吨起拍。", title="十吨起拍"), chapter(2)]
        job = self.seed(saved)
        raw = {**PASS, "blockingIssues": [{"type": "causality", "chapterNumber": 2, "quote": "不存在的假叙述。"}]}
        with patch.object(self.runtime, "_llm", return_value=model_reply(raw)) as ask, patch.object(
            self.runtime, "_request_drift_patch", new_callable=AsyncMock
        ) as patch_call:
            result = await self.runtime._audit(job, {}, job["llmConfig"], job["llmConfig"], False)
        ask.assert_awaited_once()
        patch_call.assert_not_awaited()
        self.assertTrue(result["passed"])
        self.assertEqual(result["localDiagnostics"]["advisories"][0]["code"], "ungrounded")
        self.assertEqual(self.store.get_chapter(job["id"], 1)["title"], "两吨起拍")

    async def test_successful_patch_clears_the_findings_and_reaudits_new_manuscript(self):
        verification = {"passed": False, "issues": [
            {"type": "hard_value_conflict", "severity": "error", "quote": VALUE_LINE, "message": "银币数不符"},
        ]}
        job = self.seed([chapter(1, VALUE_LINE, status="needs_review", verification=verification), chapter(2)],
                        facts={"driftLedger": [drift(1, "value", VALUE_LINE)]})
        response = {"patches": [{"driftId": "value-1", "confirmed": True, "find": VALUE_LINE,
                                 "replace": VALUE_LINE.replace("九十", "八十"), "reason": "统一银币数"}]}
        with patch.object(self.runtime, "_llm", return_value=model_reply(PASS)) as ask, patch.object(
            self.runtime, "_request_drift_patch", return_value=model_reply(response)
        ) as patch_call:
            report = await self.runtime._audit(job, {}, job["llmConfig"], job["llmConfig"], False)
        self.assertEqual(ask.await_count, 2)
        patch_call.assert_awaited_once()
        self.assertTrue(report["passed"])
        self.assertEqual(report["unresolvedDrifts"], [])
        self.assertEqual(self.store.get_chapter(job["id"], 1)["status"], "repaired")
        self.assertEqual(self.store.get_canonical_facts(job["id"])["driftLedger"][0]["status"], "repaired")

    async def test_style_and_rhythm_complete_with_revision_recommended(self):
        saved = [chapter(n, **({"status": "needs_review", "verification": {"issues": [STYLE_ISSUE]}} if n == 1 else {}))
                 for n in range(1, 8)]
        plans = repeating_arcs()
        job = self.seed(saved, plans=plans)
        with patch.object(self.runtime, "_llm", return_value=model_reply(PASS)) as ask, patch.object(
            self.runtime, "_repair_audit_chapters", new_callable=AsyncMock
        ) as repair:
            await self.runtime.start_job(job["id"])
            await self.runtime.wait_for_job(job["id"])
        final = self.store.get_job_record(job["id"])
        self.assertEqual(final["status"], "completed", final.get("error"))
        gate = final["auditGate"]
        self.assertTrue(gate["passed"])
        self.assertEqual(gate["verdict"], "revise_recommended")
        self.assertLess(gate["scoreCap"], 85)
        self.assertEqual(gate["localDiagnostics"]["stats"]["skeletonMaxSimilarRun"], 7)
        self.assertEqual(gate["needsReviewChapters"], [1])
        self.assertEqual(gate["hardBlockerCount"], 0)
        repair.assert_not_awaited()
        ask.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
