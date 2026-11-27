"""In-process writing pipeline and the phase protocol used by the workflow driver."""

import asyncio
import math
import os
import random
import re
import unicodedata
import time
from collections import Counter
from contextlib import aclosing
from copy import deepcopy
from datetime import datetime, timedelta, timezone

from .arc_planning import (
    _title_key, arc_plan_soft_feedback, arc_rhythm, horizon_memory, legacy_outline_placeholder, prior_arc_summaries,
    saved_arc_plans, validate_arc_plan,
)
from .book_plan import (
    PLAN_VERSION_V2, BookPlanValidationError, collect_boundary_state, is_v2_book_plan, validate_book_plan, volume_for_chapter,
    verified_boundary_changes,
)
from .chapter_style import can_save_unverified_draft, normalize_mock_chapter_prose, reaction_counts, run_deterministic_checks
from .chapter_title import clean_retitle, local_retitle, strip_conflicting_number, title_body_conflicts
from .docx_export import DOCX_CONTENT_TYPE, FANQIE_SIZE_LIMIT, build_fanqie_docx, format_fanqie_chapter_title, split_fanqie_paragraphs
from .draft_quality import clean_chapter_draft, normalize_style_bible, strip_draft_headings
from .drift_repair import apply_text_patches, classify_drifts, downstream_light_check, identity_alias_conflicts, touches_fact_evidence
from .evaluator import evaluate_book
from .hotspots import HotspotsFeed, select_or_generate_concept
from .ledger_checks import assess_ledger_delta
from .llm import DEFAULT_MAX_TOKENS, LLMError, build_mock_volume_summary, call_llm, output_was_truncated, protected_output_budget
from .long_memory import drift_ledger, normalize_memory_delta, normalize_volume_summary, story_ledger_view
from .memes import MemeStore
from .prompt_budget import ARC_PROMPT_BUDGET_EXCEEDED, DEFAULT_ARC_INPUT_BUDGET, read_arc_input_budget, read_review_input_budget
from .prompts import (
    STYLE_PRESETS,
    build_arc_outlines_prompt,
    build_arc_summary_prompt,
    build_book_plan_prompt,
    build_chapter_outlines_prompt,
    build_chapter_write_prompt,
    build_drift_patch_prompt,
    build_fact_extraction_prompt,
    build_full_book_audit_prompt,
    build_retitle_prompt,
    build_volume_summary_prompt,
    build_word_count_adjust_prompt,
    extract_relevant_facts,
    read_writing_options as read_prompt_writing_options,
)
from .providers import get_provider_definition, is_masked_api_key, same_endpoint_origin, thinking_capability
from .storage import _archive_touches, _is_committed, arc_context_version, count_chinese_words, public_job, replace_drift_fact_deltas, volume_source_hash
from .thinking_policy import resolve_writing_thinking, writing_profile
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
                  "ARC_SUMMARY_INVALID", "ARC_SOURCE_CHANGED", "CONTENT_BLOCKED", "BOOK_PLAN_INVALID", ARC_PROMPT_BUDGET_EXCEEDED}
_COMEDY = re.compile(r"吐槽|搞笑|反差|打脸|震惊|直播|吃瓜|反转|嘴替|荒诞|尴尬|发疯")
_PLACEHOLDERS = re.compile(r"【待补充|\[TODO\]|TBD|placeholder", re.I)
_MARKDOWN_HEADING = re.compile(r"^#{1,6}\s*")


def read_writing_options(job=None):
    return {
        **read_prompt_writing_options(job), "arc_input_budget": read_arc_input_budget(),
        "verify_input_budget": read_review_input_budget("verify"), "repair_input_budget": read_review_input_budget("repair"),
    }


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


def _drift_repair_enabled():
    return os.environ.get("NOVEL_FORGE_DRIFT_REPAIR", "1").strip() != "0"


def _new_hard_conflicts(before, after):
    def keys(issues):
        return Counter(tuple(issue.get(field) for field in ("type", "rule", "chapter", "message", "scope", "alias", "character", "quote"))
                       for issue in issues if issue.get("severity") == "error")

    return bool(keys(after) - keys(before))


def _plan_audit_evidence(plan, chapters):
    if not is_v2_book_plan(plan):
        return []
    locks = _dict(plan.get("canonLocks"))
    terms = {
        "identityAliases": {row["alias"] for row in locks.get("identityAliases", [])},
        "assetSets": {term for row in locks.get("assetSets", []) for term in (row["name"], *row["members"])},
        "boundaries": {row["subject"] for row in locks.get("boundaries", [])},
    }
    patterns = {key: re.compile("|".join(re.escape(term) for term in sorted(values, key=lambda term: (-len(term), term))))
                for key, values in terms.items() if values}
    matches = {}
    for chapter in chapters:
        number = chapter["chapterNumber"]
        body = chapter.get("content") or ""
        if not (volume := volume_for_chapter(plan, number)):
            continue
        verification = _dict(chapter.get("verification"))
        changes = []
        if verification.get("passed") is True and chapter.get("status") != "needs_review":
            changes = verified_boundary_changes(verification.get("boundaryChanges"), book_plan=plan, chapter_text=body)
        for category, pattern in patterns.items():
            if category == "boundaries" and changes:
                quote = changes[0]["quote"]
            elif hit := pattern.search(body):
                start = max(0, hit.start() - 40)
                quote = body[start:start + 120].strip()
            else:
                continue
            snippet = {"volume": volume["volume"], "category": category, "chapter": number, "quote": quote}
            pair = matches.setdefault((volume["volume"], category), [])
            if len(pair) < 2:
                pair.append(snippet)
            else:
                pair[1] = snippet
    return [snippet for pair in matches.values() for snippet in pair]


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


def _recent_titles(chapters, number):
    preceding = sorted((chapter for chapter in chapters if chapter["chapterNumber"] < number), key=_chapter_number)
    return [chapter["title"] for chapter in preceding[-12:] if chapter.get("title")]


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
    requested = patch.get("thinkingMode")
    if requested in ("enabled", "disabled", "auto"):
        updates.update(thinkingMode=requested, thinkingModeSource="auto" if requested == "auto" else "user")
    elif "thinkingMode" not in existing:
        updates.update(thinkingMode="disabled", thinkingModeSource="default")
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
            v2 = is_v2_book_plan(old_plan)
            if v2 and len(volumes) >= 24:
                raise PipelineError("全书总纲已达24卷，无法追加新卷。", "BOOK_PLAN_VOLUME_LIMIT")
            notes = _list(old_plan.get("continuationNotes"))
            title = "终局完结篇" if finalize_book else "续章新篇"
            escalation = {}
            if v2:
                previous_ladder = _dict(volumes[-1].get("escalation")) if volumes else {}
                previous_tactics = _list(previous_ladder.get("retiredTactics"))
                cost = "关键承诺落空，主角将失去现有盟友的支持"
                if previous_ladder.get("costAtRisk") == cost:
                    cost = "若处置失败，主角将失去继续调查的资格"
                escalation = {"escalation": {
                    "pressure": " ".join(direction.split())[:64] or "旧冲突的后果引来新的对手与阻力",
                    "costAtRisk": cost,
                    "retiredTactics": previous_tactics or [
                        {"tactic": "照搬前卷破局方案", "whyFails": "对手已掌握前卷公开的破局手段并作出防备"},
                    ],
                    "stageTransition": None,
                }}
            plan = {**old_plan, "volumes": [*volumes, {
                "volume": len(volumes) + 1, "title": f"第{len(volumes) + 1}卷 {title}", "chapters": [previous + 1, total],
                "summary": (direction[:240] if v2 else direction) or f"承接前{previous}章剧情与未回收伏笔继续推进主线冲突",
                **escalation,
            }], "continuationNotes": [*notes, *([f"[第{previous + 1}-{total}章续写方向] {direction}"] if direction else [])]}
            if llm_config is not None:
                await self._patch_llm(job, _dict(llm_config))
            checkpoint = {"stage": "chapter_outlines", "nextOutlineChapter": previous + 1, "nextChapter": previous + 1,
                          "completedChapter": _number(job.get("completedChapters")), "savedAt": _now()}
            if draft := _dict(job.get("checkpoint")).get("driftRepairDraft"):
                checkpoint["driftRepairDraft"] = draft
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
                    "OUTLINE_PARSE_FAILED", "ARC_SUMMARY_INVALID", "ARC_SOURCE_CHANGED", "BOOK_PLAN_INVALID", ARC_PROMPT_BUDGET_EXCEEDED,
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
        if stage == "chapter_retitle":
            options.update(retries=0, apply_output_floor=False)
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
        thinking_warnings = set()
        while True:
            try:
                async with aclosing(self._run_once(job_id, thinking_warnings=thinking_warnings)) as stages:
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

    async def _book_plan(self, job, config, meme_level, meme_samples, draft_quality, *, rolling_arc_outlines=False,
                         options=None):
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
            plan_version=PLAN_VERSION_V2,
        )
        total = job["totalChapters"]
        feedback = ""
        for attempt in range(2):
            request = prompt if not feedback else {**prompt, "prompt": (
                f"{prompt['prompt']}\n上次总纲校验：{feedback}。请逐项纠正并完整重发 JSON。"
            )}
            try:
                try:
                    reply = await self._llm(job_id, "book_plan", config, request, 0.7, retries=attempt)
                    truncated = output_was_truncated(reply.get("finishReason"))
                except LLMError as error:
                    if error.code == "EMPTY_RESPONSE" and not output_was_truncated(error.finish_reason):
                        raise BookPlanValidationError(["bookPlan: 响应为空，须返回完整 JSON"]) from error
                    if error.code != "OUTPUT_TRUNCATED" and not output_was_truncated(error.finish_reason):
                        raise
                    truncated = True
                if truncated:
                    raise BookPlanValidationError(["bookPlan: 输出被截断，压缩措辞并返回完整 JSON"])
                plan, warnings, ladder_issues = validate_book_plan(
                    parse_json_from_llm(reply.get("text")), total_chapters=total, rolling_arc_outlines=rolling_arc_outlines,
                )
            except BookPlanValidationError as error:
                feedback = str(error)
                await self._log(job_id, "warn", "book_plan", f"第{attempt + 1}次总纲校验未通过：{feedback}")
                continue
            for warning in warnings:
                await self._log(job_id, "warn", "book_plan", warning)
            if ladder_issues:
                feedback = "；".join(ladder_issues)
                if not attempt:
                    await self._log(job_id, "warn", "book_plan", f"分卷阶梯待补齐：{feedback}")
                    continue
                plan["volumeLadderStatus"] = "partial"
                await self._log(job_id, "warn", "book_plan", f"分卷阶梯仍有缺项，已保存为 partial：{feedback}")
            else:
                plan.pop("volumeLadderStatus", None)
            plan.pop("degraded", None)
            break
        else:
            if os.environ.get("NOVEL_FORGE_BOOK_PLAN_DEGRADED_OK") != "1":
                raise LLMError(f"全书总纲未通过校验：{feedback}", code="BOOK_PLAN_INVALID", status=502, retryable=True)
            plan, _, _ = validate_book_plan({
                "title": job.get("title") or _UNTITLED, "genre": job.get("genre"), "pov": job.get("pov"),
                "logline": job.get("premise") or "主角应对核心冲突",
                "worldview": {"setting": job.get("premise") or "按选题梗概铺设舞台"},
                "mainCharacters": [{"name": "主角", "role": "主角"}],
                "volumes": [{"volume": 1, "title": "第一卷", "chapters": [1, total], "summary": "承接选题推进主线"}],
            }, total_chapters=total)
            plan.update(degraded=True, volumeLadderStatus="partial")
            await self._log(job_id, "warn", "book_plan", "已启用总纲降级开关，保存临时总纲。")
        if draft_quality and "styleBible" in plan:
            style = normalize_style_bible(plan["styleBible"])
            if style is None:
                plan.pop("styleBible")
            else:
                plan["styleBible"] = style
        options = read_writing_options(job) if options is None else options
        if options["input_budget"] is not None:
            try:
                build_chapter_write_prompt(
                    book_plan=plan, outline={"chapterNumber": 1}, total_chapters=total,
                    input_budget=options["input_budget"], draft_quality=draft_quality,
                    long_memory=options["long_memory"], chapter_ai_gate=options["chapter_ai_gate"],
                    rolling_arc_outlines=rolling_arc_outlines, story_ledger=options["story_ledger"],
                    pov=job.get("pov") or "第三人称限知", target_words=job["wordsPerChapter"],
                    style_guide=job.get("styleGuide") or STYLE_PRESETS["番茄爽文"],
                )
            except LLMError as error:
                if error.code != "CHAPTER_PROMPT_BUDGET_EXCEEDED":
                    raise
                await self._log(job_id, "warn", "book_plan", f"第1章空上下文预算检查：{error}")
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
        plan = job["bookPlan"]
        history = await asyncio.to_thread(self.store.get_chapters, job_id, end_chapter=missing[-1] - 1) if is_v2_book_plan(plan) else []

        async def fill(start, end):
            prompt = build_chapter_outlines_prompt(
                book_plan=plan, start_chapter=start, end_chapter=end, total_chapters=total,
                existing_outlines=[saved[n] for n in sorted(saved) if n < start], target_words_per_chapter=job["wordsPerChapter"],
                topic_package=job.get("topicPackage"), relevant_facts=facts, continuation_direction=job.get("continuationDirection") or "",
                finalize_book=bool(job.get("finalizeBook")),
                boundary_state=collect_boundary_state(history, before_chapter=start, book_plan=plan),
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

    async def _plan_arc(self, job, arcs, outlines, start, config, archive_config, *, story_ledger=False,
                        long_memory=False, arc_input_budget=DEFAULT_ARC_INPUT_BUDGET, committed=None):
        job_id, total = job["id"], job["totalChapters"]
        checkpoint = {"stage": "chapter_outlines", "nextChapter": start, "nextOutlineChapter": start,
                      "completedChapter": start - 1}
        await self._checkpoint(job_id, **checkpoint)
        repaired = await self._refresh_arc_archives(job_id, arcs, start, archive_config,
                                                   story_ledger=story_ledger, book_plan=job.get("bookPlan"),
                                                   committed=committed, outlines=outlines)
        plan = job["bookPlan"]
        preceding = await asyncio.to_thread(self.store.get_chapters, job_id, end_chapter=start - 1)
        preceding = [row for row in preceding if _is_committed(row)]
        if long_memory:
            await self._catch_up_volumes(job_id, start - 1, {row["chapterNumber"]: row for row in preceding}, archive_config)
        await self._set_stage(job_id, "chapter_outlines")
        boundary_state = collect_boundary_state(preceding, before_chapter=start, book_plan=plan)
        facts = await asyncio.to_thread(self.store.get_canonical_facts, job_id)
        source = arc_context_version(facts, preceding, start, boundary_state=boundary_state)
        archives = prior_arc_summaries(facts, start)
        available = {(row.get("arc"), row["startChapter"], row["endChapter"]) for row in archives}
        if any((arc["arcIndex"], arc["startChapter"], arc["endChapter"]) not in available
               for arc in arcs if arc["endChapter"] < start):
            raise LLMError("已有单元的正文在规划前发生变化，请从断点重新归档。",
                           code="ARC_SOURCE_CHANGED", status=409, retryable=True)
        arc_index = max((arc["arcIndex"] for arc in arcs), default=0) + 1
        prior_arcs = [arc for arc in arcs if arc["endChapter"] < start][-2:]
        title_floor = prior_arcs[0]["startChapter"] if prior_arcs else max(1, start - 12)
        committed_titles = {row["chapterNumber"]: row.get("title") for row in preceding}
        titles = [title for n in range(title_floor, start)
                  if (title := committed_titles.get(n) or outlines.get(n, {}).get("title"))]
        rhythm = arc_rhythm(arcs, archives, start_chapter=start, total_chapters=total)
        horizon = horizon_memory(facts, arcs, start)
        await self._log(job_id, "info", "chapter_outlines", f"从第 {start} 章规划第 {arc_index} 剧情单元。")
        error = ""
        for attempt in range(2):
            request = build_arc_outlines_prompt(
                book_plan=plan, canonical_facts=facts, start_chapter=start, total_chapters=total,
                arc_index=arc_index, preceding_chapters=preceding[-5:], recent_titles=titles,
                continuation_direction=job.get("continuationDirection") or "", finalize_book=bool(job.get("finalizeBook")),
                story_ledger=story_ledger, boundary_state=boundary_state, arc_plans=arcs, input_budget=arc_input_budget,
                rhythm=rhythm, retry_note=error,
            )
            reply = await self._llm(job_id, "chapter_outlines", config, request, 0.6, retries=attempt)
            try:
                arc, batch = validate_arc_plan(parse_json_from_llm(reply.get("text")), arc_index=arc_index,
                                               start_chapter=start, total_chapters=total, recent_titles=titles,
                                               allowed_lengths=rhythm["allowedLengths"], rhythm=rhythm,
                                               themes=horizon["themes"], soft=attempt == 0)
            except ValueError as issue:
                error = str(issue)
                await self._log(job_id, "warn", "chapter_outlines", f"第 {start} 章单元章纲第 {attempt + 1} 次校验未通过：{error}")
                continue
            if attempt and (feedback := arc_plan_soft_feedback({**arc, "chapters": batch}, rhythm=rhythm, themes=horizon["themes"])):
                await self._log(job_id, "warn", "chapter_outlines", f"第 {start} 章单元章纲重试后保留，仍需留意：{feedback}")
            next_outline = arc["endChapter"] + 1
            saved = await _write(self.store.save_arc_plan, job_id, arc, batch, total=total, expected_context=source,
                                 checkpoint={**checkpoint, "stage": "chapter_writing", "nextOutlineChapter": next_outline})
            if not saved:
                raise LLMError("规划期间已提交正文或单元边界发生变化，请从断点继续。",
                               code="ARC_SOURCE_CHANGED", status=409, retryable=True)
            arcs.append(arc)
            outlines.update((row["chapterNumber"], row) for row in batch)
            await self._log(job_id, "info", "chapter_outlines", f"已锁定第 {start}–{arc['endChapter']} 章单元「{arc['arcTitle']}」。")
            return repaired
        raise LLMError(f"第 {start} 章起的单元章纲未通过校验：{error} 已保存断点，可继续补齐。",
                       code="OUTLINE_PARSE_FAILED", status=502, retryable=True)

    async def _run_once(self, job_id, *, thinking_warnings=None):
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
        job = await self._book_plan(job, config, meme_level, samples[:4], options["draft_quality"],
                                    rolling_arc_outlines=rolling, options=options)
        plan = job["bookPlan"]
        committed = {
            chapter["chapterNumber"]: chapter for chapter in await asyncio.to_thread(self.store.get_chapters, job_id)
            if _is_committed(chapter)
        }
        reaction_cache = {number: reaction_counts(chapter.get("content") or "") for number, chapter in committed.items()}
        running_words = sum(_word_count(chapter) for chapter in committed.values())

        def remember_repairs(repaired):
            nonlocal running_words
            if repaired:
                reaction_cache.update((row["chapterNumber"], reaction_counts(row["content"])) for row in repaired)
                running_words = sum(_word_count(chapter) for chapter in committed.values())

        first_missing = next((number for number in range(1, total + 1) if number not in committed), total + 1)
        profile = job.get("writingProfile")
        if thinking_warnings is None:
            thinking_warnings = set()
        capability = thinking_capability(config.get("provider"), config.get("model"), config.get("baseUrl"))

        async def configure_writing(before, facts):
            nonlocal profile
            recent = [committed[n] for n in sorted(committed) if n < before][-10:]
            current_profile = writing_profile({**job, "story_ledger": story_ledger, "canonicalFacts": facts},
                                              plan, recent_chapters=recent)
            if current_profile != profile:
                await self._update(job_id, {"writingProfile": current_profile})
                profile = current_profile
            decision = resolve_writing_thinking(config, profile, capability)
            if decision["warning"] and not thinking_warnings:
                await self._log(job_id, "warn", "chapter_writing", decision["warning"])
                thinking_warnings.add(decision["warning"])
            return {**config, "thinkingMode": decision["effective"]}

        write_config = await configure_writing(first_missing, _dict(job.get("canonicalFacts")))
        arcs = saved_arc_plans(job) if rolling else []
        if options["long_memory"] and committed:
            await self._catch_up_volumes(job_id, max(committed), committed, auxiliary)
        arc_options = {key: options[key] for key in ("story_ledger", "long_memory", "arc_input_budget")}
        if not rolling:
            yield {"phase": "outlines"}
            outlines = await self._outlines(job, config)
            job = await self._set_stage(job_id, "chapter_writing")
        else:
            existing = _saved_outlines(job)
            historical_tail = max([0, *existing, *committed, *(arc["endChapter"] for arc in arcs)])
            need_historical = bool(historical_tail and any(
                n not in existing or (legacy_outline_placeholder(existing[n], n) and n not in committed)
                for n in range(1, historical_tail + 1)
            ))
            pending_archives = await self._pending_arc_archives(job_id, arcs, first_missing,
                                                               pending_only=first_missing > total)
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
                remember_repairs(await self._refresh_arc_archives(
                    job_id, pending_archives, first_missing, archive_config, story_ledger=story_ledger,
                    book_plan=plan, committed=committed, outlines=outlines,
                ))
            if first_missing <= total and first_missing not in outlines:
                if not in_outlines:
                    yield {"phase": "outlines", **({"chapter": first_missing} if first_missing > 1 else {})}
                remember_repairs(await self._plan_arc(job, arcs, outlines, first_missing, config, archive_config,
                                                      committed=committed, **arc_options))
            elif not in_outlines and not pending_archives:
                yield {"phase": "outlines"}
            job = await self._set_stage(job_id, "chapter_writing", 15 if _number(job.get("progress")) < 15 else None)

        for number in range(1, total + 1):
            if number in committed:
                continue
            if rolling and number not in outlines:
                yield {"phase": "outlines", "chapter": number}
                remember_repairs(await self._plan_arc(job, arcs, outlines, number, config, archive_config,
                                                      committed=committed, **arc_options))
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
            write_config = await configure_writing(number, facts)
            context = " ".join(str(item or "") for item in (
                outline.get("title"), outline.get("summary"), *_list(outline.get("keyEvents")), _excerpt(prev, 300, tail=True),
            ))
            relevant = None if rolling or options["long_memory"] else extract_relevant_facts(
                facts, context_text=context, current_chapter=number, story_ledger=story_ledger,
            )
            remaining = total - len(committed)
            target = compute_dynamic_chapter_target(job, running_words, remaining, chapter_number=number)
            finale = _is_finale(number, total)
            pov = job.get("pov") or "第三人称限知"
            style_key = job.get("stylePreset") or job.get("style")
            style = job.get("styleGuide") or STYLE_PRESETS.get(style_key, STYLE_PRESETS["番茄爽文"])
            sample = None
            if number <= 3 or number % 3 == 0 or _COMEDY.search(f"{context} {outline.get('goal') or ''}"):
                sample = next((entry for entry in samples if _meme_title(entry) not in used_samples), None)
            boundary_state = collect_boundary_state(committed.values(), before_chapter=number, book_plan=plan)
            prompt = build_chapter_write_prompt(
                book_plan=plan, outline=outline, prev_chapter_ending=prev, recent_summaries=summaries,
                canonical_facts=facts, relevant_facts=relevant, input_budget=options["input_budget"],
                draft_quality=options["draft_quality"], long_memory=options["long_memory"], pov=pov,
                chapter_ai_gate=options["chapter_ai_gate"],
                rolling_arc_outlines=rolling, story_ledger=story_ledger,
                target_words=target, total_chapters=total, style_guide=style, meme_level=meme_level,
                meme_samples=[sample] if sample is not None else [], boundary_state=boundary_state, arc_plans=arcs,
            )
            meme_title = _meme_title(sample) if sample and any(
                line.startswith(f"- 【{sample['title']}】") for line in prompt["system"].splitlines()
            ) else None
            budget = None
            if write_config["thinkingMode"] == "enabled":
                base_budget = int(_number(_llm_options(config).get("max_tokens"), DEFAULT_MAX_TOKENS)) or DEFAULT_MAX_TOKENS
                budget = protected_output_budget(base_budget, max(base_budget, 16_384))
            written = await self._llm(job_id, "chapter_writing", write_config, prompt, config.get("temperature", 0.7),
                                      max_tokens=budget)
            chapter_text = str(written.get("text") or "").strip()
            chapter_text = normalize_mock_chapter_prose(chapter_text, number)
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
                    input_budget=options["verify_input_budget"],
                )
                reply = await self._llm(job_id, "fact_extraction", auxiliary, fact_prompt, 0.2)
                return _dict(parse_json_from_llm(reply.get("text"))) if not output_was_truncated(reply.get("finishReason")) else {}

            async with aclosing(review_chapter_stages(
                job_id=job_id, chapter_number=number, chapter_text=chapter_text, outline=outline, canonical_facts=facts,
                book_plan=plan, recent_chapters=recent_chapters, rolling_arc_outlines=rolling, boundary_state=boundary_state,
                story_ledger=story_ledger, extract_missing_facts=extract_missing_facts if story_ledger else None,
                prev_chapter_ending=prev, pov=pov, target_words=target, min_words=minimum, max_words=maximum,
                llm_config=write_config, verify_llm_config=auxiliary, test_scenario=config.get("testScenario"),
                max_retries=max_repairs, chapter_ai_gate=options["chapter_ai_gate"], long_memory=options["long_memory"],
                degrade_on_verify_exhausted=options["degrade_on_verify_exhausted"], store=self.store, client=self.client,
                on_event=lambda name, fields: self._publish(job_id, name, **fields),
                chapter_title=title, arc_plans=arcs, verify_input_budget=options["verify_input_budget"],
                repair_input_budget=options["repair_input_budget"],
                reaction_history={n: reaction_cache[n] for n in range(max(1, number - 9), number) if n in reaction_cache},
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
                            title=title, book_plan=plan,
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
            title, title_check = await self._reconcile_title(job_id, number, title, body,
                                                             _recent_titles(committed.values(), number), auxiliary)
            saved = await self._commit_chapter(job, number, title, body, outline, result, needs_review, delta,
                                               options["long_memory"], meme_title=meme_title, next_outline_chapter=next_outline,
                                               story_ledger=story_ledger, title_check=title_check)
            if meme_title:
                used_samples.add(meme_title)
            committed[number] = saved
            reaction_cache[number] = reaction_counts(body)
            running_words += _word_count(saved)
            arc = next((arc for arc in arcs if arc["endChapter"] == number), None)
            archive_arc = arc is not None and any(n not in committed for n in range(number + 1, total + 1))
            volume_end = (number + 9) // 10 * 10
            archive_volume = options["long_memory"] and all(
                n in committed for n in range(volume_end - 9, volume_end + 1)
            )
            if archive_arc or archive_volume:
                yield {"phase": "archive", "chapter": number if archive_arc else volume_end}
            if archive_arc:
                remember_repairs(await self._archive_arc(job_id, arc, archive_config, story_ledger=story_ledger,
                                                         book_plan=plan, committed=committed, outlines=outlines))
            if options["long_memory"]:
                await self._catch_up_volumes(job_id, volume_end if archive_volume else number, committed, auxiliary)
            if (archive_arc or archive_volume) and number < total and number + 1 in outlines:
                await self._set_stage(job_id, "chapter_writing")

        yield {"phase": "calibrate"}
        word_range = (_round(job["targetWords"] * 0.9), _round(job["targetWords"] * 1.18))
        await self._calibrate(job, outlines, committed, running_words, word_range, auxiliary)
        yield {"phase": "audit"}
        facts = await asyncio.to_thread(self.store.get_canonical_facts, job_id)
        write_config = await configure_writing(total + 1, facts)
        report = await self._audit(job, outlines, config, auxiliary, options["long_memory"], rolling_arc_outlines=rolling,
                                   story_ledger=story_ledger, write_config=write_config)
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

    async def _reconcile_title(self, job_id, number, planned_title, body, recent_titles, auxiliary, *,
                               allow_llm=True) -> tuple[str, dict | None]:
        conflicts = title_body_conflicts(planned_title, body)
        if not conflicts:
            return planned_title, None
        fixed = local_retitle(planned_title, conflicts, body)
        method = "local"
        if fixed is None and allow_llm:
            request = build_retitle_prompt(planned_title=planned_title, conflicts=conflicts, chapter_text=body,
                                           recent_titles=recent_titles)
            try:
                reply = await self._llm(job_id, "chapter_retitle", auxiliary, request, 0.2, max_tokens=128)
            except LLMError:
                reply = {}
            candidate = clean_retitle(str(reply.get("text") or ""))
            normalized_recent = {_title_key(clean_retitle(title)) for title in recent_titles if isinstance(title, str)}
            if (2 <= len(candidate) <= 30 and _title_key(candidate) not in normalized_recent
                    and not output_was_truncated(reply.get("finishReason")) and not title_body_conflicts(candidate, body)):
                fixed, method = candidate, "llm"
        if fixed is None and (candidate := strip_conflicting_number(planned_title, conflicts)) and not title_body_conflicts(candidate, body):
            fixed, method = candidate, "strip"
        if fixed is None:
            await self._log(job_id, "warn", "chapter_writing", f"第{number}章章名数值与正文冲突，已保留原章名。")
            return planned_title, None
        await self._log(job_id, "info", "chapter_writing", f"第{number}章章名由「{planned_title}」调整为「{fixed}」。")
        return fixed, {"planned": planned_title, "final": fixed, "method": method, "conflicts": conflicts}

    async def _rewritten_title(self, job, chapter, body, chapters, auxiliary):
        number = chapter["chapterNumber"]
        if body == chapter["content"] or chapter.get("authorEdited") or number <= _number(job.get("importedChapterCount")):
            return {}
        planned = _dict(chapter.get("titleCheck")).get("planned") or chapter["title"]
        title, check = await self._reconcile_title(job["id"], number, planned, body,
                                                  _recent_titles(chapters, number), auxiliary)
        return {"title": title, "titleCheck": check}

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
                              meme_title=None, next_outline_chapter=None, story_ledger=False, title_check=None):
        job_id, total = job["id"], job["totalChapters"]
        status = "needs_review" if needs_review else ("repaired" if result["repairCount"] else "verified")
        verification = result["verification"]
        if needs_review:
            verification = {**verification, "resolution": "needs_review"}
        saved = await _write(self.store.save_verified_chapter, job_id, number, {
            "title": title, "content": body, "summary": _summary(outline, body), "status": status,
            "verification": verification, "repairCount": result["repairCount"], "titleCheck": title_check,
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
            key: chapter[key] for key in ("chapterNumber", "title", "wordCount", "status", "repairCount", "titleCheck")
            if key in chapter
        }, completedChapters=job["completedChapters"], totalChapters=total, totalWords=job["totalWords"], progress=progress,
            tokenUsage=job.get("tokenUsage"), stageStats=job.get("stageStats"))
        return chapter

    async def _pending_arc_archives(self, job_id, arcs, before, *, pending_only=False):
        preceding = [arc for arc in arcs if arc["endChapter"] < before]
        if not preceding:
            return []
        facts = await asyncio.to_thread(self.store.get_canonical_facts, job_id)
        archived = {(row.get("arc"), row["startChapter"], row["endChapter"]): row
                    for row in prior_arc_summaries(facts, before) if row.get("sourceHash")}
        pending = []
        for arc in preceding:
            summary = archived.get((arc["arcIndex"], arc["startChapter"], arc["endChapter"]))
            if summary is None and not pending_only or (
                summary and _drift_repair_enabled() and _dict(summary.get("driftRepair")).get("state") == "pending"
            ):
                pending.append(arc)
        return pending

    async def _needs_volume_archive(self, job_id, start, end, chapters):
        source_hash = volume_source_hash(chapters)
        facts = await asyncio.to_thread(self.store.get_canonical_facts, job_id)
        return not any(summary.get("startChapter", start) == start and summary["endChapter"] == end
                       and summary.get("sourceHash") == source_hash
                       for summary in facts["volumeSummaries"])

    async def _catch_up_volumes(self, job_id, through, committed, config):
        for end in range(10, through + 1, 10):
            start = end - 9
            volume = [committed[number] for number in range(start, end + 1) if number in committed]
            if len(volume) == 10 and await self._needs_volume_archive(job_id, start, end, volume):
                await self._archive(job_id, start, end, volume, config)

    async def _refresh_arc_archives(self, job_id, arcs, before, config, *, story_ledger=False, book_plan=None,
                                    committed=None, outlines=None):
        repaired = []
        for arc in await self._pending_arc_archives(job_id, arcs, before):
            repaired.extend(await self._archive_arc(job_id, arc, config, story_ledger=story_ledger, book_plan=book_plan,
                                                    committed=committed, outlines=outlines))
        return repaired

    async def _archive_arc(self, job_id, arc, config, *, story_ledger=False, book_plan=None, committed=None, outlines=None):
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
            return await self._repair_arc_drifts(job_id, arc, config, story_ledger=story_ledger, book_plan=book_plan,
                                                 committed=committed, outlines=outlines)
        await self._set_stage(job_id, "arc_archive")
        arc_label = f"「{arc['arcTitle']}」" if arc.get("arcTitle") else ""
        await self._log(job_id, "info", "arc_archive", f"正在归档第 {start}–{end} 章剧情单元{arc_label}...")
        plan_canon = is_v2_book_plan(book_plan)
        history = await asyncio.to_thread(self.store.get_chapters, job_id, end_chapter=start - 1) if plan_canon else []
        boundary_state = collect_boundary_state(history, before_chapter=start, book_plan=book_plan)
        prompt = build_arc_summary_prompt(arc, chapters, story_ledger=story_ledger, book_plan=book_plan,
                                          canonical_facts=facts, boundary_state=boundary_state)
        reply = await self._llm(job_id, "arc_archive", config, prompt, 0.15,
                                max_tokens=protected_output_budget(16_384, DEFAULT_MAX_TOKENS))
        raw = None if output_was_truncated(reply.get("finishReason")) else parse_json_from_llm(reply.get("text"))
        mainline = _dict(raw).get("mainline")
        if not isinstance(mainline, str) or not mainline.strip():
            raise LLMError(f"第 {start}–{end} 章单元摘要无效。正文已保存，从断点继续会先补归档。",
                           code="ARC_SUMMARY_INVALID", status=502, retryable=True)
        if not await _write(self.store.save_arc_summary, job_id, arc, source_hash, raw,
                            story_ledger=story_ledger, plan_canon=plan_canon):
            raise LLMError(source_error, code="ARC_SOURCE_CHANGED", status=409, retryable=True)
        await self._log(job_id, "info", "arc_archive", f"已完成第 {start}–{end} 章单元归档。")
        return await self._repair_arc_drifts(job_id, arc, config, story_ledger=story_ledger, book_plan=book_plan,
                                             committed=committed, outlines=outlines)

    async def _repair_arc_drifts(self, job_id, arc, config, *, story_ledger=False, book_plan=None,
                                 committed=None, outlines=None) -> list[dict]:
        if not _drift_repair_enabled():
            return []
        job = await asyncio.to_thread(self.store.get_job_record, job_id)
        facts = _dict(job.get("canonicalFacts"))
        archive = next((row for row in prior_arc_summaries(facts, arc["endChapter"] + 1)
                        if (row.get("arc"), row["startChapter"], row["endChapter"]) == (
                            arc["arcIndex"], arc["startChapter"], arc["endChapter"]
                        ) and _dict(row.get("driftRepair")).get("state") == "pending"), None)
        if archive is None:
            return []
        source_hash = archive["sourceHash"]
        saved = {row["chapterNumber"]: row for row in await asyncio.to_thread(self.store.get_chapters, job_id) if _is_committed(row)}
        chapters = [row for n, row in saved.items() if arc["startChapter"] <= n <= arc["endChapter"]]

        async def source_changed():
            await self._log(job_id, "info", "arc_archive", "纠偏期间正文或归档状态发生变化，已保留当前稿。",
                            {"arc": arc["arcIndex"], "reason": "source_changed"})
            return []

        if volume_source_hash(chapters) != source_hash:
            return await source_changed()
        book_plan = book_plan if book_plan is not None else job.get("bookPlan")
        options = read_writing_options(job)
        ledger = drift_ledger(facts)
        queue, deferred = classify_drifts(archive, chapters, facts=facts, book_plan=book_plan,
                                          latest_committed=max(saved, default=None), ledger=ledger,
                                          total_chapters=job["totalChapters"])
        by_arc = _dict(_dict(job.get("checkpoint")).get("driftRepairDraft"))
        cache = _dict(_dict(by_arc.get(str(arc["arcIndex"]))).get(source_hash))
        tries = archive["driftRepair"].get("tries", 0)
        retry_config = {**config, "retries": 0}

        async def save(patches, deltas, updates, state):
            return await _write(self.store.apply_arc_drift_repairs, job_id, arc, expected_source_hash=source_hash,
                                patched_chapters=patches, updated_facts_deltas=deltas, ledger_updates=updates,
                                drift_repair_state=state)

        while True:
            try:
                patches, deltas, updates = await self._draft_arc_drift_repairs(
                    job, arc, source_hash, queue, cache, saved, facts, retry_config, book_plan=book_plan,
                    story_ledger=story_ledger, long_memory=options["long_memory"], outlines=outlines or _saved_outlines(job),
                    verify_input_budget=options["verify_input_budget"],
                )
                break
            except LLMError as error:
                if error.code == "DRIFT_SOURCE_CHANGED":
                    return await source_changed()
                tries += 1
                if not error.retryable or tries >= 2:
                    patches, deltas = [], {}
                    updates = [{"id": row["id"], "status": "deferred", "reason": "repair_unavailable", "autoRepairAttempts": 1}
                               for group in queue for row in group["drifts"]]
                    break
                if await save([], {}, [], {"state": "pending", "tries": tries}) is None:
                    return await source_changed()

        numbers = {row["chapterNumber"] for row in patches}
        state = {"state": "done", "tries": tries, "round": 1, "chapters": sorted(numbers), "at": _now()}
        result = await save(patches, deltas, [*deferred, *updates], state)
        if result is None:
            return await source_changed()
        repaired = [row for row in result["chapters"] if row["chapterNumber"] in numbers]
        saved.update((row["chapterNumber"], row) for row in repaired)
        if committed is not None:
            committed.update((row["chapterNumber"], row) for row in repaired)
        self._publish(job_id, "facts:updated", chapterNumber=arc["endChapter"], canonicalFacts=result["canonicalFacts"])
        if numbers and any(_archive_touches(volume, numbers) for volume in _list(facts.get("volumeSummaries"))):
            llm = _dict(job.get("llmConfig"))
            volume_config = {**config, "model": llm.get("lightModel") or llm.get("model") or config.get("model")}
            await self._catch_up_volumes(job_id, max(saved), saved, volume_config)
        return repaired

    async def _draft_arc_drift_repairs(self, job, arc, source_hash, queue, cache, chapters, facts, config, *,
                                       book_plan, story_ledger, long_memory, outlines, verify_input_budget):
        job_id = job["id"]
        previous_deltas = {row["chapter"]: _dict(row.get("delta")) for row in _list(facts.get("factHistory"))}
        patches, deltas, updates = [], {}, []
        candidate_facts = facts
        for group in sorted(queue, key=lambda row: row["chapter"]):
            number, drifts = group["chapter"], group["drifts"]
            chapter = chapters[number]
            key = str(number)
            record = cache.setdefault(key, {})
            view = story_ledger_view(candidate_facts, chapter_number=number, book_plan=book_plan)
            if "patches" not in record:
                boundary_state = collect_boundary_state(chapters.values(), before_chapter=number, book_plan=book_plan)
                prompt = build_drift_patch_prompt(chapter=chapter, drifts=drifts, book_plan=book_plan,
                                                 ledger_view=view, boundary_state=boundary_state)
                started = time.monotonic()
                reply = await self._request_drift_patch(job_id, config, prompt)
                raw = None if output_was_truncated(reply.get("finishReason")) else parse_json_from_llm(reply.get("text"))
                await self._save_drift_draft(job_id, arc, source_hash, number, record, {"patches": _list(_dict(raw).get("patches"))})
                await self._record_usage(job_id, "drift_patch", reply.get("usage"), reply.get("durationMs") or _elapsed(started),
                                         model=config.get("model") or config.get("provider"))

            text = chapter["content"]
            body, accepted, rejected = apply_text_patches(text, record["patches"], {row["id"]: row for row in drifts},
                                                         chapter_number=number, book_plan=book_plan)
            outcomes = {row["id"]: {"id": row["id"], "status": "deferred", "reason": "patch_regressed",
                                    "autoRepairAttempts": 1} for row in drifts}
            for row in rejected:
                identifier = row.get("driftId")
                if isinstance(identifier, str) and identifier in outcomes:
                    outcomes[identifier]["reason"] = row["reason"]
            updates.extend(outcomes.values())
            if not accepted:
                continue
            for row in accepted:
                outcomes[row["driftId"]]["reason"] = "patch_regressed"

            check_args = {"chapter_number": number, "canonical_facts": candidate_facts, "pov": job.get("pov") or "第三人称限知",
                          "min_words": 0, "book_plan": book_plan}
            before = run_deterministic_checks(chapter_text=text, **check_args)["issues"]
            after = run_deterministic_checks(chapter_text=body, **check_args)["issues"]
            before += identity_alias_conflicts(text, candidate_facts, book_plan, chapter_number=number)
            after += identity_alias_conflicts(body, candidate_facts, book_plan, chapter_number=number)
            if _new_hard_conflicts(before, after):
                continue

            old_delta = previous_deltas.get(number, {})
            delta = old_delta
            refresh_delta = touches_fact_evidence(text, accepted, old_delta)
            if refresh_delta:
                delta = await self._extract_drift_facts(
                    job_id, arc, source_hash, number, record, body, candidate_facts, config,
                    book_plan=book_plan, outline=outlines.get(number), story_ledger=story_ledger,
                    long_memory=long_memory, input_budget=verify_input_budget,
                )
            before = assess_ledger_delta(old_delta, text, view, book_plan=book_plan)
            after = assess_ledger_delta(delta, body, view, book_plan=book_plan)
            if _new_hard_conflicts(before["issues"], after["issues"]):
                continue

            next_facts = candidate_facts
            if refresh_delta:
                delta = {key: value for key, value in after["cleanedDelta"].items() if key != "memory"}
                memory = normalize_memory_delta(after["cleanedDelta"].get("memory"), number, body, story_ledger=story_ledger,
                                                long_memory=long_memory, book_plan=book_plan,
                                                history=candidate_facts.get("factHistory") or [])
                if memory:
                    delta["memory"] = memory
                next_facts = replace_drift_fact_deltas(candidate_facts, {number: delta}, timestamp=_now(), book_plan=book_plan)
            following = [chapters[n] for n in range(number + 1, arc["endChapter"] + 1)]
            if _new_hard_conflicts(downstream_light_check(following, candidate_facts, book_plan),
                                   downstream_light_check(following, next_facts, book_plan)):
                for row in accepted:
                    outcomes[row["driftId"]]["reason"] = "cascade"
                continue

            title, check = chapter["title"], chapter.get("titleCheck")
            if title_body_conflicts(title, body) != title_body_conflicts(title, text):
                title, check = await self._reconcile_title(job_id, number, title, body,
                                                           _recent_titles(chapters.values(), number), config, allow_llm=False)
            verification = chapter.get("verification")
            if isinstance(verification, dict):
                verification = {**verification, "wordCount": count_chinese_words(body)}
                if "boundaryChanges" in verification:
                    verification["boundaryChanges"] = verified_boundary_changes(verification["boundaryChanges"],
                                                                                 book_plan=book_plan, chapter_text=body)
            outline = outlines.get(number) or {"summary": (chapter.get("summary") or "").partition(" [正文节选:")[0]}
            patch = {"chapterNumber": number, "title": title, "content": body, "summary": _summary(outline, body),
                     "verification": verification}
            if check is not None or "titleCheck" in chapter:
                patch["titleCheck"] = check
            patches.append(patch)
            candidate_facts = next_facts
            if refresh_delta:
                deltas[number] = delta
            for row in accepted:
                outcomes[row["driftId"]].update(status="repaired", reason="auto_repair",
                                                resolution={"by": "auto", "at": _now(), "patchReason": str(row.get("reason") or "")[:40]})
        return patches, deltas, updates

    async def _request_drift_patch(self, job_id, config, prompt):
        options = {**_llm_options(config), "retries": 0, "max_tokens": 2400, "apply_output_floor": False}
        return await call_llm(**options, **prompt, temperature=0.1, cache_key=f"nf-drift-{job_id}"[:64],
                              store=self.store, client=self.client)

    async def _save_drift_draft(self, job_id, arc, source_hash, number, record, draft):
        saved = await _write(self.store.save_drift_repair_draft, job_id, arc, source_hash, number, draft)
        if saved is None:
            raise LLMError("纠偏期间归档已变化。", code="DRIFT_SOURCE_CHANGED", status=409)
        record.update(saved)

    async def _extract_drift_facts(self, job_id, arc, source_hash, number, record, body, facts, config, *,
                                   book_plan, outline, story_ledger, long_memory, input_budget):
        body_hash = volume_source_hash([{"chapterNumber": number, "content": body}])
        if record.get("factsSourceHash") == body_hash and isinstance(record.get("factsDelta"), dict):
            return record["factsDelta"]
        prompt = build_fact_extraction_prompt(
            chapter_number=number, chapter_text=body, outline=outline, canonical_facts=facts, story_ledger=story_ledger,
            long_memory=long_memory, book_plan=book_plan, input_budget=input_budget,
        )
        reply = await self._llm(job_id, "fact_extraction", config, prompt, 0.2)
        delta = None if output_was_truncated(reply.get("finishReason")) else parse_json_from_llm(reply.get("text"))
        if not isinstance(delta, dict):
            raise LLMError("局部纠偏后的事实提取无效。", code="DRIFT_FACTS_INVALID", status=502, retryable=True)
        await self._save_drift_draft(job_id, arc, source_hash, number, record,
                                    {"factsSourceHash": body_hash, "factsDelta": delta})
        return record["factsDelta"]

    async def _archive(self, job_id, start, end, volume, config):
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
            title_fields = await self._rewritten_title(job, chapter, body, committed.values(), auxiliary)
            committed[number] = await _write(self.store.save_chapter, job["id"], number, {
                **chapter, **title_fields, "wordCount": count_chinese_words(body), "content": body,
                "summary": _summary(outlines.get(number), body),
                "status": "needs_review" if chapter["status"] == "needs_review" else "repaired",
                "repairCount": _number(chapter.get("repairCount")) + 1,
            }, track_author_edit=False)

    async def _repair_audit_chapters(self, job, outlines, chapters, issues, config, auxiliary, long_memory, *,
                                     rolling_arc_outlines=False, story_ledger=False):
        job_id, pov = job["id"], job.get("pov")
        plan = job.get("bookPlan")
        for index, chapter in enumerate(chapters):
            number = chapter["chapterNumber"]
            if number not in issues:
                continue
            facts = await asyncio.to_thread(self.store.get_canonical_facts, job_id)
            result = await verify_and_repair_chapter(
                job_id=job_id, chapter_number=number, chapter_text=chapter["content"], outline=outlines.get(number),
                canonical_facts=facts, pov=pov, target_words=job["wordsPerChapter"], llm_config=config,
                verify_llm_config=auxiliary, long_memory=long_memory, max_retries=1, store=self.store, client=self.client,
                book_plan=plan, rolling_arc_outlines=rolling_arc_outlines, story_ledger=story_ledger,
                boundary_state=collect_boundary_state(chapters, before_chapter=number, book_plan=plan),
            )
            if not result["passed"]:
                continue
            text = result["chapterText"]
            title_fields = await self._rewritten_title(job, chapter, text, chapters, auxiliary)
            saved = await _write(self.store.save_verified_chapter, job_id, number, {
                **chapter, **title_fields, "content": text, "wordCount": count_chinese_words(text),
                "summary": _summary(outlines.get(number), text), "status": "repaired",
                "verification": result["verification"], "repairCount": _number(chapter.get("repairCount")) + result["repairCount"],
            }, result["factsDelta"], long_memory=long_memory, story_ledger=story_ledger)
            chapters[index] = saved["chapter"]
            try:
                await recheck_downstream_chapters(
                    changed_from_chapter=number, chapters=[ch for ch in chapters if number < ch["chapterNumber"] <= number + 3],
                    outlines=list(outlines.values()), canonical_facts=saved["canonicalFacts"], pov=pov,
                    target_words=job["wordsPerChapter"], llm_config=auxiliary, store=self.store, client=self.client,
                    book_plan=job.get("bookPlan"), long_memory=long_memory, rolling_arc_outlines=rolling_arc_outlines,
                    story_ledger=story_ledger, boundary_history=chapters,
                )
            except Exception as error:
                await self._log(job_id, "warn", "full_audit", f"第{number}章复修后的下游复核中断：{error}")

    async def _audit(self, job, outlines, config, auxiliary, long_memory, *, rolling_arc_outlines=False, story_ledger=False,
                     write_config=None):
        job_id, next_chapter = job["id"], job["totalChapters"] + 1
        await self._checkpoint(job_id, "full_audit", nextChapter=next_chapter)
        await self._set_stage(job_id, "full_audit", 95)
        await self._boundary_interrupt(job_id, config.get("testScenario"), "full_audit")
        await self._log(job_id, "info", "full_audit", "开始全书定稿一致性终审与四重门禁校验...")
        attempts = max(1, int(_number(job.get("maxAuditRetries"), 2)) + 1)
        plan = job["bookPlan"]
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
            committed = [row for row in chapters if _is_committed(row)]
            boundary_state = collect_boundary_state(committed, before_chapter=next_chapter, book_plan=plan)
            prompt = build_full_book_audit_prompt(
                book_plan=plan, chapters_summary=summaries, chapter_evidence=evidence, canonical_facts=facts,
                rolling_arc_outlines=rolling_arc_outlines, boundary_state=boundary_state,
                plan_evidence=_plan_audit_evidence(plan, committed),
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
                await self._repair_audit_chapters(job, outlines, chapters, issues, write_config or config, auxiliary, long_memory,
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
