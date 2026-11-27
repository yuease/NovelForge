"""Local admission, text patching and regression checks for arc drift repair."""

import re
from bisect import bisect_left, bisect_right

from .chapter_style import _PLACEHOLDERS, find_meta_text
from .ledger_checks import assess_ledger_delta, ledger_label_aliases
from .long_memory import drift_id, story_ledger_view
from .storage import count_chinese_words
from .text_evidence import locate_quote, normalize_quote


_PRIORITY = {"canon": 0, "value": 1, "clock": 2}
_CLOCK_TERMS = {
    "day": ("day", "日期", "第几天", "第几日", "次日", "当天", "隔日", "昨天", "今天", "明天"),
    "season": ("season", "季节", "春季", "夏季", "秋季", "冬季", "初冬", "深秋"),
    "timeOfDay": ("timeofday", "时段", "清晨", "早晨", "中午", "下午", "傍晚", "深夜", "凌晨"),
    "countdown": ("countdown", "倒计时", "剩余", "还剩", "还有", "期限"),
}
_GENERIC_ACCOUNT_TERMS = {"余额", "可用余额", "账户余额", "账面", "账上", "账户", "账本", "账目"}
_SURROGATE = re.compile(r"[\ud800-\udfff]")


def _rows(value):
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def quote_span(quote, text, *, exact=False):
    """Locate a unique quote in the original text, including its raw offsets."""
    if not isinstance(quote, str) or not quote or not isinstance(text, str):
        return None
    source = normalize_quote(text)
    proof = normalize_quote(quote) if exact and quote in text else locate_quote(quote, source)
    if not proof:
        return None
    start = source.find(proof)
    if source.find(proof, start + 1) != -1:
        return None
    if proof != normalize_quote(quote.strip('“”「」『』" ')) and (
        locate_quote(quote, source[start + 1:]) or locate_quote(quote, source[:start + len(proof) - 1])
    ):
        return None
    raw = text.find(quote)
    if raw >= 0 and normalize_quote(quote) == proof:
        return raw, raw + len(quote)

    pieces = [normalize_quote(char) for char in text]
    offsets = [0]
    for piece in pieces:
        offsets.append(offsets[-1] + len(piece))
    if "".join(pieces) != source:
        offsets = range(len(text) + 1)

        def prefix_length(end):
            return len(normalize_quote(text[:end]))

        key = prefix_length
    else:
        key = None
    left = bisect_right(offsets, start, key=key) - 1
    right = bisect_left(offsets, start + len(proof), key=key)
    if normalize_quote(text[left:right]) == proof:
        return left, right
    # A combining mark may change the last character without increasing its length.
    right = bisect_right(offsets, start + len(proof), key=key) - 1
    if normalize_quote(text[left:right]) == proof:
        return left, right
    return None


def _overlaps(left, right):
    return left[0] < right[1] and right[0] < left[1]


def _fact_deltas(facts):
    return {row.get("chapter"): row.get("delta") or {} for row in _rows(facts.get("factHistory"))}


def _cascade_risk(drift, later, deltas, book_plan):
    kind, number = drift["kind"], drift["chapter"]
    if not later or kind == "canon":
        return False
    origin = deltas.get(number, {}).get("memory") or {}
    context = normalize_quote(" ".join(str(drift.get(key) or "") for key in ("quote", "issue", "fixHint"))).casefold()
    following = [deltas.get(n, {}).get("memory") or {} for n in later]
    if kind == "clock":
        clock = origin.get("storyClock") or {}
        fields = {key for key, terms in _CLOCK_TERMS.items()
                  if any(term in context for term in terms) or (clock.get(key) and normalize_quote(clock[key]) in context)}
        if re.search(r"第[0-9一二三四五六七八九十百]+[天日]", context):
            fields.add("day")
        if not fields:
            fields = set(clock).intersection(_CLOCK_TERMS)
        return any(fields.intersection(event.get("storyClock") or {}) for event in following)

    def account_terms(row):
        return {normalize_quote(term).casefold() for term in ledger_label_aliases(row.get("label"), book_plan)} - _GENERIC_ACCOUNT_TERMS

    quote = normalize_quote(drift["quote"])
    accounts = set()
    origin_balances = _rows(origin.get("ledgerBalances"))
    later_accounts = [account_terms(row) for event in following for row in _rows(event.get("ledgerBalances"))]
    for row in origin_balances:
        evidence = normalize_quote(row.get("quote") or "")
        if evidence and (quote in evidence or evidence in quote):
            accounts.update(account_terms(row))
    if any(any(term in context for term in terms) for terms in later_accounts):
        return True
    if (not accounts and len(origin_balances) == 1 and any(term in context for term in _GENERIC_ACCOUNT_TERMS)):
        accounts = account_terms(origin_balances[0])
    return any(terms.intersection(accounts) for terms in later_accounts)


def classify_drifts(arc_summary, arc_chapters, *, facts, book_plan=None, latest_committed=None, ledger=(),
                    total_chapters=200, max_arc_chapters=2) -> tuple[list[dict], list[dict]]:
    chapters = {row["chapterNumber"]: row for row in arc_chapters}
    fresh = (latest_committed is None or arc_summary["endChapter"] == latest_committed) and not any(
        row.get("authorEdited") for row in chapters.values()
    )
    entries = {row["id"]: row for row in ledger}
    attempted = {row["chapter"] for row in entries.values() if row.get("autoRepairAttempts", 0) >= 1}
    deltas = _fact_deltas(facts)
    groups, deferred, seen = {}, [], set()

    def defer(identifier, reason):
        deferred.append({"id": identifier, "status": "deferred", "reason": reason})

    for drift in _rows(arc_summary.get("driftChecks")):
        identifier = drift_id(drift)
        prior = entries.get(identifier, {})
        if (identifier in seen or prior.get("status") in ("repaired", "dismissed")
                or (prior.get("resolution") or {}).get("by") == "human"):
            continue
        seen.add(identifier)
        number, kind = drift["chapter"], drift["kind"]
        chapter = chapters.get(number, {})
        if not fresh:
            defer(identifier, "stale_arc")
        elif chapter.get("status") == "needs_review":
            defer(identifier, "needs_review")
        elif not quote_span(drift.get("quote"), chapter.get("content")):
            defer(identifier, "quote_not_unique")
        elif number in attempted:
            defer(identifier, "attempt_cap")
        elif kind not in _PRIORITY:
            defer(identifier, "ooc_deferred")
        elif _cascade_risk(drift, [n for n in chapters if n > number], deltas, book_plan):
            defer(identifier, "cascade_risk")
        else:
            groups.setdefault(number, []).append({**drift, "id": identifier})

    remaining = max(0, max(4, total_chapters // 10) - len(attempted))
    queue = []
    for number, drifts in sorted(groups.items(), key=lambda item: (
        min(_PRIORITY[row["kind"]] for row in item[1]), item[0]
    )):
        drifts.sort(key=lambda row: _PRIORITY[row["kind"]])
        if len(queue) < min(max_arc_chapters, remaining):
            queue.append({"chapter": number, "drifts": drifts[:3]})
            drifts = drifts[3:]
        for row in drifts:
            defer(row["id"], "quota")
    return queue, deferred


def apply_text_patches(text: str, patches: list[dict], drifts_by_id: dict, *, chapter_number: int,
                       book_plan=None, tail_window=380) -> tuple[str, list[dict], list[dict]]:
    """Apply non-overlapping patches addressed to the original chapter."""
    applied, rejected, edits, used = [], [], [], set()
    span_budget = min(600, max(120, int(len(text) * 0.08)))
    words = count_chinese_words(text)
    paragraphs = sum(bool(line.strip()) for line in text.split("\n"))
    edited_span = 0
    result = text
    for patch in _rows(patches):
        identifier = patch.get("driftId")
        drift = drifts_by_id.get(identifier) if isinstance(identifier, str) else None
        find, replacement = patch.get("find"), patch.get("replace")
        reason = None
        if patch.get("confirmed") is not True:
            reason = "unconfirmed"
        elif not drift or identifier in used:
            reason = "unknown_drift" if not drift else "duplicate_patch"
        elif (not isinstance(find, str) or not 0 < len(find) <= 200 or not isinstance(replacement, str)
              or _SURROGATE.search(replacement)):
            reason = "invalid_patch"
        else:
            span = quote_span(find, text, exact=True)
            quote = quote_span(drift.get("quote"), text)
            if not span or not quote or not span[0] <= quote[0] < quote[1] <= span[1]:
                reason = "quote_not_unique"
            else:
                start, end = span
                original = text[start:end]
                if len(original) > 200 or len(replacement) > int(len(find) * 1.5) + 40:
                    reason = "patch_length"
                elif edited_span + len(original) > span_budget or any(_overlaps(span, edit[:2]) for edit in edits):
                    reason = "edit_span"
                elif original.count("\n") != replacement.count("\n"):
                    reason = "paragraph_changed"
                elif end > max(0, len(text) - tail_window) and quote[1] <= len(text) - tail_window:
                    reason = "tail_changed"
                elif original == replacement:
                    reason = "no_change"
                elif (any(pattern.search(replacement) for pattern in _PLACEHOLDERS)
                      or any(issue["severity"] == "error" for issue in find_meta_text(
                          "正文。\n" + replacement, chapter_number=chapter_number, book_plan=book_plan
                      ))):
                    reason = "patch_regressed"
                else:
                    candidate = text
                    for left, right, value in sorted([*edits, (start, end, replacement)], reverse=True):
                        candidate = candidate[:left] + value + candidate[right:]
                    if sum(bool(line.strip()) for line in candidate.split("\n")) != paragraphs:
                        reason = "paragraph_changed"
                    elif ((len(text) < 1000 and abs(len(candidate) - len(text)) > 30)
                            or (len(text) >= 1000 and abs(count_chinese_words(candidate) - words) > words * 0.03)):
                        reason = "word_delta"
                    else:
                        result = candidate
                        edits.append((start, end, replacement))
                        edited_span += len(original)
                        used.add(identifier)
                        applied.append({**patch, "start": start, "end": end})
        if reason:
            rejected.append({**patch, "reason": reason})
    return result, applied, rejected


def touches_fact_evidence(text, patches, delta):
    """Check existing journal quotes against the accepted raw edit spans."""
    source = normalize_quote(text)
    spans = [(len(normalize_quote(text[:row["start"]])), len(normalize_quote(text[:row["end"]]))) for row in patches]

    def overlaps_evidence(value):
        if isinstance(value, dict):
            quote = value.get("quote")
            if isinstance(quote, str) and (proof := locate_quote(quote, source)):
                for left, right in spans:
                    offset = source.find(proof, max(0, left - len(proof) + 1), right + len(proof) - 1)
                    if offset != -1 and _overlaps((left, right), (offset, offset + len(proof))):
                        return True
            return any(overlaps_evidence(item) for item in value.values())
        return isinstance(value, list) and any(overlaps_evidence(item) for item in value)

    return overlaps_evidence(delta)


def identity_alias_conflicts(text, facts, book_plan, *, chapter_number):
    """Flag explicit conflicting aliases in the locked scope or prior character facts."""
    locks = (book_plan or {}).get("canonLocks") or {}
    issues = []
    paragraphs = [(normalize_quote(part), re.split(r"[。！？!?\n]", part)) for part in re.split(r"\n\s*\n", text)]
    for lock in _rows(locks.get("identityAliases")):
        character, scope, alias = (normalize_quote(str(lock.get(key) or "")) for key in ("character", "scope", "alias"))
        if not character or not scope or not alias:
            continue
        recorded = (facts.get("characters") or {}).get(lock["character"], {})
        prior = recorded.get("alias") if (recorded.get("updatedAtChapter") or chapter_number) < chapter_number else None
        pattern = re.compile(rf"{re.escape(character)}.{{0,28}}?(?:网名|账号|昵称|别名|化名|称作|称为)[是为叫：:\"「『]*"
                             r"(?P<alias>@[\w-]+|(?<=[\"「『])[^\"」』,。!?]{1,24}(?=[\"」』]))")
        prior_locked = alias.startswith("@") and isinstance(prior, str) and normalize_quote(prior) == alias
        for context, sentences in paragraphs:
            scoped = scope in context
            if not scoped and not prior_locked:
                continue
            for raw in sentences:
                for match in pattern.finditer(normalize_quote(raw)):
                    observed = match["alias"]
                    if observed != alias and (scoped or observed.startswith("@")):
                        issues.append({"type": "canon_conflict", "severity": "error", "chapter": chapter_number,
                                       "character": lock["character"], "scope": lock["scope"], "alias": observed,
                                       "quote": raw.strip()[:160]})
    return issues


def downstream_light_check(chapters_after: list[dict], candidate_facts: dict, book_plan=None) -> list[dict]:
    deltas = _fact_deltas(candidate_facts)
    conflicts = []
    for chapter in chapters_after:
        number, text = chapter["chapterNumber"], chapter.get("content") or ""
        view = story_ledger_view(candidate_facts, chapter_number=number, book_plan=book_plan)
        assessment = assess_ledger_delta(deltas.get(number, chapter.get("factsDelta") or {}), text, view, book_plan=book_plan)
        conflicts.extend({**issue, "chapter": number} for issue in assessment["issues"])
        conflicts.extend(identity_alias_conflicts(text, candidate_facts, book_plan, chapter_number=number))
    return conflicts
