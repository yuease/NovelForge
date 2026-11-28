"""Deterministic evidence and scoring limits for the full-book audit."""

import hashlib
import json
import re
from collections import Counter

from .arc_planning import normalize_skeleton, skeleton_similarity
from .chapter_style import (
    _NON_FATAL_AI_CATEGORIES, _NON_FATAL_STYLE_RULES, _PLACEHOLDERS, _around_match, _narration,
    find_meta_text, reaction_counts,
)
from .chapter_title import title_body_conflicts
from .entity_salience import _named, entity_index, retired_character_status, retired_roster
from .ledger_checks import assess_ledger_delta
from .long_memory import drift_ledger, story_ledger_label_key, story_ledger_view, unresolved_drifts
from .storage import count_chinese_words
from .text_evidence import locate_quote, normalize_quote


def _rows(value):
    return [row for row in value if isinstance(row, dict)] if isinstance(value, (list, tuple)) else []


def _chapter(value):
    return value if type(value) is int and value > 0 else 0


def _hash(value):
    data = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode("ascii")).hexdigest()


def open_foreshadowing(facts):
    return sorted(
        (row for row in _rows(facts.get("foreshadowing") or facts.get("openForeshadowing"))
         if row.get("status") in (None, "open", "planted", "reinforced", "active") and not row.get("payoffChapter")),
        key=lambda row: _chapter(row.get("plantedChapter")) or _chapter(row.get("sourceChapter")),
    )


def _arc_sequence(facts, plans, outlines):
    arcs = {}
    for row in [*_rows(plans), *_rows(facts.get("arcSummaries"))]:
        start, end = _chapter(row.get("startChapter")), _chapter(row.get("endChapter"))
        if not start or end < start:
            continue
        old = arcs.get(start, {})
        arcs[start] = {**old, **row, "skeleton": normalize_skeleton(row.get("skeleton")) or old.get("skeleton", [])}
    for row in _rows(list(outlines)):
        if (start := _chapter(row.get("startChapter"))) and _chapter(row.get("endChapter")) >= start:
            arcs.setdefault(start, row)
    return [arcs[start] for start in sorted(arcs)]


def _rhythm_stats(arcs):
    length_run = similar_run = longest_length = longest_similar = 0
    previous_end = previous_length = 0
    previous_skeleton = []
    for arc in arcs:
        start, end = arc["startChapter"], arc["endChapter"]
        length, skeleton = end - start + 1, normalize_skeleton(arc.get("skeleton"))
        adjacent = previous_end + 1 == start
        length_run = length_run + 1 if adjacent and length == previous_length else 1
        similar_run = (similar_run + 1 if adjacent and skeleton_similarity(previous_skeleton, skeleton) >= 0.6
                       else int(bool(skeleton)))
        longest_length = max(longest_length, length_run)
        longest_similar = max(longest_similar, similar_run)
        previous_end, previous_length, previous_skeleton = end, length, skeleton
    return longest_length, longest_similar


def _retired_mentions(chapters, facts, plans, book_plan, total):
    before = total + 1
    index = entity_index(facts, before_chapter=before)
    names = retired_roster(facts, index, book_plan=book_plan, arc_plans=plans, current_chapter=before, limit=None)
    exits = {}

    def remember(name, number):
        if name in names and number:
            exits[name] = min(number, exits.get(name, number))

    for arc in _rows(plans):
        for chip in _rows(arc.get("villainChips")):
            if chip.get("exitPlan") == "exit":
                remember(chip.get("villain"), _chapter(arc.get("endChapter")))
    for row in _rows(facts.get("factHistory")):
        for name, state in _named((row.get("delta") or {}).get("characters")):
            if isinstance(state, dict) and (status := state.get("status")) and retired_character_status(status) == status:
                remember(name, _chapter(row.get("chapter")))
    for name in names:
        state = index["characters"].get(name, {}).get("entry", {})
        if (status := state.get("status")) and retired_character_status(status) == status:
            remember(name, _chapter(state.get("exitChapter")) or _chapter(state.get("updatedAtChapter"))
                     or _chapter(state.get("sourceChapter")))
    for chapter in chapters:
        number = chapter["chapterNumber"]
        remaining = {name for name, end in exits.items() if end < number}
        if not remaining:
            continue
        for _, prose in _narration(chapter.get("content") or ""):
            for name in sorted(remaining):
                pattern = re.escape(name)
                if name.isascii():
                    pattern = rf"(?<![\w]){pattern}(?![\w])"
                if match := re.search(pattern, prose):
                    remaining.remove(name)
                    yield {"code": "retired_character", "chapterNumber": number, "character": name,
                           "exitChapter": exits[name], "quote": _around_match(prose, match),
                           "message": f"已退场角色「{name}」在后续旁白中出现，请核对语境。"}
            if not remaining:
                break


def _fingerprint(job, chapters, facts, book_plan, plans, arcs):
    fields = ("chapterNumber", "title", "content", "status", "summary", "verification", "wordCount", "factsDelta")
    return _hash({
        "version": 1, "totalChapters": job.get("totalChapters") or len(chapters),
        "targetWords": job.get("targetWords"), "totalWords": job.get("totalWords"),
        "chapters": [{key: row.get(key) for key in fields} for row in chapters],
        "bookPlan": _hash(book_plan), "driftLedger": drift_ledger(facts), "arcPlans": plans, "arcs": arcs,
        "facts": {key: facts.get(key) for key in ("factHistory", "storyClock", "ledgerBalances", "characters",
                                                   "characterStates", "volumeSummaries", "foreshadowing",
                                                   "openForeshadowing", "themeLedger")},
    })


def audit_input_hash(job, chapters, facts, outlines=(), arc_plans=()):
    plans = list(arc_plans or job.get("arcPlans") or [])
    arcs = _arc_sequence(facts, plans, outlines.values() if isinstance(outlines, dict) else outlines)
    return _fingerprint(job, sorted(chapters, key=lambda row: row["chapterNumber"]), facts, job.get("bookPlan"), plans, arcs)


def collect_book_diagnostics(job, chapters, facts, outlines=(), arc_plans=(), *, book_plan=None) -> dict:
    """Scan the current manuscript and journal without calling a model or modifying inputs."""
    chapters = sorted(chapters, key=lambda row: row["chapterNumber"])
    total = job.get("totalChapters") or len(chapters)
    book_plan = book_plan if book_plan is not None else job.get("bookPlan")
    plans = list(arc_plans or job.get("arcPlans") or [])
    arcs = _arc_sequence(facts, plans, outlines.values() if isinstance(outlines, dict) else outlines)
    words = sum(count_chinese_words(row.get("content") or "") for row in chapters)
    chapter_by_number = {row["chapterNumber"]: row for row in chapters}
    fingerprint = _fingerprint(job, chapters, facts, book_plan, plans, arcs)
    hard, cap, advisories, seen = [], [], [], set()

    def add(target, item, *, penalty=None, group=None, ceiling=None):
        quote = item.get("quote")
        key = (item.get("chapterNumber"), item.get("rule") or item["code"],
               normalize_quote(quote) if isinstance(quote, str) else "", item.get("character"))
        if key in seen:
            return
        seen.add(key)
        if penalty is not None:
            if ceiling is not None:
                penalty = min(penalty, ceiling - sum(row["penalty"] for row in cap if row.get("group") == group))
            if penalty <= 0:
                return
            item = {**item, "penalty": penalty, **({"group": group} if group else {})}
        target.append(item)

    locks = (book_plan or {}).get("canonLocks") or {}
    canon_terms = [normalize_quote(row[field]) for key, fields in (
        ("identityAliases", ("alias", "character")), ("boundaries", ("subject", "rule")),
    ) for row in _rows(locks.get(key)) for field in fields if isinstance(row.get(field), str) and row[field].strip()]
    sources = {number: normalize_quote(row.get("content") or "") for number, row in chapter_by_number.items()}
    stale, live_drifts = 0, []
    for drift in unresolved_drifts(facts):
        number, kind, quote = drift["chapter"], drift["kind"], drift.get("quote")
        if not isinstance(quote, str) or not locate_quote(quote, sources.get(number, "")):
            stale += 1
            continue
        live_drifts.append(drift)
        reference = normalize_quote(" ".join(str(drift.get(key) or "") for key in (
            "issue", "fixHint", "quote", "rule", "category", "ledgerRef",
        ))).casefold()
        locked_canon = kind == "canon" and (
            any(key in reference for key in ("identityaliases", "boundaries"))
            or any(term.casefold() in reference for term in canon_terms)
        )
        item = {"code": f"drift_{kind}", "chapterNumber": number, "quote": quote, "message": drift["issue"],
                "fixHint": drift.get("fixHint"), "driftId": drift["id"], "kind": kind}
        if kind in ("value", "clock", "alias") or locked_canon:
            add(hard, {**item, "repairable": True})
        else:
            add(cap, item, penalty=2, group="drifts", ceiling=20)

    history = {row["chapter"]: row.get("delta") or {} for row in _rows(facts.get("factHistory")) if "chapter" in row}
    tracked = {key for row in _rows(facts.get("ledgerBalances"))
               if (key := story_ledger_label_key(row.get("label")))}
    has_ledger = bool(tracked) or any((delta.get("memory") or {}).get("ledgerBalances")
                                    for delta in [*history.values(), *(row.get("factsDelta") or {} for row in chapters)])
    pending = sum(row.get("status") == "needs_review" for row in chapters)
    reactions = Counter()
    for chapter in chapters:
        number, body = chapter["chapterNumber"], chapter.get("content") or ""
        reactions.update(reaction_counts(body))
        for issue in find_meta_text(body, chapter_number=number, book_plan=book_plan):
            if issue["severity"] == "error":
                add(hard, {**issue, "code": "meta_text", "chapterNumber": number, "repairable": True})
        for pattern in _PLACEHOLDERS:
            for match in pattern.finditer(body):
                add(hard, {"code": "meta_text", "rule": "placeholder", "chapterNumber": number,
                           "quote": _around_match(body, match), "message": f"正文含占位符「{match[0]}」。", "repairable": True})
        if has_ledger:
            view = story_ledger_view(facts, chapter_number=number, book_plan=book_plan)
            tracked.update(key for row in view.get("ledgerBalances", [])
                           if (key := story_ledger_label_key(row.get("label"))))
            delta = history.get(number, chapter.get("factsDelta") or {})
            assessment = assess_ledger_delta(delta, body, view, book_plan=book_plan)
            for issue in assessment["issues"]:
                if issue["severity"] == "error":
                    add(hard, {**issue, "code": "ledger", "chapterNumber": number, "repairable": True})
        if chapter.get("status") == "needs_review":
            verification = chapter.get("verification") or {}
            blocking = [row for row in _rows(verification.get("issues")) if row.get("severity") != "warning"]
            nonstyle = [
                row for row in blocking if not (
                    row.get("type") in (None, "ai_flavor") and (
                        row.get("rule") in _NON_FATAL_STYLE_RULES
                        or row.get("category") in _NON_FATAL_AI_CATEGORIES and row.get("priority") in (None, "P1")
                    )
                )
            ]
            nonstyle += [row for row in _rows(verification.get("aiIssues")) if row.get("priority") == "P0"]
            if nonstyle or pending > max(3, int(total * 0.03)):
                for issue in nonstyle or [{}]:
                    add(hard, {**issue, "code": "needs_review_hard", "chapterNumber": number, "repairable": True,
                               "message": issue.get("message") or "待复核章节超过完本容许数量。"})
            else:
                add(cap, {"code": "needs_review_style", "chapterNumber": number,
                          "message": "有待复核的文风问题。", "quote": (blocking[0].get("quote") if blocking else None)},
                    penalty=1.5, group="needs_review", ceiling=9)
        if conflicts := title_body_conflicts(chapter.get("title") or "", body):
            add(advisories, {"code": "title_body_conflict", "chapterNumber": number, "conflicts": conflicts,
                             "message": "章名数值与正文不一致。"})

    same_length, similar = _rhythm_stats(arcs)
    if similar >= 4:
        add(cap, {"code": "skeleton_monotony", "message": f"相似单元骨架连续出现 {similar} 次。"},
            penalty=10 if similar >= 6 else 5)
    reaction_limit = max(8, int(words / 100_000 * 6))
    if words >= 30_000:
        for family, count in reactions.most_common():
            if count > reaction_limit:
                add(cap, {"code": "reaction_overheat", "rule": f"reaction:{family}", "family": family, "count": count,
                          "message": f"「{family}」反应重复 {count} 次。"}, penalty=2, group="reaction", ceiling=8)
    retired_reuse = 0
    for arc in arcs:
        for tactic in dict.fromkeys((arc.get("volumeProgress") or {}).get("retiredReused") or []):
            retired_reuse += 1
            add(cap, {"code": "retired_tactic_reuse", "rule": f"retired:{tactic}", "chapterNumber": arc["endChapter"],
                      "message": f"单元复用已退役手段「{tactic}」。"}, penalty=1, group="retired", ceiling=8)
    early_clues = []
    for clue in open_foreshadowing(facts):
        planted = _chapter(clue.get("plantedChapter")) or _chapter(clue.get("sourceChapter"))
        if 0 < planted < total - 30:
            early_clues.append({"chapterNumber": planted, "clue": clue.get("clue"), "id": clue.get("id")})
    if early_clues:
        add(advisories, {"code": "open_foreshadowing", "clues": early_clues, "message": "较早铺设的伏笔仍未回收。"})
    if len(early_clues) > 8:
        add(cap, {"code": "open_foreshadowing_cap", "message": f"较早铺设的 {len(early_clues)} 个伏笔仍未回收。"}, penalty=5)
    for item in _retired_mentions(chapters, facts, plans, book_plan, total):
        add(advisories, item)

    if has_ledger:
        tracked.update(key for row in story_ledger_view(facts, chapter_number=total + 1, book_plan=book_plan).get("ledgerBalances", [])
                       if (key := story_ledger_label_key(row.get("label"))))
    coverage = {"chaptersScanned": len(chapters), "totalChapters": total,
                "arcsWithSkeleton": sum(bool(normalize_skeleton(row.get("skeleton"))) for row in arcs),
                "totalArcs": len(arcs), "trackedAccounts": len(tracked), "staleDriftsSkipped": stale}
    stats = {"totalWords": words, "maxSameLengthRun": same_length, "skeletonMaxSimilarRun": similar,
             "reactionCounts": dict(reactions), "reactionThreshold": reaction_limit, "retiredReused": retired_reuse,
             "openForeshadowing": len(early_clues), "needsReviewChapters": pending}
    high_risk = dict.fromkeys(
        [number for tier in (hard, cap) for item in tier if (number := item.get("chapterNumber")) in chapter_by_number]
        + [row["chapterNumber"] for row in chapters[-3:]]
    )
    base_cap = max(0, 100 - int(sum(item["penalty"] for item in cap)))
    return {"version": 1, "auditInputHash": fingerprint, "coverage": coverage, "hardBlockers": hard,
            "capItems": cap, "advisories": advisories, "scoreCap": min(base_cap, 69) if hard else base_cap,
            "stats": stats, "highRiskChapters": list(high_risk)[:12], "unresolvedDrifts": live_drifts}
