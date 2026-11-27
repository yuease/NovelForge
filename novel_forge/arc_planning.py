"""Validation and prompt projections for rolling story arcs."""

import re
import unicodedata
from itertools import islice


CANON_LOCKS_MARKER = "[BOOK_PLAN_CANON_LOCKS]"
SKELETON_CODES: dict[str, str] = {
    "betrayal": "内部背叛",
    "deadline": "限时危机",
    "investigation": "调查解谜",
    "confrontation": "正面对决",
    "negotiation": "交易博弈",
    "showcase": "公开展示/打脸",
    "acquisition": "获取资源情报",
    "reward": "结算收益升级",
    "reveal": "秘密身份揭露",
    "pursuit": "追逃潜入",
    "relationship": "情感羁绊",
    "loss": "代价挫败",
    "scheme": "布局反转",
    "respite": "日常喘息",
    "expansion": "舞台势力扩张",
    "trial": "审讯对质",
}
_CHAPTER_TITLE = re.compile(r"^(?:#{1,6}\s*)?第\s*\d+\s*章[\s:：—-]*")
_CJK_RUN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")
_GENERIC_THEMES = {"主角", "反派", "人物", "故事", "冲突", "反转", "危机", "揭示", "调查", "对决",
                   "剧情推进", "主线推进", "危机升级", "关键线索", "发现线索", "揭开真相", "终局收束"}


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


def normalize_skeleton(raw) -> list[str]:
    result = []
    for value in raw if isinstance(raw, list) else ():
        code = _text(value)
        if code in SKELETON_CODES and (not result or code != result[-1]):
            result.append(code)
            if len(result) == 5:
                break
    return result


def skeleton_similarity(a, b) -> float:
    if not a or not b:
        return 0.0
    left, right = set(a), set(b)
    lcs = [0] * (len(b) + 1)
    for code in a:
        diagonal = 0
        for index, other in enumerate(b, 1):
            previous = lcs[index]
            lcs[index] = diagonal + 1 if code == other else max(lcs[index - 1], previous)
            diagonal = previous
    return 0.5 * len(left & right) / len(left | right) + 0.5 * lcs[-1] / max(len(a), len(b))


def derive_arc_themes(row, arc_plan=None) -> list[str]:
    raw = row.get("themes")
    if isinstance(raw, list) and raw and all(_text(value) for value in raw):
        return raw
    points = (entry.get("text") for key in ("usedBeats", "turningPoints")
              if isinstance(rows := row.get(key), list)
              for entry in rows if isinstance(entry, dict) and _text(entry.get("text")))
    title = (arc_plan or {}).get("arcTitle") or row.get("arcTitle")
    return list(dict.fromkeys(tag for value in (title, *islice(points, 2)) if (tag := _clip(value, 20))))[:2]


def _theme_tags(themes):
    for row in themes:
        for tag in row["tags"]:
            key = _key(tag)
            if len(key) >= 2 and key not in _GENERIC_THEMES:
                yield f"{row['range']}:{tag}", key


def _matches_theme(tag, context, width):
    if tag in context:
        return True
    grams = {run[index:index + width] for run in _CJK_RUN.findall(tag) for index in range(len(run) - width + 1)}
    return any(gram in context for gram in grams - _GENERIC_THEMES)


def theme_hits(themes, context_text, *, limit=4) -> list[str]:
    context = _key(_text(context_text))
    return list(islice((label for label, tag in _theme_tags(themes) if _matches_theme(tag, context, 2)), limit))


def find_theme_collisions(candidate_arc, themes) -> list[str]:
    title = _key(_text(candidate_arc.get("arcTitle")))
    chapters = [(row.get("chapterNumber", index), _key("｜".join(_text(row.get(field)) for field in ("title", "goal", "summary"))))
                for index, row in enumerate(candidate_arc.get("chapters", ()), 1)]
    collisions = []
    for label, tag in _theme_tags(themes):
        hits = [f"第{number}章" for number, context in chapters if _matches_theme(tag, context, 4)]
        if not hits and _matches_theme(tag, title, 4):
            hits.append("单元标题")
        collisions.extend(f"{hit}复用已解决主题 {label}" for hit in hits)
    return collisions if len(collisions) >= 2 else []


def arc_plan_soft_feedback(candidate, *, rhythm=None, themes=()):
    skeleton = candidate.get("skeleton")
    if rhythm and rhythm.get("repeatSkeletonActive") and skeleton:
        recent = set(rhythm["recent4Codes"])
        if (skeleton_similarity(skeleton, rhythm["bannedSkeleton"]) >= 0.6
                or (len(recent) < len(SKELETON_CODES) and not set(skeleton) - recent)):
            return "叙事骨架与近期单元重复，请切换功能顺序并采用近期未用过的功能代码。"
    if collisions := find_theme_collisions(candidate, themes):
        return "；".join(collisions[:4]) + "。请改用新议题，旧主题只承接后果。"
    return ""


def legacy_outline_placeholder(entry, number):
    return (entry.get("title") == f"第{number}章 危机递进"
            and entry.get("summary") == f"第{number}章承接前文规则线索展开冲突并留下章末悬念。"
            and entry.get("keyEvents") == [f"第{number}章关键事件"])


def _title_key(title):
    return _key(_CHAPTER_TITLE.sub("", _text(title)))


def validate_arc_plan(raw, *, arc_index, start=None, total=None, recent_titles=(),
                      start_chapter=None, total_chapters=None, allowed_lengths=None, rhythm=None, themes=(), soft=True):
    start = start if start_chapter is None else start_chapter
    total = total if total_chapters is None else total_chapters
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
    if allowed_lengths is not None and (not isinstance(chapters, list) or len(chapters) not in allowed_lengths):
        raise ValueError(f"从第 {start} 章起只允许以下章数：{'、'.join(map(str, allowed_lengths))}。")
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
    arc = {"arcIndex": arc_index, "startChapter": start, "endChapter": normalized[-1]["chapterNumber"],
           "arcTitle": title, "arcGoal": goal, "villainChips": villains}
    if skeleton := normalize_skeleton(raw.get("skeleton")):
        arc["skeleton"] = skeleton
    if soft and (rhythm or themes):
        if feedback := arc_plan_soft_feedback({**arc, "chapters": normalized}, rhythm=rhythm, themes=themes):
            raise ValueError(feedback)
    return arc, normalized


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


def compact_arc_summaries(archives, limit=2, *, include_beats=True):
    return [
        {"startChapter": row["startChapter"], "endChapter": row["endChapter"], "mainline": row.get("mainline", ""),
         **({"volumeProgress": row["volumeProgress"]} if "volumeProgress" in row else {}),
         "turningPoints": [{"chapter": point.get("chapter"), "text": point.get("text")}
                           for point in (row.get("turningPoints") or [])[:2] if isinstance(point, dict)],
         **({"usedBeats": compact_used_beats([row])} if include_beats else {}),
         "usedHooks": [{key: hook[key] for key in ("chapter", "text") if key in hook}
                       for hook in (row.get("usedHooks") or []) if isinstance(hook, dict)],
         "villainStatus": row.get("villainStatus", ""),
         **({"needsReviewChapters": row["needsReviewChapters"]} if row.get("needsReviewChapters") else {})}
        for row in archives[:limit]
    ]


def _chapter_range(row):
    return row["startChapter"], row["endChapter"]


def horizon_memory(facts, arc_plans, start_chapter, *, near=2, capsule_limit=12, volume_limit=20, theme_limit=80):
    all_arcs = prior_arc_summaries(facts, start_chapter)
    near_arcs = all_arcs[:near]
    near_ids = {_chapter_range(row) for row in near_arcs}
    plan_by_arc = {row["arcIndex"]: row for row in saved_arc_plans({"arcPlans": list(arc_plans)})
                   if row["endChapter"] < start_chapter}
    volumes = prior_volume_summaries(facts, start_chapter - 23)
    v_end = volumes[0]["endChapter"] if volumes else 0
    middle = [row for row in all_arcs if _chapter_range(row) not in near_ids and row["endChapter"] > v_end][:capsule_limit]
    occupied = [_chapter_range(row) for row in (*near_arcs, *middle)]
    # Keep complete arc records when an independent ten-chapter archive straddles their boundary.
    volumes = [row for row in volumes if not any(start <= row["endChapter"] and row["startChapter"] <= end
                                               for start, end in occupied)][:volume_limit]
    compact_volumes = [
        {"volume": row.get("volume", row["endChapter"] // 10),
         "range": f"{row['startChapter']}-{row['endChapter']}",
         "mainline": _clip(row.get("mainline"), 160 if index < 3 else 60),
         **({"turningPoints": [{"chapter": point.get("chapter"), "text": _clip(point.get("text"), 90)}
                              for point in (row.get("turningPoints") or [])[:2] if isinstance(point, dict)]}
            if index < 3 else {})}
        for index, row in enumerate(volumes)
    ]
    capsules = []
    for row in middle:
        plan = plan_by_arc.get(row.get("arc"))
        progress = row.get("volumeProgress")
        escalation = _clip(progress.get("escalation"), 40) if isinstance(progress, dict) else ""
        capsules.append({
            "range": f"{row['startChapter']}-{row['endChapter']}",
            "arcTitle": _clip((plan or {}).get("arcTitle") or row.get("arcTitle"), 80),
            "mainline": _clip(row.get("mainline"), 90),
            "themes": derive_arc_themes(row, plan),
            "shape": _clip(row.get("shape"), 40),
            **({"escalation": escalation} if escalation else {}),
        })
    themes, seen = [], set()
    for row in all_arcs:
        if len(themes) >= theme_limit:
            break
        tags = []
        for tag in derive_arc_themes(row, plan_by_arc.get(row.get("arc"))):
            if (key := _key(tag)) and key not in seen:
                seen.add(key)
                tags.append(tag)
        if tags:
            themes.append({"range": f"{row['startChapter']}-{row['endChapter']}", "tags": tags})
    return {"near": near_arcs, "capsules": capsules, "volumes": compact_volumes, "themes": themes,
            "recentBeats": compact_used_beats(near_arcs, limit=12)}


def prior_volume_summaries(facts, before):
    rows = facts.get("volumeSummaries")
    return sorted((row for row in (rows if isinstance(rows, list) else []) if isinstance(row, dict)
                   and type(row.get("startChapter")) is int and type(row.get("endChapter")) is int
                   and 0 < row["startChapter"] <= row["endChapter"] < before),
                  key=lambda row: row["endChapter"], reverse=True)


def arc_rhythm(arc_plans, arc_rows, *, start_chapter, total_chapters):
    minimum, maximum = arc_chapter_span(start_chapter, total_chapters)
    terminal = total_chapters - start_chapter + 1 <= 6
    archives = prior_arc_summaries({"arcSummaries": list(arc_rows)}, start_chapter)
    plans = prior_arc_summaries({"arcSummaries": list(arc_plans)}, start_chapter)
    ranges = sorted({_chapter_range(row) for row in (*archives, *plans)}, key=lambda span: (span[1], span[0]))[-6:]
    lengths = [end - start + 1 for start, end in ranges]
    allowed = list(range(minimum, maximum + 1))
    if not terminal and len(allowed) > 1:
        if len(lengths) >= 3 and lengths[-3:] == [lengths[-1]] * 3:
            allowed = [length for length in allowed if length != lengths[-1]] or allowed
        if len(lengths) == 6 and sum(length <= 3 for length in lengths) >= 5:
            allowed = [length for length in allowed if length >= 4] or allowed

    skeletons = [normalize_skeleton(row.get("skeleton")) for row in archives[:6]]
    counts = {code: sum(code in skeleton for skeleton in skeletons) for code in SKELETON_CODES}
    counts = {code: count for code, count in counts.items() if count}
    cooldown = [code for code, count in counts.items() if count >= 4]
    repeated = len(skeletons) >= 2 and skeleton_similarity(*skeletons[:2]) >= 0.6
    banned = skeletons[0] if repeated else []
    recent_codes = [code for code in SKELETON_CODES if any(code in skeleton for skeleton in skeletons[:4])]
    directive = f"近期单元章数：{lengths}；可选章数：{allowed}。"
    if cooldown:
        directive += f"降频代码：{'、'.join(cooldown)}。"
    if repeated:
        directive += f"近期骨架重复（{' → '.join(banned)}），调整叙事结构并引入近四单元未用代码。"
    return {"recentLengths": lengths, "allowedLengths": allowed, "codeCounts": counts, "cooldownCodes": cooldown,
            "repeatSkeletonActive": repeated, "bannedSkeleton": banned, "recent4Codes": recent_codes,
            "directive": directive}
