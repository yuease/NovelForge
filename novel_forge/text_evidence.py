"""Shared source matching for chapter review and archive evidence."""

import re
import unicodedata


_SPACE = re.compile(r"[\u0009-\u000d\u0020\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+")
_COMMON_PUNCTUATION = str.maketrans({"。": ".", "、": ",", "“": '"', "”": '"', "‘": "'", "’": "'"})
_ELLIPSIS = re.compile(r"…+|\.{3,}|．{3,}")


def normalize_quote(value):
    return _SPACE.sub("", unicodedata.normalize("NFKC", value)).translate(_COMMON_PUNCTUATION)


def _meaningful_count(text):
    return sum(unicodedata.category(char)[0] in "LN" for char in text)


def locate_quote(quote, source):
    """Return a matching span of normalized source, or None if evidence is unsupported."""
    quote = re.sub(rf"^{_SPACE.pattern}|{_SPACE.pattern}$", "", quote)
    wrappers = {"“": "”", "「": "」", "『": "』", '"': '"'}
    if len(quote) >= 2 and quote[0] in wrappers and quote[-1] == wrappers[quote[0]]:
        quote = quote[1:-1]
    exact = normalize_quote(quote)
    if _meaningful_count(exact) < 4:
        return None
    if exact in source:
        return exact

    parts = [normalize_quote(part).strip(",.;:!?") for part in _ELLIPSIS.split(quote)]
    parts = [part for part in parts if part]
    if not _ELLIPSIS.search(quote) or not parts or any(_meaningful_count(part) < 2 for part in parts):
        return None
    start = source.find(parts[0])
    while start >= 0:
        cursor = start + len(parts[0])
        for part in parts[1:]:
            next_at = source.find(part, cursor)
            if next_at < 0 or next_at - cursor > 320 or len(source[cursor:next_at].encode("utf-16-le", "surrogatepass")) > 640:
                break
            cursor = next_at + len(part)
        else:
            return source[start:cursor]
        start = source.find(parts[0], start + 1)
    return None
