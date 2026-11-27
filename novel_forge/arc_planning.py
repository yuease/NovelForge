"""Validation and prompt projections for rolling story arcs."""

import re
import unicodedata


CANON_LOCKS_MARKER = "[BOOK_PLAN_CANON_LOCKS]"
_CHAPTER_TITLE = re.compile(r"^(?:#{1,6}\s*)?第\s*\d+\s*章[\s:：—-]*")


_EXIT_PLANS = {
    "retain": "retain",
    "keep": "retain",
    "保留": "retain",
    "留存": "retain",
    "继续": "retain",
    "exit": "exit",
    "退场": "exit",
    "下线": "exit",
    "败亡": "exit",
    "清除": "exit",
    "background": "background",
    "转幕后": "background",
    "幕后": "background",
    "潜伏": "background",
}


def _text(value):
    return " ".join(value.split()) if isinstance(value, str) else ""


def _clip(value, limit):
    text = _text(value)
    if len(text) <= limit:
        return text
    return text[:limit].rstrip("，,、；;：:（(")


def _key(value):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value)).casefold().rstrip("。.!！")


def normalize_character_name(value):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", _text(value))).casefold()


def character_name_registry(book_plan, facts, *, before_chapter=None):
    """Group known identities by full name, keeping deliberately repeated cast names."""
    plan = book_plan if isinstance(book_plan, dict) else {}
    facts = facts if isinstance(facts, dict) else {}
    result = {}
    cast = plan.get("mainCharacters")
    for row in cast if isinstance(cast, list) else ():
        if isinstance(row, dict) and (key := normalize_character_name(row.get("name"))):
            result.setdefault(key, []).append(row)
    stored = facts.get("characters")
    for name, value in stored.items() if isinstance(stored, dict) else ():
        row = value if isinstance(value, dict) else {}
        source = row.get("sourceChapter")
        if (before_chapter is not None and type(source) is int and source >= before_chapter):
            continue
        label = row.get("name") or name
        if (key := normalize_character_name(label)) and key not in result:
            result[key] = [{**row, "name": label}]
    return result


def normalize_canon_locks(raw):
    if not isinstance(raw, dict):
        return None
    remaining, seen, result = 420, set(), {}

    def take(value, limit):
        nonlocal remaining
        text = _text(value)
        limit = min(limit, remaining)
        if len(text) > limit:
            end = max((stop.end() for stop in re.finditer(r"[。！？!?][”’」』]?", text) if stop.end() <= limit),
                      default=0)
            text = text[:end]
        identity = _key(text)
        if not identity or identity in seen:
            return None
        seen.add(identity)
        remaining -= len(text)
        return text

    for name in ("powerScale", "currencyAndEra"):
        if text := take(raw.get(name), 100):
            result[name] = text
    for name, count, limit in (("worldTaboos", 3, 45), ("keyAssets", 4, 50)):
        values = raw.get(name)
        if not isinstance(values, list):
            continue
        entries = []
        for value in values:
            if text := take(value, limit):
                entries.append(text)
                if len(entries) == count:
                    break
        if entries:
            result[name] = entries
    return result or None


def arc_chapter_span(start, total):
    remaining = total - start + 1
    if not 1 <= start <= total:
        raise ValueError("起始章超出全书范围。")
    return (remaining, remaining) if remaining <= 6 else (3, min(6, remaining - 3))


def legacy_outline_placeholder(entry, number):
    return (entry.get("title") == f"第{number}章 危机递进"
            and entry.get("summary") == f"第{number}章承接前文规则线索展开冲突并留下章末悬念。"
            and entry.get("keyEvents") == [f"第{number}章关键事件"])


def _title_key(title):
    return _key(_CHAPTER_TITLE.sub("", _text(title)))


def validate_arc_plan(raw, *, arc_index, start, total, recent_titles=()):
    if not isinstance(raw, dict):
        raise ValueError("需要包含单元元信息和 chapters 的完整 JSON 对象。")
    title, goal = _clip(raw.get("arcTitle"), 80), _clip(raw.get("arcGoal"), 240)
    if not title or not goal:
        raise ValueError("arcTitle 和 arcGoal 必须非空，分别不超过 80 字和 240 字。")
    chips = raw.get("villainChips")
    if chips is None:
        chips = []
    if not isinstance(chips, list):
        raise ValueError("villainChips 应为数组，无反派时用空数组 []。")
    villains = []
    for row in chips[:4]:
        if not isinstance(row, dict):
            raise ValueError("villainChips 条目须为包含 villain、newChips、exitPlan 的对象。")
        name = _clip(row.get("villain"), 50)
        if not name:
            raise ValueError("villainChips 条目缺少非空 villain 名称。")
        raw_new = row.get("newChips")
        if raw_new is None:
            raw_new = []
        elif isinstance(raw_new, str):
            raw_new = [raw_new]
        if not isinstance(raw_new, list):
            raise ValueError(f"villainChips 条目「{name}」的 newChips 须为字符串数组。")
        new_chips = [_clip(value, 160) for value in raw_new if _text(value)][:3]
        raw_exit = _text(row.get("exitPlan"))
        exit_plan = _EXIT_PLANS.get(raw_exit.lower())
        if exit_plan is None:
            raise ValueError(f"villainChips 条目「{name}」的 exitPlan={raw_exit!r} 无效，须为 retain、exit 或 background。")
        villains.append({"villain": name, "newChips": new_chips, "exitPlan": exit_plan})

    minimum, maximum = arc_chapter_span(start, total)
    chapters = raw.get("chapters")
    if not isinstance(chapters, list) or not minimum <= len(chapters) <= maximum:
        raise ValueError(f"从第 {start} 章起必须返回 {minimum}–{maximum} 章。")
    seen = {_title_key(value) for value in recent_titles}
    normalized = []
    for number, entry in enumerate(chapters, start):
        if not isinstance(entry, dict) or type(entry.get("chapterNumber")) is not int or entry["chapterNumber"] != number:
            raise ValueError(f"chapters 必须按章号连续排列，此处应为整数章号 {number}。")
        chapter = {"chapterNumber": number, **{field: _text(entry.get(field)) for field in
                                               ("title", "goal", "summary", "hook")}}
        key = _title_key(chapter["title"])
        events = entry.get("keyEvents")
        if (not key or not chapter["goal"] or not chapter["summary"]
                or not isinstance(events, list) or not events or any(not _text(event) for event in events)
                or not isinstance(entry.get("hook"), str) or number < total and not chapter["hook"]
                or legacy_outline_placeholder(entry, number)):
            raise ValueError(f"第 {number} 章需要有效标题、剧情目标、摘要、关键事件和章末收束，不能使用占位章纲。")
        if key in seen:
            raise ValueError(f"第 {number} 章标题「{chapter['title']}」与单元内或近期章标题重复。")
        seen.add(key)
        normalized.append({**chapter, "keyEvents": [_text(event) for event in events]})
    return {"arcIndex": arc_index, "startChapter": start, "endChapter": normalized[-1]["chapterNumber"],
            "arcTitle": title, "arcGoal": goal, "villainChips": villains}, normalized


def prior_arc_summaries(facts, before):
    rows = facts.get("arcSummaries")
    return sorted((row for row in (rows if isinstance(rows, list) else [])
                   if isinstance(row, dict) and type(row.get("startChapter")) is int
                   and type(row.get("endChapter")) is int and 0 < row["startChapter"] <= row["endChapter"] < before),
                  key=lambda row: row["endChapter"], reverse=True)


def saved_arc_plans(job):
    rows = job.get("arcPlans")
    if not isinstance(rows, list):
        return []
    return sorted((row for row in rows if isinstance(row, dict)
                   and all(type(row.get(key)) is int for key in ("arcIndex", "startChapter", "endChapter"))
                   and 0 < row["arcIndex"] and 0 < row["startChapter"] <= row["endChapter"]),
                  key=lambda row: row["startChapter"])


def compact_used_beats(archives, limit=None):
    seen, result = set(), []
    for row in archives:
        beats = row.get("usedBeats")
        if not isinstance(beats, list):
            continue
        for entry in reversed(beats):
            if not isinstance(entry, dict) or type(entry.get("chapter")) is not int:
                continue
            text = _text(entry.get("text"))
            key = _key(text)
            if not key or key in seen:
                continue
            seen.add(key)
            result.append({"chapter": entry["chapter"], "text": text})
            if limit is not None and len(result) == limit:
                return result
    return result


def compact_arc_summaries(archives):
    return [
        {"startChapter": row["startChapter"], "endChapter": row["endChapter"], "mainline": row.get("mainline", ""),
         **({"volumeProgress": row["volumeProgress"]} if "volumeProgress" in row else {}),
         "turningPoints": [{"chapter": point.get("chapter"), "text": point.get("text")}
                           for point in (row.get("turningPoints") or [])[:2] if isinstance(point, dict)],
         "usedBeats": compact_used_beats([row]),
         "usedHooks": [{key: hook[key] for key in ("chapter", "text") if key in hook}
                       for hook in (row.get("usedHooks") or []) if isinstance(hook, dict)],
         "villainStatus": row.get("villainStatus", ""),
         **({"needsReviewChapters": row["needsReviewChapters"]} if row.get("needsReviewChapters") else {})}
        for row in archives[:2]
    ]
