"""Compare explicit title quantities with the evidence in the chapter body."""

import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


_IDIOMS = re.compile(
    "千钧一发|百战百胜|十拿九稳|一举两得|万无一失|一落千丈|九死一生|三番五次|五花八门|七零八落|"
    "一败涂地|百密一疏|千方百计|一石二鸟|一刻千金|十万火急|三心二意|四面八方|五湖四海|百年大计|"
    "一鸣惊人|一箭双雕|九牛一毛"
)
_DIGITS = dict(zip("零一二三四五六七八九", range(10))) | {"〇": 0, "两": 2}
_SMALL = {"十": 10, "百": 100, "千": 1000}
_LARGE = {"万": 10_000, "亿": 100_000_000}
_ARABIC = r"(?:[0-9０-９]{1,3}(?:[,，][0-9０-９]{3})+|[0-9０-９]+)(?:[.．][0-9０-９]+)?"
_CN = r"零〇一二两三四五六七八九十百千万亿点"
_NUMBER = rf"(?:{_ARABIC}[十百千万亿]*|[{_CN}]+|半)"
_UNITS = (
    "平方米|吨级|千克|公斤|公里|平米|两银|银圆|积分|吨|斤|两|克|石|支|米|丈|尺|寸|里|层|级|"
    "元|块|贯|文|件|台|座|扇|艘|天|日|年|倍|[%％]"
)
_MEASURE = re.compile(
    rf"(?P<number>{_NUMBER})[^\S\r\n]*(?P<unit>{_UNITS})|"
    rf"(?P<quantity>{_ARABIC}[十百千万亿]*|[{_CN}]*[万亿](?![{_CN}]))"
)
_FAMILIES = {"吨级": "吨", "公斤": "千克", "平米": "平方米", "％": "%"}
_CJK = r"[\u3400-\u9fff]"
_LEFT = re.compile(rf"({_CJK}{{2,}})\s*$")
_RIGHT = re.compile(rf"^\s*({_CJK}{{2,}})")
_HEADING = re.compile(rf"^第\s*[{_CN}0-9０-９]+\s*章[\s:：、.．—-]*")
_TITLE_WRAPPERS = " \t#*_`'\"“”‘’《》「」『』"
_EDGE_PUNCTUATION = " \t:：、,，。；;·-—|/（）()【】[]"


@dataclass(frozen=True)
class _Measure:
    raw: str
    value: Decimal
    unit: str
    family: str
    number: str
    start: int
    end: int


def _chinese_value(text):
    for unit in ("亿", "万"):
        if unit in text:
            left, right = text.split(unit, 1)
            return _chinese_value(left or "一") * _LARGE[unit] + (_chinese_value(right) if right else 0)
    if "点" in text:
        whole, fraction = text.split("点", 1)
        return _chinese_value(whole or "零") + Decimal("0." + "".join(str(_DIGITS[char]) for char in fraction))
    if all(char in _DIGITS for char in text):
        return Decimal("".join(str(_DIGITS[char]) for char in text))
    subtotal, pending, last_unit = 0, None, 10_000
    for char in text:
        if char in _DIGITS:
            pending = _DIGITS[char]
            continue
        unit = _SMALL[char]
        if unit >= last_unit:
            raise ValueError("Chinese numeral units must descend within a section")
        subtotal += (1 if pending is None else pending) * unit
        pending, last_unit = None, unit
    return Decimal(subtotal + (pending or 0))


def _numeric_value(text):
    text = unicodedata.normalize("NFKC", text)
    if text == "半":
        return Decimal("0.5")
    if text[0].isdigit() and text[0].isascii():
        number = text.rstrip("十百千万亿")
        value = Decimal(number.replace(",", ""))
        for char in text[len(number):]:
            value *= (_SMALL | _LARGE)[char]
        return value
    return _chinese_value(text)


def _measures(text):
    clean = _IDIOMS.sub(lambda match: " " * len(match[0]), text)
    measures = []
    for match in _MEASURE.finditer(clean):
        number = match["number"] or match["quantity"]
        unit = match["unit"] or ""
        start, end = match.span()
        if start and (clean[start - 1] in ".．" or clean[start - 1] in _CN):
            continue
        if not unit and end < len(clean) and clean[end] == "章":
            continue
        try:
            value = _numeric_value(number)
        except (InvalidOperation, KeyError, ValueError):
            continue
        measures.append(_Measure(text[start:end], value, unit, _FAMILIES.get(unit, unit) or "数量", number, start, end))
    return measures


def _anchors(title, measure, previous_end, next_start):
    left = _LEFT.search(title[previous_end:measure.start])
    right = _RIGHT.search(title[measure.end:next_start])
    return {word[index:index + 2] for match in (left, right) if match
            for word in (match[1],) for index in range(len(word) - 1)}


def _anchored_mentions(body, mentions, anchors):
    selected = set()
    for anchor in anchors:
        for match in re.finditer(re.escape(anchor), body):
            distances = [max(0, match.start() - mention.end, mention.start - match.end()) for mention in mentions]
            nearest = min(distances)
            if nearest <= 30:
                selected.update(mention for mention, distance in zip(mentions, distances) if distance == nearest)
    return [mention for mention in mentions if mention in selected]


def _value_text(value):
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _conflicting_mentions(title, body):
    title_mentions = _measures(title)
    if not title_mentions:
        return
    by_family = {}
    for mention in _measures(body):
        by_family.setdefault(mention.family, []).append(mention)
    for index, mention in enumerate(title_mentions):
        same_unit = by_family.get(mention.family)
        if not same_unit:
            continue
        if mention.family == "数量" and not any(
            row.number[-1] in "十百千万亿" for row in (mention, *same_unit)
        ):
            continue
        previous_end = title_mentions[index - 1].end if index else 0
        next_start = title_mentions[index + 1].start if index + 1 < len(title_mentions) else len(title)
        anchored = _anchored_mentions(body, same_unit, _anchors(title, mention, previous_end, next_start))
        candidates = anchored or same_unit
        values = list(dict.fromkeys(row.value for row in candidates))
        if mention.value in values or not anchored and len(values) > 1:
            continue
        yield mention, {
            "titleExpr": mention.raw,
            "titleValue": _value_text(mention.value),
            "unit": mention.family,
            "bodyValues": [_value_text(value) for value in values],
            "bodyExprs": list(dict.fromkeys(row.raw for row in candidates)),
            "preferredExpr": candidates[0].raw if len(values) == 1 else None,
            "reason": "正文同一语境的数值与章名不一致" if anchored else "正文同单位的数值与章名不一致",
        }


def title_body_conflicts(title: str, body: str) -> list[dict]:
    """Report quantity disagreements only when the body contains comparable evidence."""
    return [conflict for _, conflict in _conflicting_mentions(title, body)]


def _replace_expressions(title, replacements):
    # Match complete parsed spans, so, for example, changing 1吨 leaves 11吨 intact.
    pieces, cursor = [], 0
    for measure, replacement in replacements:
        pieces.extend((title[cursor:measure.start], replacement))
        cursor = measure.end
    return "".join((*pieces, title[cursor:]))


def local_retitle(title: str, conflicts: list[dict], body: str) -> str | None:
    matches = [(measure, conflict) for measure, conflict in _conflicting_mentions(title, body) if conflict in conflicts]
    if len(matches) != len(conflicts):
        return None
    replacements = []
    for original, conflict in matches:
        preferred = conflict.get("preferredExpr")
        if not preferred or len(set(conflict["bodyValues"])) != 1:
            return None
        replacement = _measures(preferred)[0]
        fixed = replacement.number + original.unit if original.unit else replacement.raw
        replacements.append((original, fixed))
    candidate = _replace_expressions(title, replacements)
    return candidate if candidate != title and 2 <= len(candidate) <= 30 and not title_body_conflicts(candidate, body) else None


def strip_conflicting_number(title: str, conflicts: list[dict]) -> str | None:
    expressions = {row["titleExpr"] for row in conflicts}
    candidate = _replace_expressions(title, ((row, "") for row in _measures(title) if row.raw in expressions))
    candidate = re.sub(r"[ \t]{2,}", " ", candidate).strip(_EDGE_PUNCTUATION)
    return candidate if candidate != title and len(candidate) >= 2 else None


def clean_retitle(text: str) -> str:
    lines = text.strip().splitlines()
    if len(lines) >= 3 and lines[0].startswith("```") and lines[-1].strip() == "```":
        lines = lines[1:-1]
    if len(lines) != 1:
        return ""
    text = lines[0].strip(_TITLE_WRAPPERS + _EDGE_PUNCTUATION)
    return _HEADING.sub("", text).strip(_TITLE_WRAPPERS + _EDGE_PUNCTUATION)
