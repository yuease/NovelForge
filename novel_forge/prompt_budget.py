"""Bound rendered prompts and distribute optional context by priority."""

import math
import os
import re

from .llm import LLMError, estimate_tokens


DEFAULT_CHAPTER_INPUT_BUDGET = 16_384
DEFAULT_ARC_INPUT_BUDGET = 16_384
DEFAULT_VERIFY_INPUT_BUDGET = 14_336
DEFAULT_REPAIR_INPUT_BUDGET = 12_288
DEFAULT_AUDIT_INPUT_BUDGET = 24_576
CHAPTER_INPUT_RESERVE = 1_024
MAX_STAGE_INPUT_BUDGET = 200_000
ARC_PROMPT_BUDGET_EXCEEDED = "ARC_PROMPT_BUDGET_EXCEEDED"
BUDGET_PROFILES = {
    "balanced": {
        "chapter": DEFAULT_CHAPTER_INPUT_BUDGET,
        "arc": DEFAULT_ARC_INPUT_BUDGET,
        "verify": DEFAULT_VERIFY_INPUT_BUDGET,
        "repair": DEFAULT_REPAIR_INPUT_BUDGET,
        "audit": DEFAULT_AUDIT_INPUT_BUDGET,
    },
    "long_book": {
        "chapter": 24_576,
        "arc": 24_576,
        "verify": 18_432,
        "repair": 16_384,
        "audit": 40_960,
    },
}
_STAGE_BUDGET_ENV = {
    "chapter": ("NOVEL_FORGE_CHAPTER_INPUT_BUDGET", "写章", "CHAPTER_PROMPT_BUDGET_INVALID"),
    "arc": ("NOVEL_FORGE_ARC_INPUT_BUDGET", "滚纲", "ARC_PROMPT_BUDGET_INVALID"),
    "verify": ("NOVEL_FORGE_REVIEW_INPUT_BUDGET", "审校", "REVIEW_PROMPT_BUDGET_INVALID"),
    "repair": ("NOVEL_FORGE_REPAIR_INPUT_BUDGET", "返修", "REPAIR_PROMPT_BUDGET_INVALID"),
    "audit": ("NOVEL_FORGE_AUDIT_INPUT_BUDGET", "终审", "AUDIT_PROMPT_BUDGET_INVALID"),
}
_SPACE = "\u0009-\u000d\u0020\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
_EDGE_SPACE = re.compile(rf"^[{_SPACE}]+|[{_SPACE}]+$")
_LEADING_SPACE = re.compile(rf"^[{_SPACE}]+")
_SENTENCE_END = re.compile(rf"[。！？!?；;]+|…+|\.{{3,}}|\.(?=[{_SPACE}]|$)|\r?\n+")
_DECIMAL = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
_UNSET = object()


def read_chapter_input_budget(raw=_UNSET):
    if raw is _UNSET:
        raw = os.environ.get("NOVEL_FORGE_CHAPTER_INPUT_BUDGET")
    return _read_input_budget(raw, DEFAULT_CHAPTER_INPUT_BUDGET, label="写章", code="CHAPTER_PROMPT_BUDGET_INVALID")


def read_arc_input_budget(raw=_UNSET):
    if raw is _UNSET:
        raw = os.environ.get("NOVEL_FORGE_ARC_INPUT_BUDGET")
    return _read_input_budget(raw, DEFAULT_ARC_INPUT_BUDGET, label="滚纲", code="ARC_PROMPT_BUDGET_INVALID")


def read_audit_input_budget(raw=_UNSET):
    if raw is _UNSET:
        raw = os.environ.get("NOVEL_FORGE_AUDIT_INPUT_BUDGET")
    return _read_input_budget(raw, DEFAULT_AUDIT_INPUT_BUDGET, label="终审", code="AUDIT_PROMPT_BUDGET_INVALID")


def read_review_input_budget(kind="verify", raw=_UNSET):
    env, default, label, code = {
        "verify": ("NOVEL_FORGE_REVIEW_INPUT_BUDGET", DEFAULT_VERIFY_INPUT_BUDGET, "审校", "REVIEW_PROMPT_BUDGET_INVALID"),
        "repair": ("NOVEL_FORGE_REPAIR_INPUT_BUDGET", DEFAULT_REPAIR_INPUT_BUDGET, "返修", "REPAIR_PROMPT_BUDGET_INVALID"),
    }[kind]
    return _read_input_budget(os.environ.get(env) if raw is _UNSET else raw, default, label=label, code=code)


def normalize_budget_profile(raw) -> str | None:
    if not isinstance(raw, str):
        return None
    cleaned = raw.strip().lower()
    return cleaned if cleaned in ("balanced", "long_book", "custom") else None


def normalize_stage_budgets(raw) -> dict[str, int]:
    if not isinstance(raw, dict):
        return {}
    cleaned = {}
    for key, (_, label, code) in _STAGE_BUDGET_ENV.items():
        if key not in raw or raw[key] is None or raw[key] == "":
            continue
        parsed = _read_input_budget(raw[key], BUDGET_PROFILES["balanced"][key], label=label, code=code)
        if parsed is None:
            cleaned[key] = 0
        elif parsed > MAX_STAGE_INPUT_BUDGET:
            raise LLMError(f"{label}输入预算不能超过 {MAX_STAGE_INPUT_BUDGET} tokens。", code=code, status=400)
        else:
            cleaned[key] = parsed
    return cleaned


def resolve_stage_budgets(job=None) -> dict[str, int | None]:
    llm = job.get("llmConfig") if isinstance(job, dict) and isinstance(job.get("llmConfig"), dict) else {}
    profile = normalize_budget_profile(llm.get("budgetProfile")) or "balanced"
    overrides = normalize_stage_budgets(llm.get("stageBudgets"))
    preset = BUDGET_PROFILES["long_book"] if profile == "long_book" else BUDGET_PROFILES["balanced"]
    resolved = {}
    for key, (env_var, label, code) in _STAGE_BUDGET_ENV.items():
        if key in overrides:
            resolved[key] = None if overrides[key] == 0 else overrides[key]
        else:
            env_val = os.environ.get(env_var)
            if env_val is not None and _EDGE_SPACE.sub("", env_val):
                resolved[key] = _read_input_budget(env_val, preset[key], label=label, code=code)
            else:
                resolved[key] = preset[key]
    return resolved


def _read_input_budget(raw, default, *, label, code):
    if raw is None:
        return default
    if isinstance(raw, str):
        raw = _EDGE_SPACE.sub("", raw)
        if not raw:
            return default
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
    raise LLMError(f"{label}输入预算必须是大于 1024 的整数，或设为 0 关闭。", code=code, status=400)


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
    try:
        return fit_prompt(render=render, slots=slots, input_budget=input_budget)
    except LLMError as error:
        if error.code == "CHAPTER_PROMPT_BUDGET_EXCEEDED":
            error.status = 400  # Preserve the chapter prompt's existing error contract.
        raise


def fit_prompt(*, render, slots, input_budget, reserve=CHAPTER_INPUT_RESERVE,
               code="CHAPTER_PROMPT_BUDGET_EXCEEDED", label="写章", on_overflow="raise"):
    """Fit ordered text or entry slots using the final render to measure their cost."""
    limit = input_budget - reserve

    def size(parts):
        return estimate_tokens(f"{parts['system']}\n{parts['prompt']}")

    def over_budget(tokens):
        return LLMError(
            f"{label}提示词需 {tokens} 个估算 Token，当前输入上限为 {limit}（已预留 {reserve}）。",
            code=code, status=422, retryable=False,
        )

    slots = list(slots)
    allocated = {slot["key"]: [] if slot.get("entries") is not None else "" for slot in slots}
    protected = render(allocated)
    mandatory = size(protected)
    if mandatory > limit:
        if on_overflow == "expand":
            return {**protected, "budgetMeta": {"budget": input_budget, "used": mandatory, "expanded": True}}
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
