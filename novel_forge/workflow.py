import asyncio
import json
import logging
from contextlib import aclosing
from datetime import datetime, timezone
from typing import NotRequired, TypedDict
from uuid import uuid4

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime

from .evaluator import EvaluationError, evaluate_candidate, evaluation_source, is_book_evaluation_running
from .llm import DEFAULT_MAX_TOKENS, LLMError, call_llm
from .pipeline import PipelineRuntime, _write, review_action
from .storage import StorageError, manuscript_fingerprint, revision_manuscript_state
from .verifier import _llm_options
from .progress import Trace, WorkflowProgress, advance_trace, empty_trace

log = logging.getLogger(__name__)

MOCK_REVISION = {
    "audit": {"facts": [], "issues": []},
    "ledger": {"rules": [], "checks": [], "financialFlows": [], "unresolved": []},
    "editor": {"patches": []},
    "verifier": {"findings": [], "outcomes": []},
    "final": {"findings": []},
}


def _timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# Each successor corresponds to a real business boundary returned by the worker.
# Error, pause and retry can exit an otherwise valid stage early.
SUCCESSORS = {
    "prepare": ("topic",),
    "topic": ("plan",),
    "plan": ("outlines",),
    "outlines": ("chapter", "calibrate"),
    "chapter": ("verify",),
    "verify": ("repair", "commit", "reject"),
    "repair": ("verify",),
    "commit": ("chapter", "calibrate"),
    "reject": (),
    "calibrate": ("audit",),
    "audit": ("finish",),
    "finish": (),
    "retry": ("topic",),
}


class WorkflowState(TypedDict):
    job_id: str
    phase: str
    chapter: int | None
    trace: NotRequired[Trace]


class RunContext(TypedDict):
    run_id: str


class WorkerError(RuntimeError):
    def __init__(self, message: str, status: int = 500, code: str = "INTERNAL_ERROR"):
        super().__init__(message)
        self.status = status
        self.code = code


class Worker:
    """Drive the writing graph and serve the revision runtime's local operations."""

    def __init__(self, pipeline: PipelineRuntime):
        self.pipeline = pipeline
        self.store = pipeline.store
        self.progress = WorkflowProgress()
        self.graph = None
        self.runs = {}
        self.de_ai_jobs = set()
        pipeline.runner = self.run

    def manuscript_active(self, job_id):
        return (self.pipeline.is_job_running(job_id) or job_id in self.de_ai_jobs
                or is_book_evaluation_running(job_id, store=self.store))

    async def request(self, action: str, *, jobId: str, payload=None, on_progress=None):
        """Preserve the revision, candidate and editor request contracts in-process."""
        data = payload or {}
        try:
            if action == "revision-model":
                return await self.revision_model(jobId, data)
            if action == "candidate-evaluation":
                return await evaluate_candidate(
                    self.store, jobId, chapters=data.get("chapters"), flash=data.get("flash"), pro=data.get("pro"),
                    checkpoint=data.get("checkpoint"), progress=on_progress, client=self.pipeline.client,
                )
            if action == "revision-source":
                return await self.revision_source(jobId)
            if action == "revision-apply":
                await _write(self.store.apply_manuscript_revision, jobId, data, self.manuscript_active)
                return None
        except (EvaluationError, LLMError, StorageError) as exc:
            raise WorkerError(str(exc), exc.status, exc.code) from exc
        raise WorkerError("Invalid local workflow operation", 400)

    async def revision_source(self, job_id):
        detail = await asyncio.to_thread(self.store.job_detail, job_id)
        if detail is None:
            raise WorkerError("找不到作品。", 404, "JOB_NOT_FOUND")
        job, chapters = detail["job"], detail["chapters"]
        report = job.get("evaluationReport") or {}
        evaluated_at = report.get("evaluatedAt")
        report_matches = bool(evaluated_at and report.get("totalChapters") == len(chapters))
        if report_matches:
            source = report.get("sourceHash")
            if source:
                report_matches = source == evaluation_source(job, chapters)
            else:
                cutoff = _timestamp(evaluated_at)
                report_matches = cutoff is not None and all(
                    (updated := _timestamp(chapter.get("updatedAt"))) is not None and updated <= cutoff
                    for chapter in chapters
                )
        readiness = revision_manuscript_state(job, chapters, self.manuscript_active(job_id))
        return {
            "version": manuscript_fingerprint(chapters), "reportMatches": report_matches, "evaluatedAt": evaluated_at,
            "idle": readiness == "ready", "readiness": readiness, "appliedRevisionId": job.get("appliedRevisionId"),
        }

    async def revision_model(self, job_id, data):
        job = await asyncio.to_thread(self.store.get_job_record, job_id)
        if job is None:
            raise WorkerError("找不到作品。", 404, "JOB_NOT_FOUND")
        defaults = job.get("llmConfig") or {}
        provider = data.get("provider") or defaults.get("provider")
        role = data.get("role")
        options = _llm_options(defaults) if provider == defaults.get("provider") else {}
        options.update(
            provider=provider, model=data.get("model") or defaults.get("model"),
            use_saved_connection=data.get("useSavedConnection") is True, max_tokens=data.get("maxTokens", DEFAULT_MAX_TOKENS),
            temperature=0.35 if role == "editor" else 0.15,
            allow_parallel=role in ("audit", "editor", "verifier"),
            timeout_ms=max(300_000, defaults.get("timeoutMs") or 0),
            max_retries=min(2, 2 if defaults.get("maxRetries") is None else defaults["maxRetries"]),
        )
        # Revision validators consume role-specific objects even in mock mode.
        if provider == "mock" and role in MOCK_REVISION:
            return {"text": json.dumps(MOCK_REVISION[role], ensure_ascii=False)}
        result = await call_llm(
            **options, system=data.get("system") or "", prompt=data.get("prompt") or "",
            store=self.store, client=self.pipeline.client,
        )
        return {"text": result["text"]}

    async def advance(self, run_id, phase):
        stages = self.runs[run_id]
        if phase == "prepare" and (await anext(stages))["phase"] != "prepare":
            raise RuntimeError("Expected the initial prepare phase")
        next_stage = await anext(stages, None)
        if next_stage is None:
            return {"phase": "done"}
        if phase == "verify" and next_stage["phase"] == "review":
            action = review_action(next_stage)
            next_stage = await stages.asend(action)
            if next_stage["phase"] != action:
                raise RuntimeError(f"Expected the {action} boundary after review")
        return next_stage

    async def read_progress(self, job_id: str):
        job = await self.read_job(job_id)
        if job is None:
            return None
        trace = None
        if job_id not in self.progress.frames:
            state = await self.graph.aget_state({"configurable": {"thread_id": job_id}})
            trace = state.values.get("trace") if isinstance(state.values, dict) else None
        return self.progress.snapshot(job_id, job.get("status"), trace)

    async def read_job(self, job_id):
        return await asyncio.to_thread(self.store.get_job_record, job_id)

    def update_progress_status(self, job_id, status):
        frame = self.progress.frames.get(job_id)
        if frame and not frame["runActive"]:
            self.progress.publish(job_id, {**frame, "jobStatus": status,
                                           "errorNode": None if status == "paused" else frame["errorNode"]})

    async def fail_workflow(self, job_id, exc):
        job = await self.read_job(job_id)
        if job and job.get("status") == "running":
            await self.pipeline._failed(job_id, job.get("stage") or "init", "WORKFLOW_ERROR", str(exc), status="interrupted")

    async def run(self, job_id: str, run_id: str | None = None):
        run_id = run_id or uuid4().hex
        self.progress.begin(job_id, run_id)
        try:
            async with aclosing(self.pipeline.run_pipeline(job_id)) as stages:
                self.runs[run_id] = stages
                await self.graph.ainvoke(
                    {"job_id": job_id, "phase": "prepare", "chapter": None, "trace": empty_trace()},
                    {"configurable": {"thread_id": job_id}, "recursion_limit": 100_000},
                    context={"run_id": run_id},
                    durability="sync",
                )
        except Exception as exc:
            log.exception("Workflow interrupted for %s", job_id)
            await self.fail_workflow(job_id, exc)
        finally:
            self.runs.pop(run_id, None)
            status = None
            try:
                job = await self.read_job(job_id)
                status = job.get("status") if job else None
            except Exception:
                log.exception("Could not read final workflow status for %s", job_id)
            self.progress.stop(job_id, run_id, status)

    async def close(self):
        await self.pipeline.close()


def build_graph(worker: Worker, checkpointer):
    graph = StateGraph(WorkflowState, context_schema=RunContext)

    def stage_node(phase: str):
        async def advance(state: WorkflowState, runtime: Runtime[RunContext]):
            job_id = state["job_id"]
            run_id = runtime.context["run_id"]
            chapter = state.get("chapter")
            worker.progress.enter(job_id, run_id, phase, chapter)
            trace = None
            try:
                result = await worker.advance(run_id, phase)
                next_phase = result["phase"]
                if next_phase not in (*SUCCESSORS[phase], "retry", "done"):
                    raise RuntimeError(f"Cannot transition from {phase} to {next_phase}")
                trace = advance_trace(state.get("trace") or empty_trace(), phase, chapter)
                return {"phase": next_phase, "chapter": result.get("chapter"), "trace": trace}
            finally:
                worker.progress.leave(job_id, run_id, trace)
        return advance

    graph.add_edge(START, "prepare")
    for phase, successors in SUCCESSORS.items():
        graph.add_node(phase, stage_node(phase))
        destinations = {node: node for node in (*successors, "retry")}
        destinations["done"] = END
        graph.add_conditional_edges(phase, lambda state: state["phase"], destinations)
    return graph.compile(checkpointer=checkpointer)
