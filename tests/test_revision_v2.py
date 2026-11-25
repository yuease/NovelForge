import asyncio
import json
import logging
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, Mock, patch

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from novel_forge.__main__ import QuietPollingAccessFilter, logging_config
from novel_forge.llm import DEFAULT_MAX_TOKENS
from novel_forge.revision_checks import RevisionContentError, audited_evidence, fingerprint, issue_map, structural_review
from novel_forge.revision_store import RevisionStore
from novel_forge.revision_v2 import RevisionWorkflowV2, validated_semantic_outcomes
from novel_forge.revision_v2_checks import (
    adjudication_evidence, edit_patches, located_evidence, merge_semantic_delta, recheck_repairs, repair_feedback, scope,
    unique_at, validated_decision_batch, validated_ledger_core, validated_plan, verified_repair,
)
from novel_forge.workflow import WorkerError


OLD = "账簿里的欠款是八十万元。"
NEW = "账簿里的欠款是六十万元。"
OLD_TOOL = "她随身带着一只青玉手环。"
NEW_TOOL = "她随身带着一枚终端芯片。"
LINK_RULE = "航标规定蓝灯表示东侧通道。"
LINK_OLD = "她朝着西侧的蓝灯通道走去。"
LINK_FIXED = "她朝着东侧的蓝灯通道走去。"
STAMP = "2025-01-01T00:00:00Z"
PORT_CLOSED = "北港仍暂停航行。"
PORT_CLOSED_VARIANT = "北港目前封航。"
PORT_OPEN = "北港已经恢复航行。"
ARCHIVE_FRAME = "她翻到了去年的旧报纸。"


class Manuscript:
    def __init__(self, damaged=False):
        self.rows = [
            {"chapterNumber": n, "title": f"第{n}章", "content": f"第{n}章开头。原稿在这里平稳进行。情节接续自然。"}
            for n in range(1, 13)
        ]
        self.rows[1]["content"] = "开门时一阵风经过。"+OLD+"结清后他们一起走了出去。"
        self.rows[7]["content"] = "她低头回望。"+OLD_TOOL+"今夜要去赴约。"
        if damaged:
            self.rows[11]["content"] = "原稿缺\ufffd损。"
        self.report = {
            "evaluatedAt": STAMP,
            "aiFlavorIssues": [
                {"chapterNumber": 3, "quote": OLD, "category": "inconsistent_details", "priority": "P0",
                 "impact": "数字需确认", "suggestion": "核对账本的欠款口径。"},
                {"chapterNumber": 2, "quote": OLD, "category": "inconsistent_details", "priority": "P0",
                 "impact": "另一处重复意见", "suggestion": "核对账本。"},
            ],
        }

    def job_detail(self, job):
        return {"job": {"title": "测试稿", "evaluationReport": self.report, "llmConfig": {
            "provider": "openai", "model": "fixture",
        }}, "chapters": self.rows}


class Worker:
    def __init__(self, manuscript):
        self.manuscript = manuscript
        self.calls = []
        self.wait_author = False
        self.break_unit = False
        self.hold_debt = False
        self.late_concern = False
        self.applied = []

    async def request(self, action, **args):
        if action == "revision-source":
            return {"version": fingerprint(self.manuscript.rows), "reportMatches": True, "idle": True, "evaluatedAt": STAMP}
        if action == "revision-apply":
            self.applied.append(args["payload"])
            return
        body = args["payload"]
        data = json.JSONDecoder().raw_decode(body["prompt"])[0]
        role = body["role"]
        self.calls.append((role, data, body["model"]))
        if role == "audit":
            assert all("\ufffd" not in row["content"] for row in data["original"])
            value = {"facts": [], "issues": []}
        elif role == "ledger":
            if data.get("ledgerRequest", {}).get("phase") == "semantic_delta":
                value = {
                    "rules": [{"track": "continuity", "decision": "随身道具已更换为终端芯片。", "chapters": [8],
                               "evidence": [{"sourceId": issue["id"] + "-Q1"}]} for issue in data["issues"]],
                    "checks": [{"mode": "absent", "text": OLD_TOOL, "chapters": [8], "allowedChapters": []}],
                    "decisions": [{"id": issue["id"], "decision": "adapt", "reason": "同步道具",
                                   "evidence": [{"sourceId": issue["id"] + "-Q1"}], "chapters": [8]}
                                  for issue in data["issues"]],
                }
                return {"text": json.dumps(value, ensure_ascii=False)}
            waiting = self.wait_author and not data["authorAnswers"]
            changes = data["requestedChange"]
            checks = [{"mode": "absent", "text": OLD, "chapters": [2], "allowedChapters": []}]
            if changes and changes.get("after") == NEW_TOOL:
                checks.append({"mode": "absent", "text": OLD_TOOL, "chapters": [8], "allowedChapters": []})
            value = {
                "rules": [], "checks": checks, "financialFlows": [], "unresolved": [], "blueprint": [],
                "decisions": [
                    {"id": "R001", "decision": "author" if waiting else "adapt", "impact": "finance",
                     "reason": "统一账本口径", "options": ["六十万元", "八十万元"],
                     "evidence": [{"chapter": 2, "quote": OLD}], "chapters": [2]},
                    {"id": "R002", "decision": "skip", "reason": "重复意见", "evidence": [], "chapters": []},
                ],
            }
            for issue in data["issues"]:
                if issue["id"].startswith("X-"):
                    checks.append({"mode": "absent", "text": OLD_TOOL, "chapters": [8], "allowedChapters": []})
                    value["decisions"].append({"id": issue["id"], "decision": "adapt", "reason": "终审查到新联动",
                                               "evidence": [{"chapter": 8, "quote": OLD_TOOL}], "chapters": [8]})
        elif role == "editor":
            numbers = data["editableChapters"]
            patches, coverage = [], []
            for row in data["chapters"]:
                n, content = row["chapterNumber"], row["content"]
                if n == 4 and self.break_unit and body["model"] == "fixture":
                    patches.append({"chapter": 4, "field": "content", "mode": "exact", "before": "不存在",
                                    "after": "换稿", "issueIds": [], "reason": "触发当批重试"})
                if n == 2 and OLD in content and not self.hold_debt:
                    patches.append({"chapter": 2, "field": "content", "mode": "range", "startAnchor": OLD[:-1],
                                    "endAnchor": "结清后他们一起走了出去。", "after": NEW + "结清后他们一起走了出去。",
                                    "issueIds": ["R001"], "reason": "R001"})
                if n == 8 and any(item["text"] == OLD_TOOL for item in data["ledger"]["checks"]) and OLD_TOOL in content:
                    patches.append({"chapter": 8, "field": "content", "mode": "exact", "before": OLD_TOOL,
                                    "after": NEW_TOOL, "issueIds": [key for key in data["requiredIssues"] if key.startswith("X-")],
                                    "reason": "同步口径"})
            if "R001" in data["requiredIssues"]:
                coverage = [{"id": "R001", "outcome": "patched" if any("R001" in p["issueIds"] for p in patches) else
                             "already_met", "reason": "按台账", "chapter": 2,
                             "quote": OLD if self.hold_debt else NEW}]
            coverage.extend({"id": key, "outcome": "patched", "reason": "更新道具"}
                            for key in data["requiredIssues"] if key.startswith("X-"))
            value = {"patches": patches, "coverage": coverage}
        elif role == "verifier":
            assert all(OLD not in row["content"] for row in data["candidate"])
            value = {"findings": [], "outcomes": [
                {"id": issue["id"], "status": "fixed", "reason": "新口径已落实。",
                 "chapter": 8 if issue["id"].startswith("X-") else 2,
                 "quote": NEW_TOOL if issue["id"].startswith("X-") else NEW}
                for issue in data["issues"]
            ]}
            if self.late_concern and 8 in data["numbers"] and any(OLD_TOOL in row["content"] for row in data["candidate"]):
                value["ledgerConcerns"] = [{"chapter": 8, "quote": OLD_TOOL, "chapters": [8], "problem": "旧手环应改为芯片"}]
        else:
            value = {"findings": []}
        return {"text": json.dumps(value, ensure_ascii=False)}


class BatchedLedgerWorker(Worker):
    def __init__(self, manuscript, issue_count, *, fail_batch=None, incorrect_batch=None):
        super().__init__(manuscript)
        manuscript.report["rewritePlan"] = [f"改稿意见 {n}，请核对叙述语气。" for n in range(1, issue_count - 1)]
        self.fail_batch = fail_batch
        self.incorrect_batch = incorrect_batch
        self.ledger_calls = []

    async def request(self, action, **args):
        if action != "revision-model" or args["payload"]["role"] != "ledger":
            return await super().request(action, **args)
        body = args["payload"]
        data, end = json.JSONDecoder().raw_decode(body["prompt"])
        correction = body["prompt"][end:]
        self.calls.append(("ledger", data, body["model"]))
        self.ledger_calls.append((data, correction, body["maxTokens"]))
        request = data.get("ledgerRequest", {})
        phase, batch = request.get("phase", "legacy"), request.get("batch")
        if batch is not None and batch == self.fail_batch:
            raise WorkerError("502 unexpected EOF", 502, "HTTP_502")

        change = data["requestedChange"]
        checks = [{"mode": "absent", "text": OLD, "chapters": [2], "allowedChapters": []}]
        if change and change.get("after") == NEW_TOOL:
            checks.append({"mode": "absent", "text": OLD_TOOL, "chapters": [8], "allowedChapters": []})
        core = {
            "rules": [{"track": "finance", "decision": "欠款按确认口径统一。", "chapters": [2],
                       "evidence": [{"chapter": 2, "quote": OLD}]}],
            "checks": checks,
            "financialFlows": [{"label": "现金", "openingCents": 100, "entries": [
                {"chapter": 2, "quote": OLD, "deltaCents": 50, "balanceCents": 150, "reason": "入账"},
            ]}],
            "blueprint": [{"kind": "plot", "chapters": [2], "preserve": [2], "joins": [], "change": "保持结清后的衔接。"}],
        }
        if phase == "core":
            return {"text": json.dumps(core, ensure_ascii=False)}

        waiting = self.wait_author and "R001" not in data["authorAnswers"]
        decisions = []
        for issue in data["issues"]:
            key = issue["id"]
            if key == "R001":
                decisions.append({"id": key, "decision": "author" if waiting else "adapt", "impact": "finance",
                                  "reason": data["authorAnswers"].get(key, "统一欠款口径。"),
                                  "options": ["六十万元", "八十万元"], "chapters": [2],
                                  "evidence": [{"sourceId": "R001-Q1"}]})
            else:
                item = {"id": key, "decision": "skip", "reason": "原文符合要求。"}
                if key == "P026":
                    item["evidence"] = [{"sourceId": "R001-Q1"}]
                elif phase == "legacy":
                    item["evidence"] = []
                decisions.append(item)
        if phase == "decisions" and batch == self.incorrect_batch and not correction:
            decisions = [*decisions[1:], decisions[-1], {"id": "UNKNOWN", "decision": "skip", "reason": "编号错误。"}]
        output = {"decisions": decisions}
        if phase == "legacy":
            output.update(core)
        return {"text": json.dumps(output, ensure_ascii=False)}


class LocalLedgerWorker(Worker):
    def __init__(self, manuscript):
        super().__init__(manuscript)
        self.late_concern = True
        self.delta_started = asyncio.Event()
        self.allow_delta = asyncio.Event()
        self.edit_started = asyncio.Event()
        self.allow_edit = asyncio.Event()

    async def request(self, action, **args):
        if action == "revision-model":
            data = json.JSONDecoder().raw_decode(args["payload"]["prompt"])[0]
            if data.get("ledgerRequest", {}).get("phase") == "semantic_delta":
                self.delta_started.set()
                await self.allow_delta.wait()
            if self.delta_started.is_set() and data.get("editableChapters") == [8]:
                self.edit_started.set()
                await self.allow_edit.wait()
        return await super().request(action, **args)


class SemanticRetryWorker(Worker):
    def __init__(self, manuscript):
        super().__init__(manuscript)
        self.hold_second_batch = True
        self.semantic_calls = []

    async def request(self, action, **args):
        response = await super().request(action, **args)
        if action != "revision-model" or args["payload"]["role"] != "verifier":
            return response
        body = args["payload"]
        data, end = json.JSONDecoder().raw_decode(body["prompt"])
        correction = body["prompt"][end:]
        self.semantic_calls.append((data["numbers"], correction, body["maxTokens"]))
        if (data["numbers"] == [1, 2, 3] and not correction) or (
                data["numbers"] == [4, 5] and self.hold_second_batch):
            value = json.loads(response["text"])
            value["outcomes"] = [{"id": "R999", "status": "open", "reason": "编号写错。"}]
            return {"text": json.dumps(value, ensure_ascii=False)}
        return response


class ReviewWorker(Worker):
    def __init__(self, manuscript, *, unchanged=False, omit_check=False, inject_hard_failure=False):
        super().__init__(manuscript)
        self.unchanged = unchanged
        self.omit_check = omit_check
        self.inject_hard_failure = inject_hard_failure
        manuscript.rows[4]["content"] += LINK_RULE
        manuscript.rows[5]["content"] += LINK_OLD
        manuscript.rows[9]["content"] += "灯还在亮着。灯还在亮着。"

    async def request(self, action, **args):
        response = await super().request(action, **args)
        if action != "revision-model":
            return response
        role, data, _ = self.calls[-1]
        value = json.loads(response["text"])
        if role == "verifier":
            previous = data.get("repairChecks", [])
            if previous and not self.omit_check:
                value["repairChecks"] = [{
                    "id": issue["repair"]["id"], "status": "fixed", "reason": "蓝灯和方位按航标规则校对。",
                    "chapter": 6, "quote": LINK_OLD if self.unchanged else LINK_FIXED,
                } for issue in previous]
            if 6 in data["numbers"] and not previous and any(LINK_OLD in row["content"] for row in data["candidate"]):
                value["findings"].append({
                    "chapter": 6, "quote": LINK_OLD, "priority": "P1", "problem": "方位与航标不一致。",
                    "requiresDecision": False, "fix": "按已公开的蓝灯航标调整方向。",
                    "basis": [{"chapter": 5, "quote": LINK_RULE}], "affectedChapters": [5, 6],
                })
            if 10 in data["numbers"]:
                value["findings"].extend([
                    {"chapter": 10, "quote": "灯还在亮着。", "priority": "P1", "problem": "反复出现同一句。",
                     "requiresDecision": False, "fix": "删除一处重复句。", "affectedChapters": [10],
                     "basis": [{"chapter": 10, "quote": "情节接续自然。"}]},
                    {"chapter": 10, "quote": "第10章开头。", "priority": "P2", "problem": "可换一种开场语气。"},
                ])
        if role == "editor" and 6 in data["editableChapters"] and any(item.get("repair") for item in data["feedback"]):
            if self.inject_hard_failure:
                value["patches"].append({
                    "chapter": 6, "field": "title", "mode": "exact", "before": "第6章", "after": "第9章 错用的标题",
                    "issueIds": [], "reason": "模拟误改标题",
                })
            elif not self.unchanged:
                value["patches"].append({
                    "chapter": 6, "field": "content", "mode": "exact", "before": LINK_OLD, "after": LINK_FIXED,
                    "issueIds": [], "reason": "按航标纠正方位",
                })
        return {"text": json.dumps(value, ensure_ascii=False)}


class CoverageWorker(Worker):
    def __init__(self, manuscript):
        super().__init__(manuscript)
        manuscript.rows[0]["content"] += PORT_CLOSED
        manuscript.rows[3]["content"] += PORT_CLOSED_VARIANT
        manuscript.rows[8]["content"] += PORT_CLOSED
        manuscript.rows[9]["content"] += ARCHIVE_FRAME + PORT_CLOSED
        manuscript.report["aiFlavorIssues"].append({
            "chapterNumber": 1, "quote": PORT_CLOSED, "priority": "P0",
            "impact": "当前通航状态在多章不一致", "suggestion": "统一现时通航状态，保留旧报纸原文。",
        })

    async def request(self, action, **args):
        response = await super().request(action, **args)
        if action != "revision-model":
            return response
        role, data, _ = self.calls[-1]
        value = json.loads(response["text"])
        if role == "ledger":
            value["decisions"].append({
                "id": "R003", "decision": "adopt", "reason": "现时航线应恢复，历史用语按上下文保留",
                "chapters": [1], "searchTerms": [PORT_CLOSED, PORT_CLOSED_VARIANT],
                "evidence": [{"chapter": 1, "quote": PORT_CLOSED}],
            })
        if role == "editor":
            for key in data["requiredIssues"]:
                if not key.startswith("R003@"):
                    continue
                chapter = int(key.split("@")[1])
                entry = {"id": key, "outcome": "needs_review", "reason": "尚待核验"}
                if chapter in (1, 4):
                    value["patches"].append({
                        "chapter": chapter, "field": "content", "mode": "exact",
                        "before": PORT_CLOSED if chapter == 1 else PORT_CLOSED_VARIANT, "after": PORT_OPEN,
                        "reason": "现时航线状态", "issueIds": [key],
                    })
                    entry.update(outcome="patched", reason="已改为复航")
                if chapter == 10:
                    entry.update(outcome="already_met", reason="旧报纸记的是过去的状态", chapter=10, quote=ARCHIVE_FRAME)
                value["coverage"].append(entry)
        if role == "verifier":
            for issue, outcome in zip(data["issues"], value["outcomes"]):
                if not issue["id"].startswith("R003@"):
                    continue
                chapter = issue["chapter"]
                # Chapter 9 falsely claims it is fixed by citing chapter 1.
                outcome.update(chapter=1 if chapter == 9 else chapter, quote=PORT_OPEN, reason="现时航线已复航")
                if chapter == 10:
                    outcome.update(status="not_applicable", reason="原句属于历史报道，应保留")
        return {"text": json.dumps(value, ensure_ascii=False)}


class PausingWorker(Worker):
    def __init__(self, manuscript):
        super().__init__(manuscript)
        self.waiting = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.should_wait = True

    async def request(self, action, **args):
        reply = await super().request(action, **args)
        if action == "revision-model":
            role, data, _ = self.calls[-1]
            if self.should_wait and role == "audit" and data["original"][0]["chapterNumber"] == 6:
                self.waiting.set()
                try:
                    await asyncio.Future()
                finally:
                    self.cancelled.set()
        return reply


class EvidenceWorker(Worker):
    def __init__(self, manuscript):
        super().__init__(manuscript)
        manuscript.report["aiFlavorIssues"][0]["quote"] = OLD.rstrip("。") + "……"
        self.corrected = False

    async def request(self, action, **args):
        response = await super().request(action, **args)
        if action != "revision-model" or self.calls[-1][0] != "ledger":
            return response
        _, data, _ = self.calls[-1]
        source = data["issueEvidence"]["R001"][0]
        assert source["chapter"] == 2 and source["quote"] == OLD.rstrip("。")
        assert OLD in source["context"]
        result = json.loads(response["text"])
        result["decisions"][0]["evidence"] = [
            {"sourceId": source["id"]} if self.corrected else {"chapter": 2, "quote": "原稿中并不存在的句子"}
        ]
        return {"text": json.dumps(result, ensure_ascii=False)}


class EditorialHandoffWorker(Worker):
    def __init__(self, manuscript):
        super().__init__(manuscript)
        manuscript.report["rewritePlan"] = ["确认大纲和需要保留的原有剧情。"]

    async def request(self, action, **args):
        response = await super().request(action, **args)
        if action != "revision-model":
            return response
        role, data, _ = self.calls[-1]
        value = json.loads(response["text"])
        if role == "ledger":
            value["decisions"].append({
                "id": "P001", "decision": "adapt", "reason": "区分大纲问题与正文问题。",
                "chapters": [4], "evidence": [{"chapter": 4, "quote": "原稿在这里平稳进行。"}],
            })
        if role == "editor" and "P001" in data["requiredIssues"]:
            value["coverage"].append({"id": "P001", "outcome": "already_met", "reason": "编辑认为这一章不涉及大纲问题。"})
            value["patches"].append({
                "chapter": 4, "field": "content", "mode": "exact", "before": "情节接续自然。", "after": "她提灯过桥。",
                "issueIds": [], "reason": "去掉冗余概述。",
            })
        if role == "verifier" and any(item["id"] == "P001" for item in data["issues"]):
            assert data["editorCoverage"] == [{
                "id": "P001", "reason": "编辑认为这一章不涉及大纲问题。",
                "note": "编辑认为无需修改，缺少可核验引文，请独立复核。",
            }]
            value["outcomes"] = [{"id": "P001", "status": "open", "reason": "仍需核对原始大纲。"}]
        return {"text": json.dumps(value, ensure_ascii=False)}


class V2Tests(unittest.IsolatedAsyncioTestCase):
    async def start(self, manuscript, worker, scenario):
        with TemporaryDirectory(ignore_cleanup_errors=True) as temp:
            path = Path(temp)
            async with AsyncSqliteSaver.from_conn_string(str(path / "graph.sqlite3")) as saver:
                workflow = RevisionWorkflowV2(path, manuscript, worker, saver)
                self.workflow = workflow
                try:
                    record = await workflow.start("story", {"concurrency": 1, "volumeSize": 5})
                    self.run = record["id"]
                    await scenario(path, saver)
                finally:
                    await workflow.close()

    async def settled(self, workflow=None):
        service = workflow or self.workflow
        if self.run in service.tasks:
            await asyncio.wait_for(asyncio.shield(service.tasks[self.run]), 10)
        return service.store.get(self.run)

    async def test_zero_applied_batch_is_saved_and_candidate_still_requires_review(self):
        manuscript = Manuscript()
        worker = Worker(manuscript)
        worker.break_unit = True

        async def scenario(_, __):
            wf = self.workflow
            record = await self.settled()
            self.assertEqual(record["status"], "needs_review", record)
            calls = [data["editableChapters"] for role, data, _ in worker.calls if role == "editor"]
            self.assertEqual(calls.count([4, 5]), 3)
            self.assertIn([11, 12], calls)
            receipt = wf.load(self.run, "v2-edit-0-0", "2")
            self.assertTrue(receipt["validationFallback"])
            self.assertEqual(receipt["result"]["changed"], [])
            result = wf.load(self.run, "v2-result", record["candidateTurn"])
            self.assertEqual(result["validationPending"][0]["chapter"], 4)
            self.assertIn("容错待复核", wf.export("story", self.run, "report")[1])
            with self.assertRaises(WorkerError):
                await wf.apply("story", self.run, record)
            self.assertEqual(worker.applied, [])

            worker.break_unit = False
            await wf.action("story", self.run, "repair", {"chapters": [8], "note": "核查第八章。"})
            record = await self.settled()
            self.assertEqual(record["status"], "needs_review")
            later = wf.load(self.run, "v2-result", record["candidateTurn"])
            self.assertEqual([item["chapter"] for item in later["validationPending"]], [4])
        await self.start(manuscript, worker, scenario)

    async def test_missing_soft_outcome_persists_open_and_strict_mode_retries_only_that_receipt(self):
        manuscript = Manuscript()
        opening = "开门时一阵风经过。"
        manuscript.report["aiFlavorIssues"].append({
            "chapterNumber": 2, "quote": opening, "category": "voice", "priority": "P1", "impact": "开场口吻",
        })

        class OmissionWorker(Worker):
            async def request(self, action, **args):
                response = await super().request(action, **args)
                if action != "revision-model":
                    return response
                role, data, _ = self.calls[-1]
                result = json.loads(response["text"])
                if role == "ledger":
                    result["decisions"].append({"id": "R003", "decision": "adapt", "reason": "核对口吻",
                                                "chapters": [2], "evidence": [{"chapter": 2, "quote": opening}]})
                elif role == "editor" and "R003" in data["requiredIssues"]:
                    result["coverage"].append({"id": "R003", "outcome": "already_met", "reason": "保留开场口吻",
                                               "chapter": 2, "quote": opening})
                elif role == "verifier":
                    result["outcomes"] = [row for row in result["outcomes"] if row["id"] != "R003"]
                return {"text": json.dumps(result, ensure_ascii=False)}

        worker = OmissionWorker(manuscript)

        async def scenario(_, __):
            wf = self.workflow
            record = await self.settled()
            self.assertEqual(record["status"], "needs_review", record)
            result = wf.load(self.run, "v2-result", record["candidateTurn"])
            self.assertEqual([row["id"] for row in result["validationPending"]], ["R003"])
            outcome = next(row for row in result["outcomes"] if row["id"] == "R003")
            self.assertEqual(outcome["status"], "open")
            batches = [data for role, data, _ in worker.calls if role == "verifier"]
            self.assertEqual(sum(data["numbers"] == [1, 2, 3] for data in batches), 3)
            normal = wf.load(self.run, "v2-soft", "batch:2")
            entries = [("batch:1", batches[0]), ("batch:2", next(data for data in batches if data["numbers"] == [4, 5]))]
            execute = AsyncMock(return_value={"outcomes": [], "findings": []})
            with patch.dict("os.environ", {"NOVEL_FORGE_REVISION_SOFT_FALLBACK": "0"}):
                await wf.units(self.run, "review", "v2-soft", entries, execute)
            self.assertEqual([call.args[0] for call in execute.await_args_list], ["batch:1"])
            self.assertEqual(wf.load(self.run, "v2-soft", "batch:2"), normal)
        await self.start(manuscript, worker, scenario)

    async def test_content_retry_budget_does_not_defer_invalid_json_transport_or_strict_mode(self):
        manuscript = Manuscript()
        worker = Worker(manuscript)

        async def scenario(_, __):
            wf = self.workflow
            await self.settled()
            verify = Mock(side_effect=RevisionContentError("本批精确校验失败"))
            fallback = Mock(return_value={"deferred": True})
            for replies, error, expected in (
                ([{"text": "{"}] * 3, RevisionContentError, 3),
                ([WorkerError("502 EOF", 502)], WorkerError, 1),
                ([{"text": "{}"}] * 3, RevisionContentError, 3),
            ):
                request = AsyncMock(side_effect=replies)
                setting = "0" if replies[0] == {"text": "{}"} else "1"
                with patch.object(worker, "request", request), patch.dict("os.environ", {
                    "NOVEL_FORGE_REVISION_SOFT_FALLBACK": setting,
                }), self.assertRaises(error):
                    await wf.model_json(self.run, "editor", "检验", {"fixture": True}, verify, fallback_on_final=fallback)
                self.assertEqual(request.await_count, expected)
            fallback.assert_not_called()
            request = AsyncMock(return_value={"text": "{}"})
            with patch.object(worker, "request", request), patch.dict("os.environ", {
                "NOVEL_FORGE_REVISION_SOFT_FALLBACK": "1",
            }), self.assertLogs("novel_forge.revision", level="WARNING"):
                self.assertEqual(await wf.model_json(self.run, "editor", "检验", {"fixture": True}, verify,
                                                     fallback_on_final=fallback), {"deferred": True})
            self.assertEqual(request.await_count, 3)
            self.assertIn("本批精确校验失败", request.await_args.kwargs["payload"]["prompt"])
            fallback.assert_called_once()
        await self.start(manuscript, worker, scenario)

    async def test_author_decision_failed_batch_resume_and_hotfix(self):
        manuscript = Manuscript()
        worker = Worker(manuscript)
        worker.wait_author = True
        worker.break_unit = True

        async def scenario(path, saver):
            wf = self.workflow
            record = await self.settled()
            self.assertEqual(record["status"], "waiting_author", record)
            self.assertFalse(any(role == "editor" for role, *_ in worker.calls))
            self.assertEqual(record["pendingDecisions"][0]["id"], "R001")
            reader = RevisionWorkflowV2(path, manuscript, worker, saver)
            try:
                with patch.dict("os.environ", {"NOVEL_FORGE_REVISION_SOFT_FALLBACK": "0"}):
                    await reader.action("story", self.run, "decide", {"decisions": {"R001": "六十万元"}})
                    record = await self.settled(reader)
                self.assertEqual(record["status"], "error", record)
                self.assertEqual(record["done"], 1)
                self.assertIn("锚点未唯一命中", record["error"])
                await reader.action("story", self.run, "resume", {"editor": {"provider": "openai", "model": "new-editor"}})
                record = await self.settled(reader)
                self.assertEqual(record["status"], "ready", record)
                self.assertEqual(record["reviewScope"], "full")
                self.assertEqual(sum(data["editableChapters"] == [1, 2, 3] for role, data, _ in worker.calls if role == "editor"), 1)
                self.assertIn(NEW, reader.export("story", self.run, "candidate")[1])
                decisions = json.loads(reader.export("story", self.run, "decisions")[1])
                self.assertEqual(decisions["data"][1]["decision"], "skip")
                worker.calls.clear()
                await reader.action("story", self.run, "repair", {
                    "chapters": [8], "note": "换成终端芯片", "amendment": {"before": OLD_TOOL, "after": NEW_TOOL, "evidence": "作者新口径"},
                })
                record = await self.settled(reader)
                self.assertEqual(record["status"], "ready", record)
                self.assertEqual(record["ledgerVersion"], 2)
                self.assertEqual(record["reviewScope"], "incremental")
                self.assertEqual([data["editableChapters"] for role, data, _ in worker.calls if role == "editor"], [[8]])
                self.assertLess(sum(role == "verifier" for role, *_ in worker.calls), 5)
                self.assertIn(OLD_TOOL, reader.load(self.run, "v2-result", 0)["candidate"][7]["content"])
                self.assertFalse(worker.applied)
                await reader.action("story", self.run, "apply", {})
                self.assertIn(NEW_TOOL, worker.applied[0]["changes"][-1]["content"])
                self.assertEqual(wf.store.get(self.run)["status"], "applied")
            finally:
                await reader.close()
        await self.start(manuscript, worker, scenario)

    async def test_missing_source_and_hard_gate(self):
        manuscript = Manuscript(damaged=True)
        worker = Worker(manuscript)

        async def scenario(_, __):
            wf = self.workflow
            record = await self.settled()
            self.assertEqual(record["status"], "waiting_source", record)
            self.assertEqual(record["blockedChapters"], [12])
            self.assertTrue(any(role == "editor" for role, *_ in worker.calls))
            self.assertFalse(any(role in ("verifier", "final") for role, *_ in worker.calls))
            self.assertFalse(any(12 in data["editableChapters"] for role, data, _ in worker.calls if role == "editor"))
            await wf.action("story", self.run, "restore", {"chapter": 12, "field": "content", "text": "可靠备份里保留的这段完整正文。", "evidence": "作者的原稿备份"})
            record = await self.settled()
            self.assertEqual(record["status"], "ready", record)
            self.assertIn("可靠备份", wf.export("story", self.run, "candidate")[1])
            self.assertFalse(worker.applied)
        await self.start(manuscript, worker, scenario)

    async def test_hard_failure_limits_automatic_repairs(self):
        manuscript = Manuscript()
        worker = Worker(manuscript)
        worker.hold_debt = True

        async def scenario(_, __):
            record = await self.settled()
            self.assertEqual(record["status"], "needs_review", record)
            self.assertEqual(record["work"]["attempt"], 2)
            self.assertFalse(any(role in ("verifier", "final") for role, *_ in worker.calls))
            worker.hold_debt = False
            await self.workflow.action("story", self.run, "repair", {"chapters": [2], "note": "校正账本金额"})
            record = await self.settled()
            self.assertEqual(record["status"], "ready", record)
            self.assertEqual(record["work"]["attempt"], 0)
        await self.start(manuscript, worker, scenario)

    async def test_semantic_discovery_can_amend_ledger(self):
        manuscript = Manuscript()
        worker = LocalLedgerWorker(manuscript)

        async def scenario(_, __):
            wf = self.workflow
            await asyncio.wait_for(worker.delta_started.wait(), 8)
            initial = wf.store.get(self.run)
            self.assertEqual(initial["ledgerVersion"], 1)
            old = wf.v2_ledger(self.run, initial)
            drafts = wf.draft(self.run)
            bucket = "v2-edit-0-0"
            units = wf.load(self.run, "v2-map")["edit"]
            receipts = {unit["id"]: wf.load(self.run, bucket, unit["id"]) for unit in units}
            soft_receipt = wf.load(self.run, "v2-soft", "batch:1")
            self.assertIsNotNone(soft_receipt)
            old_coverage = wf.load(self.run, "v2-coverage", bucket)
            self.assertIn("R001", old_coverage)
            self.assertEqual(initial["editProgress"], {"done": 5, "total": 5})

            worker.allow_delta.set()
            await asyncio.wait_for(worker.edit_started.wait(), 8)
            progress = wf.store.get(self.run)
            self.assertEqual(progress["stage"], "edit")
            self.assertEqual(progress["editProgress"], {"done": 0, "total": 1})
            self.assertEqual(progress["editProgress"], {key: progress[key] for key in ("done", "total")})
            worker.allow_edit.set()
            record = await self.settled()
            self.assertEqual(record["status"], "ready", record)
            self.assertEqual(record["ledgerVersion"], 2)
            self.assertEqual(record["affectedChapters"], [8])
            self.assertEqual(record["editProgress"], {"done": 1, "total": 1})
            ledger_calls = [data for role, data, _ in worker.calls if role == "ledger"]
            self.assertEqual(len(ledger_calls), 2)
            self.assertEqual(ledger_calls[-1]["ledgerRequest"]["phase"], "semantic_delta")
            self.assertEqual(len(ledger_calls[-1]["issues"]), 1)
            self.assertTrue(ledger_calls[-1]["issues"][0]["id"].startswith("X-"))
            self.assertEqual(ledger_calls[-1]["affectedChapters"], [8])
            updated = wf.v2_ledger(self.run, record)
            self.assertEqual(updated["decisions"][:len(old["decisions"])], old["decisions"])
            for number in drafts.keys() - {8}:
                self.assertEqual(scope(old, [number]), scope(updated, [number]))
                self.assertEqual(drafts[number], wf.draft(self.run)[number])
            self.assertNotEqual(scope(old, [8]), scope(updated, [8]))
            for slot, receipt in receipts.items():
                self.assertIsNotNone(receipt)
                self.assertEqual(wf.load(self.run, bucket, slot), receipt)
            edited_unit = next(unit["id"] for unit in units if 8 in unit["chapters"])
            self.assertIsNotNone(wf.load(self.run, bucket, edited_unit + ":ledger-2"))
            self.assertEqual(wf.load(self.run, "v2-coverage", bucket)["R001"], old_coverage["R001"])
            self.assertEqual(wf.load(self.run, "v2-soft", "batch:1"), soft_receipt)
            self.assertEqual(sum(data["numbers"] == [1, 2, 3] for role, data, _ in worker.calls if role == "verifier"), 1)
            editor_requests = [data["editableChapters"] for role, data, _ in worker.calls if role == "editor"]
            self.assertEqual(editor_requests[-1], [8])
            self.assertEqual(len(editor_requests), 6)
            updated_result = wf.load(self.run, "v2-result", 0)
            self.assertEqual({entry["chapter"] for entry in updated_result["changes"]}, {2, 8})
            self.assertIn(NEW_TOOL, wf.export("story", self.run, "candidate")[1])
        with self.assertLogs("novel_forge", level="INFO") as logs:
            await self.start(manuscript, worker, scenario)
        self.assertTrue(any("revision_stage_start" in line and "stage=edit_v2" in line for line in logs.output))
        self.assertTrue(any("revision_stage_end" in line and "ledgerVersion=2" in line and "autoAmendments=1" in line
                            for line in logs.output))
        self.assertTrue(any("revision_model_ok" in line and "elapsedMs=" in line for line in logs.output))

    async def test_verified_p1_repaired_and_advisories_exported(self):
        manuscript = Manuscript()
        worker = ReviewWorker(manuscript)

        async def scenario(_, __):
            wf = self.workflow
            record = await self.settled()
            self.assertEqual(record["status"], "ready", record)
            self.assertEqual(record["remainingIssueCounts"], {"P0": 0, "P1": 1, "P2": 1})
            self.assertEqual(record["affectedChapters"], [5, 6])
            editors = [data["editableChapters"] for role, data, _ in worker.calls if role == "editor" and data["feedback"]]
            self.assertEqual(editors, [[6]])
            self.assertTrue(any(5 in data["numbers"] and data["repairLinks"]
                                for role, data, _ in worker.calls if role == "verifier"))
            self.assertIn(LINK_FIXED, wf.export("story", self.run, "candidate")[1])
            report = wf.export("story", self.run, "report")[1]
            for text in ("自动返修复核", "修后引文", "P1 共 1 项", "P2 共 1 项", "可换一种开场语气"):
                self.assertIn(text, report)
        await self.start(manuscript, worker, scenario)

    async def test_unverified_completion_and_resume(self):
        manuscript = Manuscript()
        worker = ReviewWorker(manuscript, unchanged=True, omit_check=True)

        async def scenario(_, __):
            wf = self.workflow
            record = await self.settled()
            self.assertEqual(record["status"], "error", record)
            self.assertIn("repairChecks", record["error"])
            worker.omit_check = False
            await wf.action("story", self.run, "resume", {})
            record = await self.settled()
            self.assertEqual(record["status"], "ready", record)
            self.assertEqual(record["work"]["attempt"], 2)
            self.assertEqual(record["remainingIssueCounts"], {"P0": 0, "P1": 2, "P2": 1})
            targets = [data["editableChapters"] for role, data, _ in worker.calls if role == "editor" and data["feedback"]]
            self.assertEqual(targets, [[6], [6]])
            report = wf.export("story", self.run, "report")[1]
            self.assertIn("方位与航标不一致", report)
            self.assertNotIn("修后引文", report)
        await self.start(manuscript, worker, scenario)

    async def test_new_hard_failure_preserves_advisories(self):
        manuscript = Manuscript()
        worker = ReviewWorker(manuscript, inject_hard_failure=True)

        async def scenario(_, __):
            record = await self.settled()
            self.assertEqual(record["status"], "needs_review", record)
            self.assertEqual(record["remainingIssueCounts"], {"P0": 1, "P1": 2, "P2": 1})
            report = self.workflow.export("story", self.run, "report")[1]
            self.assertIn("来自上一轮，当前候选稿尚待语义复核", report)
            self.assertIn("方位与航标不一致", report)
            self.assertNotIn("修后引文", report)
        await self.start(manuscript, worker, scenario)

    async def test_book_search_requires_chapter_coverage(self):
        manuscript = Manuscript()
        worker = CoverageWorker(manuscript)

        async def scenario(_, __):
            wf = self.workflow
            record = await self.settled()
            self.assertEqual(record["status"], "needs_review", record)
            decision = next(item for item in wf.v2_ledger(self.run, record)["decisions"] if item["id"] == "R003")
            self.assertEqual(decision["matchedChapters"], [1, 4, 9, 10])
            self.assertEqual(record["remainingIssueCounts"]["P0"], 1)
            result = wf.load(self.run, "v2-result", 0)
            outcome = next(item for item in result["outcomes"] if item["id"] == "R003")
            self.assertEqual(outcome["status"], "open")
            self.assertEqual({item["chapter"]: item["status"] for item in outcome["chapterOutcomes"]},
                             {1: "fixed", 4: "fixed", 9: "open", 10: "not_applicable"})
            self.assertIn(PORT_CLOSED, result["candidate"][9]["content"])
            self.assertEqual([data["editableChapters"] for role, data, _ in worker.calls if role == "editor" and data["feedback"]],
                             [[9], [9]])
        await self.start(manuscript, worker, scenario)

    async def test_pause_cancels_unfinished_batch_and_resume_reuses_receipt(self):
        manuscript = Manuscript()
        worker = PausingWorker(manuscript)

        async def scenario(_, __):
            wf = self.workflow
            await asyncio.wait_for(worker.waiting.wait(), 8)
            paused = await asyncio.wait_for(wf.action("story", self.run, "pause", {}), 3)
            self.assertEqual(paused["status"], "paused", paused)
            self.assertEqual(paused["done"], 1)
            self.assertFalse(paused["pauseRequested"])
            self.assertTrue(worker.cancelled.is_set())
            self.assertEqual((await wf.action("story", self.run, "pause", {}))["status"], "paused")
            worker.should_wait = False
            await wf.action("story", self.run, "resume", {})
            self.assertEqual((await self.settled())["status"], "ready")
            audit_starts = [data["original"][0]["chapterNumber"] for role, data, _ in worker.calls if role == "audit"]
            self.assertEqual(audit_starts, [1, 6, 6, 11])
        await self.start(manuscript, worker, scenario)

    async def test_semantic_retry_names_bad_ids_and_resume_reuses_successful_receipt(self):
        manuscript = Manuscript()
        worker = SemanticRetryWorker(manuscript)

        async def scenario(_, __):
            wf = self.workflow
            record = await self.settled()
            self.assertEqual(record["status"], "error", record)
            self.assertEqual(record["done"], 1)
            self.assertIn("4、5 章：语义审校遗漏裁决项。多余/未识别 ID：R999。", record["error"])
            first_batch = [(correction, budget) for numbers, correction, budget in worker.semantic_calls
                           if numbers == [1, 2, 3]]
            self.assertEqual(len(first_batch), 2)
            self.assertEqual(first_batch[0], ("", DEFAULT_MAX_TOKENS))
            self.assertIn("缺失 ID：R001", first_batch[1][0])
            self.assertIn("多余/未识别 ID：R999", first_batch[1][0])
            self.assertEqual(first_batch[1][1], DEFAULT_MAX_TOKENS)
            receipt = wf.load(self.run, "v2-soft", "batch:1")
            self.assertEqual([item["id"] for item in receipt["result"]["outcomes"]], ["R001"])

            worker.hold_second_batch = False
            worker.semantic_calls.clear()
            await wf.action("story", self.run, "resume", {})
            record = await self.settled()
            self.assertEqual(record["status"], "ready", record)
            self.assertEqual(wf.load(self.run, "v2-soft", "batch:1"), receipt)
            self.assertNotIn([1, 2, 3], [numbers for numbers, *_ in worker.semantic_calls])
            self.assertEqual(worker.semantic_calls[0][0], [4, 5])
        await self.start(manuscript, worker, scenario)

    async def test_ledger_can_use_located_evidence_on_resume(self):
        manuscript = Manuscript()
        worker = EvidenceWorker(manuscript)

        async def scenario(_, __):
            wf = self.workflow
            record = await self.settled()
            self.assertEqual(record["status"], "error", record)
            self.assertIn("R001-Q1", record["error"])
            self.assertEqual(sum(role == "audit" for role, *_ in worker.calls), 3)
            worker.corrected = True
            await wf.action("story", self.run, "resume", {})
            record = await self.settled()
            self.assertEqual(record["status"], "ready", record)
            self.assertEqual(sum(role == "audit" for role, *_ in worker.calls), 3)
            decision = wf.v2_ledger(self.run, record)["decisions"][0]
            self.assertEqual(decision["evidence"], [{"chapter": 2, "quote": OLD.rstrip("。")}])
        await self.start(manuscript, worker, scenario)

    async def test_ledger_batch_threshold_preserves_the_single_call_path(self):
        for issue_count, threshold, sizes in ((30, "30", []), (31, "30", [25, 6]), (31, "0", [])):
            with self.subTest(issue_count=issue_count, threshold=threshold), patch.dict(
                    "os.environ", {"NOVEL_FORGE_LEDGER_BATCH_THRESHOLD": threshold}):
                manuscript = Manuscript()
                worker = BatchedLedgerWorker(manuscript, issue_count)

                async def scenario(_, __):
                    record = await self.settled()
                    self.assertEqual(record["status"], "ready", record)
                    calls = worker.ledger_calls
                    if sizes:
                        self.assertEqual(calls[0][0]["ledgerRequest"]["phase"], "core")
                        self.assertEqual([len(data["issues"]) for data, *_ in calls[1:]], sizes)
                        self.assertTrue(all(budget == DEFAULT_MAX_TOKENS for _, _, budget in calls))
                    else:
                        self.assertEqual(len(calls), 1)
                        data, correction, budget = calls[0]
                        self.assertEqual(len(data["issues"]), issue_count)
                        self.assertNotIn("ledgerRequest", data)
                        self.assertEqual((correction, budget), ("", DEFAULT_MAX_TOKENS))
                    ledger = self.workflow.v2_ledger(self.run, record)
                    self.assertEqual(len(ledger["decisions"]), issue_count)
                    self.assertEqual(set(ledger), {"rules", "checks", "financialFlows", "unresolved",
                                                  "blueprint", "decisions", "pending", "authorAmendments"})

                await self.start(manuscript, worker, scenario)

    async def test_large_ledger_resumes_failed_batch_and_keeps_both_receipt_types(self):
        manuscript = Manuscript()
        worker = BatchedLedgerWorker(manuscript, 135, fail_batch=3, incorrect_batch=2)

        async def scenario(path, saver):
            wf = self.workflow
            record = await self.settled()
            self.assertEqual(record["status"], "error", record)
            self.assertEqual((record["stage"], record["done"], record["total"]), ("ledger", 3, 7))
            self.assertIn("502 unexpected EOF", record["error"])
            audit = wf.load(self.run, "v2-audit-summary")
            self.assertEqual(len(audit["issues"]), 135)
            core = wf.load(self.run, "v2-ledger-core")
            accepted = [wf.load(self.run, "v2-ledger-decisions", f"decisions:{i}") for i in (1, 2)]
            self.assertTrue(all(accepted))
            self.assertIsNone(wf.load(self.run, "v2-ledger-decisions", "decisions:3"))
            batch_two = [(data, correction) for data, correction, _ in worker.ledger_calls
                         if data["ledgerRequest"].get("batch") == 2]
            self.assertEqual(len(batch_two), 2)
            self.assertEqual(batch_two[0][0], batch_two[1][0])
            context, correction = batch_two[1]
            self.assertIn("缺失 ID：" + context["issues"][0]["id"], correction)
            self.assertIn("多余 ID：UNKNOWN", correction)
            self.assertIn("重复 ID：" + context["issues"][-1]["id"], correction)
            self.assertEqual(wf.store.count(self.run, "ledger-draft"), 1)

            worker.fail_batch = None
            worker.calls.clear()
            worker.ledger_calls.clear()
            reader = RevisionWorkflowV2(path, manuscript, worker, saver)
            try:
                await reader.action("story", self.run, "resume", {"verifier": {"provider": "openai", "model": "fixture-next"}})
                record = await self.settled(reader)
                self.assertEqual(record["status"], "ready", record)
                self.assertEqual(reader.load(self.run, "v2-audit-summary"), audit)
                self.assertFalse(any(role == "audit" for role, *_ in worker.calls))
                self.assertEqual(reader.load(self.run, "v2-ledger-core"), core)
                self.assertEqual([reader.load(self.run, "v2-ledger-decisions", f"decisions:{i}") for i in (1, 2)], accepted)
                self.assertEqual([(data["ledgerRequest"]["batch"], len(data["issues"]), correction)
                                  for data, correction, _ in worker.ledger_calls],
                                 [(3, 25, ""), (4, 25, ""), (5, 25, ""), (6, 10, "")])
                ledger = reader.v2_ledger(self.run, record)
                self.assertEqual([entry["id"] for entry in ledger["decisions"]], [entry["id"] for entry in audit["issues"]])
                cross_batch = next(entry for entry in ledger["decisions"] if entry["id"] == "P026")
                self.assertEqual(cross_batch["evidence"], [{"chapter": 2, "quote": OLD}])
                self.assertEqual(ledger["financialFlows"][0]["entries"][0]["balanceCents"], 150)
            finally:
                await reader.close()

        with patch.dict("os.environ", {"NOVEL_FORGE_LEDGER_BATCH_THRESHOLD": "30"}):
            await self.start(manuscript, worker, scenario)

    async def test_batched_ledger_revalidates_after_author_answer_and_amendment(self):
        manuscript = Manuscript()
        worker = BatchedLedgerWorker(manuscript, 31)
        worker.wait_author = True

        async def scenario(_, __):
            wf = self.workflow
            record = await self.settled()
            self.assertEqual(record["status"], "waiting_author", record)
            self.assertFalse(record.get("ledgerVersion"))
            self.assertEqual(record["pendingDecisions"][0]["id"], "R001")
            before = wf.load(self.run, "v2-ledger-core")["inputHash"]

            worker.ledger_calls.clear()
            await wf.action("story", self.run, "decide", {"decisions": {"R001": "六十万元"}})
            record = await self.settled()
            self.assertEqual(record["status"], "ready", record)
            self.assertNotEqual(wf.load(self.run, "v2-ledger-core")["inputHash"], before)
            self.assertEqual(len(worker.ledger_calls), 3)
            self.assertTrue(all(data["authorAnswers"] == {"R001": "六十万元"} for data, *_ in worker.ledger_calls))
            old_ledger = wf.v2_ledger(self.run, record)

            worker.ledger_calls.clear()
            await wf.action("story", self.run, "repair", {
                "chapters": [8], "note": "更正道具", "amendment": {"before": OLD_TOOL, "after": NEW_TOOL, "evidence": "作者口径"},
            })
            record = await self.settled()
            self.assertEqual(record["status"], "ready", record)
            self.assertEqual(record["ledgerVersion"], 2)
            self.assertEqual(worker.ledger_calls[0][0]["oldLedger"], old_ledger)
            self.assertTrue(all(data["requestedChange"]["after"] == NEW_TOOL for data, *_ in worker.ledger_calls))
            self.assertEqual(worker.ledger_calls[1][0]["oldDecisions"], old_ledger["decisions"][:25])
            self.assertEqual(wf.load(self.run, "v2-ledger", 1), old_ledger)
            self.assertIn(NEW_TOOL, wf.export("story", self.run, "candidate")[1])

        with patch.dict("os.environ", {"NOVEL_FORGE_LEDGER_BATCH_THRESHOLD": "30"}):
            await self.start(manuscript, worker, scenario)

    async def test_batched_ledger_revalidates_after_source_restore(self):
        manuscript = Manuscript(damaged=True)
        worker = BatchedLedgerWorker(manuscript, 31)

        async def scenario(_, __):
            wf = self.workflow
            record = await self.settled()
            self.assertEqual(record["status"], "waiting_source", record)
            before = [wf.load(self.run, bucket, slot)["inputHash"] for bucket, slot in
                      (("v2-ledger-core", "all"), ("v2-ledger-decisions", "decisions:1"))]
            worker.ledger_calls.clear()
            await wf.action("story", self.run, "restore", {
                "chapter": 12, "field": "content", "text": "旧稿在这里继续。", "evidence": "作者确认的备份。",
            })
            record = await self.settled()
            self.assertEqual(record["status"], "ready", record)
            self.assertEqual(len(worker.ledger_calls), 3)
            after = [wf.load(self.run, bucket, slot)["inputHash"] for bucket, slot in
                     (("v2-ledger-core", "all"), ("v2-ledger-decisions", "decisions:1"))]
            self.assertTrue(all(old != new for old, new in zip(before, after)))

        with patch.dict("os.environ", {"NOVEL_FORGE_LEDGER_BATCH_THRESHOLD": "30"}):
            await self.start(manuscript, worker, scenario)

    async def test_unsupported_no_change_opinion_is_sent_to_final_review(self):
        manuscript = Manuscript()
        worker = EditorialHandoffWorker(manuscript)

        async def scenario(_, __):
            wf = self.workflow
            record = await self.settled()
            self.assertEqual(record["status"], "ready", record)
            self.assertEqual(record["remainingIssueCounts"]["P1"], 1)
            editor_calls = [data for role, data, _ in worker.calls if role == "editor" and "P001" in data["requiredIssues"]]
            self.assertEqual(len(editor_calls), 1)
            self.assertIn("她提灯过桥", wf.export("story", self.run, "candidate")[1])
            self.assertIn("仍需核对原始大纲", wf.export("story", self.run, "report")[1])
        await self.start(manuscript, worker, scenario)

    async def test_soft_v2_verifier_uses_readonly_tools_expands_visible_basis_and_preserves_cache_prefix(self):
        manuscript = Manuscript()
        # Chapter 1 is outside batch 3 ([7, 8, 9]) and not its neighbor; put an anchor fact in Ch 1.
        manuscript.rows[0]["content"] = "第一章写明青铜钥匙藏在灯塔顶层。" + manuscript.rows[0]["content"]
        manuscript.rows[7]["content"] = "第八章误写成青铜钥匙在地下室。" + manuscript.rows[7]["content"]

        class NativeVerifierWorker(Worker):
            supports_native_tools = True

            def __init__(self, m):
                super().__init__(m)
                self.verifier_tool_events = []
                self.prompt_key_orders = {}

            async def request(self, action, **args):
                if action != "revision-model":
                    return await super().request(action, **args)
                body = args["payload"]
                role = body["role"]
                if body.get("messages"):
                    msgs = body["messages"]
                    data = json.JSONDecoder().raw_decode(msgs[0]["content"])[0]
                    self.prompt_key_orders.setdefault(role, list(data.keys()))
                    tool_msgs = [m for m in msgs if m.get("role") == "tool"]
                    if role == "verifier" and 8 in data["numbers"] and not data.get("repairChecks") and not tool_msgs:
                        self.verifier_tool_events.append(("requested_tools", [t["name"] for t in body["tools"]]))
                        return {
                            "text": "",
                            "toolCalls": [{"id": "vc_1", "name": "read_chapter",
                                           "arguments": {"version": "candidate", "chapter": 1}}],
                            "assistantMessage": {
                                "role": "assistant", "content": "",
                                "toolCalls": [{"id": "vc_1", "name": "read_chapter",
                                               "arguments": {"version": "candidate", "chapter": 1}}],
                            },
                        }
                    if role == "verifier" and 8 in data["numbers"] and not data.get("repairChecks") and tool_msgs:
                        self.verifier_tool_events.append(("received_tool_output", tool_msgs[0]["content"]))
                        # Cite Chapter 1 as repair basis (only valid if read_chapter expanded `visible` with {1}!)
                        return {"text": json.dumps({
                            "outcomes": [{"id": item["id"], "status": "not_applicable", "reason": "无关"}
                                         for item in data["issues"]],
                            "findings": [{
                                "chapter": 8, "field": "content", "quote": "第八章误写成青铜钥匙在地下室。",
                                "priority": "P0", "problem": "青铜钥匙位置与第一章灯塔顶层冲突。",
                                "fix": "将地下室改为灯塔顶层。", "requiresDecision": False,
                                "affectedChapters": [8, 1],
                                "basis": [{"chapter": 1, "field": "content", "quote": "第一章写明青铜钥匙藏在灯塔顶层。"}],
                            }],
                        }, ensure_ascii=False)}
                    if role == "verifier" and data.get("repairChecks"):
                        return {"text": json.dumps({
                            "outcomes": [{"id": item["id"], "status": "not_applicable", "reason": "无关"}
                                         for item in data["issues"]],
                            "findings": [],
                            "repairChecks": [{"id": item["repair"]["id"], "status": "open", "reason": "需人工复核"}
                                             for item in data["repairChecks"]],
                        }, ensure_ascii=False)}
                    # Otherwise run default Worker response using msgs[0]["content"]
                    patched = {**body, "prompt": msgs[0]["content"]}
                    return await super().request(action, **{**args, "payload": patched})
                data = json.JSONDecoder().raw_decode(body["prompt"])[0]
                self.prompt_key_orders.setdefault(role, list(data.keys()))
                return await super().request(action, **args)

        worker = NativeVerifierWorker(manuscript)

        async def scenario(_, __):
            wf = self.workflow
            record = await self.settled()
            self.assertIn(record["status"], ("ready", "needs_review"), record)
            # Confirm verifier used read_chapter on Chapter 1 and received its actual text
            self.assertEqual(worker.verifier_tool_events[0][0], "requested_tools")
            self.assertIn("read_chapter", worker.verifier_tool_events[0][1])
            self.assertIn("青铜钥匙藏在灯塔顶层", worker.verifier_tool_events[1][1])
            # Confirm the cross-chapter basis (chapter 1) passed verified_repair and drove automatic repair + recheck!
            repaired_items = record["work"].get("repairChecks") or []
            self.assertTrue(repaired_items)
            self.assertEqual(repaired_items[0]["repair"]["basis"][0]["chapter"], 1)
            # Confirm prefix-stable key order (static fields before dynamic batch fields)
            self.assertLess(worker.prompt_key_orders["editor"].index("ledger"),
                            worker.prompt_key_orders["editor"].index("chapters"))
            self.assertLess(worker.prompt_key_orders["verifier"].index("ledger"),
                            worker.prompt_key_orders["verifier"].index("candidate"))
        await self.start(manuscript, worker, scenario)

    async def test_soft_v2_verifier_falls_back_cleanly_on_tools_unsupported(self):
        manuscript = Manuscript()

        class UnsupportedToolWorker(Worker):
            supports_native_tools = True

            def __init__(self, m):
                super().__init__(m)
                self.tool_flags = []

            async def request(self, action, **args):
                if action != "revision-model":
                    return await super().request(action, **args)
                body = args["payload"]
                if body["role"] in ("verifier", "final"):
                    has_tools = bool(body.get("tools"))
                    self.tool_flags.append(has_tools)
                    if has_tools:
                        raise WorkerError("不支持原生工具", 400, "TOOLS_UNSUPPORTED")
                return await super().request(action, **args)

        worker = UnsupportedToolWorker(manuscript)

        async def scenario(_, __):
            record = await self.settled()
            self.assertEqual(record["status"], "ready", record)
            # First verifier batch hit TOOLS_UNSUPPORTED once; all subsequent batches skipped tools cleanly!
            self.assertEqual(worker.tool_flags[0], True)
            self.assertTrue(all(flag is False for flag in worker.tool_flags[1:]))
        await self.start(manuscript, worker, scenario)

    async def test_force_apply_allows_needs_review_and_archived_candidates_only_with_explicit_override(self):
        manuscript = Manuscript()
        worker = Worker(manuscript)
        worker.break_unit = True

        async def scenario(_, __):
            wf = self.workflow
            record = await self.settled()
            self.assertEqual(record["status"], "needs_review", record)
            # Without explicit force=True, both needs_review and archived reject apply.
            with self.assertRaises(WorkerError):
                await wf.action("story", self.run, "apply", {})
            archived = await wf.action("story", self.run, "archive", {})
            self.assertEqual(archived["status"], "archived")
            with self.assertRaises(WorkerError):
                await wf.action("story", self.run, "apply", {})
            # With explicit force=True author override, archived candidate applies cleanly.
            applied = await wf.action("story", self.run, "apply", {"force": True})
            self.assertEqual(applied["status"], "applied")
            self.assertEqual(len(worker.applied), 1)
            self.assertIn(NEW, worker.applied[0]["changes"][0]["content"])
        await self.start(manuscript, worker, scenario)


class V2ValidationTest(unittest.TestCase):
    def test_anchor_mapping_counts_and_nearby_text(self):
        source = "开头。铜钟敲了三下，\r\n小船离开港口。"
        start, end = unique_at(source, "铜钟敲了三下,小船离开港口。")
        self.assertEqual(source[start:end], "铜钟敲了三下，\r\n小船离开港口。")
        with self.assertRaisesRegex(RevisionContentError, "2 次命中"):
            unique_at(source + source, "铜钟敲了三下")
        with self.assertRaises(RevisionContentError) as caught:
            unique_at(source, "铜钟敲了三下，小船驶离港口。")
        self.assertIn("0 次命中", str(caught.exception))
        self.assertIn(caught.exception.details["nearby"]["quote"], source)
        with self.assertRaisesRegex(RevisionContentError, "0 次命中"):
            unique_at("船票总价是一笔100．00元的款项。", "船票总价是一笔100.00元的款项")

    def test_bad_edit_can_defer_with_no_applied_patches_but_never_apply_invalid_structure(self):
        chapters = {1: {"chapterNumber": 1, "title": "夜港", "content": "灯塔还亮着。雾从海面升起。"}}
        patch = {"chapter": 1, "field": "content", "mode": "exact", "before": "原稿没有这句",
                 "after": "灯光变暗。", "reason": "调整氛围", "issueIds": ["R001"]}
        raw = {"patches": [patch], "coverage": [{"id": "R001", "outcome": "patched", "reason": "替换氛围"}]}
        result = edit_patches(raw, chapters, {1}, ["R001"], allow_deferred=True)
        self.assertEqual((result["changed"], result["log"]), ([], []))
        self.assertEqual(result["coverage"]["R001"]["outcome"], "needs_review")
        self.assertIn(patch["before"], result["deferred"][0]["anchor"])
        second = {**patch, "before": "另一个不存在的锚点"}
        self.assertEqual(len(edit_patches({**raw, "patches": [patch, second]}, chapters, {1}, ["R001"],
                                          allow_deferred=True)["deferred"]), 2)
        for extra in ({**patch, "chapter": 2}, {**patch, "before": chapters[1]["content"], "after": "  "}):
            with self.subTest(extra=extra), self.assertRaises(RevisionContentError):
                edit_patches({**raw, "patches": [patch, extra]}, chapters, {1}, ["R001"], allow_deferred=True)
        with self.assertRaises(RevisionContentError):
            edit_patches(raw, chapters, {1}, ["R001"], allow_deferred=True, protected_ids={"R001"})
        with self.assertRaises(RevisionContentError):
            edit_patches(raw, chapters, {1}, ["R001"], allow_deferred=True, protected_chapters={1})

        overlap = [{**patch, "before": text} for text in ("灯塔还亮着", "还亮着。雾")]
        result = edit_patches({**raw, "patches": overlap}, chapters, {1}, ["R001"], allow_deferred=True)
        self.assertEqual(len(result["deferred"]), 2)
        self.assertEqual(result["changed"], [])
        valid = {**patch, "before": "雾从海面升起。", "after": "海面的雾渐浓。"}
        result = edit_patches({**raw, "patches": [patch, valid]}, chapters, {1}, ["R001"], allow_deferred=True)
        self.assertEqual(result["changed"][0]["content"], "灯塔还亮着。海面的雾渐浓。")
        self.assertEqual(result["coverage"]["R001"]["outcome"], "needs_review")
        self.assertEqual(len(result["log"]), 1)

    def test_ledger_partial_evidence_and_defer_only_ordinary_issues(self):
        content = "灯塔传来三声钟响，渔船离开港口。"
        chapters = {1: {"title": "灯塔", "content": content}}
        issues = [{"id": key, "match": "matched", "chapter": 1, "category": "voice", "priority": "P1", "quote": content}
                  for key in ("R001", "R002", "R003")]
        row = {"id": "R001", "decision": "adapt", "chapters": [1], "reason": "统一叙述口吻", "evidence": [
            {"chapter": 1, "quote": "找不到这条无关证据"}, {"chapter": 1, "quote": content.replace("，", ",")},
        ]}
        raw = {"rules": [], "financialFlows": [], "checks": [], "blueprint": [],
               "decisions": [row, {"id": "R002", "decision": "skip"}]}
        with self.assertRaisesRegex(RevisionContentError, "R003"):
            validated_plan(raw, chapters, issues, {})
        accepted = validated_plan(raw, chapters, issues, {}, allow_deferred=True)["decisions"]
        self.assertEqual(accepted[0]["evidence"], [{"chapter": 1, "quote": content}])
        self.assertIn("未提供具体理由", accepted[1]["reason"])
        self.assertEqual((accepted[2]["decision"], accepted[2]["evidence"]), ("needs_review", []))
        batch = validated_decision_batch(raw, chapters, issues, {}, {}, allow_deferred=True)
        self.assertEqual([r["decision"] for r in batch], ["adapt", "skip", "needs_review"])
        finance = [*issues[:2], {**issues[2], "category": "finance"}]
        with self.assertRaisesRegex(RevisionContentError, "R003"):
            validated_plan(raw, chapters, finance, {}, allow_deferred=True)
        bad_chapter = {**row, "evidence": [*row["evidence"], {"chapter": 9, "quote": content}]}
        with self.assertRaisesRegex(RevisionContentError, "不存在的章节"):
            validated_plan({**raw, "decisions": [bad_chapter]}, chapters, issues, {}, allow_deferred=True)
        fake = {"id": "R003", "decision": "needs_review", "reason": "可忽略"}
        with self.assertRaisesRegex(RevisionContentError, "无效结论"):
            validated_plan({**raw, "decisions": [*raw["decisions"], fake]}, chapters, issues, {}, allow_deferred=True)

    def test_semantic_only_missing_ids_can_be_deferred(self):
        known = {"R001", "R002"}
        present = {"id": "R001", "status": "open", "reason": "情绪仍需核对"}
        outcomes = validated_semantic_outcomes({"outcomes": [present]}, known, allow_missing=True)
        self.assertEqual(outcomes[0], present)
        self.assertEqual((outcomes[1]["id"], outcomes[1]["status"]), ("R002", "open"))
        for rows in ([], [present, {**present, "id": "wrong"}], [present, {**present, "status": "fixed"}]):
            with self.subTest(rows=rows), self.assertRaises(RevisionContentError):
                validated_semantic_outcomes({"outcomes": rows}, known, allow_missing=True)

    def test_blueprint_keeps_only_preserve_chapters_in_edit_scope(self):
        chapters = {n: {"title": f"第{n}章", "content": "原稿。"} for n in range(1, 101)}
        row = {"kind": "plot", "chapters": [16, 73], "preserve": [1, 15, 16, 50, 72, 90], "joins": [15, 72],
               "change": "改写两处接缝并保留第十六章的关键动作。"}
        core = validated_ledger_core({"rules": [], "checks": [], "financialFlows": [], "blueprint": [
            row, {**row, "kind": "voice", "preserve": [1, 90]},
        ]}, chapters)
        plan = validated_plan({**core, "decisions": []}, chapters, [], {})
        self.assertEqual(plan["blueprint"], [
            {**row, "preserve": [16]}, {**row, "kind": "voice", "preserve": []},
        ])

    def test_blueprint_names_row_and_invalid_required_fields(self):
        chapters = {1: {"title": "第一章", "content": "原稿。"}}
        row = {"kind": "plot", "chapters": [1], "change": "调整场景衔接。"}
        for field, value, expected in (
            ("kind", None, "kind 必须为 plot 或 voice"),
            ("kind", "finance", "kind 必须为 plot 或 voice"),
            ("chapters", None, "chapters 至少需要一个改写章号"),
            ("chapters", [], "chapters 至少需要一个改写章号"),
            ("change", None, "change 缺少具体改法"),
            ("change", " \n", "change 缺少具体改法"),
        ):
            with self.subTest(field=field, value=value):
                invalid = {key: item for key, item in row.items() if key != field}
                if value is not None:
                    invalid[field] = value
                raw = {"rules": [], "checks": [], "financialFlows": [], "decisions": [], "blueprint": [row, invalid]}
                with self.assertRaises(RevisionContentError) as caught:
                    validated_plan(raw, chapters, [], {})
                self.assertIn("剧情蓝图第 2 条", str(caught.exception))
                self.assertIn(expected, str(caught.exception))
                self.assertEqual(caught.exception.details["blueprintIndex"], 2)

    def test_blueprint_rejects_nonexistent_chapters_before_filtering_preserve(self):
        chapters = {1: {"title": "第一章", "content": "原稿。"}, 2: {"title": "第二章", "content": "原稿。"}}
        row = {"kind": "voice", "chapters": [1], "preserve": [1, 2, 3], "change": "统一说话口吻。"}
        raw = {"rules": [], "checks": [], "financialFlows": [], "decisions": [], "blueprint": [row]}
        with self.assertRaisesRegex(RevisionContentError, "本书不存在的章节"):
            validated_plan(raw, chapters, [], {})

    def test_rule_evidence_resolves_source_ids_and_keeps_valid_ellipses(self):
        quote = "灯塔旁那只铜钟正在响。"
        close = "木盒里的船票全都还在。"
        body = quote + "她转身。\n" + close
        chapters = {1: {"title": "灯塔", "content": body}, 2: {"title": "远港", "content": quote + "雾" * 161 + close}}
        sources = {"R001": [{"id": "R001-Q1", "chapter": 1, "quote": quote, "field": "content"}]}
        base = {"checks": [], "financialFlows": [], "blueprint": [], "decisions": []}
        refs = [{"sourceId": "R001-Q1"}, {"chapter": 1, "quote": quote + "..." + close},
                {"chapter": 2, "quote": quote + "……" + close}]
        rule = {"track": "continuity", "decision": "保持船票状态一致", "chapters": [1], "evidence": refs}
        raw = {**base, "rules": [rule]}
        expected = [{"chapter": 1, "quote": quote}, {"chapter": 1, "quote": body}]
        self.assertEqual(validated_plan(raw, chapters, [], {}, sources)["rules"][0]["evidence"], expected)
        self.assertEqual(validated_ledger_core(raw, chapters, sources)["rules"], [rule])
        self.assertEqual(audited_evidence({"chapter": 1, "quote": "…" + close + "…"}, chapters, {1}), expected[1] | {"quote": close})
        self.assertIsNone(audited_evidence({"chapter": 1, "quote": close + "…" + quote}, chapters, {1}))
        self.assertIsNone(audited_evidence({"chapter": 1, "quote": quote + "…" + close}, chapters, {2}))

        rejected = {"track": "plot", "decision": "正确的船票去向", "evidence": [
            {"sourceId": "R001-Q1", "chapter": 2}, {"chapter": 2, "quote": "船票被她扔进炉火。"},
        ]}
        with self.assertRaises(RevisionContentError) as caught:
            validated_plan({**base, "rules": [rule, rejected]}, chapters, [], {}, sources)
        self.assertIn("第 2 条「正确的船票去向」", str(caught.exception))
        self.assertIn("第 2 章「船票被她扔进炉火。」", str(caught.exception))
        self.assertIn("R001-Q1", str(caught.exception))
        self.assertEqual(caught.exception.details["ruleIndex"], 2)

    def test_semantic_delta_rejects_replacing_a_rule_outside_its_scope(self):
        chapters = {1: {"content": OLD, "title": "原稿"}, 2: {"content": OLD_TOOL, "title": "道具"}}
        rule = {"track": "continuity", "decision": "全书旧规则", "chapters": [],
                "evidence": [{"chapter": 1, "quote": OLD}]}
        old = validated_plan({"rules": [rule], "checks": [], "blueprint": [], "financialFlows": [], "decisions": []},
                             chapters, [], {})
        replacement = {**rule, "chapters": [2], "replaces": old["rules"][0]["id"]}
        with self.assertRaisesRegex(RevisionContentError, "本次联动范围以外"):
            merge_semantic_delta({"rules": [replacement], "checks": [], "decisions": []},
                                 old, chapters, [], {}, {}, [2])

    def test_quiet_polling_access_filter_keeps_errors_writes_and_other_routes(self):
        filter_ = QuietPollingAccessFilter()
        config = logging_config()
        self.assertIn("quiet_polling", config["loggers"]["uvicorn.access"]["filters"])
        for method, path, status, visible in (
            ("GET", "/api/jobs/job-1/revisions", 200, False),
            ("GET", "/api/jobs/job-1/editor-desk?before=2", 304, False),
            ("GET", "/api/health", 200, False),
            ("GET", "/api/jobs/job-1/revisions", 500, True),
            ("GET", "/api/health", 404, True),
            ("POST", "/api/jobs/job-1/revisions", 200, True),
            ("GET", "/api/jobs/job-1/revisions/run/export/report", 200, True),
            ("GET", "/api/jobs/job-1", 200, True),
        ):
            with self.subTest(path=path, status=status, method=method):
                record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                                           ("127.0.0.1", method, path, "1.1", status), None)
                self.assertEqual(filter_.filter(record), visible)

    def test_split_ledger_enforces_financial_balance_and_decision_evidence(self):
        manuscript = Manuscript()
        chapters = {row["chapterNumber"]: row for row in manuscript.rows}
        core = {"rules": [], "checks": [], "blueprint": [], "financialFlows": [{
            "label": "现金", "openingCents": 100, "entries": [
                {"chapter": 2, "quote": OLD, "deltaCents": 50, "balanceCents": 160},
            ],
        }]}
        with self.assertRaisesRegex(RevisionContentError, "余额不闭合"):
            validated_ledger_core(core, chapters)
        issues = [{"id": "R001", "match": "audit", "chapter": 2, "quote": OLD}]
        decisions = {"decisions": [{"id": "R001", "decision": "adapt", "reason": "统一欠款",
                                    "chapters": [2], "evidence": [{"sourceId": "FAKE-Q1"}]}]}
        with self.assertRaisesRegex(RevisionContentError, "R001 的裁决证据无法在原稿核实"):
            validated_decision_batch(decisions, chapters, issues, {}, {})

    def test_semantic_outcomes_align_chapter_aliases_and_deduplicate_same_status(self):
        known = {"R003@13", "R003@14"}
        rows = [
            {"id": "R003", "chapter": 13, "status": "fixed", "quote": "本章候选稿证据。"},
            {"id": "R003@13", "chapter": 13, "status": "fixed"},
            {"id": "R003", "chapter": "14", "status": "open"},
        ]
        result = validated_semantic_outcomes({"outcomes": rows}, known)
        self.assertEqual(result, [{**rows[0], "id": "R003@13"}, {**rows[2], "id": "R003@14"}])
        self.assertEqual(rows[0]["id"], "R003")

    def test_semantic_outcomes_reject_ambiguity_true_omissions_and_status_conflicts(self):
        known = {"R003@13", "R003@14"}
        fixed = {"id": "R003@13", "status": "fixed"}
        for ambiguous in ({"id": "R003"}, {"id": "R003", "chapter": 15}):
            with self.subTest(ambiguous=ambiguous), self.assertRaises(RevisionContentError) as caught:
                validated_semantic_outcomes({"outcomes": [fixed, ambiguous]}, known)
            self.assertEqual(caught.exception.details["unknownOutcomeIds"], ["R003"])
            self.assertEqual(caught.exception.details["missingOutcomeIds"], ["R003@14"])
        with self.assertRaisesRegex(RevisionContentError, "缺失 ID：R003@14"):
            validated_semantic_outcomes({"outcomes": [fixed]}, known)
        with self.assertRaises(RevisionContentError) as caught:
            validated_semantic_outcomes({"outcomes": [
                fixed, {"id": "R003", "chapter": 13, "status": "open"},
                {"id": "R003@14", "status": "open"},
            ]}, known)
        self.assertIn("重复 ID（状态冲突）：R003@13", str(caught.exception))
        self.assertEqual(caught.exception.details["conflictingOutcomeIds"], ["R003@13"])

    def test_validated_plan_can_reference_any_verified_passage_from_the_input_snapshot(self):
        manuscript = {
            1: {"title": "起航", "content": "船队出发前再次清点船票。"},
            2: {"title": "雾港", "content": "她把旧船票投入火盆中。"},
        }
        audited = {"id": "A1-1", "match": "audit", "chapter": 2, "quote": manuscript[2]["content"]}
        unlocated = {"id": "R001", "match": "unmatched", "quote": "报告误摘了原文。"}
        issues = [audited, unlocated]
        sources = located_evidence(issues, manuscript)
        row = {"id": "R001", "decision": "adapt", "reason": "按已经核对的去向描述核查", "chapters": [2],
               "evidence": [{"sourceId": "A1-1-Q1"}]}
        raw = {"rules": [], "checks": [], "financialFlows": [], "blueprint": [], "decisions": [
            {"id": "A1-1", "decision": "skip", "reason": "已并入相关意见", "evidence": []}, row,
        ]}
        resolved = validated_plan(raw, manuscript, issues, {}, sources)
        self.assertEqual(resolved["decisions"][1]["evidence"], [{"chapter": 2, "quote": audited["quote"]}])
        row["evidence"] = [{"sourceId": "A1-1-Q1", "chapter": 1}]
        with self.assertRaises(RevisionContentError):
            validated_plan(raw, manuscript, issues, {}, sources)

    def test_audit_can_cite_matching_report_evidence_across_ids(self):
        manuscript = {
            1: {"title": "起航", "content": "船队出发前再次清点船票。领航员坐在门边。"},
            2: {"title": "雾港", "content": "船队出发前再次清点船票。"},
        }
        report = {"aiFlavorIssues": [
            {"chapterNumber": 1, "quote": "船队出发前再次清点船票。"},
            {"chapterNumber": 1, "quote": "领航员坐在门边。"},
            {"chapterNumber": 2, "quote": "船队出发前再次清点船票。"},
        ]}
        audit = {"id": "A1-1", "match": "audit", "chapter": 1, "quote": "船队出发前再次清点船票。"}
        sources = located_evidence([*issue_map(report, manuscript), audit], manuscript)["A1-1"]
        self.assertIsNotNone(adjudication_evidence({"sourceId": "A1-1-Q1"}, audit, sources, manuscript))
        self.assertIsNotNone(adjudication_evidence({"sourceId": "R001-Q1"}, audit, sources, manuscript))
        for invalid in ("R002-Q1", "R003-Q1"):
            self.assertIsNone(adjudication_evidence({"sourceId": invalid}, audit, sources, manuscript))

    def test_multichapter_plan_uses_distinct_manuscript_passages(self):
        manuscript = {
            1: {"title": "起航", "content": "船队出发前再次清点船票。"},
            2: {"title": "雾港", "content": "她把旧船票投入火盆中。"},
            3: {"title": "靠岸", "content": "领航员被救生绳拉回甲板。"},
            4: {"title": "翌日", "content": "船票曾在别处找到。"},
        }
        plan = {"id": "P001", "match": "plan", "quote": "",
                "problem": "第2-3章统一‘旧船票’。第3章解释“领航员被救”的过程。"}
        audit = [{"chapter": 2, "quote": "她把旧船票投入火盆中。", "detail": "船票去向"}]
        refs = located_evidence([plan], manuscript, audit)["P001"]
        self.assertEqual({item["chapter"] for item in refs}, {2, 3})
        self.assertTrue(all(adjudication_evidence({"sourceId": ref["id"]}, plan, refs, manuscript) for ref in refs))
        self.assertIsNone(adjudication_evidence({"sourceId": refs[0]["id"], "chapter": 4}, plan, refs, manuscript))
        self.assertIsNone(adjudication_evidence({"chapter": 2, "quote": "船票去向需要修改"}, plan, refs, manuscript))

    def test_only_located_quote_can_be_reused(self):
        manuscript = {7: {"title": "夜班守则", "content": "她记住了这个\n通行条件。随后把纸条收起来。"}}
        report = {"aiFlavorIssues": [{"chapterNumber": 7, "quote": "她记住了这个通行条件……"}]}
        issue, = issue_map(report, manuscript)
        sources = located_evidence([issue], manuscript)["R001"]
        evidence = {"chapter": 7, "quote": "她记住了这个\n通行条件"}
        self.assertEqual(adjudication_evidence({"sourceId": "R001-Q1"}, issue, sources, manuscript), evidence)
        self.assertEqual(adjudication_evidence({"chapter": 7, "quote": "她记住了这个通行条件..."}, issue, sources, manuscript), evidence)
        for ref in ({"sourceId": "R009-Q1"}, {"sourceId": "R001-Q1", "chapter": 6},
                    {"chapter": 7, "quote": "她喜欢这个通行条件……"}, {"chapter": 6, "quote": report["aiFlavorIssues"][0]["quote"]}):
            self.assertIsNone(adjudication_evidence(ref, issue, sources, manuscript))

    def test_ellipses_and_partial_quote_location(self):
        chapters = {
            1: {"title": "旧港档案", "content": "这条旧路不能再通航。北边的灯从今天开始熄灭。"},
            2: {"title": "新的岔口", "content": "这条旧路不能再通航。她没有停步。"},
        }
        report = {"aiFlavorIssues": [
            {"chapterNumber": 1, "quote": "北边的灯从今天开始熄灭……"},
            {"chapterNumber": 2, "quote": "无法匹配的上下文。这条旧路不能再通航。"},
            {"chapterNumber": 3, "quote": "这条旧路不能再通航..."},
            {"chapterNumber": 2, "quote": "早上离开前……北边的灯从今天开始熄灭。"},
        ]}
        located = issue_map(report, chapters)
        self.assertEqual([(x["match"], x["chapter"], x.get("quoteMatch")) for x in located],
                         [("matched", 1, "partial"), ("matched", 2, "partial"),
                          ("ambiguous", None, "partial"), ("relocated", 1, "partial")])
        self.assertEqual(located[2]["matches"], [1, 2])

    def test_pacing_changes_are_not_rejected_by_length(self):
        original = {1: {"title": "第1章", "content": "她犹豫了一会。" * 25 + "闸门须在三十秒内关上。"}}
        candidate = {1: {"title": "第1章", "content": "闸门须在三十秒内关上。她立即拉下手柄。"}}
        self.assertEqual(structural_review(original, candidate, {"checks": []}), [])

    def test_authority_and_scope_without_genre_rules(self):
        chapters = {
            1: {"title": "宴会上签的是退婚协议", "content": "她把签好的合伙协议交给律师。"},
            2: {"title": "下一个春天", "content": "她从未答应和他订婚。"},
        }
        item = {"chapter": 1, "field": "title", "quote": chapters[1]["title"], "priority": "P1",
                "problem": "章名和签署文件不一致。", "fix": "章名使用现有合伙协议。",
                "requiresDecision": False, "affectedChapters": [1, 2], "basis": [{"ruleId": "L-7"}]}
        rule = {"id": "L-7", "chapters": [1], "decision": "文件是合伙协议"}
        ledger = {"rules": [rule]}
        finding = verified_repair(item, chapters, {1}, {1, 2}, ledger, "测试")
        self.assertEqual(finding["repair"]["target"]["field"], "title")
        self.assertEqual(repair_feedback([finding]), [finding])
        for bad in ({"priority": "P2"}, {"requiresDecision": True}, {"affectedChapters": [1, 3]},
                    {"basis": [{"ruleId": "不存在"}]}, {"basis": [{"chapter": 1, "field": "title", "quote": item["quote"]}]}):
            self.assertIsNone(verified_repair({**item, **bad}, chapters, {1}, {1, 2}, ledger, "测试"))
        changed_rule = {"rules": [{**rule, "decision": "文件是退婚协议"}]}
        result = {"repairChecks": [{"id": finding["repair"]["id"], "status": "open", "reason": "作者更新了口径。"}]}
        pending, _ = recheck_repairs(result, [finding], chapters, {1, 2}, changed_rule)
        self.assertFalse(repair_feedback(pending))
        author = {**item, "basis": [{"authorQuote": "文件是合伙协议"}]}
        authority = {"rules": [], "authorDecisions": [{"text": "文件是合伙协议", "chapters": [1]}]}
        self.assertIsNotNone(verified_repair(author, chapters, {1}, {1, 2}, authority, "测试"))

    def test_patch_guards_and_stale_commit(self):
        source = {1: {"chapterNumber": 1, "title": "第1章", "content": "开头。独一无二的大段落。尾句。再过一会。尾句。"}}
        patch = {"chapter": 1, "field": "content", "mode": "range", "startAnchor": "开头", "endAnchor": "尾句",
                 "after": "用新内容替代。", "reason": "区间修改"}
        with self.assertRaisesRegex(RevisionContentError, "锚点未唯一命中"):
            edit_patches({"patches": [patch], "coverage": []}, source, {1}, [])
        patch.update(startAnchor="再过一会", endAnchor="开头")
        with self.assertRaisesRegex(RevisionContentError, "顺序错误"):
            edit_patches({"patches": [patch], "coverage": []}, source, {1}, [])
        with self.assertRaisesRegex(RevisionContentError, "漏报裁决"):
            edit_patches({"patches": [], "coverage": []}, source, {1}, ["R001"])
        # Batch feedback repair IDs (RF-*), ledger rule IDs (L-*), and unambiguous bare decisionIds
        # are normalized/accepted, while genuinely foreign issue IDs still fail with an actionable message.
        repair_patch = {"chapter": 1, "field": "content", "mode": "exact", "before": "独一无二的大段落。",
                        "after": "修正后的大段落。", "reason": "按台账口径修正", "issueIds": ["RF-1", "L-rule1", "A11-2"]}
        accepted = edit_patches(
            {"patches": [repair_patch],
             "coverage": [{"id": "RF-1", "outcome": "patched", "reason": "已落实"},
                          {"id": "A11-2", "outcome": "patched", "reason": "已按第1章口径修订"}]},
            source, {1}, ["A11-2@1"], known_aux_ids={"RF-1", "L-rule1"},
        )
        self.assertEqual(accepted["changed"][0]["content"], "开头。修正后的大段落。尾句。再过一会。尾句。")
        self.assertIn("A11-2@1", accepted["coverage"])
        self.assertNotIn("RF-1", accepted["coverage"])
        foreign_patch = {**repair_patch, "issueIds": ["R999"]}
        with self.assertRaisesRegex(RevisionContentError, "R999"):
            edit_patches({"patches": [foreign_patch], "coverage": []}, source, {1}, [], known_aux_ids={"RF-1"})
        with TemporaryDirectory(ignore_cleanup_errors=True) as temp:
            store = RevisionStore(Path(temp) / "revision.sqlite3")
            store.create("run", "job", {}, {"ledgerVersion": 1})
            self.assertTrue(store.commit_revision("run", expected={"ledgerVersion": 1}, ledgerVersion=2))
            self.assertFalse(store.commit_revision("run", expected={"ledgerVersion": 1},
                                                   products=[("edit", "1", {"obsolete": True})]))
            self.assertIsNone(store.load("run", "edit", "1"))
