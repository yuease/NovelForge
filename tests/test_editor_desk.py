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

    async def test_runtime_failure_requires_manual_diagnosis_without_mutating_candidate(self):
        self.seed_candidate()
        self.worker.replies = iter([
            {"tools": [{"kind": "read", "version": "original", "chapter": 2}]},
            {"reply": "先核对流水（E3）。", "citations": ["E2", "E3"], "plan": {"chapters": [1]}},
        ])

        async def fail(_run):
            raise RevisionContentError("资金台账第 2 笔余额不闭合。")

        self.revision.invoke = fail
        self.revision.schedule("run", stage="ledger", done=0, total=1)
        await self.revision.tasks["run"]
        # Failure no longer auto-invokes the chief editor; it surfaces pendingFailure for manual click.
        self.assertEqual(self.worker.requests, [])
        session_before = self.editor.session("book", None)
        self.assertEqual(session_before["turns"], [])
        self.assertEqual(session_before["pendingFailure"]["stage"], "ledger")

        self.editor.request_diagnosis("book")
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

    async def test_editor_investigates_then_hands_off_manual_checkpoint_recovery(self):
        self.seed_candidate()
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
        self.assertEqual(self.revision.store.get("run")["status"], "error")

        # User manually clicks diagnose; editor returns a signed recoveryPlan WITHOUT auto-submitting.
        self.editor.request_diagnosis("book")
        await self.editor.failure_tasks["book"]
        self.assertEqual(self.revision.store.get("run")["status"], "error")
        self.assertFalse(done.is_set())
        turn, = self.editor.session("book", None)["turns"]
        recovery_plan = turn["answer"]["recoveryPlan"]
        self.assertEqual(recovery_plan["stage"], "ledger")
        self.assertIn("余额应为1000分", recovery_plan["note"])
        kinds = {item["kind"] for item in turn["answer"]["evidence"]}
        self.assertEqual(kinds, {"checkpoint", "read", "audit", "report"})
        self.assertEqual(turn["answer"]["evidence"][-1]["version"], "original")

        # User manually confirms recovery handoff.
        await self.editor.dispatch_recovery("book", recovery_plan["token"])
        await asyncio.wait_for(done.wait(), 5)
        self.assertTrue(self.editor.session("book", None)["turns"][0]["answer"]["recoveryPlan"]["dispatched"])
        calls = [request["payload"] for action, request in self.worker.requests if action == "revision-model"]
        self.assertIn("checkpointRecoveryAdvice", json.loads(calls[-1]["prompt"]))
        self.assertEqual(calls[-1]["model"], "verifier")
        self.assertEqual(calls[3]["model"], "editor-only")
        self.assertEqual(self.detail["chapters"], self.rows)
        self.assertEqual(self.revision.store.get("run")["status"], "ready")

    async def test_existing_failure_waits_for_manual_diagnosis_and_does_not_retry_rate_limit(self):
        self.seed_candidate()
        self.revision.store.create("other-run", "other", self.revision.store.snapshot("run"), {"workflowVersion": 2})
        self.revision.store.update("other-run", "error", stage="ledger", error="资金台账无法核实。")
        await self.editor.start()
        self.assertFalse(self.editor.failure_tasks)
        self.assertTrue(self.editor.session("other", None)["failureWaitingForModel"])
        self.assertIsNotNone(self.editor.session("other", None)["pendingFailure"])
        self.assertEqual(self.worker.requests, [])
        self.editor.preferences("other", {"model": {"provider": "openai", "model": "other-model"}})
        self.assertFalse(self.editor.failure_tasks)
        self.worker.replies = iter([WorkerError("provider response", 429, "OPENAI_RATE_LIMIT")])
        self.editor.request_diagnosis("other")
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

    async def test_deep_multi_round_tools_and_whole_book_targeted_revision_without_candidate(self):
        # 25-chapter book with NO prior revision and NO evaluation report (e.g., freshly imported TXT).
        rows = [
            {"chapterNumber": i, "title": f"第{i}章",
             "content": f"第{i}章中主角去了{'美国' if i % 2 else '日本'}谈生意。"}
            for i in range(1, 26)
        ]
        self.detail = {"job": {"id": "book", "title": "跨国商战", "evaluationReport": None,
                               "llmConfig": {"provider": "openai", "model": "writer-model"}}, "chapters": rows}
        self.worker = FakeWorker(self.detail)
        direct_dir = Path(self.tmp.name) / "direct"
        direct_dir.mkdir()
        self.revision = RevisionWorkflowV2(direct_dir, self, self.worker, None)
        self.revision.schedule = lambda run: self.revision.store.update(run, "running")
        self.editor = EditorDesk(self, self.revision, self.worker)
        self.editor.preferences("book", {"model": {"provider": "openai", "model": "editor-only"}})

        # Simulate 4 consecutive tool rounds (> old 2-round cap): batch search -> read_batch -> duplicate -> finish
        self.worker.replies = iter([
            {"tools": [{"kind": "search", "version": "current", "queries": ["美国", "日本"]}]},
            {"tools": [{"kind": "read_batch", "version": "current", "chapters": [1, 2, 25], "query": "谈生意"}]},
            {"tools": [{"kind": "read", "version": "current", "chapter": 10}]},
            {"tools": [{"kind": "read", "version": "current", "chapter": 20}]},
            {"reply": "全书 25 章都提到了真实国家名（E1、E2），已准备好全书定向修改单。",
             "citations": ["E1", "E2"],
             "plan": {"chapters": list(range(1, 26)), "note": "将美国改为西洲联邦、日本改为东岛国。"}},
        ])
        response = await self.editor.chat("book", {"message": "把全书真实国家名虚构化"})
        answer = response["turn"]["answer"]
        self.assertEqual(answer["evidence"][0]["data"]["命中章数"], 25)
        self.assertEqual(len(answer["evidence"][1]["data"]["章节"]), 3)
        plan = answer["plan"]
        self.assertEqual(plan["mode"], "targeted")
        self.assertEqual(len(plan["chapters"]), 25)

        started = await self.editor.dispatch("book", plan["token"])
        self.assertTrue(started["targeted"])
        self.assertEqual(started["work"]["targets"], list(range(1, 26)))
        self.assertEqual(started["ledgerVersion"], 1)
        self.assertEqual(started["editor"]["model"], "writer-model")
        self.assertTrue(self.editor.session("book", None)["turns"][0]["answer"]["plan"]["dispatched"])

    async def test_native_tool_loop_self_correction_and_intra_turn_compaction(self):
        import os
        from unittest.mock import patch

        rows = [
            {"chapterNumber": i, "title": f"第{i}章",
             "content": f"第{i}章中角色在{'美国' if i % 2 else '日本'}港口会面。" + "细节描写" * 40}
            for i in range(1, 7)
        ]
        self.detail = {"job": {"id": "book", "title": "商战", "evaluationReport": None,
                               "llmConfig": {"provider": "openai", "model": "writer-model"}}, "chapters": rows}

        class NativeFakeWorker(FakeWorker):
            supports_native_tools = True

            async def request(self, action, **data):
                self.requests.append((action, data))
                if action == "revision-source":
                    return {"version": fingerprint(self.detail["chapters"]), "idle": True,
                            "readiness": "ready", "reportMatches": True, "evaluatedAt": "today"}
                reply = next(self.replies)
                if isinstance(reply, Exception):
                    raise reply
                return reply

        self.worker = NativeFakeWorker(self.detail)
        native_dir = Path(self.tmp.name) / "native"
        native_dir.mkdir()
        self.revision = RevisionWorkflowV2(native_dir, self, self.worker, None)
        self.revision.schedule = lambda run: self.revision.store.update(run, "running")
        self.editor = EditorDesk(self, self.revision, self.worker)
        self.editor.preferences("book", {"model": {"provider": "openai", "model": "editor-only"}})

        def call_resp(calls):
            return {
                "text": "",
                "toolCalls": calls,
                "assistantMessage": {"role": "assistant", "content": "", "toolCalls": calls},
            }

        self.worker.replies = iter([
            # Round 1: tries submit_revision_plan BEFORE checking manuscript -> server rejects with ok=False
            call_resp([{"id": "c1", "name": "submit_revision_plan",
                        "arguments": {"chapters": [1, 2], "note": "虚构化国家名"}}]),
            # Round 2: reads ch 1, 2
            call_resp([{"id": "c2", "name": "read", "arguments": {"version": "current", "chapter": 1}}]),
            # Round 3: reads ch 3, 4
            call_resp([{"id": "c3", "name": "read", "arguments": {"version": "current", "chapter": 3}}]),
            # Round 4: triggers intra-turn compaction call first (single-turn summary)
            {"text": "已核实 E1(第1章美国)、E2(第3章美国)，接下来扫全书并拟单。"},
            # Round 4 actual think: calls search + submit_revision_plan IN PARALLEL in the same round!
            call_resp([
                {"id": "c4", "name": "submit_revision_plan",
                 "arguments": {"chapters": [1, 2, 3, 4, 5, 6], "note": "将美国改为西洲、日本改为东岛。"}},
                {"id": "c5", "name": "search", "arguments": {"version": "current", "queries": ["美国", "日本"]}},
            ]),
            # Final natural prose reply citing range E1-E3
            {"text": "全书 6 章都核实完了（E1-E3），已按要求准备好定向返修单，点下方确认就会开始改。",
             "toolCalls": [], "assistantMessage": {"role": "assistant", "content": "done", "toolCalls": []}},
        ])
        with patch.dict(os.environ, {"NOVEL_FORGE_EDITOR_COMPACT_TOKENS": "220"}):
            response = await self.editor.chat("book", {"message": "把国家名都虚构化"})
        answer = response["turn"]["answer"]
        self.assertEqual([item["id"] for item in answer["evidence"]], ["E1", "E2", "E3"])
        self.assertEqual(answer["plan"]["mode"], "targeted")
        self.assertEqual(answer["plan"]["chapters"], [1, 2, 3, 4, 5, 6])

        # Verify Round 1 got ok=False validation feedback, and Round 4 received compacted digest with valid alternating roles
        model_calls = [req[1]["payload"] for req in self.worker.requests if req[0] == "revision-model"]
        r2_msgs = model_calls[1]["messages"]
        self.assertFalse(json.loads(r2_msgs[2]["content"])["ok"])
        compacted_call_msgs = model_calls[4]["messages"]
        self.assertEqual(compacted_call_msgs[1]["role"], "assistant")
        self.assertIn("已压缩前", compacted_call_msgs[1]["content"])
        self.assertEqual(compacted_call_msgs[2]["role"], "user")
        # Parallel batch executed search BEFORE submit_revision_plan so plan succeeded!
        final_call_msgs = model_calls[5]["messages"]
        plan_tool_msg = next(m for m in final_call_msgs if m.get("toolCallId") == "c4")
        self.assertTrue(json.loads(plan_tool_msg["content"])["ok"])

    async def test_native_tools_unsupported_falls_back_seamlessly_to_simulated_json(self):
        class FallbackFakeWorker(FakeWorker):
            supports_native_tools = True

        self.worker = FallbackFakeWorker(self.detail)
        self.editor = EditorDesk(self, self.revision, self.worker)
        self.editor.preferences("book", {"model": {"provider": "openai-compatible", "model": "legacy-relay"}})
        self.worker.replies = iter([
            WorkerError("不支持原生工具调用。", 400, "TOOLS_UNSUPPORTED"),
            {"tools": [{"kind": "read", "version": "current", "chapter": 1}]},
            {"reply": "第一章借了三十元（E1）。", "citations": ["E1"], "plan": None},
        ])
        response = await self.editor.chat("book", {"message": "第一章借了多少？"})
        answer = response["turn"]["answer"]
        self.assertEqual(answer["reply"], "第一章借了三十元（E1）。")
        self.assertEqual([item["id"] for item in answer["evidence"]], ["E1"])
