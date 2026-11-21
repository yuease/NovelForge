import json
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from novel_forge.editor_desk import EditorDesk, EvidenceDesk
from novel_forge.revision_checks import RevisionContentError, fingerprint, validate_ledger
from novel_forge.revision_v2 import RevisionWorkflowV2
from novel_forge.workflow import WorkerError


class FakeWorker:
    def __init__(self, detail):
        self.detail = detail
        self.replies = iter(())
        self.requests = []

    async def request(self, action, **data):
        self.requests.append((action, data))
        if action == "revision-source":
            return {"version": fingerprint(self.detail["chapters"]), "idle": True,
                    "readiness": "ready", "reportMatches": True, "evaluatedAt": "today"}
        reply = next(self.replies)
        if isinstance(reply, Exception):
            raise reply
        return {"text": json.dumps(reply, ensure_ascii=False)}


class EditorDeskTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.rows = [
            {"chapterNumber": 1, "title": "欠账", "content": "她借了三十元。"},
            {"chapterNumber": 2, "title": "还账", "content": "她拿出二十元。"},
        ]
        self.report = {"evaluatedAt": "today", "aiFlavorIssues": [{"chapterNumber": 2, "impact": "金额待核对"}]}
        self.detail = {"job": {"id": "book", "title": "小说", "evaluationReport": self.report,
                                "llmConfig": {"provider": "openai", "model": "test"}}, "chapters": self.rows}
        self.worker = FakeWorker(self.detail)
        self.revision = RevisionWorkflowV2(Path(self.tmp.name), self, self.worker, None)
        self.editor = EditorDesk(self, self.revision, self.worker)
        self.editor.preferences("book", {"model": {"provider": "openai", "model": "editor-only"}})
        self.addAsyncCleanup(self.editor.close)

    def get_job(self, job):
        detail = self.job_detail(job)
        return detail["job"] if detail else None

    def job_detail(self, job):
        if job == "book":
            return self.detail
        if job == "other":
            return {**self.detail, "job": {**self.detail["job"], "id": "other"}}
        return None

    def seed_candidate(self):
        self.revision.store.create("run", "book", {"chapters": self.rows, "report": self.report, "title": "小说"},
                                   {"workflowVersion": 2, "sourceVersion": fingerprint(self.rows),
                                    "chapterCount": 2, "isMock": False, "work": {"turn": 0}})
        self.revision.commit("run", status="ready", ledgerVersion=1, candidateTurn=0, hasCandidate=True, products=[
            ("v2-ledger", 1, {"rules": []}),
            ("v2-result", 0, {"candidate": self.rows, "candidateHash": fingerprint(self.rows),
                              "ledgerVersion": 1, "outcomes": [], "findings": []}),
        ])

    async def test_report_conversation_before_any_revision(self):
        self.worker.replies = iter([
            {"tools": [{"kind": "report", "chapter": 2}, {"kind": "read", "chapter": 2}]},
            {"reply": "第二章还了二十元。", "citations": ["E2", "E404"], "plan": None},
        ])
        result = (await self.editor.chat("book", {"message": "报告说得对吗？"}))["turn"]["answer"]
        self.assertEqual([item["id"] for item in result["evidence"]], ["E2"])
        self.assertEqual(result["evidence"][0]["data"]["正文"], self.rows[1]["content"])
        self.assertIn("金额待核对", self.worker.requests[-1][1]["payload"]["prompt"])
        self.assertIsNone(result["plan"])
        self.assertIsNone(self.revision.store.latest("book"))

    async def test_runtime_failure_reaches_the_books_editor_without_mutating_the_candidate(self):
        self.seed_candidate()
        self.revision.on_failure = self.editor.schedule_failures
        self.worker.replies = iter([
            {"tools": [{"kind": "read", "version": "original", "chapter": 2}]},
            {"reply": "先核对流水（E3）。", "citations": ["E2", "E3"], "plan": {"chapters": [1]}},
        ])

        async def fail(_run):
            raise RevisionContentError("资金台账第 2 笔余额不闭合。")

        self.revision.invoke = fail
        self.revision.schedule("run", stage="ledger", done=0, total=1)
        await self.revision.tasks["run"]
        await self.editor.failure_tasks["book"]
        history = self.editor.session("book", None)["turns"]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["answer"]["source"], "revision-failure")
        self.assertIsNone(history[0]["answer"]["plan"])
        call = self.worker.requests[0][1]["payload"]
        self.assertEqual(call["model"], "editor-only")
        self.assertEqual(json.loads(call["prompt"])["revisionFailure"]["stage"], "ledger")
        self.assertEqual(self.revision.store.get("run")["status"], "error")
        self.assertEqual(self.revision.load("run", "v2-result", 0)["candidate"], self.rows)
        self.assertEqual(self.editor.session("other", None)["turns"], [])
        await self.editor.start()
        self.assertEqual(self.editor.session("book", None)["turns"], history)
        self.assertEqual(len(self.worker.requests), 2)

    async def test_editor_investigates_then_repairs_failed_checkpoint_using_same_validator(self):
        self.seed_candidate()
        self.revision.on_failure = self.editor.schedule_failures
        self.revision.store.update("run", editor={"provider": "openai", "model": "writer"},
                                   verifier={"provider": "openai", "model": "verifier"})
        self.revision.store.save("run", "v2-audit-summary", "all", {"facts": [
            {"track": "finance", "chapter": 2, "quote": "她拿出二十元。", "detail": "第二章二十元"},
        ]})
        rejected = {"rules": [], "checks": [], "financialFlows": [{
            "label": "借款", "openingCents": 3000, "entries": [
                {"chapter": 2, "quote": "她拿出二十元。", "deltaCents": -2000, "balanceCents": 2000},
            ],
        }]}
        corrected = json.loads(json.dumps(rejected))
        corrected["financialFlows"][0]["entries"][0]["balanceCents"] = 1000
        self.worker.replies = iter([
            rejected, rejected, rejected,
            {"tools": [{"kind": "audit", "track": "finance", "chapter": 2},
                       {"kind": "report", "version": "original", "chapter": 2},
                       {"kind": "read", "version": "current", "chapter": 1}]},
            {"reply": "第二章拿出二十元（E3），借款原文在第一章（E6）。",
             "citations": ["E1", "E3", "E4", "E5", "E6"],
             "recovery": {"action": "retry_checkpoint", "note": "第2章扣除2000分后，余额应为1000分。"}},
            corrected,
        ])
        done = asyncio.Event()
        rows = {row["chapterNumber"]: row for row in self.rows}

        async def invoke(run):
            result = await self.revision.model_json(run, "ledger", "核实流水", {"chapters": self.rows},
                                                    lambda value: validate_ledger(value, rows))
            self.revision.store.update(run, "ready")
            self.assertEqual(result["financialFlows"][0]["entries"][0]["balanceCents"], 1000)
            done.set()

        self.revision.invoke = invoke
        self.revision.schedule("run", stage="ledger", done=0, total=1)
        await self.revision.tasks["run"]
        await self.editor.failure_tasks["book"]
        await asyncio.wait_for(done.wait(), 5)
        turn, = self.editor.session("book", None)["turns"]
        self.assertIn("已按核实后的方案恢复", turn["answer"]["recoveryStatus"])
        kinds = {item["kind"] for item in turn["answer"]["evidence"]}
        self.assertEqual(kinds, {"checkpoint", "read", "audit", "report"})
        self.assertEqual(turn["answer"]["evidence"][-1]["version"], "original")
        calls = [request["payload"] for action, request in self.worker.requests if action == "revision-model"]
        self.assertIn("checkpointRecoveryAdvice", json.loads(calls[-1]["prompt"]))
        self.assertEqual(calls[-1]["model"], "verifier")
        self.assertEqual(calls[3]["model"], "editor-only")
        self.assertEqual(self.detail["chapters"], self.rows)
        self.assertEqual(self.revision.store.get("run")["status"], "ready")

    async def test_existing_failure_waits_for_model_and_does_not_retry_rate_limit(self):
        self.seed_candidate()
        self.revision.store.create("other-run", "other", self.revision.store.snapshot("run"), {"workflowVersion": 2})
        self.revision.store.update("other-run", "error", stage="ledger", error="资金台账无法核实。")
        await self.editor.start()
        await self.editor.failure_tasks["other"]
        self.assertTrue(self.editor.session("other", None)["failureWaitingForModel"])
        self.assertEqual(self.worker.requests, [])
        self.editor.preferences("other", {"model": {"provider": "openai", "model": "other-model"}})
        self.worker.replies = iter([WorkerError("provider response", 429, "OPENAI_RATE_LIMIT")])
        self.editor.schedule_failures("other")
        await self.editor.failure_tasks["other"]
        turn, = self.editor.session("other", None)["turns"]
        self.assertIn("资金台账无法核实", turn["question"])
        self.assertIn("限流", turn["answer"]["reply"])
        self.assertEqual(len(self.worker.requests), 1)
        await self.editor.start()
        self.assertFalse(self.editor.failure_tasks)

    def test_invalid_financial_rows_provide_precise_retry_feedback(self):
        book = {row["chapterNumber"]: row for row in self.rows}
        event = {"chapter": 1, "quote": self.rows[0]["content"], "deltaCents": -3000, "balanceCents": 1000}
        plan = {"rules": [], "checks": [], "financialFlows": [
            {"label": "借款", "openingCents": 5000, "entries": [event]},
        ]}
        with self.assertRaisesRegex(RevisionContentError, "第 1 笔（第 1 章）.*应为 2000 分，实填 1000 分"):
            validate_ledger(plan, book)
        event["quote"] = "原稿没有这段引文"
        with self.assertRaisesRegex(RevisionContentError, "第 1 笔（第 1 章）的引文无法核实"):
            validate_ledger(plan, book)

    async def test_proposal_confirmed_once_and_only_for_its_candidate(self):
        self.seed_candidate()
        self.revision.schedule = lambda run: self.revision.store.update(run, "running")
        self.worker.replies = iter([
            {"tools": [{"kind": "search", "version": "candidate", "query": "三十元"}]},
            {"reply": "我找到了借款的原文。", "citations": ["E1"],
             "plan": {"chapters": [1], "reviewChapters": [2], "note": "改为借二十元。"}},
        ])
        response = await self.editor.chat("book", {"message": "把第一章改成借二十元。"})
        result = response["turn"]["answer"]
        self.assertEqual(result["evidence"][0]["data"]["共出现"], 1)
        token = result["plan"]["token"]
        self.assertEqual(self.revision.store.get("run")["work"]["turn"], 0)
        with self.assertRaises(WorkerError):
            await self.editor.dispatch("book", token + "0")
        restarted = EditorDesk(self, self.revision, self.worker)
        started = await restarted.dispatch("book", token)
        self.assertEqual(started["work"]["targets"], [1])
        self.assertEqual(started["work"]["reviewChapters"], [2])
        self.assertEqual(started["work"]["turn"], 1)
        self.assertTrue(restarted.session("book", None)["turns"][0]["answer"]["plan"]["dispatched"])
        with self.assertRaisesRegex(WorkerError, "候选稿或全书口径已更新"):
            await self.editor.dispatch("book", token)
        self.assertEqual(self.detail["chapters"], self.rows)

    async def test_evidence_from_older_turn(self):
        self.seed_candidate()
        changed = [self.rows[0], {**self.rows[1], "content": "她拿出三十元。"}]
        self.revision.commit("run", status="ready", candidateTurn=1, candidateHistory=[0, 1], products=[
            ("v2-result", 1, {"candidate": changed, "candidateHash": fingerprint(changed), "ledgerVersion": 1}),
        ])
        desk = EvidenceDesk(self.detail, self.revision, True)
        _, evidence = desk.run({"kind": "diff", "chapter": 2, "version": "previous"})
        self.assertIn("-她拿出二十元。", evidence["差异"])
        self.assertIn("+她拿出三十元。", evidence["差异"])

    async def test_context_compaction_preserves_conversation_and_survives_model_switch(self):
        self.editor.preferences("other", {"model": {"provider": "vertex", "model": "alternate"}})
        for index in range(10):
            self.editor.store.save_turn("book", str(index), f"原始问题 {index}", {"reply": f"原始回答 {index}"}, None)
        earlier = self.editor.store.context("book")[2]
        self.assertEqual(len(earlier), 4)
        cutoff = earlier[-1]["id"]
        self.worker.replies = iter([{"reply": "接续讨论。", "compactedContext": {
            "throughId": cutoff, "summary": "较早讨论确定保留第二章的原结尾。",
        }}])
        first = await self.editor.chat("book", {"message": "接着上次说。", "requestId": "compact-once"})
        sent = json.loads(self.worker.requests[-1][1]["payload"]["prompt"])
        self.assertEqual(sent["compactConversation"]["turns"][0]["user"], "原始问题 0")
        self.assertEqual(sent["history"][0]["user"], "原始问题 4")
        self.assertEqual(self.worker.requests[-1][1]["payload"]["model"], "editor-only")

        restarted = EditorDesk(self, self.revision, self.worker)
        session = restarted.session("book", None)
        self.assertEqual(len(session["turns"]), 11)
        self.assertEqual(session["turns"][0]["question"], "原始问题 0")
        self.assertEqual(session["turns"][-1], first["turn"])
        self.assertNotIn("memories", session)
        self.assertEqual(await restarted.chat("book", {"message": "接着上次说。", "requestId": "compact-once"}), first)
        self.assertEqual(restarted.session("other", None)["turns"], [])
        self.assertEqual(restarted.session("other", None)["model"]["model"], "alternate")

        restarted.preferences("book", {"model": {"provider": "openai", "model": "next-editor"}})
        self.worker.replies = iter([{"reply": "承接上轮。"}, {"reply": "这是另一部小说。"}])
        await restarted.chat("book", {"message": "结尾如何处理？"})
        prompt = json.loads(self.worker.requests[-1][1]["payload"]["prompt"])
        self.assertIn("保留第二章", prompt["conversationSummary"])
        self.assertEqual(prompt["history"][-1]["user"], "接着上次说。")
        self.assertEqual(self.worker.requests[-1][1]["payload"]["model"], "next-editor")
        await restarted.chat("other", {"message": "结尾如何处理？"})
        prompt = json.loads(self.worker.requests[-1][1]["payload"]["prompt"])
        self.assertEqual(prompt["conversationSummary"], "")
        self.assertEqual(prompt["history"], [])
