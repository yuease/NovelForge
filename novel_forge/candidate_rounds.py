"""Evaluate immutable candidate versions separately from the live manuscript."""

import asyncio
import logging
from collections import Counter

from .revision_checks import fingerprint
from .revision_store import timestamp
from .workflow import WorkerError


log = logging.getLogger(__name__)
ROUND_KIND = "candidate-evaluation"


def report_summary(report):
    if not report:
        return None
    issues = Counter(item.get("priority") for item in (report.get("aiFlavorIssues") or []))
    scores = {key: value["score"] for key, value in (report.get("sixDimensions") or {}).items()
              if isinstance(value, dict) and isinstance(value.get("score"), (int, float))}
    return {"evaluatedAt": report.get("evaluatedAt"), "issues": {key: issues[key] for key in ("P0", "P1", "P2")},
            "scores": scores, "models": report.get("models"), "mode": report.get("mode")}


class CandidateRounds:
    def __init__(self, revisions, worker):
        self.revisions = revisions
        self.worker = worker
        self.store = revisions.store
        self.tasks = {}

    def require_run(self, job, run):
        record = self.revisions.require(job, run)
        if record.get("workflowVersion") != 2:
            raise WorkerError("多轮评估适用于 v2 候选稿。", 409, "ROUND_REVISION")
        return record

    def view(self, run, state, *, detailed=False):
        checkpoint = state.get("checkpoint") or {}
        report = state.get("report")
        status = state["status"]
        if status == "running" and (run, state["turn"]) not in self.tasks:
            status = "paused"
        output = {key: state.get(key) for key in ("turn", "candidateHash", "updatedAt", "progress", "error")}
        output.update(status=status, selection=state["selection"], summary=report_summary(report), isMock=state.get("isMock"),
                      done=len(checkpoint.get("batchResults", [])), total=checkpoint.get("totalBatches"),
                      stage=checkpoint.get("stage"))
        if detailed:
            output["report"] = report
        return output

    def list(self, job, run):
        self.require_run(job, run)
        baseline = self.store.snapshot(run)["report"]
        return {"baseline": report_summary(baseline), "rounds": sorted(
            (self.view(run, state) for state in self.store.products(run, ROUND_KIND)), key=lambda state: state["turn"])}

    def detail(self, job, run, turn):
        self.require_run(job, run)
        state = self.revisions.load(run, ROUND_KIND, turn)
        if not state:
            raise WorkerError("这版候选稿还没有评估。", 404, "ROUND_NOT_FOUND")
        return self.view(run, state, detailed=True)

    def save(self, run, turn, state):
        state["updatedAt"] = timestamp()
        self.store.save(run, ROUND_KIND, str(turn), state)

    async def action(self, job, run, turn, payload):
        key = (run, turn)
        if payload.get("action") == "pause":
            self.require_run(job, run)
            if task := self.tasks.get(key):
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                if self.tasks.pop(key, None) is not None:
                    state = self.revisions.load(run, ROUND_KIND, turn)
                    if state["status"] == "running":
                        self.save(run, turn, {**state, "status": "paused"})
            return self.detail(job, run, turn)

        async with self.revisions.lock:
            record = self.require_run(job, run)
            latest = self.store.latest(job)
            if (latest["id"] != run or record.get("candidateTurn") != turn
                    or record["status"] not in ("ready", "needs_review", "applied")):
                raise WorkerError("请在当前候选稿交付后再评估这版。", 409, "ROUND_CANDIDATE")
            result = self.revisions.load(run, "v2-result", turn)
            if not result or fingerprint(result["candidate"]) != result["candidateHash"]:
                raise WorkerError("找不到完整的候选稿。", 409, "ROUND_CANDIDATE")
            state = self.revisions.load(run, ROUND_KIND, turn)
            if key in self.tasks or (state and state["status"] == "completed"):
                return self.view(run, state)
            if any(active_run == run for active_run, _ in self.tasks):
                raise WorkerError("还有一版在评估中，等它结束或暂停后再开始。", 409, "ROUND_RUNNING")
            if state and state["candidateHash"] != result["candidateHash"]:
                raise WorkerError("候选稿已更新，请打开新版本。", 409, "ROUND_CANDIDATE")
            previous = state["selection"] if state else {}
            selection = {
                role: self.revisions.model_selection(payload.get(role), previous.get(role) or record["verifier"])
                for role in ("flash", "pro")
            }
            if (selection["flash"]["provider"] == "mock") != (selection["pro"]["provider"] == "mock"):
                raise WorkerError("请为初审和精读都选择真实模型，或同时使用模拟模型。", 400, "ROUND_MODEL")
            checkpoint = (state or {}).get("checkpoint") if payload.get("action") != "restart" else None
            if checkpoint and selection["flash"]["provider"] == "mock":
                raise WorkerError("请选择真实模型继续这版评估。", 400, "ROUND_MODEL")
            state = {"turn": turn, "candidateHash": result["candidateHash"], "selection": selection,
                     "status": "running", "checkpoint": checkpoint, "progress": "正在评估这版候选稿"}
            self.save(run, turn, state)
            task = asyncio.create_task(self.evaluate(job, run, turn, state, result["candidate"]))
            self.tasks[key] = task
            return self.view(run, state)

    async def evaluate(self, job, run, turn, state, chapters):
        def progress(value):
            if value.get("evaluationCheckpoint"):
                state["checkpoint"] = value["evaluationCheckpoint"]
            if value.get("evaluationProgress"):
                state["progress"] = value["evaluationProgress"]
            self.save(run, turn, state)

        try:
            report = await self.worker.request("candidate-evaluation", jobId=job, on_progress=progress, payload={
                **state["selection"], "chapters": chapters, "checkpoint": state["checkpoint"],
            })
            self.save(run, turn, {**state, "report": report, "status": "completed", "progress": "评估完成",
                                  "checkpoint": None, "isMock": state["selection"]["flash"]["provider"] == "mock"})
        except asyncio.CancelledError:
            self.save(run, turn, {**state, "status": "paused", "progress": "已暂停，可从已保存的批次继续。"})
        except Exception as exc:
            if not isinstance(exc, WorkerError):
                log.exception("Candidate evaluation interrupted %s v%s", run, turn)
            self.save(run, turn, {**state, "status": "error",
                                  "error": str(exc)[:600] if isinstance(exc, WorkerError) else "评估中断，请继续已保存的批次。"})
        finally:
            self.tasks.pop((run, turn), None)

    async def close(self):
        tasks = tuple(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
