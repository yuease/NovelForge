"""Read-time entity and subplot projections for chapter and arc prompts."""

import math
import re

from .arc_planning import prior_arc_summaries


_ITEM_EXIT = re.compile(r"已消耗|耗尽|用尽|损毁|销毁|毁坏|售出|卖出|交付|遗失|丢失|废弃|\b(?:consumed|destroyed|discarded|lost)\b", re.I)
_CHARACTER_EXIT = re.compile(r"死亡|身亡|身故|阵亡|伏诛|退场|离世|失踪已久|\b(?:dead|deceased|exited)\b", re.I)
_CLOSED = {"paid_off", "resolved", "dropped", "fulfilled", "broken", "expired", "retired", "closed"}
_CHAPTER_FIELDS = ("sourceChapter", "updatedAtChapter", "lastSeenChapter")


def _dict(value):
    return value if isinstance(value, dict) else {}


def _rows(value):
    return [row for row in value if isinstance(row, dict)] if isinstance(value, (list, tuple)) else []


def _chapter(value):
    if isinstance(value, str) and len(value) <= 18 and value.isdecimal():
        value = int(value)
    return value if type(value) is int and value > 0 else 0


def _updated(row, *fields):
    return max((_chapter(row.get(field)) for field in (*_CHAPTER_FIELDS, *fields)), default=0)


def _state_updated(row):
    return max(
        _updated(row, "stanceTierChapter"),
        max((_chapter(chapter) for chapter in _dict(row.get("knownFactChapters")).values()), default=0),
        max((_updated(_dict(link)) for field in ("relationships", "storyRelationships")
             for link in _dict(row.get(field)).values()), default=0),
    )


def _history(facts, before):
    return sorted(
        ({**row, "chapter": number} for row in _rows(facts.get("factHistory"))
         if (number := _chapter(row.get("chapter"))) and (before is None or number < before)),
        key=lambda row: row["chapter"],
    )


def _named(value):
    if isinstance(value, dict):
        return value.items()
    return ((row.get("name"), row) for row in _rows(value))


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)


def _mentions(term, context):
    if not isinstance(term, str) or not term.strip():
        return False
    if term.isascii():
        return bool(re.search(rf"(?<!\w){re.escape(term)}(?!\w)", context, re.I | re.ASCII))
    return term in context


def _open_records(facts, before, history, kind, text_key):
    journal = {}

    def remember(table, row):
        key = row.get("id") or row.get(text_key)
        if isinstance(key, str) and key:
            table[key] = table.get(key, {}) | row

    for history_row in history:
        delta = _dict(history_row.get("delta"))
        if kind in ("commitments", "constraints"):
            delta = _dict(delta.get("memory"))
        for row in _rows(delta.get(kind)):
            remember(journal, row)

    records = {}
    for row in _rows(facts.get(kind, facts.get("openForeshadowing") if kind == "foreshadowing" else None)):
        key = row.get("id") or row.get(text_key)
        if before is not None and _updated(row, "plantedChapter") >= before:
            continue
        if kind == "foreshadowing" and before is not None and (
            row.get("status") == "paid_off" and _chapter(row.get("payoffChapter")) >= before
            or any(_chapter(number) >= before for number in row.get("relatedChapters") or ())
        ):
            if isinstance(key, str) and key in journal:
                continue
            # A legacy clue can retain its planting text when the update history is missing.
            row = {field: row[field] for field in ("id", "clue") if field in row} | {"status": "planted"}
        remember(records, row)
    if facts.get(kind) is not None:
        # Canonical records carry generated IDs even when the original journal only had text.
        by_text = {row[text_key] for row in records.values() if isinstance(row.get(text_key), str)}
        journal = {key: row for key, row in journal.items() if key in records or
                   not isinstance(row.get(text_key), str) or row[text_key] not in by_text}
    return [row for row in (journal | records).values() if row.get("status") not in _CLOSED]


def entity_index(facts, *, before_chapter=None) -> dict:
    """Index distinct chapter appearances and the last usable version of each entity."""
    facts = _dict(facts)
    index = {"characters": {}, "items": {}, "beforeChapter": before_chapter}
    history = _history(facts, before_chapter)

    def add(kind, name, *, entry=None, state=None, chapters=()):
        row = index[kind].setdefault(name, {"chapters": set(), "entry": {}, "state": {}})
        row["chapters"].update(n for n in chapters if n)
        if entry is not None:
            row["entry"].update(entry)
        if state is not None:
            row["state"].update(state)

    snapshots = {}
    for kind in ("characters", "items", "characterStates"):
        snapshots[kind] = {
            name: _dict(value) if isinstance(value, dict) else {"value": value}
            for name, value in _named(facts.get(kind)) if isinstance(name, str) and name
        }

    ledger_subjects = set()
    for row in history:
        number, delta = row["chapter"], _dict(row.get("delta"))
        for kind in ("characters", "items"):
            for name, entry in _named(delta.get(kind)):
                if isinstance(name, str) and name:
                    fields = {key: value for key, value in _dict(entry).items() if key not in _CHAPTER_FIELDS}
                    add(kind, name, entry={**fields, "name": name, "updatedAtChapter": number}, chapters=(number,))
        memory = _dict(delta.get("memory"))
        for state in _rows(memory.get("characterStates")):
            if isinstance(name := state.get("name"), str) and name:
                # Memory state changes can be recorded without the character appearing.
                fields = {key: state[key] for key in ("stanceTier", "knownFacts", "currentGoal", "emotion", "stance")
                          if key in state}
                if fields or state.get("seen"):
                    fields["updatedAtChapter"] = number
                    if state.get("seen"):
                        fields["lastSeenChapter"] = number
                    add("characters", name, state=fields, chapters=(number,) if state.get("seen") else ())
        for balance in _rows(memory.get("ledgerBalances")):
            ledger_subjects.update(_strings([balance.get("subject"), balance.get("label")]))

    for kind, rows in snapshots.items():
        target = "characters" if kind == "characterStates" else kind
        for name, entry in rows.items():
            updated = _state_updated(entry) if kind == "characterStates" else _updated(entry)
            if before_chapter is not None and updated >= before_chapter:
                continue
            current = index[target].get(name, {})
            chapters = (_chapter(entry.get("sourceChapter")),
                        _chapter(entry.get("lastSeenChapter")) or _chapter(entry.get("updatedAtChapter")))
            if kind == "characterStates":
                add(target, name, state=entry, chapters=chapters)
            elif not current.get("entry") or _updated(entry) >= _updated(current["entry"]):
                add(target, name, entry=entry, chapters=chapters)

    for balance in _rows(facts.get("ledgerBalances")):
        if before_chapter is None or _updated(balance) < before_chapter:
            ledger_subjects.update(_strings([balance.get("subject"), balance.get("label")]))

    open_records = [row for kind, text_key in (("foreshadowing", "clue"), ("secrets", "secret"),
                                               ("commitments", "text"), ("constraints", "text"))
                    for row in _open_records(facts, before_chapter, history, kind, text_key)]
    references = "\n".join(_strings(open_records))
    for kind in ("characters", "items"):
        for name, row in index[kind].items():
            chapters = row["chapters"]
            row.update(firstChapter=min(chapters, default=None), lastChapter=max(chapters, default=None),
                       clueLinked=_mentions(name, references),
                       ledgerCore=name in ledger_subjects or bool(row["state"].get("stanceTier") or
                                                                  row["state"].get("knownFacts")))
            if row["entry"] and chapters:
                row["entry"].setdefault("sourceChapter", row["firstChapter"])
    return index


def _planned_exits(arc_plans, chapter):
    return dict.fromkeys(
        chip["villain"] for arc in _rows(arc_plans)
        if (end := _chapter(arc.get("endChapter"))) and chapter is not None and end < chapter
        for chip in _rows(arc.get("villainChips")) if chip.get("exitPlan") == "exit"
        and isinstance(chip.get("villain"), str) and chip["villain"].strip()
    )


def entity_tier(name, kind, entry, index, *, book_plan=None, arc_plans=(), current_chapter=None,
                context_text="", theme_ranges=()) -> str:
    entry = _dict(entry)
    row = _dict(index.get(kind)).get(name, {})
    if current_chapter is not None and _updated(entry) >= current_chapter:
        entry = row.get("state", {}) | row.get("entry", {})
    if kind == "items":
        if _ITEM_EXIT.search(f"{entry.get('status', '')} {entry.get('location', '')}"):
            return "retired"
    elif _CHARACTER_EXIT.search(str(entry.get("status", ""))) or name in _planned_exits(arc_plans, current_chapter):
        return "retired"
    plan = _dict(book_plan)
    locks = _dict(plan.get("canonLocks"))
    if kind == "characters":
        core = any(character.get("name") == name for character in _rows(plan.get("mainCharacters")))
        core |= any(name in (alias.get("character"), alias.get("alias")) for alias in _rows(locks.get("identityAliases")))
    else:
        core = any(_mentions(name, asset) for asset in _strings(locks.get("keyAssets")))
        core |= any(name in group.get("members", ()) for group in _rows(locks.get("assetSets")) if group.get("members"))
    if core or row.get("ledgerCore"):
        return "core"
    last = row.get("lastChapter")
    age = current_chapter - last if current_chapter is not None and last is not None else 0
    count = len(row.get("chapters", ()))
    if not row.get("clueLinked") and count <= 1 and age >= 5:
        return "ephemeral"
    return "active" if age <= 15 or count >= 3 or row.get("clueLinked") and count < 2 else "dormant"


def _in_theme_range(chapter, entry, current):
    if isinstance(entry, dict):
        entry = entry.get("range") or (entry.get("startChapter"), entry.get("endChapter"))
    if isinstance(entry, str):
        entry = re.split(r"\s*[-–—]\s*", entry, maxsplit=1)
        entry = tuple(int(part) for part in entry) if len(entry) == 2 and all(part.isdecimal() for part in entry) else ()
    if not isinstance(entry, (list, tuple)) or len(entry) != 2:
        return False
    start, end = map(_chapter, entry)
    return 0 < start <= chapter <= end and (current is None or end < current)


def salience_score(name, kind, entry, tier, index, *, current_chapter=None, context_text="",
                   resolved_theme_ranges=()) -> float:
    row = _dict(index.get(kind)).get(name, {})
    last = row.get("lastChapter")
    age = current_chapter - last if current_chapter is not None and last is not None else 0
    score = (
        8.0 * (tier == "core") + 4.0 * _mentions(name, context_text)
        + 2.0 * math.log2(1 + len(row.get("chapters", ()))) + 3.0 * math.exp(-max(0, age) / 20.0)
        + 1.0 * bool(row.get("clueLinked"))
    )
    if kind == "items" and tier != "core" and (first := row.get("firstChapter")):
        if any(_in_theme_range(first, arc, current_chapter) for arc in resolved_theme_ranges):
            score *= 0.5
    return score


def ranked_entities(kind, index, *, book_plan=None, arc_plans=(), current_chapter=None, context_text="",
                    resolved_theme_ranges=()):
    """Yield visible candidates in descending prompt priority."""
    candidates = []
    for name, row in index[kind].items():
        if not row["entry"] and kind == "items":
            continue
        entry = {**row["state"], **row["entry"]}
        tier = entity_tier(name, kind, entry, index, book_plan=book_plan, arc_plans=arc_plans,
                           current_chapter=current_chapter)
        hit = _mentions(name, context_text)
        if tier in ("ephemeral", "retired") and not hit:
            continue
        score = salience_score(name, kind, entry, tier, index, current_chapter=current_chapter,
                               context_text=context_text, resolved_theme_ranges=resolved_theme_ranges)
        candidates.append((name, tier, score, hit))
    return sorted(candidates, key=lambda row: (row[1] != "dormant" or row[3], row[2]), reverse=True)


def retired_roster(facts, index, *, book_plan=None, arc_plans=(), current_chapter=None, limit=30) -> list[str]:
    if current_chapter is not None and index.get("beforeChapter") != current_chapter:
        index = entity_index(facts, before_chapter=current_chapter)
    names = _planned_exits(arc_plans, current_chapter)
    for name, row in index["characters"].items():
        if _CHARACTER_EXIT.search(str((row["entry"] | row["state"]).get("status", ""))):
            names[name] = None
    return list(names) if limit is None else list(names)[:max(0, limit)]


def retired_character_status(status):
    return status if _CHARACTER_EXIT.search(str(status)) else "退场"


def active_subplots_view(facts, *, current_chapter=None, context_text="", idle_chapters=35) -> tuple[list[dict], list[dict]]:
    facts = _dict(facts)
    archives = prior_arc_summaries(facts, current_chapter) if current_chapter is not None else _rows(facts.get("arcSummaries"))
    closed = {entry["id"] for archive in archives for entry in _rows(archive.get("subplotClosures"))
              if entry.get("status") in ("resolved", "dropped") and isinstance(entry.get("id"), str)}
    history = _history(facts, current_chapter)
    clues = _open_records(facts, current_chapter, history, "foreshadowing", "clue")
    clue_text = "\n".join(_strings(clues))
    snapshots = {row["id"]: row for row in _rows(facts.get("subplots")) if isinstance(row.get("id"), str)}
    future = {key for key, row in snapshots.items() if current_chapter is not None and _updated(row) >= current_chapter}
    if future:
        past = {}
        for row in history:
            for change in _rows(_dict(_dict(row.get("delta")).get("memory")).get("subplots")):
                identifier = change.get("id")
                if isinstance(identifier, str) and identifier in future and (identifier in past or change.get("isNew")):
                    old = past.get(identifier, {"status": "active"})
                    past[identifier] = {**old, **{field: change[field] for field in ("id", "status", "text", "characters")
                                                 if field in change}, "updatedAtChapter": row["chapter"]}
        snapshots = {key: (past.get(key) if key in future else row) for key, row in snapshots.items()}
    visible, stale = [], []
    for key, row in snapshots.items():
        if not row or key in closed or row.get("status") in _CLOSED:
            continue
        terms = [key, row.get("text"), row.get("title"), *list(_strings(row.get("characters")))]
        last = _updated(row)
        idle = current_chapter is not None and last and current_chapter - last > idle_chapters
        linked = row.get("foreshadowingId") or row.get("foreshadowingIds")
        if idle and not (
            any(_mentions(term, context_text) or _mentions(term, clue_text) for term in terms)
            or any(_mentions(term, clue_text) for term in _strings(linked))
        ):
            stale.append(row)
        else:
            visible.append(row)
    priority = lambda row: (any(_mentions(term, context_text) for term in _strings(row)), _updated(row))
    return sorted(visible, key=priority, reverse=True), sorted(stale, key=priority, reverse=True)
