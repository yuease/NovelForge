"""Select a writing reasoning mode from local manuscript and provider metadata."""

import re
from urllib.parse import urlsplit


_PRECISION = re.compile("历史|考据|朝代|年号|度量衡|换算|测评|硬核|科普|基建|工业|商战|财务|账目|推理|破案|悬疑|权谋|算术|汇率|斤|两|石")
_LEDGER = re.compile(r"台账|账本|账簿|账目|账款|账房|账册|账务|记账|结算|余额|财务|换算|汇率|度量衡|\bledger\b|\baccounts?\b|\bconversion\b", re.I)
_MEASUREMENT = re.compile(r"\d|元|两|斤|石|贯|文|银|币|吨|米|级|层|积分")
_REASONING_ISSUES = {"hard_value_conflict", "world_rule_conflict", "timeline_conflict", "timeline_contradiction"}
_ALWAYS_OPENAI = re.compile(r"^(?:o[134](?:-|$)|gpt-[5-9](?:[.-]|$))", re.I)
_GEMINI_THINKING = re.compile(
    r"^gemini-(?:2\.5|3(?:\.\d+)?|4(?:\.\d+)?|pro-latest|flash-latest|flash-lite-latest)(?:-|$)", re.I,
)
_ARGON_MODEL = re.compile(r"^gemini-4-argon(?:-|$)", re.I)
_UNSUPPORTED_WARNING = "当前题材含重算术或考据要素，但所选模型不支持思考开关，建议切换推理模型。"
_DISABLED_WARNING = "当前题材含重算术或考据要素，但已手动关闭思考模式，可能增加数值或史实出错概率。"

STAGE_THINKING_BUCKETS = ("book_plan", "chapter_outlines", "chapter_writing", "chapter_verify", "archive", "full_audit")
STAGE_THINKING_LEVELS = ("auto", "off", "minimal", "low", "medium", "high")
_STAGE_TO_BUCKET = {
    "book_plan": "book_plan",
    "topic_selection": "book_plan",
    "chapter_outlines": "chapter_outlines",
    "arc_outlines": "chapter_outlines",
    "chapter_writing": "chapter_writing",
    "chapter_repair": "chapter_writing",
    "chapter_verify": "chapter_verify",
    "drift_patch": "chapter_verify",
    "fact_extraction": "archive",
    "arc_archive": "archive",
    "volume_archive": "archive",
    "chapter_retitle": "archive",
    "full_audit": "full_audit",
}


def _dict(value):
    return value if isinstance(value, dict) else {}


def is_openai_gemini_model(provider: str, model: str) -> bool:
    return (provider or "").strip().lower() in ("openai", "openai-compatible") and bool(
        _GEMINI_THINKING.match((model or "").strip())
    )


def preferred_enabled_effort(model: str) -> str:
    """gemini-4-argon only activates reasoning tokens at high; other Gemini models scale from medium."""
    return "high" if _ARGON_MODEL.match((model or "").strip()) else "medium"


def stage_thinking_bucket(stage: str) -> str:
    return _STAGE_TO_BUCKET.get((stage or "").strip(), "chapter_writing")


def normalize_stage_thinking(raw) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    cleaned = {}
    for key, value in raw.items():
        if key in STAGE_THINKING_BUCKETS and isinstance(value, str):
            level = value.strip().lower()
            if level in STAGE_THINKING_LEVELS and level != "auto":
                cleaned[key] = level
    return cleaned


def writing_profile(job, book_plan=None, *, recent_chapters=()) -> dict:
    plan = _dict(job.get("bookPlan") if book_plan is None else book_plan)
    locks = _dict(plan.get("canonLocks"))
    worldview = _dict(plan.get("worldview"))
    premise = str(job.get("premise") or "")
    text = " ".join(str(value or "") for value in (
        job.get("genre"), premise, worldview.get("setting"), worldview.get("coreRules"),
    ))
    signals, score = [], 0
    if _dict(plan.get("styleBible")).get("domainTaboos"):
        score += 2
        signals.append("domainTaboos")
    if any(_MEASUREMENT.search(str(locks.get(key) or "")) for key in ("currencyAndEra", "powerScale")):
        score += 2
        signals.append("quantitativeCanon")
    if (job.get("story_ledger") or job.get("storyLedger") or _dict(job.get("canonicalFacts")).get("ledgerBalances")) and (
        _LEDGER.search(premise) or _LEDGER.search(str(plan))
    ):
        score += 2
        signals.append("ledger")
    keywords = list(dict.fromkeys(_PRECISION.findall(text)))[:3]
    score += len(keywords)
    signals.extend(keywords)
    issues = sum(
        _dict(issue).get("type") in _REASONING_ISSUES
        for chapter in recent_chapters[-10:]
        for issue in _dict(_dict(chapter).get("verification")).get("issues") or ()
    )
    if issues >= 3:
        score += 3
        signals.append("recentVerificationConflicts")
    return {"reasoningHeavy": score >= 4, "score": score, "signals": signals}


def strip_vertex_model_suffix(model=""):
    return re.sub(r"-(?:search|build)$", "", str(model or "gemini-3.8-flash").strip(), flags=re.I)


def resolve_vertex_thinking_config(model=""):
    model = strip_vertex_model_suffix(model).lower()
    if model.startswith("gemini-4-"):
        return {"thinkingLevel": "LOW"}
    if "3.8-flash" in model:
        return {"thinkingBudget": 0}
    if "flash" in model:
        return {"thinkingLevel": "MINIMAL"}
    return None


def thinking_capability(provider: str, model: str, base_url: str = "") -> str:
    provider, model = (provider or "").strip().lower(), (model or "").strip().lower()
    if provider in ("openai", "openai-siwc") and _ALWAYS_OPENAI.match(model):
        return "always"
    if provider in ("openai", "openai-compatible") and _GEMINI_THINKING.match(model):
        return "toggle"
    if provider == "deepseek":
        return "always" if model.endswith("reasoner") else "toggle"
    if provider == "mock":
        return "toggle"
    if provider in ("vertex", "vertex-new"):
        if base_url:
            try:
                host = urlsplit(base_url).hostname or ""
            except ValueError:
                return "none"
            if not re.fullmatch(r"(?:[a-z0-9-]+-)?aiplatform\.googleapis\.com", host):
                return "none"
        thinking = resolve_vertex_thinking_config(model)
        if thinking and thinking.get("thinkingBudget") != 0:
            return "toggle"
    return "none"


def resolve_writing_thinking(config: dict, profile: dict, capability: str) -> dict:
    requested = config.get("thinkingMode") or "disabled"
    source = config.get("thinkingModeSource") or (
        "user" if "thinkingMode" in config and config.get("thinkingMode") != "auto" else "default"
    )
    heavy = profile.get("reasoningHeavy")
    effective, auto_enabled, warning = "disabled", False, None
    if requested == "enabled":
        if capability == "none":
            warning = _UNSUPPORTED_WARNING if heavy else "所选模型不支持思考开关，无法启用思考模式。"
        else:
            effective = "enabled"
    elif requested == "auto" or source == "default" and requested == "disabled":
        if heavy:
            if capability == "none":
                warning = _UNSUPPORTED_WARNING
            else:
                effective = "enabled"
                auto_enabled = capability == "toggle"
    elif source == "user" and requested == "disabled" and heavy:
        warning = _DISABLED_WARNING
    return {"requested": requested, "source": source, "effective": effective,
            "autoEnabled": auto_enabled, "warning": warning}


def resolve_stage_thinking(stage: str, config: dict, *, profile: dict | None = None, capability: str | None = None) -> dict:
    """Resolve per-stage thinkingMode + OpenAI reasoningEffort while preserving legacy defaults."""
    cfg = _dict(config)
    provider = str(cfg.get("provider") or "")
    model = str(cfg.get("model") or "")
    base_url = str(cfg.get("baseUrl") or "")
    cap = capability if capability is not None else thinking_capability(provider, model, base_url)
    bucket = stage_thinking_bucket(stage)
    overrides = normalize_stage_thinking(cfg.get("stageThinking"))
    override = overrides.get(bucket)
    gemini_oai = is_openai_gemini_model(provider, model)
    requested = cfg.get("thinkingMode") or "disabled"
    source = cfg.get("thinkingModeSource") or (
        "user" if "thinkingMode" in cfg and cfg.get("thinkingMode") != "auto" else "default"
    )
    user_disabled = source == "user" and requested == "disabled"

    # 1. Explicit per-stage override takes top priority across all capable providers.
    if override is not None:
        if override in ("off", "minimal"):
            return {"thinkingMode": "disabled", "reasoningEffort": "minimal" if gemini_oai else None}
        if cap == "none":
            return {"thinkingMode": "disabled", "reasoningEffort": None}
        if gemini_oai:
            effort = "high" if (_ARGON_MODEL.match(model) and override in ("medium", "high")) else override
            return {"thinkingMode": "enabled" if effort in ("medium", "high") else "disabled", "reasoningEffort": effort}
        if provider == "openai" and _ALWAYS_OPENAI.match(model):
            return {"thinkingMode": "enabled", "reasoningEffort": override}
        return {"thinkingMode": "enabled", "reasoningEffort": None}

    # 2. OpenAI-compatible Gemini models: full stage-aware reasoning_effort routing.
    if gemini_oai:
        if user_disabled:
            return {"thinkingMode": "disabled", "reasoningEffort": "minimal"}
        if bucket in ("book_plan", "chapter_outlines", "full_audit"):
            return {"thinkingMode": "enabled", "reasoningEffort": preferred_enabled_effort(model)}
        if bucket == "chapter_writing":
            decision = resolve_writing_thinking(cfg, _dict(profile), cap)
            if decision["effective"] == "enabled":
                return {"thinkingMode": "enabled", "reasoningEffort": preferred_enabled_effort(model)}
            return {"thinkingMode": "disabled", "reasoningEffort": "minimal"}
        if bucket == "chapter_verify":
            return {"thinkingMode": "disabled", "reasoningEffort": "low"}
        # archive / fact_extraction / volume_archive / chapter_retitle
        return {"thinkingMode": "disabled", "reasoningEffort": "minimal"}

    # 3. Non-OpenAI-Gemini providers (DeepSeek / Vertex / Mock / standard OpenAI):
    # Preserve existing Stage 3 behavior for default/auto; enable core stages when user explicitly chooses enabled.
    if bucket == "chapter_writing":
        decision = resolve_writing_thinking(cfg, _dict(profile), cap)
        return {"thinkingMode": decision["effective"], "reasoningEffort": None}
    if requested == "enabled" and source == "user" and cap != "none" and bucket in ("book_plan", "chapter_outlines", "full_audit"):
        return {"thinkingMode": "enabled", "reasoningEffort": None}
    return {"thinkingMode": "disabled", "reasoningEffort": None}
