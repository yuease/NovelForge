"""Chapter verification, targeted repair, and the stages used by the writer."""

import asyncio
import inspect
import json
import math
import re
from contextlib import aclosing
from decimal import Decimal

from .arc_planning import character_name_registry, normalize_character_name
from .book_plan import collect_boundary_state, is_v2_book_plan, valid_plan_ref, verified_boundary_changes
from .chapter_style import (
    can_save_unverified_draft,
    find_chapter_style_issues,
    run_deterministic_checks,
    verified_ai_blockers,
)
from .llm import LLMError, call_llm, output_was_truncated
from .long_memory import story_ledger_label_key, story_ledger_view
from .prompts import build_repair_prompt, build_verify_prompt
from .text_evidence import locate_quote, normalize_quote


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


_HARD_CONFLICTS = {"hard_value_conflict", "world_rule_conflict", "core_scene_repetition"}
_STORY_TYPES = {"hard_value_conflict", "character_ooc"}
_DISTINCT_NAME = re.compile(r"同名|重名|名字也叫|另(?:一个|一位)[^。！？.!?\n]{0,12}(?:也叫|叫|名叫|同名)|不是同一人|并非同一人|不是[^。！？.!?\n]{0,10}本人")
_NAME_EXCEPTIONS = re.compile(r"(?:并不|没有|不是|并非|可能|好像|以为)同名|化名|笔名|艺名|曾用名|假扮|改名")
_EXACT_BALANCE = re.compile(r"([+-]?\d{1,20}(?:\.\d{1,6})?)(万元|亿元|元|积分|点|分|级|枚|块)")


def _hard_conflict_type(issue, kind):
    if kind == "inconsistent_details":
        kind = issue.get("hardConflictType")
    return kind if isinstance(kind, str) and kind in _HARD_CONFLICTS else None


def _merge_issues(local, remote):
    merged = list(local)
    for issue in remote:
        if not isinstance(issue, dict) or not issue.get("message"):
            continue
        fields = ("type", "sourceChapter", "message", "planRef")
        identity = tuple(issue.get(field) for field in fields)
        if not any(tuple(item.get(field) for field in fields) == identity for item in merged):
            merged.append({
                "type": issue.get("type") or "consistency",
                "severity": issue.get("severity") or "error",
                "message": issue["message"],
                **({"quote": issue["quote"]} if (_hard_conflict_type(issue, issue.get("type")) or issue.get("type") == "character_ooc")
                   and issue.get("quote") else {}),
                **({"ledgerRef": issue["ledgerRef"]} if "ledgerRef" in issue else {}),
                **({"planRef": issue["planRef"]} if "planRef" in issue else {}),
                **({"sourceChapter": _number(issue["sourceChapter"])} if "sourceChapter" in issue else {}),
            })
    return merged


def _items(value):
    return value if isinstance(value, list) else []


def _confirmed_name_collisions(candidates, chapter_text, facts, book_plan, chapter_number):
    registry = character_name_registry(book_plan, facts, before_chapter=chapter_number)
    source = normalize_quote(chapter_text)
    seen, issues = set(), []
    for entry in _items(candidates)[:12]:
        if not isinstance(entry, dict):
            continue
        key = normalize_character_name(entry.get("name"))
        identities = registry.get(key, ())
        identity, quote = entry.get("identity"), entry.get("quote")
        if (len(key) < 2 or key in seen or len(identities) != 1 or not isinstance(identity, str)
                or not identity.strip() or not isinstance(quote, str) or not 4 <= len(quote.strip()) <= 120):
            continue
        proof = locate_quote(quote, source)
        if not proof or len(proof) > 160 or _NAME_EXCEPTIONS.search(proof):
            continue
        proof = proof.casefold()
        name_token = (rf"(?:^|[^\w]|名叫|也叫|叫作|叫|名为|同名的|这个|那个)"
                      rf"{re.escape(key)}(?=$|[^\w]|是|与|和|也|本人|并非|来了)")
        if not any(name.end() >= marker.start() - 24 and name.start() <= marker.end() + 24
                   for name in re.finditer(name_token, proof) for marker in _DISTINCT_NAME.finditer(proof)):
            continue
        seen.add(key)
        name = identities[0]["name"]
        issues.append({"type": "character_name_collision", "severity": "error", "quote": quote.strip(),
                       "message": f"新出场人物与已有角色「{name}」完全同名，原文明示是不同的人。",
                       "suggestion": f"为新人物更名并同步修改本章称呼，保留原角色「{name}」的姓名。"})
    return issues


def _story_conflict(issue, kind, view, source):
    ref = issue.get("ledgerRef")
    if not isinstance(kind, str) or kind not in _STORY_TYPES or not isinstance(ref, dict) or not isinstance(ref.get("key"), str):
        return None
    quote = issue.get("quote")
    if not isinstance(quote, str) or len(quote) > 320 or not locate_quote(quote, source):
        return None
    record, key = ref.get("kind"), ref["key"]
    if kind == "character_ooc" and record == "character":
        state = next((state for name, state in view.get("characterStates", {}).items()
                      if normalize_character_name(name) == normalize_character_name(key)), {})
        chapter = state.get("stanceTierChapter") or state.get("updatedAtChapter")
        supported = state.get("stanceTier") or state.get("knownFacts")
    elif kind == "hard_value_conflict" and record == "clock":
        clock = view.get("storyClock", {})
        supported = key in ("day", "season", "timeOfDay", "countdown") and clock.get(key)
        chapter = clock.get("fieldChapters", {}).get(key)
    elif kind == "hard_value_conflict" and record == "balance":
        balance = next((row for row in view.get("ledgerBalances", [])
                        if story_ledger_label_key(row["label"]) == story_ledger_label_key(key)), {})
        supported, chapter = balance.get("label"), balance.get("updatedAtChapter")
    else:
        return None
    if not supported:
        return None
    return {**issue, "type": kind, "severity": "error", "ledgerRef": {"kind": record, "key": key},
            **({"sourceChapter": chapter} if chapter is not None else {})}


def ledger_arithmetic_conflicts(delta, chapter_text, view):
    """Check only explicit chapter net change and closing balance in matching units."""
    if not isinstance(delta, dict) or not isinstance(delta.get("memory"), dict):
        return []
    rows = _items(delta["memory"].get("ledgerBalances"))
    balances = {story_ledger_label_key(row["label"]): row for row in view.get("ledgerBalances", [])}
    source = normalize_quote(chapter_text)
    issues = []
    seen = set()
    for entry in rows[:12]:
        if not isinstance(entry, dict):
            continue
        label = story_ledger_label_key(entry.get("label"))
        before = balances.get(label)
        value, change, quote = (entry.get(key) for key in ("value", "change", "quote"))
        if not before or label in seen or not all(isinstance(item, str) for item in (before.get("value"), value, change, quote)):
            continue
        quote = quote.strip()
        if not 4 <= len(quote) <= 120:
            continue
        proof = locate_quote(quote, source)
        if not proof or len(proof) > 160 or label not in proof.casefold():
            continue
        start, end, net = (normalize_quote(item) for item in (before["value"], value, change))
        amounts = [_EXACT_BALANCE.fullmatch(item) for item in (start, end, net)]
        if not all(amounts) or len({item[2] for item in amounts}) != 1 or not net.startswith(("+", "-")):
            continue
        # Require a stated net for the chapter and the closing balance in the actual excerpt.
        if not re.search(rf"(?:本章|本轮|本次)(?:合计|累计)?净(?:变动|额)?[:=]?{re.escape(net)}(?!\d)", proof):
            continue
        if not re.search(rf"(?:期末余额|结算余额|最终余额)[:=]?{re.escape(end)}(?!\d)", proof):
            continue
        if Decimal(amounts[0][1]) + Decimal(amounts[2][1]) != Decimal(amounts[1][1]):
            seen.add(label)
            issues.append({
                "type": "hard_value_conflict", "severity": "error", "quote": quote,
                "sourceChapter": before.get("updatedAtChapter"), "ledgerRef": {"kind": "balance", "key": before["label"]},
                "message": f"「{before['label']}」的前值 {before['value']} 与本轮净变动 {change} 无法得到期末余额 {value}。",
            })
    return issues


def _min_words(minimum, target):
    return max(200, math.floor(target * 0.75 + 0.5)) if minimum is None else minimum


async def verify_chapter(*, chapter_number, chapter_text, outline=None, canonical_facts=None, prev_chapter_ending="",
                         pov=_DEFAULT_POV, target_words=1500, min_words=None, max_words=None, llm_config=None,
                         verify_llm_config=None, test_scenario=None, chapter_ai_gate=False, long_memory=False,
                         strict_review_result=False, store=None, client=None, book_plan=None, recent_chapters=None,
                         rolling_arc_outlines=False, story_ledger=False, boundary_state=()):
    facts = canonical_facts or {}
    minimum = _min_words(min_words, target_words)
    maximum = math.floor(target_words * 1.35 + 0.5) if max_words is None else max_words
    checks = run_deterministic_checks(chapter_number=chapter_number, chapter_text=chapter_text, canonical_facts=facts,
                                      pov=pov, min_words=minimum, max_words=maximum)
    local, word_count = checks["issues"], checks["wordCount"]
    if chapter_ai_gate and (style := find_chapter_style_issues(str(chapter_text or ""), facts, word_count,
                                                              recent_chapters=recent_chapters)):
        issues = [*local, *style]
        return {"passed": False, "chapterNumber": chapter_number, "wordCount": word_count, "issues": issues,
                "aiIssues": [], "factsDelta": None, "usage": None,
                "summary": f"第{chapter_number}章发现 {len(issues)} 处问题"}

    prompt = build_verify_prompt(
        chapter_number=chapter_number, chapter_text=chapter_text, outline=outline, canonical_facts=facts,
        prev_chapter_ending=prev_chapter_ending, pov=pov, min_words=minimum, max_words=maximum,
        chapter_ai_gate=chapter_ai_gate, long_memory=long_memory, book_plan=book_plan, rolling_arc_outlines=rolling_arc_outlines,
        story_ledger=story_ledger, boundary_state=boundary_state,
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
    hard_quotes = set()
    v2 = is_v2_book_plan(book_plan)
    plan_source = normalize_quote(chapter_text) if v2 else ""
    ledger_view, ledger_source = {}, ""
    if story_ledger:
        ledger_view = story_ledger_view(facts, chapter_number=chapter_number, book_plan=book_plan,
                                        context_text=f"{chapter_text}\n{json.dumps(outline or {}, ensure_ascii=False)}")
        ledger_source = normalize_quote(chapter_text)
        local.extend(_confirmed_name_collisions(parsed.get("newCharacterCandidates"), chapter_text, facts, book_plan, chapter_number))
        remote.extend(ledger_arithmetic_conflicts(parsed.get("factsDelta"), chapter_text, ledger_view))

    def grounded(issue):
        return bool(verified_ai_blockers([{**issue, "priority": "P1"}], chapter_text))

    def plan_conflict(issue, kind):
        quote = issue.get("quote")
        confirmed = (kind and valid_plan_ref(book_plan, issue.get("planRef")) and isinstance(quote, str)
                     and locate_quote(quote, plan_source))
        return {**issue, "type": kind or issue.get("type"), "severity": "error" if confirmed else "warning"}

    for issue in _items(parsed.get("issues")):
        if not isinstance(issue, dict) or (chapter_ai_gate and issue.get("type") == "ai_flavor"):
            continue
        if issue.get("type") == "character_name_collision":
            continue
        kind = _hard_conflict_type(issue, issue.get("type"))
        if v2 and "planRef" in issue:
            remote.append(plan_conflict(issue, kind))
            continue
        if "ledgerRef" in issue or issue.get("type") == "character_ooc":
            if story_ledger and (confirmed := _story_conflict(issue, issue.get("type"), ledger_view, ledger_source)):
                remote.append(confirmed)
            continue
        if kind and grounded(issue):
            issue = {**issue, "type": kind, "severity": "error"}
            if isinstance(issue.get("quote"), str):
                hard_quotes.add((kind, issue["quote"].strip()))
        remote.append({**issue, "severity": "warning"} if issue.get("type") == "word_count" and not has_word_error else issue)

    ai_issues = []
    ordinary_ai = []
    for issue in _items(parsed.get("aiIssues")):
        if not isinstance(issue, dict) or not any(issue.get(key) for key in ("quote", "suggestion", "impact")):
            continue
        entry = {
            "id": issue.get("id") or f"ch{chapter_number}_ai_{len(ai_issues) + 1}",
            "chapterNumber": _number(chapter_number),
            "category": issue.get("category") or "over_explaining",
            "quote": str(issue.get("quote") or "").strip(),
            "impact": str(issue.get("impact") or "").strip(),
            "priority": issue["priority"] if issue.get("priority") in _PRIORITIES else "P2",
            "suggestion": str(issue.get("suggestion") or "").strip(),
            "resolved": False,
        }
        if v2 and "planRef" in issue:
            conflict = plan_conflict({**entry, "planRef": issue["planRef"], "type": entry["category"],
                                      "message": entry["impact"] or entry["suggestion"] or "总纲口径冲突。"},
                                     _hard_conflict_type(issue, entry["category"]))
            remote.append(conflict)
            ai_issues.append({**entry, "priority": "P1" if conflict["severity"] == "error" else "P2"})
            continue
        if "ledgerRef" in issue or entry["category"] in ("character_ooc", "character_name_collision"):
            candidate = {**issue, "message": entry["impact"] or entry["suggestion"] or "正文与已核实的随章台账冲突。"}
            if story_ledger and (confirmed := _story_conflict(candidate, entry["category"], ledger_view, ledger_source)):
                remote.append(confirmed)
                ai_issues.append({**entry, "priority": "P1" if entry["priority"] == "P2" else entry["priority"]})
            continue
        if (kind := _hard_conflict_type(issue, entry["category"])) and grounded(entry):
            entry["priority"] = "P1" if entry["priority"] == "P2" else entry["priority"]
            key = kind, entry["quote"]
            if key not in hard_quotes:
                remote.append({"type": kind, "severity": "error", "quote": entry["quote"],
                               "message": entry["impact"] or entry["suggestion"] or "核心设定或桥段存在冲突。"})
                hard_quotes.add(key)
        else:
            ordinary_ai.append(entry)
        ai_issues.append(entry)
    if chapter_ai_gate:
        local = [*local, *verified_ai_blockers(ordinary_ai, chapter_text)]
    issues = _merge_issues(local, remote)
    passed = not any((issue.get("severity") or "error") == "error" for issue in issues) and (
        all(issue.get("severity") == "warning" for issue in issues) or parsed.get("passed") is not False
    )
    delta = parsed.get("factsDelta")
    changes = verified_boundary_changes(parsed.get("boundaryChanges"), book_plan=book_plan, chapter_text=chapter_text) if passed else []
    return {
        "passed": passed, "chapterNumber": chapter_number, "wordCount": word_count, "issues": issues,
        "aiIssues": ai_issues, "factsDelta": delta if passed and isinstance(delta, dict) else None,
        "summary": (f"第{chapter_number}章发现 {len(issues)} 处问题" if issues
                    else parsed.get("summary") or f"第{chapter_number}章一致性审校通过"),
        "usage": reply.get("usage"),
        **({"boundaryChanges": changes} if changes else {}),
    }


async def review_chapter_stages(*, chapter_number, chapter_text, job_id=None, outline=None, canonical_facts=None,
                                prev_chapter_ending="", pov=_DEFAULT_POV, target_words=1500, min_words=None,
                                max_words=None, llm_config=None, verify_llm_config=None, test_scenario=None,
                                max_retries=2, on_event=None, chapter_ai_gate=False, long_memory=False,
                                degrade_on_verify_exhausted=False, store=None, client=None, book_plan=None,
                                recent_chapters=None, rolling_arc_outlines=False, story_ledger=False,
                                extract_missing_facts=None, boundary_state=()):
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
        "chapter_ai_gate": chapter_ai_gate, "book_plan": book_plan, "long_memory": long_memory,
        "rolling_arc_outlines": rolling_arc_outlines, "story_ledger": story_ledger, "boundary_state": boundary_state,
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
            verify_llm_config=verify_llm_config, test_scenario=test_scenario,
            strict_review_result=degrade_on_verify_exhausted, store=store, client=client, recent_chapters=recent_chapters,
        )
        if story_ledger and extract_missing_facts and verification["passed"] and not isinstance(verification["factsDelta"], dict):
            delta = await extract_missing_facts(current)
            view = story_ledger_view(facts, chapter_number=chapter_number, book_plan=book_plan)
            conflicts = ledger_arithmetic_conflicts(delta, current, view)
            verification["factsDelta"] = delta if not conflicts else None
            if conflicts:
                verification.pop("boundaryChanges", None)
                verification.update(passed=False, issues=[*verification["issues"], *conflicts],
                                    summary=f"第{chapter_number}章补提事实时发现余额硬冲突")
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


async def verify_and_repair_chapter(*, book_plan=None, **options):
    async with aclosing(review_chapter_stages(book_plan=book_plan, **options)) as stages:
        async for phase in stages:
            if phase["phase"] == "review":
                result = phase["result"]
    return result


async def recheck_downstream_chapters(*, changed_from_chapter, chapters=(), outlines=(), canonical_facts=None,
                                     pov=_DEFAULT_POV, target_words=1500, llm_config=None, test_scenario=None,
                                     store=None, client=None, book_plan=None, long_memory=False, rolling_arc_outlines=False,
                                     story_ledger=False, boundary_history=None):
    chapter_order = lambda entry: _number(entry.get("chapterNumber")) or 0
    changed = _number(changed_from_chapter)
    downstream = sorted((chapter for chapter in chapters if chapter_order(chapter) > changed), key=chapter_order)
    by_chapter = {}
    for outline in outlines:
        by_chapter.setdefault(chapter_order(outline), outline)
    affected = []
    for chapter in downstream:
        number = chapter["chapterNumber"]
        boundary_state = collect_boundary_state(chapters if boundary_history is None else boundary_history,
                                                before_chapter=number, book_plan=book_plan)
        result = await verify_chapter(
            chapter_number=number, chapter_text=chapter.get("content") or "", outline=by_chapter.get(chapter_order(chapter)),
            canonical_facts=canonical_facts, pov=pov, target_words=target_words, llm_config=llm_config,
            test_scenario=test_scenario, store=store, client=client, book_plan=book_plan, long_memory=long_memory,
            rolling_arc_outlines=rolling_arc_outlines, story_ledger=story_ledger, boundary_state=boundary_state,
        )
        if not result["passed"]:
            affected.append({"chapterNumber": number, "issues": result["issues"], "summary": result["summary"]})
    return {"changedFromChapter": changed, "checkedCount": len(downstream), "affectedChapters": affected}
