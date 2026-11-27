"""Validation and prompt projections for BookPlan V2."""

import re

from .text_evidence import locate_quote, normalize_quote


PLAN_VERSION_V2 = 2
PLAN_V2_MARKER = "[BOOK_PLAN_V2]"
CANON_LOCKS_V2_BUDGET = 900
_SCALAR_LOCKS = {"powerScale": (100, "力量"), "currencyAndEra": (120, "时代与货币")}
_LIST_LOCKS = {"worldTaboos": (4, 65, "禁区"), "keyAssets": (6, 65, "道具")}
_STRUCTURED_LOCKS = {"identityAliases": 6, "boundaries": 4, "assetSets": 3}
_COMMITTED = {"completed", "verified", "repaired", "needs_review"}
_REF_PART = re.compile(r"([a-zA-Z][a-zA-Z0-9]*)(?:\[(0|[1-9][0-9]?)\])?")


def _record(value):
    return value if isinstance(value, dict) else {}


def _list(value):
    return value if isinstance(value, list) else []


def _text(value):
    return " ".join(value.split()) if isinstance(value, str) else ""


def is_v2_book_plan(plan) -> bool:
    return isinstance(plan, dict) and plan.get("planVersion") == PLAN_VERSION_V2


class BookPlanValidationError(ValueError):
    def __init__(self, field_errors: list[str]):
        self.field_errors = field_errors
        super().__init__("；".join(field_errors))


def _validate_locks(raw, errors):
    def string(value, path, limit):
        if not _text(value) or len(value) > limit:
            errors.append(f"{path}: 必须是非空字符串且不超过{limit}字")
        return _text(value)

    def array(value, path, limit, minimum=0):
        if not isinstance(value, list) or not minimum <= len(value) <= limit:
            errors.append(f"{path}: 必须是包含{minimum}–{limit}项的数组")
        return _list(value)

    if not isinstance(raw, dict):
        errors.append("canonLocks: 必须是对象")
        return {}
    locks = dict(raw)
    for key, (limit, _) in _SCALAR_LOCKS.items():
        if key in raw:
            locks[key] = string(raw[key], f"canonLocks.{key}", limit)
    for key, (count, limit, _) in _LIST_LOCKS.items():
        if key in raw:
            path = f"canonLocks.{key}"
            locks[key] = [string(value, f"{path}[{i}]", limit)
                          for i, value in enumerate(array(raw[key], path, count))]

    aliases = set()
    for key, count in _STRUCTURED_LOCKS.items():
        if key not in raw:
            continue
        path = f"canonLocks.{key}"
        locks[key] = rows = []
        for i, entry in enumerate(array(raw[key], path, count)):
            ref = f"{path}[{i}]"
            if not isinstance(entry, dict):
                errors.append(f"{ref}: 必须是对象")
                continue
            row = dict(entry)
            if key == "identityAliases":
                for name, limit in (("character", 24), ("scope", 16), ("alias", 24)):
                    row[name] = string(entry.get(name), f"{ref}.{name}", limit)
                pair = row["character"], row["scope"]
                if pair in aliases:
                    errors.append(f"{ref}: character 与 scope 组合重复或对应多个 alias")
                aliases.add(pair)
            elif key == "boundaries":
                row["subject"] = string(entry.get("subject"), f"{ref}.subject", 20)
                row["rule"] = string(entry.get("rule"), f"{ref}.rule", 60)
                if "changeWhen" not in entry:
                    errors.append(f"{ref}.changeWhen: 必须给出变化条件或 null")
                elif entry["changeWhen"] is not None:
                    row["changeWhen"] = string(entry["changeWhen"], f"{ref}.changeWhen", 48)
            else:
                row["name"] = string(entry.get("name"), f"{ref}.name", 24)
                members = array(entry.get("members"), f"{ref}.members", 10, minimum=1)
                row["members"] = normalized = []
                for j, value in enumerate(members):
                    member = string(value, f"{ref}.members[{j}]", 20)
                    if member in normalized:
                        errors.append(f"{ref}.members[{j}]: 成员重复")
                    normalized.append(member)
            rows.append(row)
    return locks


def _normalize_escalation(raw):
    if not isinstance(raw, dict):
        return None
    tactics = {}
    for entry in _list(raw.get("retiredTactics")):
        row = _record(entry)
        tactic = _text(row.get("tactic"))[:32]
        why = _text(row.get("whyFails"))[:40]
        if tactic and why:
            tactics.setdefault(tactic, {**row, "tactic": tactic, "whyFails": why})
        if len(tactics) == 3:
            break
    return {**raw, "pressure": _text(raw.get("pressure"))[:64], "costAtRisk": _text(raw.get("costAtRisk"))[:64],
            "retiredTactics": list(tactics.values()), "stageTransition": _text(raw.get("stageTransition"))[:60] or None}


def validate_book_plan(raw, *, total_chapters, rolling_arc_outlines=False, strict_ladder=None) -> tuple[dict, list[str], list[str]]:
    """Validate chapter coverage and canon before accepting a total plan in either outline mode."""
    if not isinstance(raw, dict) or not raw:
        raise BookPlanValidationError(["bookPlan: 必须返回非空的完整 JSON 对象"])
    errors, warnings, ladder_issues = [], [], []
    world = _record(raw.get("worldview"))
    title, logline, setting = map(_text, (raw.get("title"), raw.get("logline"), world.get("setting")))
    for path, value in (("title", title), ("logline", logline), ("worldview.setting", setting)):
        if not value:
            errors.append(f"{path}: 必须是非空字符串")
    cast = [{**row, "name": _text(row["name"])} for row in _list(raw.get("mainCharacters"))
            if isinstance(row, dict) and _text(row.get("name"))]
    if not cast:
        errors.append("mainCharacters: 必须是数组且至少一项含非空 name")
    raw_volumes = _list(raw.get("volumes"))
    if not 1 <= len(raw_volumes) <= 24:
        errors.append("volumes: 必须是包含1–24卷的数组")
    volumes, expected_start = [], 1
    for i, entry in enumerate(raw_volumes):
        path = f"volumes[{i}]"
        if not isinstance(entry, dict):
            errors.append(f"{path}: 必须是对象")
            expected_start = None
            continue
        number, span = entry.get("volume"), entry.get("chapters")
        if type(number) is not int or number != i + 1:
            errors.append(f"{path}.volume: 必须是整数{i + 1}")
        if not isinstance(span, list) or len(span) != 2:
            errors.append(f"{path}.chapters: 必须是两个整数组成的数组")
            expected_start = None
        elif any(type(n) is not int for n in span):
            errors.extend(f"{path}.chapters[{j}]: 必须是整数章号" for j, n in enumerate(span) if type(n) is not int)
            expected_start = None
        else:
            start, end = span
            if expected_start is not None and start != expected_start:
                errors.append(f"{path}.chapters[0]: 应从第{expected_start}章开始，保证卷间连续")
            if not 1 <= start <= end <= total_chapters or (i == len(raw_volumes) - 1 and end != total_chapters):
                errors.append(f"{path}.chapters[1]: 须不早于起始章，且各卷连续覆盖第1–{total_chapters}章")
            expected_start = end + 1
        volume = {**entry, "title": _text(entry.get("title"))[:80] or f"第{i + 1}卷",
                  "summary": _text(entry.get("summary"))[:240]}
        if (escalation := _normalize_escalation(entry.get("escalation"))) is not None:
            volume["escalation"] = escalation
        else:
            volume.pop("escalation", None)
        volumes.append(volume)

    locks = _validate_locks(raw["canonLocks"], errors) if "canonLocks" in raw else {}
    if not errors and len(canon_locks_text(locks)) > CANON_LOCKS_V2_BUDGET:
        errors.append(f"canonLocks: 渲染超过{CANON_LOCKS_V2_BUDGET}字，请压缩措辞并完整保留锁与集合成员")
    if errors:
        raise BookPlanValidationError(errors)

    if strict_ladder is None:
        strict_ladder = total_chapters >= 30
    if strict_ladder:
        previous = None
        for i, volume in enumerate(volumes):
            path = f"volumes[{i}].escalation"
            ladder = volume.get("escalation", {})
            for key in ("pressure", "costAtRisk", *(("retiredTactics",) if i else ())):
                if not ladder.get(key):
                    ladder_issues.append(f"{path}.{key}: 第{i + 1}卷缺少有效内容")
            pair = ladder.get("pressure"), ladder.get("costAtRisk")
            if all(pair) and pair == previous:
                ladder_issues.append(f"{path}.pressure / costAtRisk: 第{i + 1}卷与上一卷相同，须升级压迫和代价")
            previous = pair

    plan = {**raw, "planVersion": PLAN_VERSION_V2, "title": title, "logline": logline[:120],
            "worldview": {**world, "setting": setting[:220],
                          "coreRules": [rule[:120] for value in _list(world.get("coreRules")) if (rule := _text(value))][:8]},
            "mainCharacters": cast[:12], "volumes": volumes, "foreshadowingPlan": _list(raw.get("foreshadowingPlan"))[:16]}
    if "canonLocks" in raw:
        plan["canonLocks"] = locks
    return plan, warnings, ladder_issues


def canon_locks_text(locks, *, boundary_state=()) -> str:
    locks = _record(locks)
    lines = []
    for key, (_, label) in _SCALAR_LOCKS.items():
        if value := _text(locks.get(key)):
            lines.append(f"{label}：{value}")
    for key, (_, _, label) in _LIST_LOCKS.items():
        if values := [_text(value) for value in _list(locks.get(key)) if _text(value)]:
            lines.append(f"{label}：" + "；".join(f"[{i}]{value}" for i, value in enumerate(values)))
    for i, row in enumerate(_list(locks.get("identityAliases"))):
        lines.append(f"别名[{i}]：{row['character']}在{row['scope']}称「{row['alias']}」")
    for i, row in enumerate(_list(locks.get("assetSets"))):
        lines.append(f"集合[{i}]：{row['name']}（全员：{'、'.join(row['members'])}）")
    updates = {row["planRef"]: row for row in boundary_state}
    for i, row in enumerate(_list(locks.get("boundaries"))):
        line = f"边界[{i}]：{row['subject']}，{row['rule']}"
        if row["changeWhen"] is not None:
            line += f"；变更条件：{row['changeWhen']}"
            if update := updates.get(f"canonLocks.boundaries[{i}]"):
                line += f"（第{update['chapter']}章起已变更：{update['result']}）"
        lines.append(line)
    return "不可变核心口径锁（初始值仅为历史基线）：\n" + "\n".join(lines) if lines else ""


def volumes_for_range(plan, start_chapter, end_chapter) -> list[dict]:
    if not is_v2_book_plan(plan) or start_chapter > end_chapter:
        return []
    return [row for row in _list(plan.get("volumes")) if isinstance(row, dict)
            and len(span := _list(row.get("chapters"))) == 2 and all(type(n) is int for n in span)
            and span[0] <= end_chapter and start_chapter <= span[1]]


def volume_for_chapter(plan, chapter_number) -> dict | None:
    return next(iter(volumes_for_range(plan, chapter_number, chapter_number)), None)


def _volume_heading(volume):
    start, end = volume["chapters"]
    return f"第{volume['volume']}卷（{start}–{end}章）"


def _abbreviate(text, limit):
    return text if len(text) <= limit else text[:limit - 1] + "…"


def volume_card_text(plan, chapter_number, *, limit=300) -> str:
    volume = volume_for_chapter(plan, chapter_number)
    if not volume:
        return ""
    heading = f"【本卷升级阶梯 · {_volume_heading(volume)}】"
    ladder = _record(volume.get("escalation"))
    tactics = _list(ladder.get("retiredTactics"))
    stakes = [f"{label}：{ladder[key]}" for key, label in
              (("pressure", "新增压迫"), ("costAtRisk", "真实代价")) if ladder.get(key)]
    clauses = [heading]
    if transition := ladder.get("stageTransition"):
        clauses.append(f"跃迁条件：{transition}")
    if tactics:
        clauses.append("退役手段：" + "；".join(row["tactic"] for row in tactics))
    room = limit - len("\n".join(clauses))
    if room < 0:
        return ""
    for i, detail in enumerate(stakes):
        budget = room // (len(stakes) - i) - 1
        if budget > 5:
            clause = _abbreviate(detail, budget)
            clauses.append(clause)
            room -= len(clause) + 1
    for i, tactic in enumerate(tactics, 1):
        why = f"手段{i}失效：{tactic['whyFails']}"
        if len(why) < room:
            clauses.append(why)
            room -= len(why) + 1
    return "\n".join(clauses) if len(clauses) > 1 else ""


def volume_ladder_text(plan, *, limit=1600) -> str:
    if not is_v2_book_plan(plan) or limit <= 0:
        return ""
    volumes = _list(plan.get("volumes"))
    if not volumes:
        return ""
    per_volume = (limit + 1) // len(volumes) - 1
    lines = []
    for volume in volumes:
        heading = _volume_heading(volume)
        if len(heading) > per_volume:
            continue
        room = per_volume - len(heading) - 1
        ladder = _record(volume.get("escalation"))
        retired = "、".join(row["tactic"] for row in _list(ladder.get("retiredTactics")))
        details = [(label, value) for label, value in (
            ("压迫", ladder.get("pressure")), ("代价", ladder.get("costAtRisk")),
            ("退役", retired), ("跃迁", ladder.get("stageTransition")),
        ) if value]
        if details and room >= 5 * len(details) - 1:
            share = (room + 1) // len(details) - 4
            heading += "：" + "；".join(f"{label} {_abbreviate(value, share)}" for label, value in details)
        elif room > 1 and volume.get("summary"):
            heading += "：" + _abbreviate(volume["summary"], room)
        lines.append(heading)
    return "\n".join(lines)


def valid_plan_ref(plan, ref) -> bool:
    """Accept references to real canon clauses or volume entries and their fields."""
    if not is_v2_book_plan(plan) or not isinstance(ref, str):
        return False
    parts = ref.split(".")
    if not (2 <= len(parts) <= 5 or len(parts) == 1 and ref.startswith("volumes[")):
        return False
    if parts[0] == "canonLocks":
        field = parts[1].split("[", 1)[0]
        if field not in (*_SCALAR_LOCKS, *_LIST_LOCKS, *_STRUCTURED_LOCKS):
            return False
    elif not parts[0].startswith("volumes["):
        return False
    value = plan
    for part in parts:
        match = _REF_PART.fullmatch(part)
        if not match:
            return False
        key, index = match.groups()
        value = _record(value).get(key)
        if index is not None:
            items = _list(value)
            if int(index) >= len(items):
                return False
            value = items[int(index)]
    return value is not None and value != "" and value != []


def verified_boundary_changes(changes, *, book_plan, chapter_text) -> list[dict]:
    if not is_v2_book_plan(book_plan) or not changes:
        return []
    boundaries = _list(_record(book_plan.get("canonLocks")).get("boundaries"))
    refs = {f"canonLocks.boundaries[{i}]" for i, row in enumerate(boundaries) if _text(_record(row).get("changeWhen"))}
    source = normalize_quote(chapter_text)
    accepted = {}
    for row in _list(changes):
        entry = _record(row)
        ref, result, quote = (entry.get(key) for key in ("planRef", "result", "quote"))
        if (isinstance(ref, str) and ref in refs and _text(result) and len(result) <= 48
                and isinstance(quote, str) and 4 <= len(quote.strip()) <= 120 and locate_quote(quote, source)):
            accepted[ref] = {"planRef": ref, "result": _text(result), "quote": quote.strip()}
    return list(accepted.values())


def collect_boundary_state(chapters, *, before_chapter=None, book_plan=None) -> list[dict]:
    if not is_v2_book_plan(book_plan) or not _record(book_plan.get("canonLocks")).get("boundaries"):
        return []
    state = {}
    past = sorted((row for row in chapters if row.get("status") in _COMMITTED
                   and row.get("status") != "needs_review" and type(row.get("chapterNumber")) is int
                   and (before_chapter is None or row["chapterNumber"] < before_chapter)),
                  key=lambda row: row["chapterNumber"])
    for row in past:
        verification = _record(row.get("verification"))
        if verification.get("passed") is True:
            changes = verified_boundary_changes(verification.get("boundaryChanges"), book_plan=book_plan,
                                                chapter_text=row.get("content") or "")
            for change in changes:
                state[change["planRef"]] = {**change, "chapter": row["chapterNumber"]}
    return list(state.values())
