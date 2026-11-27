import asyncio
import json
import logging
import math
import os
import re
from contextlib import aclosing, asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from .acceptance import run_acceptance
from .book_plan import collect_boundary_state, is_v2_book_plan
from .candidate_rounds import CandidateRounds
from .editor_desk import EditorDesk
from .evaluator import EvaluationError, de_ai_rewrite_chapters, is_book_evaluation_running, recover_interrupted_evaluations, start_book_evaluation
from .hotspots import generate_concept
from .llm import LLMError, test_connection
from .openai_siwc import OpenAISiwc, SiwcError
from .pipeline import PipelineError, PipelineRuntime, _write
from .prompt_budget import read_review_input_budget
from .providers import ProviderCatalogError, list_model_catalog, sync_provider_models, update_model_settings
from .storage import StorageError, Store, claim_data_dir, public_job
from .siwc_routes import siwc_router
from .txt_import import parse_txt_manuscript
from .verifier import recheck_downstream_chapters, verify_chapter
from .revision_v2 import RevisionWorkflowV2
from .workflow import Worker, WorkerError, build_graph

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web-gemini" / "dist"
log = logging.getLogger(__name__)
_LOCAL_ORIGIN = re.compile(r"^https?://(?:127\.0\.0\.1|localhost|\[::1\])(?::\d+)?$", re.I)
_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
EVALUATION_OPTIONS = (
    "mode batchSize forceRescan restartEvaluation resumeEvaluation repairFailedBatches provider flashProvider "
    "flashModel proProvider proModel useSavedConnection useSavedFlashConnection useSavedProConnection "
    "baseUrl apiKey timeoutMs maxRetries"
)


def error_response(message: str, status: int):
    return JSONResponse({"error": message}, status_code=status)


async def bounded_body(request: Request):
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 5 * 1024 * 1024:
            raise WorkerError("request entity too large", 413)
    return bytes(body)


async def job_payload(request: Request):
    body = await bounded_body(request)
    content_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
    if not body or content_type != "application/json":
        return {}
    try:
        value = json.loads(body)
    except (ValueError, UnicodeError) as exc:
        raise WorkerError("Invalid JSON request", 400) from exc
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        return {}
    raise WorkerError("Invalid JSON request", 400)


def options(payload, fields):
    return {re.sub(r"[A-Z]", lambda letter: "_" + letter[0].lower(), key): payload[key]
            for key in fields.split() if key in payload}


def page_number(value, default, *, min_value=0, max_value=math.inf):
    try:
        number = math.floor(float(value))
        return min(number, max_value) if number >= min_value else default
    except (TypeError, ValueError, OverflowError):
        return default


async def refresh_periodically(refresh, interval, *, first=None):
    operation = first or refresh
    while True:
        try:
            await operation()
        except Exception:
            log.exception("Background feed refresh failed")
        operation = refresh
        await asyncio.sleep(interval)


def create_app(data_dir: Path | None = None, *, client=None):
    directory = Path(data_dir or os.getenv("NOVEL_FORGE_DATA_DIR", ROOT / "data")).resolve()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        with claim_data_dir(directory):
            store = app.state.store = Store(directory)
            store.openai_siwc = OpenAISiwc(directory, client)
            pipeline = app.state.pipeline = PipelineRuntime(store, client=client)
            worker = app.state.worker = Worker(pipeline)
            async with AsyncSqliteSaver.from_conn_string(str(directory / "workflow.sqlite3")) as saver:
                worker.graph = build_graph(worker, saver)
                revisions = app.state.revisions = RevisionWorkflowV2(directory, store, worker, saver)
                editor_desk = app.state.editor_desk = EditorDesk(store, revisions, worker)
                candidate_rounds = app.state.candidate_rounds = CandidateRounds(revisions, worker)
                background = app.state.background = set()
                app.state.de_ai_jobs = worker.de_ai_jobs
                try:
                    await recover_interrupted_evaluations(store)
                    if os.getenv("NOVEL_FORGE_AUTO_RECOVER") != "0":
                        await pipeline.recover_interrupted_jobs()
                    if os.getenv("NOVEL_FORGE_BACKGROUND_FEEDS") != "0":
                        track(asyncio.create_task(refresh_periodically(
                            pipeline.hotspots_feed.refresh_hotspots_feed, 1800,
                        )))
                        track(asyncio.create_task(refresh_periodically(
                            pipeline.meme_store.sync_memes, 21_600,
                            first=lambda: pipeline.meme_store.sync_memes(max_pages=2),
                        )))
                    await editor_desk.start()
                    yield
                finally:
                    tasks = tuple(background)
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    await candidate_rounds.close()
                    revisions.on_failure = None
                    await revisions.close()
                    await editor_desk.close()
                    await worker.close()

    app = FastAPI(title="Novel Forge", lifespan=lifespan)
    app.include_router(siwc_router(job_payload))
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=_LOCAL_ORIGIN.pattern,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def enforce_local_origin(request: Request, call_next):
        path = request.url.path
        if path == "/api" or path.startswith("/api/"):
            origin = (request.headers.get("origin") or "").strip()
            if origin and not _LOCAL_ORIGIN.fullmatch(origin):
                return error_response("Cross-origin API requests are not allowed.", 403)
            if (not origin and request.method in _MUTATING_METHODS
                    and (request.headers.get("sec-fetch-site") or "").strip().lower() == "cross-site"):
                return error_response("Cross-site API requests are not allowed.", 403)
        return await call_next(request)

    def track(task):
        app.state.background.add(task)

        def finished(done):
            app.state.background.discard(done)
            if not done.cancelled() and (error := done.exception()) is not None:
                log.warning("Background operation failed: %s", error)
        task.add_done_callback(finished)
        return task

    async def service_error(_request: Request, exc):
        return JSONResponse({"error": str(exc), "code": exc.code}, status_code=exc.status)

    for error_type in (WorkerError, PipelineError, LLMError, StorageError, EvaluationError, ProviderCatalogError, SiwcError):
        app.add_exception_handler(error_type, service_error)

    @app.get("/api/health")
    async def health(response: Response):
        response.headers["X-Novel-Forge-Backend"] = "fastapi"
        return {"ok": True, "status": "running",
                "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")}

    @app.get("/api/model-catalog")
    async def model_catalog():
        return await list_model_catalog(app.state.store)

    @app.put("/api/model-catalog/{provider}")
    async def save_models(provider: str, request: Request):
        return {"provider": await update_model_settings(app.state.store, provider, await job_payload(request))}

    @app.post("/api/model-catalog/{provider}/discover")
    async def discover_models(provider: str):
        return {"provider": await sync_provider_models(app.state.store, provider, client=client)}

    def public_config(provider=None):
        store = app.state.store
        entries = store.get_all_public_provider_configs()
        return {"providers": store.get_public_provider_config(provider), "allProviders": entries["map"],
                "activeProvider": entries["activeProvider"]}

    @app.get("/api/config/providers")
    def provider_config(provider: str | None = None):
        return public_config(provider)

    @app.put("/api/config/providers")
    async def set_provider_config(request: Request):
        payload = await job_payload(request)
        await _write(app.state.store.save_provider_config, payload)
        return {"ok": True, **await asyncio.to_thread(public_config)}

    @app.post("/api/providers/test")
    @app.post("/api/config/test-connection")
    async def probe_provider(request: Request):
        payload = await job_payload(request)
        return await test_connection(
            **options(payload, "provider baseUrl apiKey model thinkingMode timeoutMs"), store=app.state.store, client=client,
        )

    @app.get("/api/hotspots")
    async def hotspots(platform: str = "", genre: str = "", query: str = ""):
        return await app.state.pipeline.hotspots_feed.get_hotspots(platform=platform, genre=genre, query=query)

    @app.post("/api/hotspots/refresh")
    async def refresh_hotspots(request: Request):
        payload = await job_payload(request)
        return await app.state.pipeline.hotspots_feed.refresh_hotspots_feed(
            **options(payload, "sourceUrl customItems boardDate sourceName timeoutMs"),
        )

    @app.post("/api/hotspots/concept")
    async def concept(request: Request):
        payload = await job_payload(request)
        return await generate_concept(**options(payload, "tropeId platform genre keywords llmConfig"),
                                      store=app.state.store, client=client)

    async def meme_counts(now):
        store = await app.state.pipeline.meme_store.reverify_cached_memes(now=now)
        counts = dict.fromkeys(("all", "active", "pending_verification", "rejected_old", "expired"), 0)
        counts["all"] = len(store["entries"])
        for item in store["entries"].values():
            status = item.get("status")
            if status in counts and status != "all":
                counts[status] += 1
        return counts

    @app.get("/api/memes")
    async def memes(status: str = "", q: str = "", tag: str = "", now: str | None = None,
                    limit: str | None = None, offset: str | None = None):
        size = page_number(limit, 50, min_value=1, max_value=500)
        start = page_number(offset, 0)
        result = await app.state.pipeline.meme_store.get_memes(status=status, q=q, tag=tag, now=now, limit=size, offset=start)
        return {**result, "limit": size, "offset": start, "counts": await meme_counts(now)}

    @app.post("/api/memes/sync")
    async def sync_memes(request: Request):
        payload = await job_payload(request)
        size = page_number(payload.get("limit"), 50, min_value=1, max_value=500)
        config = options(payload, "baseUrl now overlapMs maxPages limit verifyDetails customItems simulateFailure backoffBaseMs maxRetries")
        if "limit" in config:
            config["limit"] = page_number(config["limit"], 30, min_value=1, max_value=500)
        result = await app.state.pipeline.meme_store.sync_memes(**config)
        return {**result, "items": result["items"][:size], "total": len(result["items"]),
                "limit": size, "offset": 0, "counts": await meme_counts(payload.get("now"))}

    @app.get("/api/memes/{slug}")
    async def meme_detail(slug: str):
        result = await app.state.pipeline.meme_store.get_memes()
        for item in result["items"]:
            if item.get("slug") == slug or slug in (item.get("mergedSlugs") or []):
                return {"item": item}
        return error_response("Meme not found", 404)

    @app.get("/api/jobs")
    def list_jobs():
        return {"jobs": app.state.store.list_jobs()}

    @app.post("/api/jobs", status_code=201)
    async def create_job(request: Request):
        payload = await job_payload(request)
        store = app.state.store
        job = await _write(store.create_job, payload)
        if payload.get("autoStart") is not False:
            await app.state.pipeline.start_job(job["id"])
        return {"job": await asyncio.to_thread(store.get_job, job["id"])}

    @app.post("/api/jobs/import-txt", status_code=201)
    async def import_txt(request: Request):
        payload = await job_payload(request)
        text = payload.get("text")
        title = payload.get("title", "")
        mode = payload.get("splitMode", "auto")
        chunk_words = payload.get("chunkWords", 2500)
        if not isinstance(text, str):
            return error_response("请上传 TXT 文件或粘贴小说正文。", 400)
        if not isinstance(title, str) or any(mark in title.strip() for mark in ("\r", "\n")):
            return error_response("书名须为单行文字。", 400)
        if re.search(r"[\x00\ud800-\udfff]", text) or re.search(r"[\x00\ud800-\udfff]", title):
            return error_response("正文或书名包含无效字符，请检查 TXT 文件的编码。", 400)
        try:
            parsed = await asyncio.to_thread(
                parse_txt_manuscript, text, fallback_title=title, split_mode=mode, chunk_words=chunk_words,
            )
        except ValueError as exc:
            return error_response(str(exc), 400)
        job = await _write(app.state.store.create_imported_manuscript, parsed, title=title.strip())
        return {"job": public_job(job), **{key: value for key, value in parsed.items() if key != "chapters"}}

    async def apply_job_action(request: Request, job_id: str, action: str):
        if not await asyncio.to_thread(app.state.store.get_job, job_id):
            return error_response("Job not found", 404)
        payload = await job_payload(request)
        config = payload.get("llmConfig")
        if action == "config" and not isinstance(config, (dict, list)):
            return error_response("llmConfig object is required", 400)
        config = config if isinstance(config, dict) else None
        pipeline = app.state.pipeline
        if action == "pause":
            job = await pipeline.pause_job(job_id)
            if job["status"] == "paused":
                app.state.worker.update_progress_status(job_id, "paused")
        elif action == "resume":
            job = await pipeline.resume_job(job_id, llm_config=config)
        elif action == "extend":
            job = await pipeline.extend_job(
                job_id, **options(payload, "addChapters addWords targetTotalWords direction finalizeBook memeLevel memeSourceMode"),
                llm_config=config,
            )
        else:
            job = await pipeline.update_job_llm_config(job_id, config)
        return {"job": job}

    @app.post("/api/jobs/{job_id}/pause")
    async def pause_job(request: Request, job_id: str):
        return await apply_job_action(request, job_id, "pause")

    @app.post("/api/jobs/{job_id}/resume")
    async def resume_job(request: Request, job_id: str):
        return await apply_job_action(request, job_id, "resume")

    @app.post("/api/jobs/{job_id}/extend")
    async def extend_job(request: Request, job_id: str):
        return await apply_job_action(request, job_id, "extend")

    @app.patch("/api/jobs/{job_id}/config")
    async def update_job_config(request: Request, job_id: str):
        return await apply_job_action(request, job_id, "config")

    @app.get("/api/jobs/{job_id}")
    def job_detail(job_id: str):
        detail = app.state.store.job_detail(job_id)
        return detail if detail else error_response("Job not found", 404)

    @app.patch("/api/jobs/{job_id}/drifts/{drift_id}")
    async def update_drift(job_id: str, drift_id: str, request: Request):
        payload = await job_payload(request)
        status, reason = payload.get("status"), payload.get("reason", "human")
        if status not in ("dismissed", "deferred") or not isinstance(reason, str) or not reason.strip():
            return error_response("Expected status dismissed or deferred and a non-empty reason", 400)
        entry = await asyncio.to_thread(app.state.store.set_drift_status, job_id, drift_id, status, reason.strip())
        return entry if entry is not None else error_response("Job or drift not found", 404)

    @app.get("/api/jobs/{job_id}/workflow")
    async def workflow_snapshot(job_id: str):
        frame = await app.state.worker.read_progress(job_id)
        return frame if frame is not None else error_response("Job not found", 404)

    @app.get("/api/jobs/{job_id}/workflow/stream")
    async def workflow_stream(job_id: str):
        worker = app.state.worker
        if not await asyncio.to_thread(app.state.store.get_job, job_id):
            return error_response("Job not found", 404)

        def event(frame):
            return (
                f"id: {frame['epoch']}:{frame['revision']}\n"
                f"event: workflow\ndata: {json.dumps(frame, ensure_ascii=False)}\n\n"
            )

        async def frames():
            with worker.progress.subscribe(job_id) as queue:
                snapshot = await worker.read_progress(job_id)
                if snapshot is None:
                    return
                revision = snapshot["revision"]
                yield event(snapshot)
                while True:
                    try:
                        changed = await asyncio.wait_for(queue.get(), timeout=12)
                    except TimeoutError:
                        yield ": heartbeat\n\n"
                        continue
                    if changed["revision"] > revision:
                        revision = changed["revision"]
                        yield event(changed)

        return StreamingResponse(
            frames(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/jobs/{job_id}/chapters/{number}")
    def get_chapter(job_id: str, number: str):
        try:
            chapter = app.state.store.get_chapter(job_id, int(number))
        except (ValueError, OverflowError):
            chapter = None
        return {"chapter": chapter} if chapter else error_response("Chapter not found", 404)

    @app.get("/api/jobs/{job_id}/stream")
    async def job_stream(job_id: str):
        if not await asyncio.to_thread(app.state.store.get_job, job_id):
            return error_response("Job not found", 404)
        pipeline = app.state.pipeline

        async def frames():
            async with aclosing(pipeline.subscribe_job_events(job_id)) as events:
                pending = asyncio.create_task(anext(events))
                try:
                    while True:
                        _, waiting = await asyncio.wait((pending,), timeout=15)
                        if waiting:
                            yield ": heartbeat\n\n"
                            continue
                        payload = pending.result()
                        name = payload["event"]
                        if name == "snapshot":
                            payload = {"job": payload["job"]}
                        yield f"event: {name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                        pending = asyncio.create_task(anext(events))
                finally:
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)

        return StreamingResponse(frames(), media_type="text/event-stream", headers={
            "Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no",
        })

    @app.put("/api/jobs/{job_id}/chapters/{number}")
    async def save_chapter(job_id: str, number: str, request: Request):
        store = app.state.store
        job = await asyncio.to_thread(store.get_job_record, job_id)
        if job is None:
            return error_response("Job not found", 404)
        try:
            chapter_number = int(number)
            existing = await asyncio.to_thread(store.get_chapter, job_id, chapter_number)
        except (ValueError, OverflowError):
            existing = None
        if existing is None:
            return error_response("Chapter not found", 404)

        payload = await job_payload(request)
        draft = {**existing, **{key: str(payload[key]) for key in ("title", "content", "summary") if key in payload}}
        if "content" in payload:
            draft.pop("wordCount", None)
        verification = existing.get("verification")
        outlines = job.get("outlines") or []
        settings = {"pov": job.get("pov") or "第三人称限知", "target_words": job.get("wordsPerChapter") or 2000,
                    "llm_config": job.get("llmConfig"), "test_scenario": job.get("testScenario"),
                    "store": store, "client": client, "book_plan": job.get("bookPlan"),
                    "long_memory": os.getenv("NOVEL_FORGE_LONG_MEMORY") != "0",
                    "rolling_arc_outlines": os.getenv("NOVEL_FORGE_ROLLING_ARC_OUTLINES") != "0",
                    "story_ledger": os.getenv("NOVEL_FORGE_STORY_LEDGER") != "0"}
        if payload.get("reverify"):
            facts = await asyncio.to_thread(store.get_canonical_facts, job_id)
            pending = existing.get("status") == "needs_review"
            outline = next((item for item in outlines if str(item.get("chapterNumber")) == str(chapter_number)), {})
            boundary_state = ()
            if is_v2_book_plan(job.get("bookPlan")) and chapter_number > 1:
                prior = await asyncio.to_thread(store.get_chapters, job_id, end_chapter=chapter_number - 1)
                boundary_state = collect_boundary_state(prior, before_chapter=chapter_number, book_plan=job.get("bookPlan"))
            verification = await verify_chapter(
                chapter_number=chapter_number, chapter_text=draft["content"], outline=outline, canonical_facts=facts,
                strict_review_result=pending, chapter_ai_gate=pending and os.getenv("NOVEL_FORGE_WRITE_AI_GATE") != "0",
                boundary_state=boundary_state, chapter_title=draft.get("title"),
                input_budget=read_review_input_budget(), **settings,
            )
            if pending and verification["passed"]:
                draft["status"] = "verified"
        saved = await _write(store.save_chapter, job_id, chapter_number, {**draft, "verification": verification})
        downstream = None
        if payload.get("recheckDownstream"):
            chapters, facts = await asyncio.gather(asyncio.to_thread(store.get_chapters, job_id),
                                                  asyncio.to_thread(store.get_canonical_facts, job_id))
            downstream = await recheck_downstream_chapters(
                changed_from_chapter=chapter_number, chapters=chapters, outlines=outlines, canonical_facts=facts, **settings,
            )
        return {"chapter": saved, "verification": verification, "downstreamAudit": downstream}

    def evaluation_busy(job_id, job):
        return (job.get("evaluationStatus") == "running" or job_id in app.state.de_ai_jobs
                or is_book_evaluation_running(job_id, store=app.state.store))

    @app.post("/api/jobs/{job_id}/evaluate")
    async def evaluate(job_id: str, request: Request):
        store = app.state.store
        payload = await job_payload(request)
        job = await asyncio.to_thread(store.get_job, job_id)
        if job is None:
            return error_response("Job not found", 404)
        background = payload.get("background") is True
        try:
            if evaluation_busy(job_id, job):
                raise EvaluationError("此作品正在评估或精修。", "EVALUATION_RUNNING")
            started = await start_book_evaluation(store, job_id, **options(payload, EVALUATION_OPTIONS), client=client)
        except EvaluationError as exc:
            if not background or exc.code != "EVALUATION_RUNNING":
                raise
            current = await asyncio.to_thread(store.get_job, job_id)
            return JSONResponse({"started": False, "alreadyRunning": True, "job": current}, status_code=202)
        completion = track(started["completion"])
        if background:
            return JSONResponse({"started": True, "job": public_job(started["job"])}, status_code=202)
        result = await completion
        return {"job": public_job(result["job"]), "evaluationReport": result["evaluationReport"]}

    @app.post("/api/jobs/{job_id}/de-ai")
    async def de_ai(job_id: str, request: Request):
        store = app.state.store
        payload = await job_payload(request)
        job = await asyncio.to_thread(store.get_job, job_id)
        if job is None:
            return error_response("Job not found", 404)
        if evaluation_busy(job_id, job):
            raise EvaluationError("此作品正在评估或精修。", "EVALUATION_RUNNING")
        commands = options(payload, "chapterNumbers priorities categories provider model useSavedConnection timeoutMs maxRetries")
        background = payload.get("background") is True
        active = app.state.de_ai_jobs
        active.add(job_id)

        async def rewrite():
            try:
                result = await de_ai_rewrite_chapters(store, job_id, **commands, client=client)
                if background:
                    await _write(store.update_job, job_id, {
                        "evaluationStatus": "completed",
                        "evaluationProgress": f"已完成 {len(result['updatedChapters'])} 章定点去 AI 味精修",
                    })
                return {**result, "job": public_job(result["job"])}
            except asyncio.CancelledError:
                if background:
                    await _write(store.update_job, job_id, {"evaluationStatus": "error", "evaluationProgress": "定点精修已中断。"})
                raise
            except Exception as error:
                if background:
                    await _write(store.update_job, job_id, {
                        "evaluationStatus": "error", "evaluationProgress": f"去 AI 味精修失败: {error}",
                    })
                raise
            finally:
                active.discard(job_id)

        if not background:
            return await rewrite()
        try:
            queued = await _write(store.update_job, job_id, {
                "evaluationStatus": "running", "evaluationProgress": "正在后台执行定点去 AI 味精修...",
            })
            track(asyncio.create_task(rewrite(), name=f"de-ai:{job_id}"))
        except BaseException:
            active.discard(job_id)
            raise
        return JSONResponse({"started": True, "job": public_job(queued)}, status_code=202)

    @app.get("/api/jobs/{job_id}/export")
    async def export_novel(job_id: str, format: str = "txt"):
        exported = await app.state.pipeline.export_novel(job_id, format)
        headers = {"Content-Disposition": "attachment; filename*=UTF-8''" + quote(exported["filename"], safe="")}
        if "sizeBytes" in exported:
            headers["X-Export-Size-Bytes"] = str(exported["sizeBytes"])
            headers["X-Fanqie-Within-Limit"] = str(not exported["exceedsFanqieLimit"]).lower()
        return Response(exported["content"], media_type=exported["contentType"], headers=headers)

    @app.post("/api/test/run-acceptance")
    async def acceptance(request: Request):
        payload = await job_payload(request)
        return await run_acceptance(app.state.pipeline, str(payload.get("suite") or "all"))

    @app.get("/api/jobs/{job_id}/revisions")
    def latest_revision(job_id: str, request: Request):
        store = request.app.state.revisions.store
        latest = store.latest(job_id)
        active = latest if latest and latest.get("workflowVersion") == 2 else None
        return {"revision": active, "legacyRevision": store.latest_archive(job_id) if active else latest}

    @app.post("/api/jobs/{job_id}/revisions", status_code=202)
    async def start_revision(job_id: str, request: Request):
        revision = await request.app.state.revisions.start(job_id, await job_payload(request))
        return {"revision": revision}

    @app.get("/api/jobs/{job_id}/revisions/{revision_id}/export/{kind}")
    def export_revision(job_id: str, revision_id: str, kind: str, request: Request):
        filename, contents, media_type = request.app.state.revisions.export(job_id, revision_id, kind)
        return Response(contents, media_type=media_type,
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename, safe='')}"})

    @app.post("/api/jobs/{job_id}/revisions/{revision_id}/{action}")
    async def change_revision(job_id: str, revision_id: str, action: str, request: Request):
        revision = await request.app.state.revisions.action(job_id, revision_id, action, await job_payload(request))
        return {"revision": revision}

    @app.get("/api/jobs/{job_id}/revisions/{revision_id}/round-evaluations")
    def candidate_rounds(job_id: str, revision_id: str, request: Request):
        return request.app.state.candidate_rounds.list(job_id, revision_id)

    @app.get("/api/jobs/{job_id}/revisions/{revision_id}/round-evaluations/{turn}")
    def candidate_assessment(job_id: str, revision_id: str, turn: int, request: Request):
        return request.app.state.candidate_rounds.detail(job_id, revision_id, turn)

    @app.post("/api/jobs/{job_id}/revisions/{revision_id}/round-evaluations/{turn}", status_code=202)
    async def candidate_assessment_action(job_id: str, revision_id: str, turn: int, request: Request):
        return await request.app.state.candidate_rounds.action(job_id, revision_id, turn, await job_payload(request))

    @app.get("/api/jobs/{job_id}/editor-desk")
    def editor_session(job_id: str, request: Request, before: int | None = None):
        return request.app.state.editor_desk.session(job_id, before)

    @app.put("/api/jobs/{job_id}/editor-desk/settings")
    async def editor_preferences(job_id: str, request: Request):
        payload = await job_payload(request)
        editor_desk = request.app.state.editor_desk
        return await asyncio.to_thread(editor_desk.preferences, job_id, payload)

    @app.post("/api/jobs/{job_id}/editor-desk/chat")
    async def editor_chat(job_id: str, request: Request):
        return await request.app.state.editor_desk.chat(job_id, await job_payload(request))

    @app.post("/api/jobs/{job_id}/editor-desk/diagnose", status_code=202)
    async def editor_diagnose(job_id: str, request: Request):
        request.app.state.editor_desk.request_diagnosis(job_id)
        return {"accepted": True}

    @app.post("/api/jobs/{job_id}/editor-desk/dispatch", status_code=202)
    async def editor_dispatch(job_id: str, request: Request):
        payload = await job_payload(request)
        return {"revision": await request.app.state.editor_desk.dispatch(job_id, payload.get("token"))}

    @app.post("/api/jobs/{job_id}/editor-desk/recover", status_code=202)
    async def editor_recover(job_id: str, request: Request):
        payload = await job_payload(request)
        return {"revision": await request.app.state.editor_desk.dispatch_recovery(job_id, payload.get("token"))}

    @app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
                   include_in_schema=False)
    def unknown_api(path: str):
        return error_response("API route not found", 404)

    @app.api_route("/{path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    def frontend(path: str):
        if path == "api":
            return error_response("API route not found", 404)
        requested = (WEB / path).resolve()
        if requested.is_relative_to(WEB) and requested.is_file():
            return FileResponse(requested)
        index = WEB / "index.html"
        return FileResponse(index) if index.is_file() else error_response("React build missing. Run npm --prefix web-gemini run build.", 404)

    return app


app = create_app()
