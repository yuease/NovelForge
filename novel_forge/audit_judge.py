"""Ground model findings and apply the independent local audit gate."""

import math

from .text_evidence import locate_quote, normalize_quote


def _rows(value):
    return value if isinstance(value, list) else []


def _number(value):
    if type(value) is int and value > 0:
        return value
    if isinstance(value, str) and len(value) <= 12 and value.isdecimal() and int(value) > 0:
        return int(value)
    return None


def _finding_key(item):
    quote = item.get("quote")
    return (item.get("chapterNumber"), str(item.get("rule") or item.get("code") or item.get("type")),
            normalize_quote(quote) if isinstance(quote, str) else str(item.get("message") or ""))


def ground_blockers(report: dict, chapters: list[dict], *, strict_quote=False) -> tuple[list[dict], list[dict]]:
    """Return grounded blockers and downgraded findings, including finale score caps."""
    sources = {row["chapterNumber"]: row.get("content") or "" for row in chapters}
    finale = set(sorted(sources)[-3:])
    grounded, downgraded, seen = [], [], set()
    findings = [row for row in _rows(report.get("blockingIssues")) if isinstance(row, dict)]
    strict_quote = (strict_quote or report.get("strict_quote") is True or report.get("auditProtocolVersion") == 2
                    or any("quote" in row for row in findings))
    referenced = {_number(row.get("chapterNumber")) for row in findings}
    # Pre-WF-15 callers also used issueChapters alone to request a chapter review.
    if report.get("passed") is False and not strict_quote:
        findings.extend({"chapterNumber": number, "message": report.get("summary") or "终审要求复核本章。"}
                        for value in _rows(report.get("issueChapters"))
                        if (number := _number(value)) in sources and number not in referenced)
    normalized = {}
    for raw in findings:
        number = _number(raw.get("chapterNumber"))
        quote, kind = raw.get("quote"), raw.get("type")
        has_quote = isinstance(quote, str) and bool(quote.strip())
        if has_quote and number in sources and number not in normalized:
            normalized[number] = normalize_quote(sources[number])
        proof = locate_quote(quote, normalized[number]) if has_quote and number in normalized else None
        item = {"source": "llm", "chapterNumber": number, "type": kind if isinstance(kind, str) else "chapter_review",
                "message": raw.get("message") if isinstance(raw.get("message"), str) else "终审要求复核本章。"}
        if has_quote:
            item["quote"] = quote
        key = _finding_key(item)
        if key in seen:
            continue
        seen.add(key)
        macro = kind in ("unresolved_mainline", "ending")
        legacy = not strict_quote and not macro and ("type" not in raw or report.get("passed") is False)
        if number in sources and (proof or legacy):
            grounded.append({**item, "code": "llm_blocker", "repairable": True})
        elif number in finale and macro:
            downgraded.append({**item, "code": "ungrounded", "grounding": "cap", "penalty": 15})
        else:
            downgraded.append({**item, "code": "ungrounded", "grounding": "ungrounded"})
    return grounded, downgraded


def adjudicate_audit(report: dict, diagnostics: dict, chapters: list[dict]) -> dict:
    """Require both the model's pass and the absence of grounded hard blockers."""
    grounded, downgraded = ground_blockers(report, chapters)
    blockers, seen, local_quotes = [], set(), {}
    for item in [*diagnostics["hardBlockers"], *grounded]:
        key = _finding_key(item)
        number, quote = item.get("chapterNumber"), item.get("quote")
        proof = normalize_quote(quote) if isinstance(quote, str) and quote.strip() else None
        if key in seen or (item.get("source") == "llm" and proof and proof in local_quotes.get(number, ())):
            continue
        seen.add(key)
        if proof and item.get("source") != "llm":
            local_quotes.setdefault(number, set()).add(proof)
        blockers.append(item)

    raw_score = report.get("score")
    if type(raw_score) not in (int, float) or (isinstance(raw_score, float) and not math.isfinite(raw_score)):
        raw_score = 85 if report.get("passed") is True else 55
    raw_score = min(100, max(0, raw_score))
    macro_penalty = sum(item.get("penalty", 0) for item in downgraded)
    score_cap = diagnostics["scoreCap"]
    if macro_penalty:
        local_penalty = int(sum(item["penalty"] for item in diagnostics.get("capItems", [])))
        score_cap = min(score_cap, max(0, 100 - local_penalty - macro_penalty))
    if grounded:
        score_cap = min(score_cap, 69)
    score = min(raw_score, score_cap)
    passed = report.get("passed") is True and not blockers
    summary = report.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        summary = "全书终审通过。" if passed else "全书终审存在阻塞问题或返回格式无效。"
    return {
        **report, "passed": passed, "score": score, "rawScore": raw_score, "auditScoreCap": score_cap,
        "verdict": ("ready" if score >= 85 else "revise_recommended") if passed else "blocked",
        "summary": summary,
        "blockingIssues": blockers, "issueChapters": sorted({item["chapterNumber"] for item in blockers}),
        "recommendations": _rows(report.get("recommendations")),
        "auditInputHash": diagnostics["auditInputHash"],
        "localDiagnostics": {
            "version": diagnostics.get("version", 1), "coverage": diagnostics["coverage"], "stats": diagnostics["stats"],
            "hardBlockers": diagnostics["hardBlockers"][:50], "hardBlockerCount": len(diagnostics["hardBlockers"]),
            "capItems": diagnostics.get("capItems", []), "advisories": [*diagnostics.get("advisories", []), *downgraded],
        },
        "unresolvedDrifts": diagnostics.get("unresolvedDrifts", []),
    }


def select_audit_repairs(blockers: list[dict], repair_ledger: dict[str, int], *, limit=4) -> list[dict]:
    by_chapter = {}
    for item in blockers:
        number = _number(item.get("chapterNumber"))
        if item.get("repairable") is not True or number is None or _number(repair_ledger.get(str(number))) is not None:
            continue
        by_chapter.setdefault(number, []).append(item)

    def priority(item):
        return item.get("source") == "llm" or item.get("code") == "llm_blocker"

    numbers = sorted(by_chapter, key=lambda n: (min(map(priority, by_chapter[n])), -n))
    return [{"chapterNumber": number, "findings": sorted(by_chapter[number], key=priority)}
            for number in numbers[:max(0, limit)]]
