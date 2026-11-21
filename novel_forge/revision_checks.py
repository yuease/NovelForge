"""Source matching and deterministic checks used by the revision graph."""

import hashlib
import json
import re
from bisect import bisect_left
from collections import Counter
from difflib import SequenceMatcher


TRACKS = ("finance", "timeline", "continuity", "plot", "voice")
PRIORITIES = ("P0", "P1", "P2")
COMMON_PHRASES = ("瞳孔骤缩", "指节发白", "呼吸一滞", "动作一顿", "嘴角勾起一抹", "鸦雀无声")


class RevisionContentError(RuntimeError):
    def __init__(self, message, **details):
        super().__init__(message)
        self.details = details


def compact_json(data):
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def fingerprint(chapters):
    source = [(c["chapterNumber"], c["title"], c["content"]) for c in chapters]
    return hashlib.sha256(compact_json(source).encode("utf-8")).hexdigest()


def partition(chapters, volume_size):
    volumes = []
    for start in range(0, len(chapters), volume_size):
        numbers = [c["chapterNumber"] for c in chapters[start:start + volume_size]]
        volumes.append({"number": len(volumes) + 1, "chapters": numbers})
    return volumes


def windows(volumes, chapters, *, max_chars, max_chapters):
    result = []
    for volume in volumes:
        current, length = [], 0
        for n in volume["chapters"]:
            size = len(chapters[n]["content"])
            if current and (length + size > max_chars or len(current) >= max_chapters):
                result.append({"id": str(len(result) + 1), "volume": volume["number"], "chapters": current})
                current, length = [], 0
            current.append(n)
            length += size
        if current:
            result.append({"id": str(len(result) + 1), "volume": volume["number"], "chapters": current})
    return result


def normalized(text):
    return re.sub(r"\s+", "", str(text or ""))


_PUNCTUATION = dict(zip("，。．；：！？（）“”‘’", ",..;:!?()\"\"''"))
_NUMERIC_SPACE = re.compile(r"(?<=\d)\s+(?=\d)")


def indexed_text(text):
    chars, offsets = [], []
    number_spaces = {match.start() for match in _NUMERIC_SPACE.finditer(text)}
    for offset, char in enumerate(text):
        if char.isspace():
            if offset in number_spaces:
                chars.append(" ")
                offsets.append(offset)
            continue
        # Keep the spelling and separators of amounts intact.
        numeric = char in "，。．" and any(
            text[index].isdigit() for index in (offset - 1, offset + 1) if 0 <= index < len(text)
        )
        chars.append(char if numeric else _PUNCTUATION.get(char, char))
        offsets.append(offset)
    return "".join(chars), offsets


def occurrences(text, key):
    start = text.find(key)
    while start >= 0:
        yield start
        start = text.find(key, start + 1)


def nearby_excerpt(text, quote):
    key, _ = indexed_text(quote[:240])
    compact, positions = indexed_text(text)
    if len(key) < 6:
        return None
    width = min(12, max(6, len(key) // 3))
    windows = set()
    for index in sorted({0, (len(key) - width) // 2, len(key) - width, *range(0, len(key) - width + 1, 6)}):
        hits = []
        for at in occurrences(compact, key[index:index + width]):
            hits.append(at)
            if len(hits) > 3:
                break
        if 0 < len(hits) <= 3:
            windows.update(max(0, at - index) for at in hits)
        if len(windows) >= 12:
            break
    scored = []
    for start in sorted(windows):
        excerpt = compact[start:start + len(key)]
        match = SequenceMatcher(None, key, excerpt, autojunk=False)
        if match.ratio() >= 0.45:
            scored.append((match.find_longest_match().size, match.ratio(), start))
    ranked = sorted(scored, reverse=True)
    if not ranked or (len(ranked) > 1 and ranked[0][:2] == ranked[1][:2]):
        return None
    start = ranked[0][2]
    first = positions[start]
    end = positions[min(len(positions) - 1, start + min(len(key), 100) - 1)] + 1
    return {"offset": first, "quote": text[first:min(end, first + 160)]}


def evidence_excerpt(ref, chapters):
    chapter = chapter_number(ref.get("chapter"))
    field = ref.get("field", "content")
    if chapter in chapters and field in ("title", "content"):
        return nearby_excerpt(chapters[chapter][field], string(ref.get("quote"), 320))
    return None


def validate_reference_chapter(ref, chapters):
    if "chapter" in ref and chapter_number(ref["chapter"]) not in chapters:
        raise RevisionContentError(f"证据引用了本书不存在的章节 {ref['chapter']}。")


def chapter_number(value):
    if type(value) is int:
        return value
    return int(value) if isinstance(value, str) and value.isascii() and value.isdecimal() and len(value) < 8 else None


def quote_parts(quote):
    key = normalized(quote.strip().strip("“”‘’\"「」『』"))
    trimmed = re.sub(r"^(?:…+|\.{3,})|(?:…+|\.{3,})$", "", key).strip("“”‘’\"「」『』")
    fragments = tuple(dict.fromkeys(
        item.strip("，,。；;：“”‘’\"「」『』")
        for item in re.split(r"…+|\.{3,}|[。！？!?；;]", trimmed)
    ))
    anchors = tuple(dict.fromkeys(fragment for fragment in (trimmed, *fragments) if len(fragment) >= 6))
    return key, anchors


def quote_locations(quote, corpus):
    key, anchors = quote_parts(quote)
    matches = [n for n, body in corpus.items() if len(key) >= 6 and key in body]
    if matches:
        return matches, key, None
    if not anchors:
        return [], key, None
    scores = {
        n: sum(len(anchor) for anchor in anchors if anchor in body)
        for n, body in corpus.items()
    }
    best = max(scores.values(), default=0)
    if not best:
        return [], key, None
    matches = [n for n, score in scores.items() if score == best]
    return matches, key, "partial"


def issue_map(report, chapters):
    corpus = {n: normalized(c.get("title")) + "\0" + normalized(c["content"]) for n, c in chapters.items()}
    results, seen = [], set()
    for i, entry in enumerate(report.get("aiFlavorIssues") or [], 1):
        if not isinstance(entry, dict):
            continue
        claimed = chapter_number(entry.get("chapterNumber"))
        quote = str(entry.get("quote") or "").strip()
        matches, key, quote_match = quote_locations(quote, corpus)
        found = claimed if claimed in matches else matches[0] if len(matches) == 1 else None
        state = "matched" if found == claimed and found is not None else "relocated" if found else "ambiguous" if matches else "unmatched"
        identity = (found, key, str(entry.get("category") or ""))
        if found and identity in seen:
            state = "duplicate"
        if found:
            seen.add(identity)
        results.append({
            "id": f"R{i:03d}", "claimedChapter": claimed, "chapter": found,
            "matches": matches if state == "ambiguous" else [], "match": state,
            **({"quoteMatch": quote_match} if quote_match else {}),
            "priority": entry.get("priority") if entry.get("priority") in PRIORITIES else "P1",
            "category": str(entry.get("category") or ""),
            "quote": quote, "problem": str(entry.get("impact") or ""),
            "suggestion": str(entry.get("suggestion") or ""),
        })
    return results


def broken_ranges(report, chapters):
    result = set()
    for batch in report.get("batchSummaries") or []:
        if not isinstance(batch, dict) or "解析失败" not in str(batch.get("timelineSummary") or ""):
            continue
        match = re.fullmatch(r"(\d+)\s*[-–~至]\s*(\d+)", str(batch.get("batchRange", "")))
        if match:
            start, end = map(int, match.groups())
            result.update(n for n in chapters if start <= n <= end)
    return sorted(result)


def model_object(text, stage):
    stripped = str(text or "").strip()
    fence = chr(96) * 3
    if stripped.startswith(fence) and stripped.endswith(fence):
        stripped = stripped.partition("\n")[2][:-3].strip()
    try:
        data = json.loads(stripped)
    except (ValueError, RecursionError) as exc:
        raise RevisionContentError(f"{stage}没有返回完整 JSON，已保存完成的批次。") from exc
    if not isinstance(data, dict):
        raise RevisionContentError(f"{stage}返回格式不正确。")
    return data


def required_list(value, key):
    entries = value.get(key)
    if not isinstance(entries, list) or any(not isinstance(e, dict) for e in entries):
        raise RevisionContentError(f"模型结果缺少有效的 {key} 列表。")
    return entries


def string(value, limit=1000):
    return value.strip()[:limit] if isinstance(value, str) else ""


def match_ellipsis_excerpt(quote, source, compact_source, *, offsets=None, punctuation=False):
    pieces = re.split(r"…+|\.{3,}", normalized(quote))
    if len(pieces) == 1:
        return None
    parts = [piece for piece in pieces if piece]
    if punctuation:
        parts = [indexed_text(piece)[0] for piece in parts]
    if not any(len(part) >= 6 for part in parts):
        return None

    # Find valid suffixes first, so repeated anchors can use a later occurrence
    # without relying on an unbounded backtracking regular expression.
    starts, ends = [], []
    for part in reversed(parts):
        matches = [match.start() for match in re.finditer(f"(?={re.escape(part)})", compact_source)]
        if starts:
            linked = []
            for start in matches:
                end = start + len(part)
                successor = bisect_left(starts, end)
                if successor < len(starts) and starts[successor] - end <= 160:
                    linked.append((start, ends[successor]))
            starts, ends = [first for first, _ in linked], [last for _, last in linked]
        else:
            starts, ends = matches, [start + len(part) for start in matches]
        if not starts:
            return None

    if offsets is None:
        offsets = [i for i, character in enumerate(source) if not character.isspace()]
    for start, end in zip(starts, ends):
        first, last = offsets[start], offsets[end - 1] + 1
        if last - first <= 320:
            return source[first:last]
    return None


def audited_evidence(entry, chapters, allowed, *, punctuation=False):
    n = chapter_number(entry.get("chapter"))
    quote = string(entry.get("quote"), 320)
    field = entry.get("field", "content")
    key = normalized(quote)
    if n not in allowed or field not in ("content", "title") or len(key) < 6:
        return None
    source = chapters[n][field]
    compact_source = normalized(source)
    if key not in compact_source:
        exact = match_ellipsis_excerpt(quote, source, compact_source)
        if exact is None and punctuation:
            folded, offsets = indexed_text(source)
            needle, _ = indexed_text(quote)
            at = folded.find(needle)
            exact = (source[offsets[at]:offsets[at + len(needle) - 1] + 1] if at >= 0
                     else match_ellipsis_excerpt(quote, source, folded, offsets=offsets, punctuation=True))
        if exact is None:
            return None
        quote = exact
    return {"chapter": n, "quote": quote, **({"field": "title"} if field == "title" else {})}


def validate_audit(data, chapters, unit):
    allowed = set(unit["chapters"])
    facts, issues, unsupported = [], [], []
    for entry in required_list(data, "facts"):
        evidence = audited_evidence(entry, chapters, allowed)
        detail = string(entry.get("detail"), 320)
        if evidence and detail and entry.get("track") in TRACKS:
            facts.append({**evidence, "track": entry["track"], "detail": detail})
        elif detail:
            unsupported.append({"chapter": chapter_number(entry.get("chapter")), "note": detail})
    for index, entry in enumerate(required_list(data, "issues"), 1):
        evidence = audited_evidence(entry, chapters, allowed)
        problem = string(entry.get("problem"), 420)
        if evidence and problem:
            issues.append({
                **evidence, "id": f"A{unit['id']}-{index}", "match": "audit",
                "priority": entry.get("priority") if entry.get("priority") in PRIORITIES else "P1",
                "category": entry.get("track") if entry.get("track") in TRACKS else "continuity",
                "problem": problem, "suggestion": string(entry.get("suggestion"), 420),
            })
        elif problem:
            unsupported.append({"chapter": chapter_number(entry.get("chapter")), "note": problem})
    return {"facts": facts, "issues": issues, "unsupported": unsupported}


def valid_numbers(values, chapters):
    if not isinstance(values, list):
        raise RevisionContentError("台账章节范围格式无效。")
    numbers = [chapter_number(n) for n in values]
    if any(n not in chapters for n in numbers):
        raise RevisionContentError("台账引用了本书不存在的章节。")
    return sorted(set(numbers))


def resolve_rule_evidence(ref, source_refs, chapters, allowed):
    if "sourceId" not in ref:
        return audited_evidence(ref, chapters, allowed, punctuation=True)
    key = ref["sourceId"]
    source = source_refs.get(key) if isinstance(key, str) else None
    if not source or ("chapter" in ref and chapter_number(ref["chapter"]) != source["chapter"]):
        return None
    if "field" in ref and ref["field"] != source.get("field", "content"):
        return None
    return audited_evidence(source, chapters, allowed)


def validate_ledger(data, chapters, *, source_refs=None):
    rules, flows, checks = [], [], []
    source_refs = source_refs or {}
    all_numbers = set(chapters)
    for index, entry in enumerate(required_list(data, "rules"), 1):
        decision = string(entry.get("decision"), 1800)
        if not decision or entry.get("track") not in TRACKS:
            raise RevisionContentError("台账规则缺少明确的定稿口径或类别。")
        refs = entry.get("evidence")
        refs = refs if isinstance(refs, list) else []
        for ref in refs:
            if isinstance(ref, dict):
                validate_reference_chapter(ref, chapters)
        evidence = [proof for ref in refs if isinstance(ref, dict)
                    if (proof := resolve_rule_evidence(ref, source_refs, chapters, all_numbers))]
        if not evidence:
            rejected = []
            for ref in refs[:8]:
                if not isinstance(ref, dict):
                    ref = {"quote": ref}
                source_id = ref.get("sourceId")
                source = source_refs.get(source_id, {}) if isinstance(source_id, str) else {}
                rejected.append({"chapter": chapter_number(ref.get("chapter", source.get("chapter"))),
                                 "sourceId": string(source_id, 80),
                                 "quote": string(ref.get("quote") or source.get("quote"), 160)})
                if hint := evidence_excerpt(ref, chapters):
                    rejected[-1]["nearby"] = hint
            summary = "；".join(
                f"第 {ref['chapter'] if ref['chapter'] is not None else '?'} 章「{ref['quote'][:60] or ref['sourceId'] or '未提供引文'}」"
                + (f"（{ref['sourceId']}）" if ref["sourceId"] and ref["quote"] else "")
                + (f"。附近原文「{ref['nearby']['quote'][:90]}」" if "nearby" in ref else "")
                for ref in rejected[:3]
            ) or "未提供证据"
            raise RevisionContentError(
                f"台账规则第 {index} 条「{decision[:80]}」没有可核实的原稿证据。未命中：{summary}。",
                check="rule_evidence", ruleIndex=index, rule=decision[:120],
                chapters=sorted({ref["chapter"] for ref in rejected if ref["chapter"] is not None}),
                rejected=rejected,
            )
        rules.append({
            "id": f"L{len(rules) + 1:03d}", "track": entry["track"], "decision": decision,
            "chapters": valid_numbers(entry.get("chapters", []), chapters), "evidence": evidence,
        })
    for entry in required_list(data, "checks"):
        text = string(entry.get("text"), 240)
        if len(text) < 2 or entry.get("mode") not in ("absent", "present"):
            raise RevisionContentError("台账检索规则必须说明准确文本和保留或删除要求。")
        targets = valid_numbers(entry.get("chapters", []), chapters)
        exceptions = valid_numbers(entry.get("allowedChapters", []), chapters)
        if entry["mode"] == "present" and not targets:
            raise RevisionContentError("必须保留的引文需要指定章节。")
        checks.append({"text": text, "mode": entry["mode"], "chapters": targets, "allowedChapters": exceptions})
    for entry in required_list(data, "financialFlows"):
        label = string(entry.get("label"), 100)
        balance = entry.get("openingCents")
        if not label or type(balance) is not int:
            raise RevisionContentError("资金台账需要以分为单位的整数起始余额。")
        opening, past_n, rows = balance, 0, []
        for index, event in enumerate(required_list(entry, "entries"), 1):
            n = chapter_number(event.get("chapter"))
            delta, next_balance = event.get("deltaCents"), event.get("balanceCents")
            evidence = audited_evidence(event, chapters, all_numbers)
            location = f"资金台账「{label}」第 {index} 笔" + (f"（第 {n} 章）" if n else "")
            row = {"label": label, "index": index, "chapter": n, "quote": string(event.get("quote"), 320)}
            if not evidence:
                raise RevisionContentError(f"{location}的引文无法核实。请对照审计证据，填写对应章号和连续原文。",
                                           check="finance_evidence", row=row)
            if n < past_n:
                raise RevisionContentError(f"{location}排在第 {past_n} 章之后。请按章序排列后重新计算余额。",
                                           check="finance_order", row=row, previousChapter=past_n)
            if type(delta) is not int or type(next_balance) is not int:
                raise RevisionContentError(f"{location}的变动额和余额必须是以分为单位的整数。")
            if balance + delta != next_balance:
                raise RevisionContentError(f"{location}余额不闭合。前一笔余额 {balance} 分，本笔变动 {delta:+d} 分，"
                                           f"应为 {balance + delta} 分，实填 {next_balance} 分。请核对金额与口径。",
                                           check="finance_balance", row=row, previousBalance=balance, change=delta,
                                           actualBalance=next_balance, expectedBalance=balance + delta)
            rows.append({**evidence, "deltaCents": delta, "balanceCents": next_balance, "reason": string(event.get("reason"), 240)})
            past_n, balance = n, next_balance
        if rows:
            flows.append({"label": label, "openingCents": opening, "entries": rows})
    suspects = data.get("unresolved", [])
    if not isinstance(suspects, list):
        raise RevisionContentError("未决台账备注格式不正确。")
    return {"rules": rules, "checks": checks, "financialFlows": flows, "unresolved": [v for item in suspects if (v := string(item, 600))]}


def risk(n, message, quote="", priority="P0", origin="structure"):
    return {"chapter": n, "priority": priority, "problem": message, "quote": quote, "origin": origin}


def structural_review(original, candidate, ledger):
    results = []
    expected, actual = list(original), list(candidate)
    if actual != expected:
        return [risk(None, "合稿章节缺失、重复或顺序错乱。")]
    for n, c in candidate.items():
        body, title = c["content"], c["title"]
        explicit = re.match(r"^第\s*(\d+)\s*章", title)
        if not title.strip() or not body.strip() or (explicit and int(explicit[1]) != n):
            results.append(risk(n, "章名、章号或正文缺失。"))
        if "\ufffd" in body or "\ufffd" in title:
            results.append(risk(n, "正文或章名仍有编码损坏字符。", "\ufffd"))
        if re.search(r"(?m)^#{1,6}\s*(?:改稿记录|修订说明|修改清单)\s*$", body):
            results.append(risk(n, "正文疑似混入了改稿说明。"))
        if n - 1 in candidate:
            prior = candidate[n - 1]["content"].strip().splitlines()
            beginning = body.strip().splitlines()
            if prior and beginning and len(beginning[0].strip()) >= 25 and prior[-1].strip() == beginning[0].strip():
                results.append(risk(n, "上一章结尾与本章首段重复。", beginning[0].strip(), "P1"))
    for check in ledger["checks"]:
        targets = check["chapters"] or expected
        for n in targets:
            if n in check["allowedChapters"]:
                continue
            present = check["text"] in candidate[n]["content"] or check["text"] in candidate[n]["title"]
            if present != (check["mode"] == "present"):
                label = "仍有被列为废弃的措辞" if present else "缺少台账要求保留的锚点"
                results.append(risk(n, label, check["text"], origin="ledger"))
    return results


def phrase_counts(chapters, phrases):
    return Counter({phrase: sum(c["content"].count(phrase) for c in chapters.values()) for phrase in phrases})
