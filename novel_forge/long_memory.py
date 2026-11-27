"""Normalize, replay and select long-term memory without storage side effects."""

import re
import unicodedata
from decimal import Decimal
from itertools import islice

from .arc_planning import normalize_skeleton
from .entity_salience import _mentions, entity_tier, retired_character_status, salience_score
from .text_evidence import locate_quote, normalize_quote

_ITEM_TYPES = {
    "constraints": ("active", "fulfilled", "expired"),
    "commitments": ("active", "fulfilled", "expired"),
    "subplots": ("active", "resolved"),
}
_STATE_FIELDS = ("currentGoal", "emotion", "stance")
_CLOCK_FIELDS = ("day", "season", "timeOfDay", "countdown")
_STANCE_TIERS = ("hostile", "wary", "transactional", "ally", "intimate")
STORY_CHARACTER_FIELDS = ("stanceTier", "stanceTierChapter", "knownFacts", "knownFactChapters", "storyRelationships")
_EXACT_VALUE = re.compile(r"([+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)([^\d.,]*)", re.ASCII)
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


def _normalize_ordinary_memory(raw, chapter, chapter_text):
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


def story_ledger_label_key(value):
    return _compact_quote(value).casefold() if isinstance(value, str) else ""


def _bounded_text(value, limit):
    text = _EDGE_SPACE.sub("", value) if isinstance(value, str) else ""
    return text if _utf16_length(text) <= limit else ""


def _balance_value(value):
    if type(value) is int and - (2**53 - 1) <= value <= 2**53 - 1:
        return str(value)
    return _bounded_text(value, 60)


def _value_key(value):
    text = story_ledger_label_key(value)
    if match := _EXACT_VALUE.fullmatch(text):
        return Decimal(match[1].replace(",", "")), match[2]
    return text


def _memory_rows(history, before_chapter=None):
    rows = []
    for row in _list(history):
        number = _record(row).get("chapter")
        memory = _record(_record(_record(row).get("delta")).get("memory"))
        version = memory.get("version")
        if (_safe_integer(number) and number > 0 and (before_chapter is None or number < before_chapter)
                and type(version) in (int, float) and version == _JOURNAL_VERSION):
            rows.append((int(number), memory))
    return sorted(rows, key=lambda row: row[0])


def _cast_names(book_plan):
    cast = _list(_record(book_plan).get("mainCharacters"))
    lead = protagonist_name(cast)
    names = {}
    for name in [lead, *(_record(entry).get("name") for entry in cast)]:
        name = _bounded_text(name, 40)
        if key := story_ledger_label_key(name):
            names.setdefault(key, name)
    return names


def _observed_names(memory):
    return [name for entry in [*_list(memory.get("storySeen")), *_list(memory.get("characterStates"))]
            if (name := _bounded_text(_record(entry).get("name"), 40)) and entry.get("seen") is True]


def _distinct_facts(values):
    seen, result = set(), []
    for value in _list(values):
        text = _bounded_text(value, 60)
        key = _commitment_key(text)
        if key and key not in seen:
            seen.add(key)
            result.append(text)
            if len(result) == 3:
                break
    return result


def _normalize_story_delta(raw, chapter, chapter_text, book_plan, history):
    source = normalize_quote(chapter_text)
    prior_rows = _memory_rows(history, chapter)

    def evidence(entry):
        quote = _bounded_text(_record(entry).get("quote"), 120)
        return quote if quote and locate_quote(quote, source) else ""

    result = {}
    clock = _record(raw.get("storyClock"))
    fields = {key: text for key in _CLOCK_FIELDS if (text := _bounded_text(clock.get(key), 40))}
    if fields and (quote := evidence(clock)):
        result["storyClock"] = {**fields, "quote": quote}

    balances = {}
    conflicting = set()
    for raw_balance in _list(raw.get("ledgerBalances")):
        balance = _record(raw_balance)
        label = _bounded_text(balance.get("label"), 40)
        value = _balance_value(balance.get("value"))
        if not (label and value and (quote := evidence(balance))):
            continue
        key = story_ledger_label_key(label)
        change = _balance_value(balance.get("change"))
        prior = balances.get(key)
        if prior and (_value_key(prior["value"]) != _value_key(value)
                      or change and prior.get("change") and _value_key(prior["change"]) != _value_key(change)):
            # Two different closing values for the same account leave its event ambiguous.
            conflicting.add(key)
        elif not prior or change and not prior.get("change"):
            balances[key] = {"label": label, "value": value, "quote": quote, **({"change": change} if change else {})}
    tracked = {story_ledger_label_key(row["label"]) for row in _replay_story_ledger(prior_rows, book_plan)[2]} if balances else set()
    usable = []
    for key, entry in balances.items():
        if key not in conflicting and (key in tracked or len(tracked) < 6):
            tracked.add(key)
            usable.append(entry)
    if usable:
        result["ledgerBalances"] = usable

    cast = _cast_names(book_plan)
    prior_seen = {}
    for number, memory in prior_rows:
        if number >= chapter - 11:
            for name in _observed_names(memory):
                prior_seen.setdefault(story_ledger_label_key(name), set()).add(number)
    states, observed = {}, {}
    for name, raw_state in _record(raw.get("characterStates")).items():
        name = _bounded_text(name, 40)
        key = story_ledger_label_key(name)
        state = _record(raw_state)
        if not key or not (quote := evidence(state)):
            continue
        name = cast.get(key, name)
        seen = state.get("seen") is True and normalize_quote(name) in normalize_quote(quote)
        if seen and len(observed) < 16:
            observed.setdefault(key, {"name": name, "seen": True, "quote": quote})
        if key not in cast and len(prior_seen.get(key, ())) + int(seen) < 2:
            continue
        row = {"name": name, "quote": quote}
        if state.get("stanceTier") in _STANCE_TIERS:
            row["stanceTier"] = state["stanceTier"]
        if known := _distinct_facts(state.get("knownFacts")):
            row["knownFacts"] = known
        links = {}
        for other, change in list(_record(state.get("relationships")).items())[:3]:
            other, change = _bounded_text(other, 40), _bounded_text(change, 90)
            if other and change:
                links[other] = change
        if links:
            row["storyRelationships"] = links
        if len(row) > 2:
            states.setdefault(key, row)
    if observed:
        result["storySeen"] = list(observed.values())
    if states:
        ordered = [key for key in cast if key in states] + [key for key in states if key not in cast]
        result["characterStates"] = [states[key] for key in ordered[:12]]
    return result


def normalize_memory_delta(raw, chapter, chapter_text, *, story_ledger=False, long_memory=True, book_plan=None, history=()):
    if not isinstance(raw, dict) or not isinstance(chapter_text, str):
        return None
    ordinary = _normalize_ordinary_memory(raw, chapter, chapter_text) if long_memory else None
    if not story_ledger:
        return ordinary
    story = _normalize_story_delta(raw, chapter, chapter_text, book_plan, history)
    ordinary = ordinary or {}
    combined = {row["name"]: row for row in ordinary.get("characterStates", [])}
    for row in story.pop("characterStates", []):
        combined[row["name"]] = {**combined.get(row["name"], {}), **row}
    if combined:
        story["characterStates"] = list(combined.values())
    return {"version": _JOURNAL_VERSION, **ordinary, **story} if ordinary or story else None


def _commitment_key(text):
    return _compact_quote(text).casefold().rstrip("。.!") if isinstance(text, str) else ""


def _matching_commitment(table, text, characters):
    key = _commitment_key(text)
    names = {name for name in _list(characters) if isinstance(name, str)}
    if not key:
        return None
    matches = [
        identifier for identifier, item in table.items()
        if item["status"] == "active" and _commitment_key(item.get("text")) == key
        and (not names or not item["characters"] or names.intersection(item["characters"]))
    ]
    return matches[0] if len(matches) == 1 else None


def _replay_story_ledger(rows, book_plan):
    clock, field_chapters, balances, actors, last_seen = {}, {}, {}, {}, {}
    cast = _cast_names(book_plan)
    for number, memory in rows:
        for name in _observed_names(memory):
            last_seen[story_ledger_label_key(name)] = number
        event = _record(memory.get("storyClock"))
        for key in _CLOCK_FIELDS:
            text = _bounded_text(event.get(key), 40)
            if text and story_ledger_label_key(text) != story_ledger_label_key(clock.get(key)):
                clock[key] = text
                field_chapters[key] = number
        for row in _list(memory.get("ledgerBalances")):
            row = _record(row)
            label, value = _bounded_text(row.get("label"), 40), _balance_value(row.get("value"))
            key = story_ledger_label_key(label)
            if not key or not value or key not in balances and len(balances) == 6:
                continue
            record = balances.setdefault(key, {"label": label, "recentChanges": []})
            previous = record.get("value")
            change = _balance_value(row.get("change"))
            if previous is None or _value_key(previous) != _value_key(value):
                if previous is not None or change:
                    record["recentChanges"] = [*record["recentChanges"],
                                               {"chapter": number, "value": value, **({"change": change} if change else {})}][-3:]
                record.update(value=value, updatedAtChapter=number)

        for row in _list(memory.get("characterStates")):
            row = _record(row)
            name = _bounded_text(row.get("name"), 40)
            key = story_ledger_label_key(name)
            if not key or not any(field in row for field in ("stanceTier", "knownFacts", "storyRelationships")):
                continue
            actor = actors.setdefault(key, {"name": cast.get(key, name), "updatedAtChapter": number,
                                             "knownFactChapters": {}, "storyRelationships": {}})
            actor["updatedAtChapter"] = number
            if row.get("stanceTier") in _STANCE_TIERS:
                actor.update(stanceTier=row["stanceTier"], stanceTierChapter=number)
            knowledge = actor["knownFactChapters"]
            existing = {_commitment_key(text) for text in knowledge}
            for text in _distinct_facts(row.get("knownFacts")):
                identity = _commitment_key(text)
                if len(knowledge) < 3 and identity not in existing:
                    knowledge[text] = number
                    existing.add(identity)
            for other, change in _record(row.get("storyRelationships")).items():
                if isinstance(other, str) and isinstance(change, str):
                    links = actor["storyRelationships"]
                    links[other] = {"change": change, "sourceChapter": links.get(other, {}).get("sourceChapter", number),
                                    "updatedAtChapter": number}

    order = [key for key in cast if key in actors]
    order.extend(sorted((key for key in actors if key not in cast),
                        key=lambda key: (last_seen.get(key, 0), actors[key]["updatedAtChapter"]), reverse=True))
    states = {}
    for key in order[:12]:
        actor = actors[key]
        fields = {field: actor[field] for field in STORY_CHARACTER_FIELDS if actor.get(field)}
        if facts := actor["knownFactChapters"]:
            fields["knownFacts"] = list(facts)
        states[actor["name"]] = fields
    return clock, field_chapters, list(balances.values()), states


def replay_long_memory(history, *, book_plan=None, before_chapter=None):
    characters, items = {}, {kind: {} for kind in _ITEM_TYPES}
    rows = _memory_rows(history, before_chapter)
    commitment_aliases = {}

    for number, memory in rows:
        for entry in _list(memory.get("characterStates")):
            name = _record(entry).get("name")
            if not isinstance(name, str) or not name:
                continue
            if "seen" not in entry and any(field in entry for field in STORY_CHARACTER_FIELDS):
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
                if kind == "commitments":
                    if identifier in commitment_aliases:
                        identifier = commitment_aliases[identifier]
                    elif identifier not in table and entry.get("isNew"):
                        match = _matching_commitment(table, text, entry.get("characters"))
                        if match is not None:
                            commitment_aliases[identifier] = match
                            identifier = match
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
    clock, field_chapters, balances, locks = _replay_story_ledger(rows, book_plan)
    for name, state in locks.items():
        characters[name] = {**characters.get(name, {}), **state}
    return {"characterStates": characters, **{kind: list(table.values()) for kind, table in items.items()},
            **({"storyClock": {**clock, "fieldChapters": field_chapters}} if clock else {}),
            **({"ledgerBalances": balances} if balances else {})}


def story_ledger_view(facts, *, chapter_number, book_plan=None, context_text=None):
    """Project verified facts before the requested chapter without changing the job."""
    facts = _record(facts)
    history = facts.get("factHistory")
    if history:
        facts = replay_long_memory(history, book_plan=book_plan, before_chapter=chapter_number)
    result = {}
    if clock := _record(facts.get("storyClock")):
        sources = _record(clock.get("fieldChapters"))
        current = {key: clock[key] for key in _CLOCK_FIELDS if key in clock and _number(sources.get(key)) < chapter_number}
        if current:
            result["storyClock"] = {**current, "fieldChapters": {key: sources[key] for key in current if key in sources}}
    balances = []
    for row in _list(facts.get("ledgerBalances")):
        row = _record(row)
        label, value = _bounded_text(row.get("label"), 40), _balance_value(row.get("value"))
        if not label or not value or _number(row.get("updatedAtChapter")) >= chapter_number:
            continue
        moves = [{key: move[key] for key in ("chapter", "value", "change") if key in move}
                 for move in _list(row.get("recentChanges")) if isinstance(move, dict)
                 and _number(move.get("chapter")) < chapter_number]
        balances.append({"label": label, "value": value, "updatedAtChapter": row.get("updatedAtChapter"),
                         "recentChanges": moves[-3:]})
        if len(balances) == 6:
            break
    if balances:
        result["ledgerBalances"] = balances

    context = normalize_quote(context_text) if isinstance(context_text, str) else None
    states = {}
    for name, raw in _record(facts.get("characterStates")).items():
        if not isinstance(name, str) or not name or context is not None and normalize_quote(name) not in context:
            continue
        raw = _record(raw)
        record = {}
        stance_at = _number(raw.get("stanceTierChapter"))
        if raw.get("stanceTier") in _STANCE_TIERS and stance_at < chapter_number:
            record.update(stanceTier=raw["stanceTier"], stanceTierChapter=raw.get("stanceTierChapter"))
        sources = _record(raw.get("knownFactChapters"))
        known = [fact for fact in _distinct_facts(raw.get("knownFacts")) if _number(sources.get(fact)) < chapter_number]
        if known:
            record.update(knownFacts=known, knownFactChapters={fact: sources[fact] for fact in known if fact in sources})
        if not record and not raw.get("storyRelationships"):
            continue
        links = {**_record(raw.get("relationships")), **_record(raw.get("storyRelationships"))}
        recent = sorted(((who, link) for who, link in links.items() if
                         0 < chapter_number - _number(_record(link).get("updatedAtChapter")) <= 12),
                        key=lambda entry: _number(entry[1].get("updatedAtChapter")), reverse=True)
        if recent:
            record["relationships"] = dict(recent[:3])
        if record:
            states[name] = record
            if len(states) == 12:
                break
    if states:
        result["characterStates"] = states
    return result


def story_ledger_events(facts, start_chapter, end_chapter, *, book_plan=None):
    """Derive admitted changes in chronological order for one arc review."""
    events = []
    rows = _memory_rows(_record(facts).get("factHistory"), end_chapter + 1)
    first = next((index for index, (number, _) in enumerate(rows) if number >= start_chapter), len(rows))
    prior_clock, _, _, prior_actors = _replay_story_ledger(rows[:first], book_plan)
    for index in range(first, len(rows)):
        number = rows[index][0]
        clock, _, accounts, actors = _replay_story_ledger(rows[:index + 1], book_plan)
        event = {"chapter": number}
        clock_updates = {key: value for key, value in clock.items() if prior_clock.get(key) != value}
        if clock_updates:
            event["storyClock"] = clock_updates
        balances = []
        for account in accounts:
            if account["updatedAtChapter"] != number:
                continue
            row = {key: account[key] for key in ("label", "value")}
            if account["recentChanges"] and account["recentChanges"][-1].get("chapter") == number:
                if change := account["recentChanges"][-1].get("change"):
                    row["change"] = change
            balances.append(row)
        if balances:
            event["ledgerBalances"] = balances
        states = {}
        for name, state in actors.items():
            prior = prior_actors.get(name, {})
            changes = {}
            if state.get("stanceTierChapter") == number and state.get("stanceTier") != prior.get("stanceTier"):
                changes["stanceTier"] = state["stanceTier"]
            known = [text for text, chapter in state.get("knownFactChapters", {}).items() if chapter == number]
            if known:
                changes["knownFacts"] = known
            links = {who: value["change"] for who, value in state.get("storyRelationships", {}).items()
                     if value["updatedAtChapter"] == number}
            if links:
                changes["relationships"] = links
            if changes:
                states[name] = changes
        if states:
            event["characterStates"] = states
        if len(event) > 1:
            events.append(event)
        prior_clock, prior_actors = clock, actors
    return events


def normalize_volume_summary(raw, start_chapter, end_chapter, *, mainline_limit=360, turn_limit=4):
    raw = _record(raw)
    mainline = _clean_text(raw.get("mainline"), mainline_limit)
    if not mainline:
        return None
    turns = []
    for point in _list(raw.get("turningPoints")):
        chapter = _record(point).get("chapter")
        text = _clean_text(_record(point).get("text"), 90)
        if _safe_integer(chapter) and start_chapter <= chapter <= end_chapter and text:
            turns.append({"chapter": int(chapter), "text": text})
            if len(turns) == turn_limit:
                break
    return {"mainline": mainline, "turningPoints": turns}


def normalize_arc_summary(raw, chapters, *, story_ledger=False, plan_canon=False):
    if not chapters:
        return None
    sources = {row["chapterNumber"]: normalize_quote(row.get("content") or "") for row in chapters}
    meta = normalize_volume_summary(raw, min(sources), max(sources), mainline_limit=280, turn_limit=3)
    if meta is None:
        return None

    def source_reference(entry):
        number = entry.get("chapter")
        quote = entry.get("quote")
        if (type(number) is int and number in sources and isinstance(quote, str)
                and 4 <= len(quote.strip()) <= 120 and locate_quote(quote, sources[number])):
            return {"chapter": number, "quote": quote.strip()}
        return None

    def evidenced(key):
        for entry in _list(raw.get(key)):
            entry = _record(entry)
            text = _clean_text(entry.get("text"), 60)
            if text and (proof := source_reference(entry)):
                yield {"chapter": proof["chapter"], "text": text, "quote": proof["quote"]}

    beats_by_chapter = {}
    for beat in evidenced("usedBeats"):
        beats = beats_by_chapter.setdefault(beat["chapter"], [])
        if len(beats) < 2:
            beats.append(beat)
    cap = min(6, max(4, len(chapters)))
    beat_groups = [beats_by_chapter[number] for number in sorted(beats_by_chapter)]
    used_beats = [beats[index] for index in range(2) for beats in beat_groups
                  if index < len(beats)][:cap]
    result = {**meta, "usedBeats": used_beats, "usedHooks": list(islice(evidenced("usedHooks"), 3)),
              "villainStatus": _clean_text(raw.get("villainStatus"), 120)}
    closures = {}
    for entry in _list(raw.get("subplotClosures")):
        entry = _record(entry)
        identifier, status, quote = entry.get("id"), entry.get("status"), entry.get("quote", "")
        if not isinstance(identifier, str) or not 1 <= len(identifier.strip()) <= 80:
            continue
        if status not in ("resolved", "dropped") or not isinstance(quote, str):
            continue
        identifier, quote = identifier.strip(), quote.strip()
        if status == "resolved" and not (4 <= len(quote) <= 120 and any(
            locate_quote(quote, source) for source in sources.values()
        )):
            continue
        if status == "dropped" and len(quote) > 120:
            continue
        closures.setdefault(identifier, {"id": identifier, "status": status, "quote": quote})
    if closures:
        result["subplotClosures"] = list(closures.values())
    if themes := list(dict.fromkeys(text for value in _list(raw.get("themes"))
                                   if (text := _clean_text(value, 24))))[:3]:
        result["themes"] = themes
    if skeleton := normalize_skeleton(raw.get("skeleton")):
        result["skeleton"] = skeleton
    if shape := _clean_text(raw.get("shape"), 40):
        result["shape"] = shape
    progress = _record(raw.get("volumeProgress"))
    if progress:
        normalized = {key: text for key, limit in (("escalation", 40), ("cost", 30))
                      if (text := _clean_text(progress.get(key), limit))}
        if isinstance(progress.get("retiredReused"), list):
            normalized["retiredReused"] = list(dict.fromkeys(
                text for value in progress["retiredReused"] if (text := _clean_text(value, 24))
            ))[:2]
        if normalized:
            result["volumeProgress"] = normalized
    if story_ledger or plan_canon:
        drifts, seen = [], set()
        allowed = ("value", "clock", "canon", "character_ooc") if story_ledger else ("canon",)
        for entry in _list(raw.get("driftChecks")):
            entry = _record(entry)
            kind = entry.get("kind")
            issue = _clean_text(entry.get("issue"), 61)
            fix_hint = _clean_text(entry.get("fixHint"), 81)
            if (kind not in allowed
                    or not (1 <= len(issue) <= 60 and 1 <= len(fix_hint) <= 80)):
                continue
            if proof := source_reference(entry):
                identity = proof["chapter"], kind, normalize_quote(issue).casefold()
                if identity in seen:
                    continue
                seen.add(identity)
                drifts.append({"chapter": proof["chapter"], "kind": kind, "issue": issue,
                               "fixHint": fix_hint, "quote": proof["quote"]})
                if len(drifts) == 4:
                    break
        if story_ledger or drifts:
            result["driftChecks"] = drifts
    return result


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


def relevant_memory_characters(facts, base_characters, lead, context, *, salience_index=None,
                               core_names=frozenset(), retired_names=frozenset()):
    base = _record(base_characters)
    states = _record(_record(facts).get("characterStates"))
    current = None
    if salience_index is not None:
        current = salience_index.get("beforeChapter")
        rows = salience_index["characters"]
        base = {name: row["entry"] for name, row in rows.items()}
        states = {name: row["state"] for name, row in rows.items() if row["state"]}
    candidates = dict.fromkeys([*base, *states, *([lead] if lead else [])])

    def priority(name):
        seen = _record(states.get(name)).get("lastSeenChapter") or _record(base.get(name)).get("sourceChapter")
        hit = _mentions(name, context)
        if salience_index is None and not (core_names or retired_names):
            return name == lead, _utf16_length(name) >= 2 and name in context, _number(seen)
        tier = tiers.get(name)
        score = salience_score(name, "characters", base.get(name), tier, salience_index, current_chapter=current,
                               context_text=context) if salience_index is not None else 0
        return name == lead, name in core_names, hit, score, _number(seen)

    tiers = {}
    for name in candidates:
        entry = {**_record(states.get(name)), **_record(base.get(name))}
        if name in retired_names:
            tiers[name] = "retired"
        elif name in core_names or name == lead:
            tiers[name] = "core"
        elif salience_index is not None:
            tiers[name] = entity_tier(name, "characters", entry, salience_index, current_chapter=current)
    result = []
    visible = [name for name in candidates if tiers.get(name) not in ("ephemeral", "retired") or _mentions(name, context)]
    for name in sorted(visible, key=priority, reverse=True)[:20]:
        value = {**_record(base.get(name)), "name": name}
        if tiers.get(name) == "retired":
            value["status"] = retired_character_status(value.get("status") or _record(states.get(name)).get("status"))
        state = states.get(name)
        if isinstance(state, dict):
            links = sorted(
                _record(state.get("relationships")).items(),
                key=lambda item: _number(_record(item[1]).get("updatedAtChapter")), reverse=True,
            )
            value["state"] = {**state, "relationships": dict(links[:3])}
        result.append({"key": name, "value": value})
    return result
