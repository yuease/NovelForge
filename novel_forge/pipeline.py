"""In-process writing pipeline and the phase protocol used by the workflow driver."""

import asyncio
import math
import os
import random
import re
import unicodedata
import time
from contextlib import aclosing
from copy import deepcopy
from datetime import datetime, timedelta, timezone

from .arc_planning import legacy_outline_placeholder, normalize_canon_locks, prior_arc_summaries, saved_arc_plans, validate_arc_plan
from .chapter_style import can_save_unverified_draft
from .docx_export import DOCX_CONTENT_TYPE, FANQIE_SIZE_LIMIT, build_fanqie_docx, format_fanqie_chapter_title, split_fanqie_paragraphs
from .draft_quality import clean_chapter_draft, normalize_style_bible, strip_draft_headings
from .evaluator import evaluate_book
from .hotspots import HotspotsFeed, select_or_generate_concept
from .llm import DEFAULT_MAX_TOKENS, LLMError, build_mock_volume_summary, call_llm, output_was_truncated, protected_output_budget
from .long_memory import normalize_volume_summary
from .memes import MemeStore
from .prompts import (
    STYLE_PRESETS,
    build_arc_outlines_prompt,
    build_arc_summary_prompt,
    build_book_plan_prompt,
    build_chapter_outlines_prompt,
    build_chapter_write_prompt,
    build_fact_extraction_prompt,
    build_full_book_audit_prompt,
    build_volume_summary_prompt,
    build_word_count_adjust_prompt,
    extract_relevant_facts,
    read_writing_options,
)
from .providers import get_provider_definition, is_masked_api_key, same_endpoint_origin
from .storage import _is_committed, arc_context_version, count_chinese_words, public_job, volume_source_hash
from .verifier import (
    _llm_options, parse_json_from_llm, recheck_downstream_chapters,
    review_chapter_stages, verify_and_repair_chapter,
)


_WORD_TARGET_DEFAULT = 2000
_UNTITLED = "未命名小说项目"
_USAGE_KEYS = ("promptTokens", "completionTokens", "totalTokens", "cacheHitTokens")
_MAX_AUTO_RESUMES = 15
_TRANSIENT_ERRORS = {
    "HTTP_429", "TIMEOUT", "HTTP_503", "HTTP_502", "HTTP_504", "NETWORK_ERROR", "EMPTY_RESPONSE",
    "ECONNRESET", "ETIMEDOUT", "UND_ERR_CONNECT_TIMEOUT",
}
_MANUAL_RESUME = {"QUOTA_EXCEEDED", "CIRCUIT_OPEN", "OUTPUT_TRUNCATED", "OPENAI_RATE_LIMIT", "OUTLINE_PARSE_FAILED",
                  "ARC_SUMMARY_INVALID", "ARC_SOURCE_CHANGED", "CONTENT_BLOCKED"}
_COMEDY = re.compile(r"吐槽|搞笑|反差|打脸|震惊|直播|吃瓜|反转|嘴替|荒诞|尴尬|发疯")
_PLACEHOLDERS = re.compile(r"【待补充|\[TODO\]|TBD|placeholder", re.I)
_MARKDOWN_HEADING = re.compile(r"^#{1,6}\s*")


class PipelineError(RuntimeError):
    def __init__(self, message, code, status=409):
        super().__init__(message)
        self.code = code
        self.status = status


def _now(after_ms=0):
    stamp = datetime.now(timezone.utc) + timedelta(milliseconds=after_ms)
    return stamp.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _number(value, default=0):
    try:
        number = float(value)
        if math.isfinite(number):
            return int(number) if number.is_integer() else number
    except (TypeError, ValueError, OverflowError):
        pass
    return default


def _dict(value):
    return value if isinstance(value, dict) else {}


def _list(value):
    return value if isinstance(value, list) else []


def _round(number):
    return math.floor(number + 0.5)


def _elapsed(start):
    return max(1, _round((time.monotonic() - start) * 1000))


def _chapter_number(chapter):
    return _number(chapter.get("chapterNumber"))


def _word_count(chapter):
    saved_count = _number(chapter.get("wordCount"), None)
    return saved_count if saved_count is not None else count_chinese_words(chapter.get("content") or "")


def _utf16_length(text):
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def _excerpt(text, units, *, tail=False):
    raw = str(text or "").encode("utf-16-le", "surrogatepass")
    return (raw[-2 * units:] if tail else raw[:2 * units]).decode("utf-16-le", errors="ignore")


def _summary(outline, chapter_text):
    clean = re.sub(r"\s+", " ", str(chapter_text or "")).strip()
    head = _excerpt(clean, 90)
    tail = " ... " + _excerpt(clean, 70, tail=True) if _utf16_length(clean) > 180 else ""
    base = str(_dict(outline).get("summary") or "").strip()
    return f"{base} [正文节选: {head}{tail}]".strip()


def _safe_job(job):
    return public_job(deepcopy(job)) if job is not None else None


async def _write(method, *args, **kwargs):
    """Keep a cancelled run registered until any transaction already in flight settles."""
    task = asyncio.create_task(asyncio.to_thread(method, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


def review_action(event):
    if event.get("passed") or event.get("canCommitForReview"):
        return "commit"
    return "repair" if event.get("canRepair") else "reject"


def _is_finale(chapter_number, total):
    count = 3 if total >= 6 else 2 if total >= 4 else 1
    return 0 < chapter_number <= total and chapter_number > total - count


def compute_dynamic_chapter_target(job, written_words, remaining_chapters, *, chapter_number=None):
    base = _number(job.get("wordsPerChapter")) or _WORD_TARGET_DEFAULT
    chapters = _number(job.get("totalChapters"))
    total = _number(job.get("targetWords")) or base * chapters
    if remaining_chapters <= 0:
        return base
    number = chapters - remaining_chapters + 1 if chapter_number is None else chapter_number
    floor = base if _is_finale(number, chapters) else _round(base * 0.85)
    ideal = _round(max(300, total - written_words) / remaining_chapters)
    return min(_round(base * 1.5), max(300, floor, ideal))


def _meme_title(sample):
    title = _dict(sample).get("title")
    return " ".join(unicodedata.normalize("NFKC", title).split()).casefold() if isinstance(title, str) else ""


def _saved_outlines(job):
    saved = {}
    for value in _list(job.get("outlines")):
        entry = _dict(value)
        number = _chapter_number(entry)
        if type(number) is int and 1 <= number <= job["totalChapters"]:
            saved.setdefault(number, entry)
    return saved


def _display_title(chapter):
    return _MARKDOWN_HEADING.sub("", str(chapter.get("title") or f"第{chapter['chapterNumber']}章")).strip()


def strip_leading_chapter_heading(content="", chapter_title="", chapter_number=None):
    text = str(content or "")
    lines = text.replace("\r\n", "\n").split("\n")
    first = next((index for index, line in enumerate(lines) if line.strip()), len(lines))
    if first == len(lines):
        return ""
    line = lines[first].strip()
    clean = _MARKDOWN_HEADING.sub("", line).strip()
    title = _MARKDOWN_HEADING.sub("", str(chapter_title or "")).strip()
    markdown = re.match(r"^#{1,6}\s*第\s*[0-9一二三四五六七八九十百千万]+\s*章(?:\s|$|[：:\-—])", line)
    chapter = re.escape(str(_number(chapter_number)))
    numbered = chapter_number is not None and _utf16_length(clean) <= 40 and re.match(
        rf"^(?:#{{1,6}}\s*)?第\s*{chapter}\s*章(?:\s|$|[：:\-—])", line,
    )
    return "\n".join(lines[first + 1:]).strip() if (title and clean == title) or markdown or numbered else text.strip()


def _export(job, chapters, output_format):
    title = job.get("title") or "未命名小说"
    kind = str(output_format or "txt").lower()
    imported_count = _number(job.get("importedChapterCount"))
    if kind in ("docx", "fanqie", "fanqie-docx"):
        entries = []
        for index, chapter in enumerate(sorted(chapters, key=_chapter_number), 1):
            number = max(1, math.floor(_chapter_number(chapter) or index))
            heading = format_fanqie_chapter_title(chapter.get("title"), number)
            body = chapter.get("content") or ""
            if number > imported_count:
                body = strip_leading_chapter_heading(body, chapter.get("title") or heading, number)
                body = strip_leading_chapter_heading(body, heading, number)
            entries.append({"heading": heading, "paragraphs": split_fanqie_paragraphs(body)})
        content = build_fanqie_docx(entries)
        return {"filename": f"{title}-番茄投稿.docx", "contentType": DOCX_CONTENT_TYPE, "content": content,
                "sizeBytes": len(content), "exceedsFanqieLimit": len(content) > FANQIE_SIZE_LIMIT}
    logline = _dict(job.get("bookPlan")).get("logline") or job.get("premise") or ""
    genre, pov, words = job.get("genre", ""), job.get("pov", ""), job.get("totalWords", 0)
    if kind == "md":
        lines = [f"# {title}", "", f"- **题材**：{genre}", f"- **叙事视角**：{pov}",
                 f"- **章节数**：{len(chapters)} / {job.get('totalChapters', 0)}", f"- **总字数**：{words}",
                 "", f"> {logline}", "", "---", ""]
    else:
        lines = [f"《{title}》", f"题材：{genre} | 视角：{pov} | 总字数：{words}", f"简介：{logline}", "=" * 40, ""]
    for chapter in chapters:
        heading = _display_title(chapter)
        body = (chapter.get("content") or "").strip("\n")
        if chapter["chapterNumber"] > imported_count:
            body = strip_leading_chapter_heading(body, heading, chapter["chapterNumber"])
        lines.extend([f"## {heading}" if kind == "md" else heading, "", body, ""])
        if kind != "md":
            lines.extend(["-" * 30, ""])
    suffix, mime = ("md", "markdown") if kind == "md" else ("txt", "plain")
    return {"filename": f"{title}.{suffix}", "contentType": f"text/{mime}; charset=utf-8", "content": "\n".join(lines)}


def _patched_connection(existing, patch, saved):
    provider = str(patch.get("provider") or existing.get("provider") or "deepseek").strip().lower()
    changed = provider != existing.get("provider")
    key = patch.get("apiKey")
    real_key = isinstance(key, str) and bool(key.strip()) and not is_masked_api_key(key)
    url = patch.get("baseUrl")
    has_url = isinstance(url, str)
    saved_mode = patch.get("useSavedConnection") is True or (
        patch.get("useSavedConnection") is not False and existing.get("useSavedConnection") is True
        and not changed and not real_key and not has_url
    )
    saved_url = saved.get("baseUrl") or ""
    if saved_mode:
        base = saved_url
    elif changed or has_url:
        base = (url.strip() if has_url else "") or saved_url
    else:
        base = existing.get("baseUrl") or saved_url
    updates = {**existing, "provider": provider, "baseUrl": base}
    if saved_mode:
        updates["useSavedConnection"] = True
        updates.pop("apiKey", None)
    else:
        updates.pop("useSavedConnection", None)
        if real_key:
            updates["apiKey"] = key.strip()
        elif not changed and same_endpoint_origin(base, existing.get("baseUrl")) and existing.get("apiKey"):
            updates["apiKey"] = existing["apiKey"]
        elif same_endpoint_origin(base, saved_url) and saved.get("apiKey"):
            updates["apiKey"] = saved["apiKey"]
        else:
            updates.pop("apiKey", None)
    model = patch.get("model")
    updates["model"] = (model.strip() if isinstance(model, str) else "") or (
        existing.get("model") if not changed else None
    ) or saved.get("model") or ""
    light = patch.get("lightModel")
    if isinstance(light, str) and light.strip():
        updates["lightModel"] = light.strip()
    elif changed or "lightModel" in patch:
        updates.pop("lightModel", None)
    if changed or patch.get("thinkingMode"):
        updates["thinkingMode"] = patch.get("thinkingMode") or saved.get("thinkingMode") or "disabled"
    for key, low, high, default in (("timeoutMs", 1000, math.inf, 300_000), ("maxRetries", 0, 20, 5)):
        number = _number(patch.get(key), None)
        updates[key] = (max(low, min(high, math.floor(number))) if number is not None
                        else _number(existing.get(key), default))
    return updates


class PipelineRuntime:
    def __init__(self, store, hotspots_feed=None, meme_store=None, client=None):
        self.store = store
        self.hotspots_feed = hotspots_feed if hotspots_feed is not None else HotspotsFeed(store.directory, client=client)
        self.meme_store = meme_store if meme_store is not None else MemeStore(store.directory, client=client)
        self.client = client
        self._active = {}
        self._locks = {}
        self._subscribers = {}
        self.runner = self._consume
        self._closing = False

    async def _job(self, job_id):
        job = await asyncio.to_thread(self.store.get_job_record, job_id)
        if job is None:
            raise PipelineError("找不到作品。", "JOB_NOT_FOUND", 404)
        return job

    def _lock(self, job_id):
        return self._locks.setdefault(job_id, asyncio.Lock())

    def _publish(self, job_id, event, **fields):
        if "job" in fields:
            fields["job"] = _safe_job(fields["job"])
        payload = {"jobId": job_id, "event": event, "ts": _now(), **fields}
        for queue in self._subscribers.get(job_id, ()):
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(payload)

    async def subscribe_job_events(self, job_id, *, max_pending=128, include_snapshot=True):
        queue = asyncio.Queue(maxsize=max(1, max_pending))
        listeners = self._subscribers.setdefault(job_id, set())
        listeners.add(queue)
        try:
            if include_snapshot:
                yield {"jobId": job_id, "event": "snapshot", "ts": _now(), "job": _safe_job(await self._job(job_id))}
            while True:
                yield deepcopy(await queue.get())
        finally:
            listeners.discard(queue)
            if not listeners:
                self._subscribers.pop(job_id, None)

    async def _log(self, job_id, level, stage, message, meta=None):
        entry = await _write(self.store.append_job_log, job_id,
                             {"level": level, "stage": stage, "message": message, "meta": meta})
        if entry is not None:
            self._publish(job_id, "job:log", log=entry)

    async def _update(self, job_id, patch):
        return await _write(self.store.update_job, job_id, patch)

    async def _checkpoint(self, job_id, stage, **fields):
        return await _write(self.store.save_checkpoint, job_id, {"stage": stage, **fields})

    def is_job_running(self, job_id):
        task = self._active.get(job_id)
        return task is not None and not task.done()

    async def _consume(self, job_id):
        async with aclosing(self.run_pipeline(job_id)) as stages:
            phase = await anext(stages)
            while True:
                try:
                    phase = await stages.asend(review_action(phase) if phase["phase"] == "review" else None)
                except StopAsyncIteration:
                    return

    async def _launch(self, job):
        if self._closing:
            raise PipelineError("工作台正在关闭。", "SERVICE_STOPPING", 503)
        job_id = job["id"]
        if self.is_job_running(job_id) or job.get("status") == "completed":
            return _safe_job(job)
        running = await self._update(job_id, {"status": "running", "error": None, "autoRetryState": None})
        task = asyncio.create_task(self.runner(job_id), name=f"write:{job_id}")
        self._active[job_id] = task

        def finished(done):
            if self._active.get(job_id) is done:
                self._active.pop(job_id, None)
            if not done.cancelled():
                done.exception()

        task.add_done_callback(finished)
        return _safe_job(running)

    async def start_job(self, job_id):
        async with self._lock(job_id):
            return await self._launch(await self._job(job_id))

    async def wait_for_job(self, job_id):
        task = self._active.get(job_id)
        if task is not None:
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling() or not task.cancelled():
                    raise
                # Pause owns the state change after the writer has stopped.
                async with self._lock(job_id):
                    return _safe_job(await self._job(job_id))
        return _safe_job(await self._job(job_id))

    async def pause_job(self, job_id):
        async with self._lock(job_id):
            task = self._active.get(job_id)
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            job = await self._job(job_id)
            if job.get("status") not in ("completed", "paused"):
                job = await self._update(job_id, {"status": "paused", "autoRetryState": None})
                await self._log(job_id, "info", job.get("stage"), "任务已由用户暂停，断点已保存")
                job = await self._job(job_id)
                self._publish(job_id, "job:paused", job=job)
            return _safe_job(job)

    async def _patch_llm(self, job, patch):
        provider = str(patch.get("provider") or _dict(job.get("llmConfig")).get("provider") or "deepseek").strip().lower()
        if get_provider_definition(provider) is None:
            raise PipelineError("不支持此模型供应商。", "UNSUPPORTED_PROVIDER", 400)
        stored = await asyncio.to_thread(self.store.get_provider_config, provider)
        config = _patched_connection(_dict(job.get("llmConfig")), patch, stored)
        updates = {"llmConfig": config}
        if type(patch.get("autoResumeOnRateLimit")) is bool:
            updates["autoResumeOnRateLimit"] = patch["autoResumeOnRateLimit"]
        changed = await self._update(job["id"], updates)
        await self._log(job["id"], "info", job.get("stage") or "config",
                        f"⚙️ 模型配置已更新: provider={provider}, model={config.get('model') or '(默认)'}")
        return changed

    async def update_job_llm_config(self, job_id, llm_patch=None):
        async with self._lock(job_id):
            return _safe_job(await self._patch_llm(await self._job(job_id), _dict(llm_patch)))

    async def resume_job(self, job_id, *, llm_config=None):
        async with self._lock(job_id):
            job = await self._job(job_id)
            if job.get("status") == "completed":
                return _safe_job(job)
            previous = self._active.get(job_id)
            if previous is not None and not previous.done():
                if job.get("status") not in ("paused", "interrupted", "failed"):
                    return _safe_job(job)
                # The terminal status can be visible before LangGraph finishes its last checkpoint.
                await asyncio.shield(asyncio.gather(previous, return_exceptions=True))
                job = await self._job(job_id)
                if job.get("status") == "completed":
                    return _safe_job(job)
            if llm_config is not None:
                job = await self._patch_llm(job, _dict(llm_config))
            return await self._launch(job)

    async def extend_job(self, job_id, *, add_chapters=None, add_words=None, target_total_words=None,
                         direction="", finalize_book=False, meme_level=None, meme_source_mode=None, llm_config=None):
        async with self._lock(job_id):
            if self.is_job_running(job_id):
                raise PipelineError("任务正在运行中，请等待当前阶段完成或先暂停后再续写", "JOB_RUNNING")
            job = await self._job(job_id)
            previous = int(_number(job.get("totalChapters")))
            actual = _number(job.get("totalWords"))
            target = _number(job.get("targetWords"))
            base = _number(job.get("wordsPerChapter")) or _round((target or 20000) / max(1, previous)) or _WORD_TARGET_DEFAULT
            requested = _number(target_total_words)
            baseline = actual or target
            if requested > baseline:
                total_words = requested
                added = int(_number(add_chapters)) or _round((requested - baseline) / max(800, base))
            else:
                added = int(_number(add_chapters)) or 10
                total_words = max(target, actual) + (max(500, _number(add_words)) if add_words else max(1, min(300, added)) * base)
            added = max(1, min(300, added))
            total = previous + added
            direction = str(direction or "").strip()
            old_plan = _dict(job.get("bookPlan"))
            volumes = _list(old_plan.get("volumes"))
            notes = _list(old_plan.get("continuationNotes"))
            title = "终局完结篇" if finalize_book else "续章新篇"
            plan = {**old_plan, "volumes": [*volumes, {
                "volume": len(volumes) + 1, "title": f"第{len(volumes) + 1}卷 {title}", "chapters": [previous + 1, total],
                "summary": direction or f"承接前{previous}章剧情与未回收伏笔继续推进主线冲突",
            }], "continuationNotes": [*notes, *([f"[第{previous + 1}-{total}章续写方向] {direction}"] if direction else [])]}
            if llm_config is not None:
                await self._patch_llm(job, _dict(llm_config))
            checkpoint = {"stage": "chapter_outlines", "nextOutlineChapter": previous + 1, "nextChapter": previous + 1,
                          "completedChapter": _number(job.get("completedChapters")), "savedAt": _now()}
            patch = {
                "stage": "chapter_outlines", "totalChapters": total, "targetWords": total_words, "bookPlan": plan,
                "continuationDirection": direction or job.get("continuationDirection") or "", "extendedFromChapter": previous,
                "finalizeBook": bool(finalize_book), "auditGate": None, "auditReport": None, "error": None, "checkpoint": checkpoint,
                "status": "running",
            }
            if meme_level:
                patch["memeLevel"] = meme_level
            if meme_source_mode:
                patch["memeSourceMode"] = meme_source_mode
            next_job = await self._update(job_id, patch)
            await self._log(job_id, "info", "chapter_outlines", f"发起续写：追加第 {previous + 1}~{total} 章（新目标总字数 {total_words} 字）")
            return await self._launch(next_job)

    async def recover_interrupted_jobs(self, *, auto_resume=True):
        recovered = []
        for listed in await asyncio.to_thread(self.store.list_jobs):
            job_id = listed["id"]
            if listed.get("status") not in ("running", "interrupted"):
                continue
            async with self._lock(job_id):
                job = await self._job(job_id)
                if self.is_job_running(job_id) or job.get("status") not in ("running", "interrupted"):
                    continue
                updated = await self._update(job_id, {"status": "interrupted", "autoRetryState": None})
                recovered.append(_safe_job(updated))
                stage = _dict(updated.get("checkpoint")).get("stage") or updated.get("stage")
                await self._log(job_id, "warn", updated.get("stage"), f"检测到服务重启前的未完成任务，从断点恢复（stage={stage}）")
                if auto_resume and _dict(updated.get("error")).get("code") not in (
                    "OUTLINE_PARSE_FAILED", "ARC_SUMMARY_INVALID", "ARC_SOURCE_CHANGED",
                ):
                    await self._launch(updated)
        return recovered

    async def close(self):
        self._closing = True
        running = tuple(self._active.items())
        for _, task in running:
            task.cancel()
        await asyncio.gather(*(task for _, task in running), return_exceptions=True)
        for job_id, _ in running:
            await self._update(job_id, lambda job: {"status": "interrupted", "autoRetryState": None}
                               if job.get("status") == "running" else {})

    async def export_novel(self, job_id, format="txt"):
        detail = await asyncio.to_thread(self.store.job_detail, job_id)
        if detail is None:
            raise PipelineError("找不到作品。", "JOB_NOT_FOUND", 404)
        return await asyncio.to_thread(_export, detail["job"], detail["chapters"], format)

    async def _record_usage(self, job_id, stage, usage, duration_ms, retries=0, model=None):
        usage = _dict(usage)
        increments = {key: _number(usage.get(key)) for key in _USAGE_KEYS}

        def add_usage(prior):
            return {key: _number(_dict(prior).get(key)) + increments[key] for key in _USAGE_KEYS}

        def patch(job):
            stats = _dict(job.get("stageStats"))
            prior = _dict(stats.get(stage))
            stage_stats = {
                **add_usage(prior), "calls": _number(prior.get("calls")) + 1,
                "retries": _number(prior.get("retries")) + retries,
                "durationMs": _number(prior.get("durationMs")) + _round(duration_ms),
            }
            by_model = _dict(job.get("tokenUsageByModel"))
            if model and any(increments.values()):
                by_model = {**by_model, model: add_usage(by_model.get(model))}
            return {"tokenUsage": add_usage(job.get("tokenUsage")), "tokenUsageByModel": by_model,
                    "stageStats": {**stats, stage: stage_stats}}

        await self._update(job_id, patch)

    async def _llm(self, job_id, stage, config, prompt, temperature, *, max_tokens=None, retries=0):
        options = _llm_options(config)
        if max_tokens is not None:
            options["max_tokens"] = max_tokens
        started = time.monotonic()
        reply = await call_llm(
            **options, **prompt, temperature=temperature,
            cache_key=f"nf-write-{job_id}-{stage}"[:64],
            store=self.store, client=self.client,
        )
        await self._record_usage(job_id, stage, reply.get("usage"), reply.get("durationMs") or _elapsed(started),
                                 retries, config.get("model") or config.get("provider"))
        return reply

    async def _set_stage(self, job_id, stage, progress=None):
        patch = {"stage": stage}
        if progress is not None:
            patch["progress"] = progress
        job = await self._update(job_id, patch)
        self._publish(job_id, "job:stage", stage=stage, progress=job.get("progress", 0))
        return job

    async def _boundary_interrupt(self, job_id, scenario, stage):
        if not isinstance(scenario, dict) or scenario.get("interruptAtStage") != stage:
            return
        key = f"interrupt_{stage}"
        if _dict(scenario.get("_triggered")).get(key):
            return
        updated = {**scenario, "_triggered": {**_dict(scenario.get("_triggered")), key: True}}
        await self._update(job_id, {"testScenario": updated})
        raise LLMError(f"Simulated interruption at stage {stage}", code="TIMEOUT", status=408, retryable=True)

    async def _failed(self, job_id, stage, code, message, *, status="failed", retryable=True, fields=None, **error_fields):
        error = {"code": code, "message": message, "retryable": retryable, **error_fields}
        await self._update(job_id, {"status": status, "stage": stage, "error": error, "autoRetryState": None, **(fields or {})})
        await self._log(job_id, "error", stage, message, {"chapterNumber": error_fields["chapterNumber"]}
                        if error_fields.get("chapterNumber") else None)
        self._publish(job_id, "job:error", job=await self._job(job_id), error=error)

    async def run_pipeline(self, job_id):
        """Advance by awaiting the next phase, or send the action chosen at review."""
        yield {"phase": "prepare"}
        progress_key, retry_count = None, 0
        while True:
            try:
                async with aclosing(self._run_once(job_id)) as stages:
                    phase = await anext(stages)
                    while True:
                        action = yield phase
                        phase = await stages.asend(action)
            except StopAsyncIteration:
                return
            except asyncio.CancelledError:
                raise
            except Exception as error:
                current = await self._job(job_id)
                key = current.get("stage"), _number(current.get("completedChapters"))
                retry_count = retry_count + 1 if key == progress_key else 1
                progress_key = key
                code = getattr(error, "code", "PIPELINE_ERROR")
                if code in ("ABORTED", "JOB_PAUSED"):
                    paused = await self._update(job_id, {"status": "paused", "autoRetryState": None})
                    self._publish(job_id, "job:paused", job=paused)
                    return
                if not self._can_auto_retry(current, error, code, retry_count):
                    retry_at = getattr(error, "retry_at", None) or getattr(error, "circuit_retry_at", None)
                    output_budget = getattr(error, "request_max_tokens", None)
                    stage = current.get("stage") or "init"
                    retryable = (code in _MANUAL_RESUME or code in _TRANSIENT_ERRORS or bool(getattr(error, "retryable", False)))
                    await self._failed(
                        job_id, stage, code, f"{error}", status="interrupted" if retryable else "failed", retryable=retryable,
                        chapterNumber=_dict(current.get("checkpoint")).get("nextChapter") if stage == "chapter_writing" else None,
                        **({"retryAt": retry_at} if retry_at else {}),
                        **({"outputBudget": output_budget, "finishReason": getattr(error, "finish_reason", None)}
                           if output_budget else {}),
                    )
                    return
                yield {"phase": "retry"}
                await self._cooldown(job_id, current, error, code, retry_count)

    @staticmethod
    def _can_auto_retry(job, error, code, attempts):
        return (
            attempts <= _MAX_AUTO_RESUMES and not job.get("testScenario") and code not in _MANUAL_RESUME
            and not getattr(error, "circuit_retry_at", None)
            and (code in _TRANSIENT_ERRORS or getattr(error, "retryable", False))
            and job.get("autoResumeOnRateLimit") is not False
            and (_dict(job.get("llmConfig")).get("provider") != "mock" or job.get("autoResumeOnRateLimit") is True)
        )

    async def _cooldown(self, job_id, job, error, code, attempt):
        steps = (45_000, 75_000, 120_000, 180_000, 240_000, 300_000) if code == "HTTP_429" else (
            15_000, 30_000, 60_000, 120_000, 180_000,
        )
        configured = _number(job.get("autoResumeCooldownMs"))
        delay = configured if configured > 0 else steps[min(attempt - 1, len(steps) - 1)] + random.randrange(4000)
        state = {"waiting": True, "code": code, "message": str(error)[:160], "attempt": attempt,
                 "maxAttempts": _MAX_AUTO_RESUMES, "cooldownMs": delay, "resumeAt": _now(delay)}
        stage = job.get("stage") or "chapter_writing"
        await self._update(job_id, {"status": "running", "error": None, "autoRetryState": state})
        await self._log(job_id, "warn", stage,
                        f"触发限流/网络波动（{code}），保留已完成断点，自动冷却 {max(1, _round(delay / 1000))} 秒后继续创作")
        self._publish(job_id, "job:progress", stage=stage, progress=job.get("progress"), autoRetryState=state)
        await asyncio.sleep(delay / 1000)
        await self._update(job_id, {"autoRetryState": None})
        await self._log(job_id, "info", stage, "自动冷却完成，正在从断点继续创作")

    async def _topic(self, job, config):
        if job.get("topicPackage") is not None:
            return job
        job_id = job["id"]
        started = time.monotonic()
        await self._set_stage(job_id, "topic_selection", 2)
        await self._log(job_id, "info", "topic_selection", "正在锁定选题立项方案...")
        package = await select_or_generate_concept(
            premise=job.get("premise") or "", title="" if job.get("title") == _UNTITLED else job.get("title") or "",
            genre=job.get("genre") or "都市爽文", pov=job.get("pov") or "第三人称限知", llm_config=config,
            prefer_freeform=bool(job.get("preferFreeform")), meme_level=job.get("memeLevel") or "none",
            meme_source_mode=job.get("memeSourceMode") or "active_only", feed=self.hotspots_feed, meme_store=self.meme_store,
            store=self.store, client=self.client,
        )
        package = {**package, "lockedAt": package.get("lockedAt") or _now()}
        await self._record_usage(job_id, "topic_selection", package.get("usage"), _elapsed(started), model=config.get("model"))
        job = await self._update(job_id, {
            "title": (package.get("title") or _UNTITLED) if job.get("title") == _UNTITLED else job.get("title"),
            "premise": job.get("premise") or package.get("premise") or "", "topicPackage": package,
            "inspirationMode": package.get("inspirationMode") or "user_premise", "progress": 4,
        })
        await self._checkpoint(job_id, "topic_locked", nextChapter=1, completedChapter=0)
        await self._log(job_id, "info", "topic_selection", f"选题已锁定（模式：{job['inspirationMode']}）：《{job['title']}》")
        self._publish(job_id, "job:progress", stage="topic_selection", progress=4, topicPackage=package,
                      inspirationMode=job["inspirationMode"])
        await self._boundary_interrupt(job_id, config.get("testScenario"), "topic_locked")
        return job

    async def _book_plan(self, job, config, meme_level, meme_samples, draft_quality, *, rolling_arc_outlines=False):
        if job.get("bookPlan") is not None:
            return job
        job_id = job["id"]
        await self._set_stage(job_id, "book_plan", 5)
        await self._log(job_id, "info", "book_plan", "开始生成全书总纲 (BookPlan)...")
        prompt = build_book_plan_prompt(
            title=job.get("title") or "", premise=job.get("premise") or "", genre=job.get("genre") or "都市爽文",
            pov=job.get("pov") or "第三人称限知", target_words=job["targetWords"], total_chapters=job["totalChapters"],
            hotspot_context=job.get("hotspotContext"), topic_package=job.get("topicPackage"), meme_level=meme_level,
            meme_samples=meme_samples, draft_quality=draft_quality, rolling_arc_outlines=rolling_arc_outlines,
        )
        reply = await self._llm(job_id, "book_plan", config, prompt, 0.7)
        fallback = {
            "title": job.get("title"), "genre": job.get("genre"), "pov": job.get("pov"),
            "logline": job.get("premise") or "主角逆袭破局", "foreshadowingPlan": [],
            "volumes": [{"volume": 1, "title": "第一卷", "chapters": [1, job["totalChapters"]], "summary": "主线推进"}],
        }
        parsed = parse_json_from_llm(reply.get("text"))
        plan = parsed if isinstance(parsed, dict) else fallback
        locks = plan.pop("canonLocks", None)
        if rolling_arc_outlines and (locks := normalize_canon_locks(locks)):
            plan["canonLocks"] = locks
        if draft_quality and "styleBible" in plan:
            style = normalize_style_bible(plan["styleBible"])
            if style is None:
                plan.pop("styleBible")
            else:
                plan["styleBible"] = style
        title = (plan.get("title") or _UNTITLED) if job.get("title") == _UNTITLED else job.get("title")
        job = await self._update(job_id, {"bookPlan": plan, "title": title, "progress": 10})
        await self._checkpoint(job_id, "chapter_outlines", nextOutlineChapter=1)
        await self._log(job_id, "info", "book_plan", f"全书总纲生成完成：《{title}》")
        self._publish(job_id, "job:progress", stage="book_plan", progress=10, bookPlan=plan)
        return job

    async def _outlines(self, job, config, *, through=None):
        job_id, total = job["id"], int(job["totalChapters"])
        saved = _saved_outlines(job)
        placeholders = {n for n, row in saved.items() if legacy_outline_placeholder(row, n)}
        if placeholders:
            chapters = await asyncio.to_thread(self.store.get_chapters, job_id)
            committed = {_chapter_number(chapter) for chapter in chapters if _is_committed(chapter)}
            for number in placeholders - committed:
                saved.pop(number, None)

        def missing_numbers():
            return [number for number in range(1, total + 1) if number not in saved]

        def missing_ranges(numbers):
            if not numbers:
                return
            start = end = numbers[0]
            for number in numbers[1:]:
                if number != end + 1 or number - start >= 5:
                    yield start, end
                    start = number
                end = number
            yield start, end

        missing = [n for n in missing_numbers() if through is None or n <= through]
        if not missing:
            return saved
        await self._set_stage(job_id, "chapter_outlines", 12 if through is None else None)
        message = f"开始生成 {total} 章连续章纲..." if through is None else f"补齐历史章纲缺口（至第 {through} 章）。"
        await self._log(job_id, "info", "chapter_outlines", message)
        facts = extract_relevant_facts(await asyncio.to_thread(self.store.get_canonical_facts, job_id)) if missing[0] > 1 else None

        async def fill(start, end):
            prompt = build_chapter_outlines_prompt(
                book_plan=job["bookPlan"], start_chapter=start, end_chapter=end, total_chapters=total,
                existing_outlines=[saved[n] for n in sorted(saved) if n < start], target_words_per_chapter=job["wordsPerChapter"],
                topic_package=job.get("topicPackage"), relevant_facts=facts, continuation_direction=job.get("continuationDirection") or "",
                finalize_book=bool(job.get("finalizeBook")),
            )
            reply = await self._llm(job_id, "chapter_outlines", config, prompt, 0.6)
            decoded = parse_json_from_llm(reply.get("text"), [])
            batch = decoded if isinstance(decoded, list) else _list(_dict(decoded).get("chapters")) or _list(_dict(decoded).get("outlines"))
            for entry in batch:
                entry = _dict(entry)
                number = _chapter_number(entry)
                title, summary = entry.get("title"), entry.get("summary")
                has_story = (isinstance(summary, str) and summary.strip()) or any(
                    isinstance(event, str) and event.strip() for event in _list(entry.get("keyEvents"))
                )
                if (type(number) is int and start <= number <= end and isinstance(title, str)
                        and title.strip() and has_story and not legacy_outline_placeholder(entry, number)):
                    saved.setdefault(number, {**entry, "chapterNumber": number})
            pending = missing_numbers()
            await _write(self.store.save_checkpoint, job_id, {
                "stage": "chapter_outlines", "nextOutlineChapter": pending[0] if pending else total + 1,
            }, outlines=[saved[n] for n in sorted(saved)])

        for start, end in missing_ranges(missing):
            await fill(start, end)
            remaining = [number for number in range(start, end + 1) if number not in saved]
            for first, last in missing_ranges(remaining):
                await fill(first, last)
            remaining = [number for number in remaining if number not in saved]
            if remaining:
                chapters = "、".join(map(str, remaining))
                raise LLMError(f"第 {chapters} 章缺少可用章纲。已保存有效章纲，可从断点补齐缺章。",
                               code="OUTLINE_PARSE_FAILED", status=502, retryable=True)
        if through is not None:
            return saved
        await self._update(job_id, {"progress": 15})
        chapters = await asyncio.to_thread(self.store.get_chapters, job_id)
        committed = {_chapter_number(chapter) for chapter in chapters if _is_committed(chapter)}
        next_missing = next((number for number in range(1, total + 1) if number not in committed), total + 1)
        await self._checkpoint(job_id, "chapter_writing", nextChapter=next_missing, completedChapter=next_missing - 1)
        await self._log(job_id, "info", "chapter_outlines", f"全部 {len(saved)} 章章纲生成完成")
        self._publish(job_id, "job:progress", stage="chapter_outlines", progress=15)
        return saved

    async def _plan_arc(self, job, arcs, outlines, start, config, archive_config, *, story_ledger=False):
        job_id, total = job["id"], job["totalChapters"]
        checkpoint = {"stage": "chapter_outlines", "nextChapter": start, "nextOutlineChapter": start,
                      "completedChapter": start - 1}
        await self._set_stage(job_id, "chapter_outlines")
        await self._checkpoint(job_id, **checkpoint)
        await self._refresh_arc_archives(job_id, arcs, start, archive_config,
                                         story_ledger=story_ledger, book_plan=job.get("bookPlan"))
        preceding = await asyncio.to_thread(self.store.get_chapters, job_id, end_chapter=start - 1)
        preceding = [row for row in preceding if _is_committed(row)][-5:]
        facts = await asyncio.to_thread(self.store.get_canonical_facts, job_id)
        source = arc_context_version(facts, preceding, start)
        available = {(row.get("arc"), row["startChapter"], row["endChapter"]) for row in prior_arc_summaries(facts, start)}
        if any((arc["arcIndex"], arc["startChapter"], arc["endChapter"]) not in available
               for arc in arcs if arc["endChapter"] < start):
            raise LLMError("已有单元的正文在规划前发生变化，请从断点重新归档。",
                           code="ARC_SOURCE_CHANGED", status=409, retryable=True)
        arc_index = max((arc["arcIndex"] for arc in arcs), default=0) + 1
        prior_arcs = [arc for arc in arcs if arc["endChapter"] < start][-2:]
        title_floor = prior_arcs[0]["startChapter"] if prior_arcs else max(1, start - 12)
        titles = [outlines[n].get("title") for n in sorted(outlines) if title_floor <= n < start]
        prompt = build_arc_outlines_prompt(
            book_plan=job["bookPlan"], canonical_facts=facts, start_chapter=start, total_chapters=total,
            arc_index=arc_index, preceding_chapters=preceding, recent_titles=titles,
            continuation_direction=job.get("continuationDirection") or "", finalize_book=bool(job.get("finalizeBook")),
            story_ledger=story_ledger,
        )
        await self._log(job_id, "info", "chapter_outlines", f"从第 {start} 章规划第 {arc_index} 剧情单元。")
        error = ""
        for attempt in range(2):
            request = prompt if not attempt else {**prompt, "prompt": (
                f"{prompt['prompt']}\n上次校验未通过：{error} 请纠正并完整重发本单元 JSON。"
            )}
            reply = await self._llm(job_id, "chapter_outlines", config, request, 0.6, retries=attempt)
            try:
                arc, batch = validate_arc_plan(parse_json_from_llm(reply.get("text")), arc_index=arc_index,
                                               start=start, total=total, recent_titles=titles)
            except ValueError as issue:
                error = str(issue)
                await self._log(job_id, "warn", "chapter_outlines", f"第 {start} 章单元章纲第 {attempt + 1} 次校验未通过：{error}")
                continue
            next_outline = arc["endChapter"] + 1
            saved = await _write(self.store.save_arc_plan, job_id, arc, batch, total=total, expected_context=source,
                                 checkpoint={**checkpoint, "stage": "chapter_writing", "nextOutlineChapter": next_outline})
            if not saved:
                raise LLMError("规划期间已提交正文或单元边界发生变化，请从断点继续。",
                               code="ARC_SOURCE_CHANGED", status=409, retryable=True)
            arcs.append(arc)
            outlines.update((row["chapterNumber"], row) for row in batch)
            await self._log(job_id, "info", "chapter_outlines", f"已锁定第 {start}–{arc['endChapter']} 章单元「{arc['arcTitle']}」。")
            return
        raise LLMError(f"第 {start} 章起的单元章纲未通过校验：{error} 已保存断点，可继续补齐。",
                       code="OUTLINE_PARSE_FAILED", status=502, retryable=True)

    async def _run_once(self, job_id):
        job = await self._update(job_id, {"status": "running", "error": None, "autoRetryState": None})
        options = read_writing_options(job)
        rolling = options["rolling_arc_outlines"]
        story_ledger = options["story_ledger"]
        config = {**_dict(job.get("llmConfig")), "testScenario": job.get("testScenario")}
        auxiliary = {**config, "model": config.get("lightModel") or config.get("model")}
        archive_model = os.environ.get("NOVEL_FORGE_ARC_REVIEW_MODEL", "").strip() if story_ledger else ""
        archive_config = {**auxiliary, "model": archive_model} if archive_model else auxiliary
        total = int(job["totalChapters"])
        meme_level = job.get("memeLevel") or "none"

        yield {"phase": "topic"}
        job = await self._topic(job, config)
        samples = []
        if meme_level != "none":
            mode = job.get("memeSourceMode") or "active_only"
            limit = min(12, max(4, total // 4))
            pool = _dict(job.get("memeSamplePool"))
            if pool.get("mode") == mode and pool.get("limit") == limit:
                samples = _list(pool.get("samples"))
            if not samples:
                picks = await self.meme_store.get_meme_inspiration_samples(
                    genre=job.get("genre") or "", meme_source_mode=mode, limit=limit,
                )
                unique = {}
                for sample in picks:
                    if key := _meme_title(sample):
                        unique.setdefault(key, sample)
                samples = list(unique.values())[:limit]
                if samples:
                    job = await self._update(job_id, {"memeSamplePool": {"mode": mode, "limit": limit, "samples": samples}})
        used_samples = set(title for title in _list(job.get("usedMemeTitles")) if isinstance(title, str))
        yield {"phase": "plan"}
        job = await self._book_plan(job, config, meme_level, samples[:4], options["draft_quality"], rolling_arc_outlines=rolling)
        committed = {
            chapter["chapterNumber"]: chapter for chapter in await asyncio.to_thread(self.store.get_chapters, job_id)
            if _is_committed(chapter)
        }
        running_words = sum(_word_count(chapter) for chapter in committed.values())
        first_missing = next((number for number in range(1, total + 1) if number not in committed), total + 1)
        arcs = saved_arc_plans(job) if rolling else []
        if not rolling:
            yield {"phase": "outlines"}
            outlines = await self._outlines(job, config)
            if options["long_memory"] and (last_full_volume := (first_missing - 1) // 10 * 10):
                if await self._needs_volume_archive(job_id, last_full_volume, committed):
                    yield {"phase": "archive", "chapter": last_full_volume}
                    await self._archive(job_id, last_full_volume, committed, auxiliary)
            job = await self._set_stage(job_id, "chapter_writing")
        else:
            existing = _saved_outlines(job)
            historical_tail = max([0, *existing, *committed, *(arc["endChapter"] for arc in arcs)])
            need_historical = bool(historical_tail and any(
                n not in existing or (legacy_outline_placeholder(existing[n], n) and n not in committed)
                for n in range(1, historical_tail + 1)
            ))
            pending_archives = await self._pending_arc_archives(job_id, arcs, first_missing) if first_missing <= total else []
            in_outlines = False
            if need_historical:
                yield {"phase": "outlines"}
                in_outlines = True
                outlines = await self._outlines(job, config, through=historical_tail)
            else:
                outlines = existing
            if pending_archives:
                yield {"phase": "archive", "chapter": pending_archives[-1]["endChapter"]}
                in_outlines = False
                await self._refresh_arc_archives(job_id, arcs, first_missing, archive_config,
                                                 story_ledger=story_ledger, book_plan=job.get("bookPlan"))
            if first_missing <= total and first_missing not in outlines:
                if not in_outlines:
                    yield {"phase": "outlines", **({"chapter": first_missing} if first_missing > 1 else {})}
                await self._plan_arc(job, arcs, outlines, first_missing, config, archive_config, story_ledger=story_ledger)
            elif not in_outlines and not pending_archives:
                yield {"phase": "outlines"}
            job = await self._set_stage(job_id, "chapter_writing", 15 if _number(job.get("progress")) < 15 else None)

        for number in range(1, total + 1):
            if number in committed:
                continue
            if rolling and number not in outlines:
                yield {"phase": "outlines", "chapter": number}
                await self._plan_arc(job, arcs, outlines, number, config, archive_config, story_ledger=story_ledger)
                await self._set_stage(job_id, "chapter_writing")
            outline = outlines[number]
            next_outline = max(outlines) + 1 if rolling else None
            outline_cursor = {"nextOutlineChapter": next_outline} if rolling else {}
            yield {"phase": "chapter", "chapter": number}
            # A previous attempt can leave provisional facts. Preserve committed facts past an imported gap.
            if not any(saved > number for saved in committed):
                await _write(self.store.delete_uncommitted_facts, job_id, number)
            await self._checkpoint(job_id, "chapter_writing", nextChapter=number, completedChapter=number - 1, **outline_cursor)
            await self._boundary_interrupt(job_id, config.get("testScenario"), "chapter_writing")
            self._publish(job_id, "chapter:started", chapterNumber=number, totalChapters=total, title=outline.get("title"))
            recent = [committed[n] for n in sorted(committed) if n < number][-5:]
            recent_chapters = [
                committed.get(n, {}).get("content") or "" for n in range(max(1, number - 3), number)
            ] if options["chapter_ai_gate"] else None
            prev = (recent[-1].get("content") or "") if recent else ""
            summaries = [{"chapterNumber": ch["chapterNumber"], "summary": ch.get("summary") or "",
                          "needsReview": ch.get("status") == "needs_review"} for ch in recent]
            facts = await asyncio.to_thread(self.store.get_canonical_facts, job_id)
            context = " ".join(str(item or "") for item in (
                outline.get("title"), outline.get("summary"), *_list(outline.get("keyEvents")), _excerpt(prev, 300, tail=True),
            ))
            relevant = extract_relevant_facts(facts, context_text=context, current_chapter=number, story_ledger=story_ledger)
            remaining = total - len(committed)
            target = compute_dynamic_chapter_target(job, running_words, remaining, chapter_number=number)
            finale = _is_finale(number, total)
            pov = job.get("pov") or "第三人称限知"
            style_key = job.get("stylePreset") or job.get("style")
            style = job.get("styleGuide") or STYLE_PRESETS.get(style_key, STYLE_PRESETS["番茄爽文"])
            sample = None
            if number <= 3 or number % 3 == 0 or _COMEDY.search(f"{context} {outline.get('goal') or ''}"):
                sample = next((entry for entry in samples if _meme_title(entry) not in used_samples), None)
            prompt = build_chapter_write_prompt(
                book_plan=job["bookPlan"], outline=outline, prev_chapter_ending=prev, recent_summaries=summaries,
                canonical_facts=facts, relevant_facts=relevant, input_budget=options["input_budget"],
                draft_quality=options["draft_quality"], long_memory=options["long_memory"], pov=pov,
                chapter_ai_gate=options["chapter_ai_gate"],
                rolling_arc_outlines=rolling, story_ledger=story_ledger,
                target_words=target, total_chapters=total, style_guide=style, meme_level=meme_level,
                meme_samples=[sample] if sample is not None else [],
            )
            meme_title = _meme_title(sample) if sample and any(
                line.startswith(f"- 【{sample['title']}】") for line in prompt["system"].splitlines()
            ) else None
            written = await self._llm(job_id, "chapter_writing", config, prompt, config.get("temperature", 0.7))
            chapter_text = str(written.get("text") or "").strip()
            if output_was_truncated(written.get("finishReason")):
                raise LLMError("写章输出达到上限，请从断点继续。", code="OUTPUT_TRUNCATED", status=502, retryable=True)
            words = count_chinese_words(chapter_text)
            minimum = max(200, _round(target * (0.82 if finale else 0.72)))
            maximum = _round(target * (1.25 if finale else 1.38))
            if (words < max(200, _round(target * 0.72)) or words > _round(target * 1.38)
                    or finale and (words < target * 0.85 or words > target * 1.2)):
                chapter_text = await self._adjust_words(job_id, number, chapter_text, words, target, outline, pov, auxiliary)
            title = _MARKDOWN_HEADING.sub("", str(outline.get("title") or f"第{number}章")).strip()
            if options["draft_quality"]:
                chapter_text = clean_chapter_draft(chapter_text, title=title, chapter_number=number,
                                                  previous_ending=prev, min_words=minimum)

            max_repairs = max(0, int(_number(job.get("maxRepairRetries"), 2)))
            started = time.monotonic()

            async def extract_missing_facts(body):
                fact_prompt = build_fact_extraction_prompt(
                    chapter_number=number, chapter_text=body, outline=outline, canonical_facts=facts,
                    long_memory=options["long_memory"], story_ledger=story_ledger, book_plan=job.get("bookPlan"),
                )
                reply = await self._llm(job_id, "fact_extraction", auxiliary, fact_prompt, 0.2)
                return _dict(parse_json_from_llm(reply.get("text"))) if not output_was_truncated(reply.get("finishReason")) else {}

            async with aclosing(review_chapter_stages(
                job_id=job_id, chapter_number=number, chapter_text=chapter_text, outline=outline, canonical_facts=facts,
                book_plan=job.get("bookPlan"), recent_chapters=recent_chapters, rolling_arc_outlines=rolling,
                story_ledger=story_ledger, extract_missing_facts=extract_missing_facts if story_ledger else None,
                prev_chapter_ending=prev, pov=pov, target_words=target, min_words=minimum, max_words=maximum,
                llm_config=config, verify_llm_config=auxiliary, test_scenario=config.get("testScenario"),
                max_retries=max_repairs, chapter_ai_gate=options["chapter_ai_gate"], long_memory=options["long_memory"],
                degrade_on_verify_exhausted=options["degrade_on_verify_exhausted"], store=self.store, client=self.client,
                on_event=lambda name, fields: self._publish(job_id, name, **fields),
            )) as review:
                verify = await anext(review)
                while True:
                    yield verify
                    decision = await anext(review)
                    result = decision["result"]
                    body = result["chapterText"]
                    if not options["draft_quality"]:
                        body = strip_leading_chapter_heading(body, title, number)
                    elif result["repairCount"]:
                        body = strip_draft_headings(body, title=title, chapter_number=number, min_words=minimum)
                    needs_review = result["canCommitForReview"] and (
                        body == result["chapterText"] or can_save_unverified_draft(
                            body, result["verification"], chapter_number=number, canonical_facts=facts, pov=pov, min_words=minimum,
                        )
                    )
                    decision = {**decision, "canCommitForReview": needs_review}
                    expected = review_action(decision)
                    action = yield decision
                    if action is not None and action != expected:
                        raise PipelineError(f"阶段 {expected} 收到不匹配的审校动作 {action}。", "WORKFLOW_ERROR")
                    if expected != "repair":
                        break
                    yield {"phase": "repair", "chapter": number}
                    verify = await review.asend("repair")

            review_model = config if result["repairCount"] else auxiliary
            await self._record_usage(job_id, "chapter_verify", result["usage"], _elapsed(started),
                                     result["repairCount"], review_model.get("model") or review_model.get("provider"))
            yield {"phase": expected, "chapter": number}
            if expected == "reject":
                issues = result["verification"]["issues"]
                reason = "；".join(str(issue.get("message") or issue.get("type") or "") for issue in issues)
                message = f"第{number}章一致性审校未通过（已达最大自动修复次数 {max_repairs}）：{reason}"
                await self._failed(job_id, "error", "VERIFICATION_FAILED", message, chapterNumber=number, issues=issues)
                return

            delta = None if needs_review else result["factsDelta"]
            if not needs_review and not isinstance(delta, dict):
                delta = await extract_missing_facts(body)
            saved = await self._commit_chapter(job, number, title, body, outline, result, needs_review, delta,
                                               options["long_memory"], meme_title=meme_title, next_outline_chapter=next_outline,
                                               story_ledger=story_ledger)
            if meme_title:
                used_samples.add(meme_title)
            committed[number] = saved
            running_words += _word_count(saved)
            if rolling and number < total:
                arc = next((arc for arc in arcs if arc["endChapter"] == number), None)
                if arc is not None and any(n not in committed for n in range(number + 1, total + 1)):
                    yield {"phase": "archive", "chapter": number}
                    await self._archive_arc(job_id, arc, archive_config, story_ledger=story_ledger, book_plan=job.get("bookPlan"))
                    if (number + 1) in outlines:
                        await self._set_stage(job_id, "chapter_writing")
            elif not rolling and options["long_memory"] and number % 10 == 0:
                if await self._needs_volume_archive(job_id, number, committed):
                    yield {"phase": "archive", "chapter": number}
                    await self._archive(job_id, number, committed, auxiliary)
                    if number < total:
                        await self._set_stage(job_id, "chapter_writing")

        yield {"phase": "calibrate"}
        word_range = (_round(job["targetWords"] * 0.9), _round(job["targetWords"] * 1.18))
        await self._calibrate(job, outlines, committed, running_words, word_range, auxiliary)
        yield {"phase": "audit"}
        report = await self._audit(job, outlines, config, auxiliary, options["long_memory"], rolling_arc_outlines=rolling,
                                   story_ledger=story_ledger)
        gate = await self._completion_gate(job, report, word_range)
        if not gate["passed"]:
            await self._checkpoint(job_id, "full_audit", nextChapter=total + 1)
            reasons = [reason for test, reason in (
                ("chaptersComplete", "章节不完整或含空章/占位符"),
                ("wordCountInRange", f"总字数 {gate['totalWords']} 超出目标区间 [{word_range[0]}, {word_range[1]}]"),
                ("auditPassed", f"全书终审未通过：{report.get('summary') or '无效响应'}"),
                ("exportValid", "TXT/MD 导出校验失败"),
            ) if not gate[test]]
            await self._failed(job_id, "error", "AUDIT_GATE_FAILED", f"完本四重门禁未通过：{'；'.join(reasons)}",
                               fields={"auditReport": report, "auditGate": gate})
            return
        await self._checkpoint(job_id, "completed", nextChapter=total + 1)
        yield {"phase": "finish"}
        await self._finish(job, config, report, gate)

    async def _adjust_words(self, job_id, number, body, words, target, outline, pov, auxiliary):
        prompt = build_word_count_adjust_prompt(chapter_number=number, chapter_text=body, current_words=words,
                                               target_words=target, outline=outline, pov=pov)
        reply = await self._llm(job_id, "chapter_writing", auxiliary, prompt, 0.5, retries=1)
        revised = str(reply.get("text") or "").strip()
        if (not output_was_truncated(reply.get("finishReason")) and _utf16_length(revised) > 100
                and abs(count_chinese_words(revised) - target) < abs(words - target)):
            return revised
        return body

    async def _commit_chapter(self, job, number, title, body, outline, result, needs_review, delta, long_memory, *,
                              meme_title=None, next_outline_chapter=None, story_ledger=False):
        job_id, total = job["id"], job["totalChapters"]
        status = "needs_review" if needs_review else ("repaired" if result["repairCount"] else "verified")
        verification = result["verification"]
        if needs_review:
            verification = {**verification, "resolution": "needs_review"}
        saved = await _write(self.store.save_verified_chapter, job_id, number, {
            "title": title, "content": body, "summary": _summary(outline, body), "status": status,
            "verification": verification, "repairCount": result["repairCount"],
        }, delta, long_memory=long_memory, meme_title=meme_title, story_ledger=story_ledger)
        if not needs_review:
            self._publish(job_id, "facts:updated", chapterNumber=number, canonicalFacts=saved["canonicalFacts"])
        progress = min(92, 15 + _round(number / total * 77))
        job = await self._update(job_id, {"progress": progress})
        outline_cursor = {"nextOutlineChapter": next_outline_chapter} if next_outline_chapter is not None else {}
        await self._checkpoint(job_id, "chapter_writing", nextChapter=number + 1, completedChapter=number, **outline_cursor)
        chapter = saved["chapter"]
        if needs_review:
            await self._log(job_id, "warn", "chapter_writing", f"第{number}章返修用尽，正文已保存为待复核，继续写下一章。",
                            {"chapterNumber": number, "issues": verification["issues"]})
        else:
            repairs = f"，自动修复 {result['repairCount']} 次" if result["repairCount"] else ""
            await self._log(job_id, "info", "chapter_writing", f"第{number}/{total}章完成（{chapter['wordCount']}字{repairs}）")
        self._publish(job_id, "chapter:completed", chapter={
            key: chapter[key] for key in ("chapterNumber", "title", "wordCount", "status", "repairCount")
        }, completedChapters=job["completedChapters"], totalChapters=total, totalWords=job["totalWords"], progress=progress,
            tokenUsage=job.get("tokenUsage"), stageStats=job.get("stageStats"))
        return chapter

    async def _pending_arc_archives(self, job_id, arcs, before):
        preceding = [arc for arc in arcs if arc["endChapter"] < before]
        if not preceding:
            return []
        facts = await asyncio.to_thread(self.store.get_canonical_facts, job_id)
        archived = {(row.get("arc"), row["startChapter"], row["endChapter"]) for row in prior_arc_summaries(facts, before)
                    if row.get("sourceHash")}
        return [arc for arc in preceding if (arc["arcIndex"], arc["startChapter"], arc["endChapter"]) not in archived]

    async def _needs_volume_archive(self, job_id, end, committed):
        start = end - 9
        if end < 10 or not all(number in committed for number in range(start, end + 1)):
            return False
        source_hash = volume_source_hash([committed[number] for number in range(start, end + 1)])
        facts = await asyncio.to_thread(self.store.get_canonical_facts, job_id)
        return not any(summary["endChapter"] == end and summary.get("sourceHash") == source_hash
                       for summary in facts["volumeSummaries"])

    async def _refresh_arc_archives(self, job_id, arcs, before, config, *, story_ledger=False, book_plan=None):
        for arc in await self._pending_arc_archives(job_id, arcs, before):
            await self._archive_arc(job_id, arc, config, story_ledger=story_ledger, book_plan=book_plan)

    async def _archive_arc(self, job_id, arc, config, *, story_ledger=False, book_plan=None):
        start, end = arc["startChapter"], arc["endChapter"]
        source_error = f"第 {start}–{end} 章正文未齐或在归档期间发生变化，请从断点继续。"
        chapters = await asyncio.to_thread(self.store.get_chapters, job_id, start_chapter=start, end_chapter=end)
        if ([row["chapterNumber"] for row in chapters] != list(range(start, end + 1))
                or not all(_is_committed(row) for row in chapters)):
            raise LLMError(source_error, code="ARC_SOURCE_CHANGED", status=409, retryable=True)
        source_hash = volume_source_hash(chapters)
        facts = await asyncio.to_thread(self.store.get_canonical_facts, job_id)
        if any(row.get("arc") == arc["arcIndex"] and row.get("sourceHash") == source_hash
               and row["startChapter"] == start and row["endChapter"] == end
               for row in prior_arc_summaries(facts, end + 1)):
            return
        await self._set_stage(job_id, "arc_archive")
        arc_label = f"「{arc['arcTitle']}」" if arc.get("arcTitle") else ""
        await self._log(job_id, "info", "arc_archive", f"正在归档第 {start}–{end} 章剧情单元{arc_label}...")
        prompt = build_arc_summary_prompt(arc, chapters, story_ledger=story_ledger, book_plan=book_plan, canonical_facts=facts)
        reply = await self._llm(job_id, "arc_archive", config, prompt, 0.15,
                                max_tokens=protected_output_budget(16_384, DEFAULT_MAX_TOKENS))
        raw = None if output_was_truncated(reply.get("finishReason")) else parse_json_from_llm(reply.get("text"))
        mainline = _dict(raw).get("mainline")
        if not isinstance(mainline, str) or not mainline.strip():
            raise LLMError(f"第 {start}–{end} 章单元摘要无效。正文已保存，从断点继续会先补归档。",
                           code="ARC_SUMMARY_INVALID", status=502, retryable=True)
        if not await _write(self.store.save_arc_summary, job_id, arc, source_hash, raw, story_ledger=story_ledger):
            raise LLMError(source_error, code="ARC_SOURCE_CHANGED", status=409, retryable=True)
        await self._log(job_id, "info", "arc_archive", f"已完成第 {start}–{end} 章单元归档。")

    async def _archive(self, job_id, end, committed, config):
        start = end - 9
        if not all(number in committed for number in range(start, end + 1)):
            return
        volume = [committed[number] for number in range(start, end + 1)]
        source_hash = volume_source_hash(volume)
        facts = await asyncio.to_thread(self.store.get_canonical_facts, job_id)
        if any(summary["endChapter"] == end and summary.get("sourceHash") == source_hash
               for summary in facts["volumeSummaries"]):
            return
        await self._set_stage(job_id, "volume_archive")
        await self._log(job_id, "info", "volume_archive", f"正在归档第 {start}–{end} 章卷摘要...")
        started = time.monotonic()
        usage = {}
        try:
            if config.get("provider") == "mock":
                raw = build_mock_volume_summary(volume)
            else:
                options = {**_llm_options(config), "max_tokens": protected_output_budget(16_384, DEFAULT_MAX_TOKENS)}
                reply = await call_llm(**options, **build_volume_summary_prompt(volume), temperature=0.15,
                                       store=self.store, client=self.client)
                usage = reply.get("usage")
                raw = parse_json_from_llm(reply.get("text")) if not output_was_truncated(reply.get("finishReason")) else None
        finally:
            await self._record_usage(job_id, "volume_archive", usage, _elapsed(started),
                                     model=config.get("model") or config.get("provider"))
        summary = normalize_volume_summary(raw, start, end)
        if summary is None:
            raise LLMError(f"第{start}–{end}章卷摘要格式无效，已保存正文和断点，可以继续归档。",
                           code="VOLUME_SUMMARY_INVALID", status=502)
        if not await _write(self.store.save_volume_summary, job_id, end, source_hash, summary):
            raise LLMError(f"第{start}–{end}章在归档期间有变动，请从断点继续。", code="VOLUME_SOURCE_CHANGED", status=409)
        await self._log(job_id, "info", "volume_archive", f"已完成第 {start}–{end} 章卷摘要归档。")

    async def _calibrate(self, job, outlines, committed, running_words, word_range, auxiliary):
        if word_range[0] <= running_words <= word_range[1] or not committed:
            return
        extension_start = _number(job.get("extendedFromChapter"))
        total = job["totalChapters"]
        base = _number(job.get("wordsPerChapter")) or _WORD_TARGET_DEFAULT
        shrinking = running_words > word_range[1]
        def shrink_floor(number):
            return base if _is_finale(number, total) else 300

        pool = [chapter for number, chapter in committed.items() if number > extension_start and (
            not shrinking or _word_count(chapter) > shrink_floor(number)
        )]
        if not pool:
            return
        if shrinking:
            protected_start = total - min(3, total // 5) + 1
            chapter = max(pool, key=lambda entry: (entry["chapterNumber"] < protected_start, _word_count(entry)))
        else:
            chapter = min(pool, key=_word_count)
        number = chapter["chapterNumber"]
        floor = shrink_floor(number) if shrinking else 300
        target = max(floor, _word_count(chapter) + job["targetWords"] - running_words)
        body = await self._adjust_words(job["id"], number, chapter["content"], _word_count(chapter), target,
                                        outlines.get(number), job.get("pov"), auxiliary)
        if (body != chapter["content"] and not _PLACEHOLDERS.search(body)
                and (not shrinking or count_chinese_words(body) >= floor)):
            await _write(self.store.save_chapter, job["id"], number, {
                **chapter, "wordCount": count_chinese_words(body), "content": body,
                "summary": _summary(outlines.get(number), body),
                "status": "needs_review" if chapter["status"] == "needs_review" else "repaired",
                "repairCount": _number(chapter.get("repairCount")) + 1,
            })

    async def _repair_audit_chapters(self, job, outlines, chapters, issues, config, auxiliary, long_memory, *,
                                     rolling_arc_outlines=False, story_ledger=False):
        job_id, pov = job["id"], job.get("pov")
        for chapter in chapters:
            number = chapter["chapterNumber"]
            if number not in issues:
                continue
            facts = await asyncio.to_thread(self.store.get_canonical_facts, job_id)
            result = await verify_and_repair_chapter(
                job_id=job_id, chapter_number=number, chapter_text=chapter["content"], outline=outlines.get(number),
                canonical_facts=facts, pov=pov, target_words=job["wordsPerChapter"], llm_config=config,
                verify_llm_config=auxiliary, long_memory=long_memory, max_retries=1, store=self.store, client=self.client,
                book_plan=job.get("bookPlan"), rolling_arc_outlines=rolling_arc_outlines, story_ledger=story_ledger,
            )
            if not result["passed"]:
                continue
            text = result["chapterText"]
            saved = await _write(self.store.save_verified_chapter, job_id, number, {
                **chapter, "content": text, "wordCount": count_chinese_words(text),
                "summary": _summary(outlines.get(number), text), "status": "repaired",
                "verification": result["verification"], "repairCount": _number(chapter.get("repairCount")) + result["repairCount"],
            }, result["factsDelta"], long_memory=long_memory, story_ledger=story_ledger)
            try:
                await recheck_downstream_chapters(
                    changed_from_chapter=number, chapters=[ch for ch in chapters if number < ch["chapterNumber"] <= number + 3],
                    outlines=list(outlines.values()), canonical_facts=saved["canonicalFacts"], pov=pov,
                    target_words=job["wordsPerChapter"], llm_config=auxiliary, store=self.store, client=self.client,
                    book_plan=job.get("bookPlan"), long_memory=long_memory, rolling_arc_outlines=rolling_arc_outlines,
                    story_ledger=story_ledger,
                )
            except Exception as error:
                await self._log(job_id, "warn", "full_audit", f"第{number}章复修后的下游复核中断：{error}")

    async def _audit(self, job, outlines, config, auxiliary, long_memory, *, rolling_arc_outlines=False, story_ledger=False):
        job_id, next_chapter = job["id"], job["totalChapters"] + 1
        await self._checkpoint(job_id, "full_audit", nextChapter=next_chapter)
        await self._set_stage(job_id, "full_audit", 95)
        await self._boundary_interrupt(job_id, config.get("testScenario"), "full_audit")
        await self._log(job_id, "info", "full_audit", "开始全书定稿一致性终审与四重门禁校验...")
        attempts = max(1, int(_number(job.get("maxAuditRetries"), 2)) + 1)
        for attempt in range(1, attempts + 1):
            detail = await asyncio.to_thread(self.store.job_detail, job_id)
            chapters = detail["chapters"]
            words = detail["job"]["totalWords"]
            target = job["targetWords"]
            summaries = [{key: chapter.get(key) for key in ("chapterNumber", "title", "wordCount", "summary")}
                         for chapter in chapters]
            evidence = [{
                **{key: chapter.get(key) for key in ("chapterNumber", "title", "wordCount")},
                "opening": _excerpt(chapter.get("content"), 120), "ending": _excerpt(chapter.get("content"), 120, tail=True),
                **({"status": "needs_review"} if chapter["status"] == "needs_review" else {}),
            } for chapter in chapters]
            facts = await asyncio.to_thread(self.store.get_canonical_facts, job_id)
            prompt = build_full_book_audit_prompt(
                book_plan=job["bookPlan"], chapters_summary=summaries, chapter_evidence=evidence, canonical_facts=facts,
                rolling_arc_outlines=rolling_arc_outlines,
                word_stats={"targetWords": target, "totalWords": words, "totalChapters": job["totalChapters"],
                            "completedChapters": len(chapters), "deviationPct": round((words - target) / target * 100, 2) if target else 0},
            )
            reply = await self._llm(job_id, "full_audit", config, prompt, 0.2, retries=int(attempt > 1))
            report = _dict(parse_json_from_llm(reply.get("text"))) if not output_was_truncated(reply.get("finishReason")) else {}
            blockers = _list(report.get("blockingIssues"))
            if report.get("passed") is True and not blockers:
                return report
            report = {**(report or {"summary": "全书终审返回格式无效或未通过"}), "passed": False}
            if attempt < attempts:
                await self._checkpoint(job_id, "audit_revising", nextChapter=next_chapter, attempt=attempt)
                await self._log(job_id, "warn", "full_audit", f"全书终审第 {attempt} 次存在阻塞问题，正在复修受影响章节...")
                issues = {_number(value) for value in (
                    *_list(report.get("issueChapters")), *(_dict(item).get("chapterNumber") for item in blockers),
                )}
                await self._repair_audit_chapters(job, outlines, chapters, issues, config, auxiliary, long_memory,
                                                  rolling_arc_outlines=rolling_arc_outlines, story_ledger=story_ledger)
        return report

    async def _completion_gate(self, job, report, word_range):
        detail = await asyncio.to_thread(self.store.job_detail, job["id"])
        job, chapters = detail["job"], detail["chapters"]
        words, target, total = job["totalWords"], job["targetWords"], job["totalChapters"]
        complete = [chapter["chapterNumber"] for chapter in chapters] == list(range(1, total + 1)) and all(
            _is_committed(chapter) and (chapter.get("content") or "").strip() and not _PLACEHOLDERS.search(chapter["content"])
            for chapter in chapters
        )
        export_valid = False
        try:
            export_valid = all(_export(job, chapters, fmt)["content"].strip() for fmt in ("txt", "md"))
        except (ValueError, TypeError, KeyError):
            pass
        checks = {"chaptersComplete": complete, "wordCountInRange": word_range[0] <= words <= word_range[1],
                  "auditPassed": report.get("passed") is True, "exportValid": export_valid}
        return {"passed": all(checks.values()), **checks, "totalWords": words, "targetWords": target,
                "allowedRange": list(word_range),
                "needsReviewChapters": [ch["chapterNumber"] for ch in chapters if ch["status"] == "needs_review"]}

    async def _finish(self, job, config, report, gate):
        job_id = job["id"]
        if job.get("enableEvaluation"):
            await self._log(job_id, "info", "evaluation", "正在启动完本后六维模型评估与 AI 味排查...")
            ev = _dict(job.get("evalConfig"))
            provider = ev.get("provider") or config.get("provider") or "vertex"
            definition = get_provider_definition(provider) or {}
            separate_vertex = provider != "vertex" and definition.get("transport") == "vertex"
            model = config.get("model")
            light = model
            if separate_vertex and provider != config.get("provider"):
                model = (await asyncio.to_thread(self.store.get_provider_config, provider)).get("model")
                light = model
            elif separate_vertex:
                light = config.get("lightModel") or model
            try:
                await evaluate_book(
                    self.store, job_id, mode=ev.get("mode") or "full", provider=provider,
                    flash_provider=ev.get("flashProvider") or provider,
                    flash_model=ev.get("flashModel") or ("gemini-3.8-flash" if provider == "vertex" else light),
                    pro_provider=ev.get("proProvider") or provider,
                    pro_model=ev.get("proModel") or ("gemini-3.1-pro-preview" if provider == "vertex" else model),
                    use_saved_connection=ev.get("useSavedConnection"), use_saved_flash_connection=ev.get("useSavedFlashConnection"),
                    use_saved_pro_connection=ev.get("useSavedProConnection"), client=self.client,
                    on_progress=lambda message: self._publish(job_id, "job:log", ts=_now(), level="info",
                                                              stage="evaluation", message=message),
                )
            except Exception as error:
                await self._log(job_id, "warn", "evaluation", f"完本后评估未完成（正文已保存）：{error}")
        current = await self._update(job_id, {"status": "completed", "stage": "completed", "progress": 100,
                                              "auditReport": report, "auditGate": gate})
        pending = len(gate["needsReviewChapters"])
        note = f"，{pending} 章待复核" if pending else ""
        await self._log(job_id, "info", "completed",
                        f"全书生成完成并通过四重门禁！共 {current['completedChapters']} 章，总计 {current['totalWords']} 字{note}")
        self._publish(job_id, "job:completed", job=await self._job(job_id))
