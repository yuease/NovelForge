"""Bound the rendered chapter prompt and distribute optional context by priority."""

import math
import os
import re

from .llm import LLMError, estimate_tokens


DEFAULT_CHAPTER_INPUT_BUDGET = 16_384
CHAPTER_INPUT_RESERVE = 1_024
_SPACE = "\u0009-\u000d\u0020\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
_EDGE_SPACE = re.compile(rf"^[{_SPACE}]+|[{_SPACE}]+$")
_LEADING_SPACE = re.compile(rf"^[{_SPACE}]+")
_SENTENCE_END = re.compile(rf"[。！？!?；;]+|…+|\.{{3,}}|\.(?=[{_SPACE}]|$)|\r?\n+")
_DECIMAL = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
_UNSET = object()


def read_chapter_input_budget(raw=_UNSET):
    if raw is _UNSET:
        raw = os.environ.get("NOVEL_FORGE_CHAPTER_INPUT_BUDGET")
    if raw is None:
        return DEFAULT_CHAPTER_INPUT_BUDGET
    if isinstance(raw, str):
        raw = _EDGE_SPACE.sub("", raw)
        if not raw:
            return DEFAULT_CHAPTER_INPUT_BUDGET
        if _DECIMAL.fullmatch(raw):
            raw = float(raw)
        elif re.fullmatch(r"0(?:[xX][0-9a-fA-F]+|[oO][0-7]+|[bB][01]+)", raw):
            raw = int(raw, 0)
    if isinstance(raw, bool):
        raw = int(raw)
    if raw == 0:
        return None
    if isinstance(raw, (int, float)) and CHAPTER_INPUT_RESERVE < raw <= 2**53 - 1 and int(raw) == raw:
        return int(raw)
    raise LLMError("写章输入预算必须是大于 1024 的整数，或设为 0 关闭。",
                   code="CHAPTER_PROMPT_BUDGET_INVALID", status=400)


def _fit_text(text, from_end, fits):
    if fits(text):
        return text

    def portion(length):
        if not length:
            return ""
        return text[-length:] if from_end else text[:length]

    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if fits(portion(middle)):
            low = middle
        else:
            high = middle - 1
    clipped = portion(low)
    if from_end:
        start = len(text) - len(clipped)
        for mark in _SENTENCE_END.finditer(text):
            if start <= mark.end() < len(text):
                return _LEADING_SPACE.sub("", text[mark.end():])
        return clipped
    end = max((mark.end() for mark in _SENTENCE_END.finditer(clipped)), default=0)
    return clipped[:end] if end else clipped


def _entry_key(entry):
    # JS Set compares objects by identity and primitive values by value.
    if entry is None or isinstance(entry, (str, bool)):
        return type(entry), entry
    if isinstance(entry, (int, float)):
        return float, entry
    return object, id(entry)


def _js_truthy(entry):
    if entry is None or isinstance(entry, (str, bool, int, float)):
        return bool(entry) and not (isinstance(entry, float) and math.isnan(entry))
    return True


def _utf16_length(text):
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def fit_chapter_prompt(*, render, slots, input_budget):
    """Fit ordered text or entry slots using the final render to measure their cost."""
    limit = input_budget - CHAPTER_INPUT_RESERVE

    def size(parts):
        return estimate_tokens(f"{parts['system']}\n{parts['prompt']}")

    def over_budget(tokens):
        return LLMError(
            f"写章提示词需 {tokens} 个估算 Token，当前输入上限为 {limit}（已预留 {CHAPTER_INPUT_RESERVE}）。",
            code="CHAPTER_PROMPT_BUDGET_EXCEEDED", status=400,
        )

    slots = list(slots)
    allocated = {slot["key"]: [] if slot.get("entries") is not None else "" for slot in slots}
    mandatory = size(render(allocated))
    if mandatory > limit:
        raise over_budget(mandatory)
    reserved = {}
    for slot in slots:
        key = slot["key"]
        entries = slot.get("entries")
        if not slot.get("allocate_first") or not entries:
            continue
        first = next((entry for entry in (entries[0], slot.get("first_fallback"))
                      if _js_truthy(entry) and size(render({**allocated, key: [entry]})) <= limit), None)
        if first is not None:
            allocated[key] = [first]
        reserved[key] = [first, *entries[1:]] if first is not None else []
    remaining = limit - size(render(allocated))

    def fill(slot, ceiling):
        key = slot["key"]

        def fits(value):
            return size(render({**allocated, key: value})) <= ceiling

        if slot.get("entries") is not None:
            entries = reserved.get(key, slot["entries"])
            keyed = [(_entry_key(entry), entry) for entry in entries]
            selected = {_entry_key(entry) for entry in allocated[key]}
            for identity, _ in keyed:
                if identity in selected:
                    continue
                selected.add(identity)
                if not fits([entry for item_key, entry in keyed if item_key in selected]):
                    selected.remove(identity)
            allocated[key] = [entry for identity, entry in keyed if identity in selected]
        elif slot.get("text"):
            value = _fit_text(slot["text"], slot.get("from_end", False), fits)
            if _utf16_length(value) >= _utf16_length(allocated[key]):
                allocated[key] = value

    for slot in slots:
        share = math.floor(remaining * slot["share"] / 100)
        fill(slot, min(limit, size(render(allocated)) + share))
    for slot in slots:
        fill(slot, limit)
    result = render(allocated)
    used = size(result)
    if used > limit:
        raise over_budget(used)
    return result
