"""Parse provider reset hints without depending on an HTTP transport."""

import math
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime


LONG_QUOTA_SECONDS = 300

_UNITS = {
    **dict.fromkeys(("d", "day", "days", "天"), 86400),
    **dict.fromkeys(("h", "hr", "hrs", "hour", "hours", "小时", "个小时"), 3600),
    **dict.fromkeys(("m", "min", "mins", "minute", "minutes", "分", "分钟"), 60),
    **dict.fromkeys(("s", "sec", "secs", "second", "seconds", "秒"), 1),
    **dict.fromkeys(("ms", "millisecond", "milliseconds"), 0.001),
}
_UNIT = "|".join(sorted(_UNITS, key=len, reverse=True))
_PART = r"(\d+(?:\.\d+)?)\s*(" + _UNIT + r")(?![a-z])"
_SEPARATOR = r"(?:\s*,\s*|\s+and\s+|\s*)"
_DURATION = re.compile(_PART, re.I)
_AFTER = re.compile(
    r"(?:resets?|try again|retry|available again|wait)\s*(?:again\s*)?(?:in|after)\s+"
    r"(" + _PART + r"(?:" + _SEPARATOR + _PART + r")*)", re.I,
)
_AT = re.compile(
    r"\bresets?\s+(?:at|on)\s+(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}"
    r"(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2}|[ ]+(?:UTC|GMT)))(?![\w:+-])", re.I,
)
_CHINESE = re.compile(
    r"(?<![\d.+-])(\d+(?:\.\d+)?\s*(?:天|个?小时|分钟?|秒)"
    r"(?:\s*\d+(?:\.\d+)?\s*(?:小时|分钟?|秒))*)\s*(?:之后|后)?(?:重置|恢复|再试|可用)",
)
_BILLING = re.compile(
    r"insufficient_quota|exceeded your current quota|billing|余额不足|欠费|credit.*exhausted", re.I,
)
_USAGE = re.compile(
    r"reached your usage limit|usage_limit_exceeded|subscription_sharing_usage_limit_exceeded|"
    r"daily.*limit|per\s*day|\brpd\b|weekly.*limit|monthly.*limit|今日额度|本日额度|"
    r"额度.*(?:用完|用尽|耗尽)|到达?限额", re.I,
)
_RATE = re.compile(r"\brpm\b|\btpm\b|per\s*minute|requests per min|too many requests|并发", re.I)
_JSON_SECONDS = {"retry_after", "retry_after_seconds", "retryAfter", "resets_in_seconds", "reset_seconds"}
_QUOTA_CODES = {"QUOTA_EXCEEDED", "SIWC_USAGE_LIMIT", "QUOTA_SUSPECTED"}


@dataclass(frozen=True)
class ResetHint:
    seconds: float | None
    source: str
    scope: str
    kind: str

    @property
    def retry_after_ms(self) -> int | None:
        if self.seconds is None:
            return None
        milliseconds = self.seconds * 1000
        return int(milliseconds) if math.isfinite(milliseconds) else int(self.seconds) * 1000


def positive_env_int(name, default):
    try:
        value = int(os.environ.get(name, ""))
        if value > 0:
            return value
    except ValueError:
        pass
    return default


def long_quota_threshold():
    return positive_env_int("NOVEL_FORGE_LONG_QUOTA_SECONDS", LONG_QUOTA_SECONDS)


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) and value >= 0 else None


def parse_duration(text: str) -> float | None:
    text = re.sub(r"^in\s+", "", text.strip(), flags=re.I)
    seconds = 0.0
    end = 0
    for part in _DURATION.finditer(text):
        gap = text[end:part.start()]
        if (end == 0 and gap) or re.fullmatch(_SEPARATOR, gap, re.I) is None:
            return None
        seconds += float(part[1]) * _UNITS[part[2].lower()]
        end = part.end()
    return seconds if end == len(text) and math.isfinite(seconds) and seconds > 0 else None


def _utc(now):
    if now is None:
        return datetime.now(timezone.utc)
    return now.astimezone(timezone.utc) if now.tzinfo else now.replace(tzinfo=timezone.utc)


def _iso_datetime(value):
    if not isinstance(value, str):
        return None
    value = re.sub(r"\s+(?:UTC|GMT)$", "+00:00", value.strip(), flags=re.I)
    if value.endswith("z"):
        value = value[:-1] + "Z"
    try:
        stamp = datetime.fromisoformat(value)
        return stamp if stamp.tzinfo is not None else None
    except ValueError:
        return None


def seconds_until(retry_at, now=None):
    stamp = _iso_datetime(retry_at)
    return (stamp - _utc(now)).total_seconds() if stamp is not None else None


def _absolute_wait(value, now):
    number = _number(value)
    if number is not None:
        stamp = number / 1000 if number >= 1e12 else number
        seconds = stamp - now.timestamp()
    else:
        seconds = seconds_until(value, now)
    return seconds if seconds is not None and seconds >= 0 else None


def _header_wait(headers, now):
    headers = {key.lower(): value for key, value in (headers or {}).items()}
    retry_after = headers.get("retry-after")
    seconds = _number(retry_after)
    if seconds is None and retry_after:
        try:
            stamp = parsedate_to_datetime(retry_after)
            seconds = (_utc(stamp) - now).total_seconds()
        except (TypeError, ValueError, OverflowError):
            pass
    if seconds is not None and seconds >= 0:
        return seconds, "header:retry-after"
    milliseconds = _number(headers.get("retry-after-ms"))
    if milliseconds is not None:
        return milliseconds / 1000, "header:retry-after-ms"

    resets = []
    for name, value in headers.items():
        if name == "x-ratelimit-reset":
            remaining = "x-ratelimit-remaining"
        elif name.startswith("x-ratelimit-reset-"):
            remaining = name.replace("-reset-", "-remaining-", 1)
        elif name.startswith("anthropic-ratelimit-") and name.endswith("-reset"):
            remaining = name.removesuffix("-reset") + "-remaining"
        else:
            continue
        if (_number(headers.get(remaining)) or 0) > 0:
            continue
        seconds = _number(value)
        if seconds is None and isinstance(value, str):
            seconds = parse_duration(value)
            if seconds is None:
                seconds = _absolute_wait(value, now)
        elif seconds is not None and seconds >= 1e9:
            seconds = _absolute_wait(seconds, now)
        if seconds is not None:
            resets.append(seconds)
    return (max(resets), "header:x-ratelimit-reset") if resets else (None, "none")


def _json_fields(payload, depth=0):
    if depth > 4:
        return
    if isinstance(payload, dict):
        yield from payload.items()
        for value in payload.values():
            yield from _json_fields(value, depth + 1)
    elif isinstance(payload, list):
        for value in payload:
            yield from _json_fields(value, depth + 1)


def _json_wait(fields, now):
    for key, value in fields:
        if key == "retryDelay":
            seconds = parse_duration(value) if isinstance(value, str) else None
            if seconds is not None:
                return seconds, "json:retryDelay"
        elif key in {"resets_at", "reset_at"}:
            seconds = _absolute_wait(value, now)
            if seconds is not None:
                return seconds, "json:field"
        elif key in _JSON_SECONDS or key == "retry_after_ms":
            seconds = _number(value)
            if seconds is not None:
                return (seconds / 1000 if key == "retry_after_ms" else seconds), "json:field"
    return None, "none"


def _text_wait(text, now):
    for pattern, source in ((_AFTER, "text:duration"), (_AT, "text:absolute"), (_CHINESE, "text:zh")):
        for match in pattern.finditer(text):
            seconds = seconds_until(match[1], now) if pattern is _AT else parse_duration(match[1])
            if seconds is not None and seconds >= 0:
                return seconds, source
    return None, "none"


def parse_reset_hint(*, headers=None, text="", payload=None, now=None, long_threshold=None) -> ResetHint:
    now = _utc(now)
    threshold = long_quota_threshold() if long_threshold is None else long_threshold
    fields = list(_json_fields(payload))
    messages = (value for key, value in fields if key in {"message", "code", "type", "error", "status", "reason"}
                and isinstance(value, str))
    evidence = text[:8000] + "\n" + "\n".join(value[:8000] for value in messages)[:8000]
    seconds, source = _header_wait(headers, now)
    if seconds is None:
        seconds, source = _json_wait(fields, now)
    if seconds is None:
        seconds, source = _text_wait(evidence, now)
    if _BILLING.search(evidence):
        scope, kind = "long", "billing"
    elif seconds is not None:
        scope, kind = ("long", "usage") if seconds > threshold else ("short", "rate")
    elif _USAGE.search(evidence):
        scope, kind = "long", "usage"
    elif _RATE.search(evidence):
        scope, kind = "short", "rate"
    else:
        scope, kind = "unknown", "unknown"
    return ResetHint(seconds, source, scope, kind)


def retry_at_iso(seconds: float | None, now=None) -> str | None:
    seconds = _number(seconds)
    if seconds:
        try:
            return (_utc(now) + timedelta(seconds=seconds)).isoformat(timespec="seconds").replace("+00:00", "Z")
        except OverflowError:
            pass
    return None


def quota_retry_at(hint, now=None):
    if hint.kind == "billing" and (hint.seconds or 0) <= long_quota_threshold():
        return None
    return retry_at_iso(hint.seconds, now)


def humanize_wait(seconds: float | None) -> str:
    seconds = _number(seconds)
    if not seconds:
        return ""
    seconds = math.ceil(seconds)
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    if days:
        return f"约 {days} 天" + (f" {hours} 小时" if hours else "")
    if hours >= 2:
        minutes = remainder // 60
        return f"约 {hours} 小时" + (f" {minutes} 分" if minutes else "")
    if seconds >= 60:
        return f"约 {math.ceil(seconds / 60)} 分钟"
    return f"约 {seconds} 秒"


def is_long_quota(error, *, threshold_ms=None) -> bool:
    if threshold_ms is None:
        threshold_ms = long_quota_threshold() * 1000
    return (
        getattr(error, "code", None) in _QUOTA_CODES or getattr(error, "quota_scope", None) == "long"
        or (getattr(error, "status", None) == 429 and (getattr(error, "retry_after_ms", None) or 0) > threshold_ms)
    )
