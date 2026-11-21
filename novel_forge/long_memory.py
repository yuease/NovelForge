"""Normalize, replay and select long-term memory without storage side effects."""

import re
import unicodedata


_ITEM_TYPES = {
    "constraints": ("active", "fulfilled", "expired"),
    "commitments": ("active", "fulfilled", "expired"),
    "subplots": ("active", "resolved"),
}
_STATE_FIELDS = ("currentGoal", "emotion", "stance")
_JOURNAL_VERSION = 1
_SPACE = r"\u0009-\u000d\u0020\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
_WHITESPACE = re.compile(rf"[{_SPACE}]+")
_EDGE_SPACE = re.compile(rf"^[{_SPACE}]+|[{_SPACE}]+\Z")


def _record(value):
    return value if isinstance(value, dict) else {}


def _list(value):
    return value if isinstance(value, list) else []


def _clean_text(value, limit):
    return _EDGE_SPACE.sub("", value)[:limit] if isinstance(value, str) else ""


def _compact_quote(value):
    return _WHITESPACE.sub("", unicodedata.normalize("NFKC", value))


def _utf16_length(value):
    return len(value.encode("utf-16-le", "surrogatepass")) // 2


def _safe_integer(value):
    return (type(value) in (int, float) and - (2**53 - 1) <= value <= 2**53 - 1
            and int(value) == value)


def empty_long_memory():
    return {"characterStates": {}, "constraints": [], "commitments": [], "subplots": []}


def normalize_memory_delta(raw, chapter, chapter_text):
    if not isinstance(raw, dict) or not isinstance(chapter_text, str):
        return None
    canonical = _compact_quote(chapter_text)

    def supported(entry):
        quote = _record(entry).get("quote")
        if not isinstance(quote, str) or _utf16_length(quote) > 500:
            return False
        quote = _compact_quote(quote)
        return _utf16_length(quote) >= 4 and quote in canonical

    delta = {"version": _JOURNAL_VERSION}
    characters = []
    for key, entry in list(_record(raw.get("characterStates")).items())[:8]:
        name = _clean_text(key, 40)
        if not name or not supported(entry):
            continue
        state = {"name": name, "seen": entry.get("seen") is True}
        for field in _STATE_FIELDS:
            value = _clean_text(entry.get(field), 100)
            if value or (field in entry and entry[field] is None):
                state[field] = value or None
        if isinstance(entry.get("relationships"), dict):
            links = ((_clean_text(who, 40), _clean_text(change, 90))
                     for who, change in list(entry["relationships"].items())[:4])
            state["relationships"] = {who: change for who, change in links if who and change}
        if state["seen"] or any(field in state for field in _STATE_FIELDS) or state.get("relationships"):
            characters.append(state)
    if characters:
        delta["characterStates"] = characters

    for kind, choices in _ITEM_TYPES.items():
        entries = []
        for index, entry in enumerate(_list(raw.get(kind))[:6], 1):
            if not supported(entry):
                continue
            identifier = _clean_text(entry.get("id"), 80)
            text = _clean_text(entry.get("text"), 160)
            status = entry.get("status")
            if not (identifier or text) or (status is not None and status not in choices):
                continue
            item = {"id": identifier or f"{kind}-{chapter}-{index}", "isNew": not identifier}
            if text:
                item["text"] = text
            if status in choices:
                item["status"] = status
            if isinstance(entry.get("characters"), list):
                names = (_clean_text(name, 40) for name in entry["characters"])
                item["characters"] = list(dict.fromkeys(filter(None, names)))[:6]
            if kind == "commitments" and "deadline" in entry and (
                entry["deadline"] is None or isinstance(entry["deadline"], str)
            ):
                item["deadline"] = _clean_text(entry["deadline"], 90) or None
            entries.append(item)
        if entries:
            delta[kind] = entries
    return delta if len(delta) > 1 else None


def replay_long_memory(history):
    characters, items = {}, {kind: {} for kind in _ITEM_TYPES}
    rows = []
    for row in _list(history):
        number = _record(row).get("chapter")
        memory = _record(_record(_record(row).get("delta")).get("memory"))
        version = memory.get("version")
        if _safe_integer(number) and number > 0 and type(version) in (int, float) and version == _JOURNAL_VERSION:
            rows.append((int(number), memory))

    for number, memory in sorted(rows, key=lambda row: row[0]):
        for entry in _list(memory.get("characterStates")):
            name = _record(entry).get("name")
            if not isinstance(name, str) or not name:
                continue
            state = characters.setdefault(name, {
                "sourceChapter": number, "lastSeenChapter": 0,
                **dict.fromkeys(_STATE_FIELDS), "relationships": {},
            })
            links = state["relationships"]
            for who, change in _record(entry.get("relationships")).items():
                if isinstance(change, str):
                    links[who] = {"change": change, "sourceChapter": links.get(who, {}).get("sourceChapter", number),
                                  "updatedAtChapter": number}
            state["updatedAtChapter"] = number
            for field in _STATE_FIELDS:
                if field in entry and (entry[field] is None or isinstance(entry[field], str)):
                    state[field] = entry[field]
            if entry.get("seen"):
                state["lastSeenChapter"] = number

        for kind, choices in _ITEM_TYPES.items():
            for entry in _list(memory.get(kind)):
                identifier = _record(entry).get("id")
                if not isinstance(identifier, str):
                    continue
                table = items[kind]
                text = entry.get("text")
                if identifier not in table:
                    if not entry.get("isNew") or not isinstance(text, str) or not text:
                        continue
                    table[identifier] = {"id": identifier, "sourceChapter": number, "status": "active", "characters": []}
                    if kind == "commitments":
                        table[identifier]["deadline"] = None
                record = table[identifier]
                record["updatedAtChapter"] = number
                if isinstance(text, str) and text:
                    record["text"] = text
                if entry.get("status") in choices:
                    record["status"] = entry["status"]
                if isinstance(entry.get("characters"), list):
                    record["characters"] = [name for name in entry["characters"] if isinstance(name, str)][:6]
                if kind == "commitments" and "deadline" in entry and (
                    entry["deadline"] is None or isinstance(entry["deadline"], str)
                ):
                    record["deadline"] = entry["deadline"]
    return {"characterStates": characters, **{kind: list(table.values()) for kind, table in items.items()}}


def normalize_volume_summary(raw, start_chapter, end_chapter):
    raw = _record(raw)
    mainline = _clean_text(raw.get("mainline"), 360)
    if not mainline:
        return None
    turns = []
    for point in _list(raw.get("turningPoints")):
        chapter = _record(point).get("chapter")
        text = _clean_text(_record(point).get("text"), 90)
        if _safe_integer(chapter) and start_chapter <= chapter <= end_chapter and text:
            turns.append({"chapter": int(chapter), "text": text})
            if len(turns) == 4:
                break
    return {"mainline": mainline, "turningPoints": turns}


def _number(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def active_memory_items(facts, kind, context="", limit=8):
    def priority(entry):
        terms = [entry.get("text"), *_list(entry.get("characters"))]
        references = sum(
            isinstance(term, str) and _utf16_length(term) >= 2 and term in context for term in terms
        )
        return references, _number(entry.get("updatedAtChapter"))

    active = [item for item in _list(_record(facts).get(kind)) if _record(item).get("status") == "active"]
    return sorted(active, key=priority, reverse=True)[:limit]


def protagonist_name(main_characters):
    cast = [character for character in _list(main_characters)
            if isinstance(_record(character).get("name"), str) and character["name"]]
    lead = next((character for character in cast
                 if re.search(r"主角|主人公|男主|女主|protagonist", str(character.get("role") or ""), re.I)),
                cast[0] if cast else None)
    return lead["name"] if lead else None


def relevant_memory_characters(facts, base_characters, lead, context):
    base = _record(base_characters)
    states = _record(_record(facts).get("characterStates"))
    candidates = dict.fromkeys([*base, *states, *([lead] if lead else [])])

    def priority(name):
        seen = _record(states.get(name)).get("lastSeenChapter") or _record(base.get(name)).get("sourceChapter")
        return name == lead, _utf16_length(name) >= 2 and name in context, _number(seen)

    result = []
    for name in sorted(candidates, key=priority, reverse=True)[:20]:
        value = {**_record(base.get(name)), "name": name}
        state = states.get(name)
        if isinstance(state, dict):
            links = sorted(
                _record(state.get("relationships")).items(),
                key=lambda item: _number(_record(item[1]).get("updatedAtChapter")), reverse=True,
            )
            value["state"] = {**state, "relationships": dict(links[:3])}
        result.append({"key": name, "value": value})
    return result
