"""Style guides and deterministic cleanup of newly written chapters."""

import re
import unicodedata

from .llm import STYLE_BIBLE_MARKER
from .storage import count_chinese_words


_STYLE_LIMIT = 300
_STYLE_FIELDS = (
    ("narrativeVoice", "叙事", 1, 80, True),
    ("dialogueRules", "对白", 3, 70, False),
    ("domainTaboos", "考据禁区", 3, 58, False),
    ("forbiddenCliches", "禁用套话", 5, 24, False),
)
_SPACE = r"\u0009-\u000d\u0020\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
_WHITESPACE = re.compile(rf"[{_SPACE}]+")
_EDGE_SPACE = re.compile(rf"^[{_SPACE}]+|[{_SPACE}]+\Z")
_TRAILING_SPACE = re.compile(rf"[{_SPACE}]+\Z")
_SENTENCE_END = re.compile(rf"[。！？!?]+|\.(?=[{_SPACE}]|$)")
_DIALOGUE_MARK = re.compile(r"""[“”「」『』"'‘’：:]|(?:^|\n)[ \t\u3000]*(?:>|[-—]{2,})""")
_MARKDOWN_HEADING = re.compile(rf"^#{{1,6}}[{_SPACE}]*")
_NUMBERED = re.compile(rf"^第[{_SPACE}]*([0-9]+)[{_SPACE}]*章[{_SPACE}:—-]*(.*)$")
_PUNCTUATION = str.maketrans("。！？，；", ".!?,;")
_ASCII_ALPHANUMERIC = re.compile(r"[a-zA-Z0-9]")


def normalize_style_bible(bible):
    """Return compact valid entries, or None for a missing or unusable guide."""
    if not isinstance(bible, dict):
        return None
    normalized = {}
    used = 0
    for key, label, count, limit, single in _STYLE_FIELDS:
        entries = [bible.get(key)] if single else bible.get(key)
        if not isinstance(entries, list):
            continue
        seen = {}
        for value in entries[:count]:
            if not isinstance(value, str):
                continue
            text = _WHITESPACE.sub(" ", value).strip()
            if not text or text in seen or len(text) > limit:
                continue
            length = len(f"{label}：{text}") + bool(used)
            if used + length <= _STYLE_LIMIT:
                seen[text] = None
                used += length
        if seen:
            normalized[key] = next(iter(seen)) if single else list(seen)
    return normalized or None


def compact_style_bible(bible):
    normalized = normalize_style_bible(bible) or {}
    lines = []
    for key, label, _, _, single in _STYLE_FIELDS:
        if key in normalized:
            entries = [normalized[key]] if single else normalized[key]
            lines.extend(f"{label}：{entry}" for entry in entries)
    return lines


def _is_draft_heading(line, title, chapter_number):
    def plain(value):
        return unicodedata.normalize("NFKC", _MARKDOWN_HEADING.sub("", _EDGE_SPACE.sub("", str(value or ""))))

    current, candidate = plain(title), plain(line)
    heading = _NUMBERED.fullmatch(candidate)
    if heading:
        try:
            if int(heading[1]) != float(chapter_number):
                return False
        except (ValueError, TypeError):
            return False
    if current and candidate == current:
        return True
    if not heading:
        return False
    numbered_title = _NUMBERED.fullmatch(current)
    subtitle = (numbered_title[2] if numbered_title else "") or current
    return not heading[2] or _EDGE_SPACE.sub("", heading[2]) == subtitle


def strip_draft_headings(text, *, title=None, chapter_number=None, min_words=0):
    lines = text.split("\n")
    next_line = 0
    for _ in range(2):
        if next_line >= len(lines) or not _is_draft_heading(lines[next_line], title, chapter_number):
            break
        next_line += 1
        while next_line < len(lines) and not _EDGE_SPACE.sub("", lines[next_line]):
            next_line += 1
    if not next_line:
        return text
    body = "\n".join(lines[next_line:])
    return body if _EDGE_SPACE.sub("", body) and count_chinese_words(body) >= min_words else text


def _comparable(text):
    def space(match):
        if (match.start() and match.end() < len(match.string)
                and _ASCII_ALPHANUMERIC.fullmatch(match.string[match.start() - 1])
                and _ASCII_ALPHANUMERIC.fullmatch(match.string[match.end()])):
            return " "
        return ""

    return _WHITESPACE.sub(space, text.translate(_PUNCTUATION))


def _meaningful_count(text):
    return sum(unicodedata.category(char)[0] in "LN" for char in text)


def _complete_sentences(text):
    sentences = []
    start = 0
    for mark in _SENTENCE_END.finditer(text):
        end = mark.end()
        value = text[start:end]
        if _meaningful_count(value):
            sentences.append((end, value, _comparable(value)))
        start = end
    return sentences


def _duplicate_opening_end(text, previous_ending):
    previous = _TRAILING_SPACE.sub("", str(previous_ending or ""))
    start = max(0, len(previous) - 480)
    tail = previous[start:]
    opening = _complete_sentences(text[:240])
    closing = _complete_sentences(tail)
    if start and previous[start - 1] not in "。！？!?":
        closing = closing[1:]
    if not closing or closing[-1][0] != len(tail):
        return 0
    max_cut = min(160, len(text) // 10)
    for count in range(min(len(opening), len(closing)), 1, -1):
        recent = closing[-count:]
        if any(previous_sentence[2] != current_sentence[2]
               for previous_sentence, current_sentence in zip(recent, opening)):
            continue
        end = opening[count - 1][0]
        prefix = text[:end]
        if (_meaningful_count(prefix) < 40 or end > max_cut or _DIALOGUE_MARK.search(prefix)
                or any(_DIALOGUE_MARK.search(value) for _, value, _ in recent)):
            return 0
        return end
    return 0


def clean_chapter_draft(draft, *, title=None, chapter_number=None, previous_ending="", min_words=0):
    normalized = str(draft or "").replace("\r\n", "\n").replace("\r", "\n")
    normalized = _EDGE_SPACE.sub("", re.sub(r"\n(?:[ \t\u3000]*\n){2,}", "\n\n", normalized))
    formatted = strip_draft_headings(normalized, title=title, chapter_number=chapter_number, min_words=min_words)
    end = _duplicate_opening_end(formatted, previous_ending)
    if not end:
        return formatted
    rest = re.sub(r"^(?:[ \t\u3000]*\n)+", "", formatted[end:])
    return rest if _EDGE_SPACE.sub("", rest) and count_chinese_words(rest) >= min_words else formatted
