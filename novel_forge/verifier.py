"""Chapter verification, targeted repair, and the stages used by the writer."""

import asyncio
import inspect
import json
import math
import re
from contextlib import aclosing

from .chapter_style import (
    can_save_unverified_draft,
    find_chapter_style_issues,
    run_deterministic_checks,
    verified_ai_blockers,
)
from .llm import LLMError, call_llm, output_was_truncated
from .prompts import build_repair_prompt, build_verify_prompt


_DEFAULT_POV = "第三人称限知"
_FENCED_JSON = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.I)
_PRIORITIES = ("P0", "P1", "P2")
_CONNECTION_FIELDS = {
    "provider": "provider",
    "baseUrl": "base_url",
    "apiKey": "api_key",
    "model": "model",
    "thinkingMode": "thinking_mode",
    "timeoutMs": "timeout_ms",
    "maxTokens": "max_tokens",
    "maxRetries": "max_retries",
    "retries": "retries",
    "useSavedConnection": "use_saved_connection",
    "testScenario": "test_scenario",
    "allowParallel": "allow_parallel",
    "retryEmptyResponse": "retry_empty_response",
    "applyOutputFloor": "apply_output_floor",
}


def _llm_options(config):
    config = config or {}
    return {snake: config[snake] if snake in config else config[camel]
            for camel, snake in _CONNECTION_FIELDS.items() if snake in config or camel in config}


async def _notify(callback, *args):
    if callback is not None:
        result = callback(*args)
        if inspect.isawaitable(result):
            await result


def _reject_constant(value):
    raise ValueError(f"Invalid JSON constant: {value}")


def _strict_json(text):
    return json.loads(text, parse_constant=_reject_constant)


def _recover_objects(text):
    recovered = []
    start = None
    depth = 0
    in_string = escaped = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}" and depth:
            depth -= 1
            if depth == 0:
                try:
                    recovered.append(_strict_json(text[start:index + 1]))
                except (ValueError, RecursionError):
                    continue
    return recovered


def parse_json_from_llm(text, fallback=None):
    """Parse an LLM response, optionally recovering complete items from a truncated array."""
    if not isinstance(text, str) or not text:
        return fallback
    text = text.strip().strip("\ufeff").strip()
    candidates = [text]
    if fenced := _FENCED_JSON.search(text):
        candidates.append(fenced[1].strip())
    object_start = text.find("{")
    array_start = text.find("[")
    if object_start >= 0 and (array_start < 0 or object_start < array_start):
        start, end = object_start, text.rfind("}")
    else:
        start, end = array_start, text.rfind("]")
    if start >= 0 and end > start:
        candidates.append(text[start:end + 1])
    for candidate in candidates:
        try:
            return _strict_json(candidate)
        except (ValueError, RecursionError):
            continue
    if array_start >= 0 and isinstance(fallback, list):
        return _recover_objects(text[array_start + 1:]) or fallback
    return fallback


def _number(value):
    try:
        number = float(value)
        if math.isfinite(number):
            return int(number) if number.is_integer() else number
    except (ValueError, TypeError, OverflowError):
        pass
    return None


def _merge_issues(local, remote):
    merged = list(local)
    for issue in remote:
        if not isinstance(issue, dict) or not issue.get("message"):
            continue
        identity = tuple(issue.get(field) for field in ("type", "sourceChapter", "message"))
        if not any(tuple(item.get(field) for field in ("type", "sourceChapter", "message")) == identity for item in merged):
            merged.append({
                "type": issue.get("type") or "consistency",
                "severity": issue.get("severity") or "error",
                "message": issue["message"],
                **({"sourceChapter": _number(issue["sourceChapter"])} if "sourceChapter" in issue else {}),
            })
    return merged


def _items(value):
    return value if isinstance(value, list) else []


def _min_words(minimum, target):
    return max(200, math.floor(target * 0.75 + 0.5)) if minimum is None else minimum


async def verify_chapter(*, chapter_number, chapter_text, outline=None, canonical_facts=None, prev_chapter_ending="",
                         pov=_DEFAULT_POV, target_words=1500, min_words=None, max_words=None, llm_config=None,
                         verify_llm_config=None, test_scenario=None, chapter_ai_gate=False, long_memory=False,
                         strict_review_result=False, store=None, client=None):
    facts = canonical_facts or {}
    minimum = _min_words(min_words, target_words)
    maximum = math.floor(target_words * 1.35 + 0.5) if max_words is None else max_words
    checks = run_deterministic_checks(chapter_number=chapter_number, chapter_text=chapter_text, canonical_facts=facts,
                                      pov=pov, min_words=minimum, max_words=maximum)
    local, word_count = checks["issues"], checks["wordCount"]
    if chapter_ai_gate and (style := find_chapter_style_issues(str(chapter_text or ""), facts, word_count)):
        issues = [*local, *style]
        return {"passed": False, "chapterNumber": chapter_number, "wordCount": word_count, "issues": issues,
                "aiIssues": [], "factsDelta": None, "usage": None,
                "summary": f"第{chapter_number}章发现 {len(issues)} 处问题"}

    prompt = build_verify_prompt(
        chapter_number=chapter_number, chapter_text=chapter_text, outline=outline, canonical_facts=facts,
        prev_chapter_ending=prev_chapter_ending, pov=pov, min_words=minimum, max_words=maximum,
        chapter_ai_gate=chapter_ai_gate, long_memory=long_memory,
    )
    config = _llm_options(verify_llm_config if verify_llm_config is not None else llm_config)
    scenario = test_scenario if test_scenario is not None else _llm_options(llm_config).get("test_scenario")
    reply = await call_llm(**{**config, "test_scenario": scenario}, **prompt, temperature=0.2, store=store, client=client)
    strict = chapter_ai_gate or strict_review_result
    parsed = parse_json_from_llm(reply.get("text")) if not output_was_truncated(reply.get("finishReason")) else None
    if strict and (not isinstance(parsed, dict) or type(parsed.get("passed")) is not bool
                   or not isinstance(parsed.get("issues"), list) or not isinstance(parsed.get("aiIssues"), list)):
        raise LLMError(f"第{chapter_number}章审校响应缺少有效 JSON，无法核验该章。",
                       code="CHAPTER_REVIEW_INVALID_JSON", status=502)
    parsed = parsed if isinstance(parsed, dict) else {}
    has_word_error = any(issue["type"] == "word_count" and issue["severity"] == "error" for issue in local)
    remote = []
    for issue in _items(parsed.get("issues")):
        if not isinstance(issue, dict) or (chapter_ai_gate and issue.get("type") == "ai_flavor"):
            continue
        remote.append({**issue, "severity": "warning"} if issue.get("type") == "word_count" and not has_word_error else issue)

    ai_issues = []
    for issue in _items(parsed.get("aiIssues")):
        if not isinstance(issue, dict) or not any(issue.get(key) for key in ("quote", "suggestion", "impact")):
            continue
        ai_issues.append({
            "id": issue.get("id") or f"ch{chapter_number}_ai_{len(ai_issues) + 1}",
            "chapterNumber": _number(chapter_number),
            "category": issue.get("category") or "over_explaining",
            "quote": str(issue.get("quote") or "").strip(),
            "impact": str(issue.get("impact") or "").strip(),
            "priority": issue["priority"] if issue.get("priority") in _PRIORITIES else "P2",
            "suggestion": str(issue.get("suggestion") or "").strip(),
            "resolved": False,
        })
    if chapter_ai_gate:
        local = [*local, *verified_ai_blockers(ai_issues, chapter_text)]
    issues = _merge_issues(local, remote)
    passed = not any((issue.get("severity") or "error") == "error" for issue in issues) and (
        all(issue.get("severity") == "warning" for issue in issues) or parsed.get("passed") is not False
    )
    delta = parsed.get("factsDelta")
    return {
        "passed": passed, "chapterNumber": chapter_number, "wordCount": word_count, "issues": issues,
        "aiIssues": ai_issues, "factsDelta": delta if passed and isinstance(delta, dict) else None,
        "summary": (f"第{chapter_number}章发现 {len(issues)} 处问题" if issues
                    else parsed.get("summary") or f"第{chapter_number}章一致性审校通过"),
        "usage": reply.get("usage"),
    }


async def review_chapter_stages(*, chapter_number, chapter_text, job_id=None, outline=None, canonical_facts=None,
                                prev_chapter_ending="", pov=_DEFAULT_POV, target_words=1500, min_words=None,
                                max_words=None, llm_config=None, verify_llm_config=None, test_scenario=None,
                                max_retries=2, on_event=None, chapter_ai_gate=False, long_memory=False,
                                degrade_on_verify_exhausted=False, store=None, client=None):
    """Yield verify/review phases. Send 'repair' or advance normally to accept repair."""
    current = chapter_text
    repair_count = 0
    repair_input = None
    repairs = []
    usage = dict.fromkeys(("promptTokens", "completionTokens", "totalTokens"), 0)
    facts = canonical_facts or {}
    context = {
        "chapter_number": chapter_number, "outline": outline, "canonical_facts": facts,
        "prev_chapter_ending": prev_chapter_ending, "pov": pov, "target_words": target_words,
        "chapter_ai_gate": chapter_ai_gate,
    }
    check_options = {"chapter_number": chapter_number, "canonical_facts": facts, "pov": pov,
                     "min_words": _min_words(min_words, target_words)}

    def add_usage(reply_usage):
        for key in usage:
            usage[key] += (reply_usage or {}).get(key) or 0

    while True:
        yield {"phase": "verify", "chapter": chapter_number}
        verification = await verify_chapter(
            **context, chapter_text=current, min_words=min_words, max_words=max_words, llm_config=llm_config,
            verify_llm_config=verify_llm_config, test_scenario=test_scenario, long_memory=long_memory,
            strict_review_result=degrade_on_verify_exhausted, store=store, client=client,
        )
        add_usage(verification.get("usage"))
        passed = verification["passed"]
        if repair_input is not None:
            entry = {"chapterNumber": chapter_number, "attempt": repair_count, "issues": repair_input,
                     "action": "auto_repair", "resolved": passed}
            repairs.append(entry)
            if job_id and store is not None:
                await asyncio.to_thread(store.append_repair_log, job_id, entry)
            await _notify(on_event, "chapter:repaired", {
                "chapterNumber": chapter_number, "attempt": repair_count, "resolved": passed,
                "remainingIssues": verification["issues"],
            })

        can_repair = not passed and repair_count < max_retries
        exhausted = not passed and repair_count >= max_retries
        can_commit = bool(degrade_on_verify_exhausted and exhausted
                          and can_save_unverified_draft(current, verification, **check_options))
        result = {
            "passed": passed, "canCommitForReview": can_commit, "exceededMaxRetries": exhausted,
            "chapterNumber": chapter_number, "chapterText": current, "verification": verification,
            "aiIssues": verification["aiIssues"], "factsDelta": verification["factsDelta"] if passed else None,
            "repairCount": repair_count, "repairs": list(repairs), "usage": dict(usage),
        }
        action = yield {"phase": "review", "chapter": chapter_number, "passed": passed, "canRepair": can_repair,
                        "canCommitForReview": can_commit, "result": result}
        if not can_repair or action not in (None, "repair"):
            return
        repair_count += 1
        repair_input = verification["issues"]
        await _notify(on_event, "chapter:repair_started", {
            "chapterNumber": chapter_number, "attempt": repair_count, "maxRetries": max_retries, "issues": repair_input,
        })
        prompt = build_repair_prompt(
            **context, chapter_text=current, issues=repair_input,
            ai_issues=[] if chapter_ai_gate else [issue for issue in verification["aiIssues"] if issue["priority"] in ("P0", "P1")],
        )
        config = _llm_options(llm_config)
        if test_scenario is not None:
            config["test_scenario"] = test_scenario
        reply = await call_llm(**config, **prompt, temperature=0.4, store=store, client=client)
        add_usage(reply.get("usage"))
        rewritten = str(reply.get("text") or "").strip()
        if not output_was_truncated(reply.get("finishReason")) and len(rewritten.encode("utf-16-le", "surrogatepass")) >= 100:
            current = rewritten


async def verify_and_repair_chapter(**options):
    async with aclosing(review_chapter_stages(**options)) as stages:
        async for phase in stages:
            if phase["phase"] == "review":
                result = phase["result"]
    return result


async def recheck_downstream_chapters(*, changed_from_chapter, chapters=(), outlines=(), canonical_facts=None,
                                     pov=_DEFAULT_POV, target_words=1500, llm_config=None, test_scenario=None,
                                     store=None, client=None):
    chapter_order = lambda entry: _number(entry.get("chapterNumber")) or 0
    changed = _number(changed_from_chapter)
    downstream = sorted((chapter for chapter in chapters if chapter_order(chapter) > changed), key=chapter_order)
    by_chapter = {}
    for outline in outlines:
        by_chapter.setdefault(chapter_order(outline), outline)
    affected = []
    for chapter in downstream:
        number = chapter["chapterNumber"]
        result = await verify_chapter(
            chapter_number=number, chapter_text=chapter.get("content") or "", outline=by_chapter.get(chapter_order(chapter)),
            canonical_facts=canonical_facts, pov=pov, target_words=target_words, llm_config=llm_config,
            test_scenario=test_scenario, store=store, client=client,
        )
        if not result["passed"]:
            affected.append({"chapterNumber": number, "issues": result["issues"], "summary": result["summary"]})
    return {"changedFromChapter": changed, "checkedCount": len(downstream), "affectedChapters": affected}
