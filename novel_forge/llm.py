"""Asynchronous LLM transports and retry policies used by the writing pipeline."""

import asyncio
import ipaddress
import json
import logging
import math
import os
import random
import re
import threading
import time
import weakref
from asyncio import sleep as _sleep
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit

import httpx

from .arc_planning import CANON_LOCKS_MARKER
from .book_plan import PLAN_VERSION_V2, PLAN_V2_MARKER
from .openai_responses import call_responses
from .openai_siwc import PROVIDER as SIWC, RESOURCE as SIWC_RESOURCE, SiwcError, all_stages as siwc_all_stages
from .providers import get_provider_definition, is_masked_api_key, provider_requires_key, same_endpoint_origin
from .rate_limits import is_long_quota, parse_reset_hint, quota_retry_at, seconds_until
from .thinking_policy import (
    is_openai_gemini_model, preferred_enabled_effort, resolve_vertex_thinking_config, strip_vertex_model_suffix,
)


DEFAULT_TIMEOUT_MS = 300_000
DEFAULT_MAX_TOKENS = 65_536
DEFAULT_RETRIES = 5
STYLE_BIBLE_MARKER = "[BOOK_PLAN_STYLE_BIBLE]"
STORY_LEDGER_MARKER = "[STORY_LEDGER]"
_CJK = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")
_JS_SPACE = re.compile(r"[\u0009-\u000d\u0020\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+")
_URI_COMPONENT_SAFE = "-_.!~*'()"
_VERTEX_SAFETY_SETTINGS = [
    {"category": f"HARM_CATEGORY_{category}", "threshold": "BLOCK_NONE"}
    for category in ("HARASSMENT", "HATE_SPEECH", "SEXUALLY_EXPLICIT", "DANGEROUS_CONTENT")
]
log = logging.getLogger(__name__)


class LLMError(RuntimeError):
    def __init__(self, message, *, code="LLM_ERROR", status=500, retryable=False,
                 finish_reason=None, retry_after_ms=None, retry_at=None,
                 circuit_retry_at=None, request_max_tokens=None, rejected_output_tokens=None,
                 quota_scope=None, quota_kind=None, reset_source=None, provider_code=None, no_replay=False):
        super().__init__(message)
        self.code = code
        self.status = status
        self.retryable = retryable
        self.finish_reason = finish_reason
        self.retry_after_ms = retry_after_ms
        self.retry_at = retry_at
        self.circuit_retry_at = circuit_retry_at
        self.request_max_tokens = request_max_tokens
        self.rejected_output_tokens = rejected_output_tokens
        self.quota_scope = quota_scope
        self.quota_kind = quota_kind
        self.reset_source = reset_source
        self.provider_code = provider_code
        self.no_replay = no_replay


def _siwc_llm_error(error):
    return LLMError(str(error), code=error.code, status=error.status, retryable=error.retryable,
                    retry_after_ms=error.retry_after_ms, retry_at=error.retry_at,
                    quota_scope=error.quota_scope, quota_kind=error.quota_kind, reset_source=error.reset_source,
                    no_replay=error.no_replay)


def estimate_tokens(text=""):
    if not text:
        return 0
    text = str(text)
    cjk = len(_CJK.findall(text))
    words = 0
    for chunk in _JS_SPACE.split(_CJK.sub(" ", text)):
        if not chunk:
            continue
        if len(chunk) <= 12:
            words += 1
        elif chunk.isascii() and chunk.isalpha():
            words += max(1, math.ceil(len(chunk) / 4))
        else:
            words += max(1, math.ceil(len(chunk) / 2.2))
    return max(1, math.ceil(cjk * 1.5 + words * 1.3))


def output_was_truncated(reason):
    if reason is None:
        return False
    return str(reason).strip().lower() in {"length", "max_tokens", "max_output_tokens", "token_limit"}


def _guard_output():
    return os.environ.get("NOVEL_FORGE_OUTPUT_GUARD") != "0"


def protected_output_budget(previous, increased):
    return increased if _guard_output() else previous


def next_output_budget(current, rejected_at=math.inf):
    upper = min(DEFAULT_MAX_TOKENS, rejected_at - 1)
    if type(current) is not int or not 0 < current < upper:
        return None
    doubled = min(2 * current, DEFAULT_MAX_TOKENS)
    return doubled if doubled <= upper else (current + upper + 1) // 2


def _env_positive_int(name, default):
    try:
        value = float(os.environ.get(name, ""))
        if value.is_integer() and 0 < value <= 2**53 - 1:
            return int(value)
    except ValueError:
        pass
    return default


@dataclass(frozen=True)
class _Credentials:
    provider: str
    base_url: str
    api_key: str
    model: str
    thinking_mode: str
    siwc: object = field(default=None, repr=False)
    siwc_epoch: int = 0


async def _resolve_credentials(provider, base_url, api_key, model, thinking_mode, use_saved_connection, store):
    provider = str(provider or "mock").lower()
    if provider == "mock":
        return _Credentials(provider, "", "", model or "mock-novel-v1", thinking_mode or "disabled")
    definition = get_provider_definition(provider) or {}
    if store is None:
        presets = definition.get("models") or [""]
        stored = {
            "baseUrl": definition.get("baseUrl", ""),
            "apiKey": os.environ.get(definition["env"], "") if definition.get("env") else "",
            "model": "gemini-3.8-flash" if provider == "vertex" else presets[0],
        }
    else:
        try:
            stored = await asyncio.to_thread(store.get_provider_config, provider)
        except (OSError, ValueError):
            stored = {}
    resolved_base = (base_url if not use_saved_connection else "") or stored.get("baseUrl") or ""
    if not use_saved_connection and api_key and not is_masked_api_key(api_key):
        key = api_key.strip()
    elif same_endpoint_origin(resolved_base, stored.get("baseUrl")):
        key = stored.get("apiKey") or ""
    else:
        key = ""
    clean_model = str(model).strip() if model and model != "mock-novel-v1" else ""
    return _Credentials(provider, resolved_base, key, clean_model or stored.get("model") or "",
                        thinking_mode or stored.get("thinkingMode") or "disabled")


_CGNAT_NET = ipaddress.ip_network("100.64.0.0/10")


def _is_local(base_url):
    return bool(re.match(r"^https?://(?:127\.0\.0\.1|localhost)(?::|/|$)", base_url, re.I))


def _is_private_or_local(base_url):
    if _is_local(base_url):
        return True
    try:
        host = (urlsplit(base_url or "").hostname or "").strip().lower()
    except ValueError:
        return False
    if not host:
        return False
    if host in ("localhost", "localhost.localdomain") or host.endswith(".local") or host.endswith(".ts.net"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return bool(ip.is_loopback or ip.is_private or ip.is_link_local or (ip.version == 4 and ip in _CGNAT_NET))


def _circuit_key(creds):
    if creds.provider == "mock":
        return None
    if not creds.base_url:
        return creds.provider, ""
    try:
        url = httpx.URL(creds.base_url)
        if url.scheme not in ("http", "https") or not url.host:
            return None
        if url.port == (443 if url.scheme == "https" else 80):
            url = url.copy_with(port=None)
        return creds.provider, str(url.copy_with(username=None, password=None, query=None, fragment=None)).rstrip("/")
    except (httpx.InvalidURL, ValueError):
        return None


@dataclass
class _Circuit:
    failures: int = 0
    open_until: float = 0
    probing: bool = False
    epoch: int = 0
    waiters: set[asyncio.Future[str | None]] = field(default_factory=set)


_circuits = {}
_state_lock = threading.Lock()
_quota_blocks: dict[tuple[tuple[str, str], str], dict] = {}
_UNSUPPORTED_THINKING_KEYS: set[tuple[str, str, str]] = set()


def reset_quota_blocks():
    with _state_lock:
        _quota_blocks.clear()


def _check_quota_block(key):
    if key is None:
        return
    with _state_lock:
        block = _quota_blocks.get(key)
        if block is None:
            return
        remaining = seconds_until(block["retry_at"])
        if remaining is None or remaining <= 0:
            _quota_blocks.pop(key)
            return
    error = LLMError("当前模型的用量额度暂未恢复，请等待恢复或切换模型后继续。",
                     code="QUOTA_EXCEEDED", status=429, retry_after_ms=math.ceil(remaining * 1000),
                     quota_scope="long", **block)
    error.provider, error.model = key[0][0], key[1]
    raise error


def _remember_quota_block(key, error):
    if key is None or (seconds_until(error.retry_at) or 0) <= 0:
        return
    with _state_lock:
        existing = _quota_blocks.get(key)
        if existing is None or error.retry_at > existing["retry_at"]:
            _quota_blocks[key] = {
                "retry_at": error.retry_at, "quota_kind": error.quota_kind, "reset_source": error.reset_source,
                "provider_code": error.provider_code,
            }


def reset_thinking_fallback_cache():
    with _state_lock:
        _UNSUPPORTED_THINKING_KEYS.clear()


def reset_provider_circuit_state():
    with _state_lock:
        for state in _circuits.values():
            _wake_probe_waiters(state)
        _circuits.clear()


def _clock():
    return time.monotonic()


def _as_iso_retry_at(monotonic_deadline):
    stamp = time.time() + max(0, monotonic_deadline - _clock())
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _circuit_error(retry_at):
    return LLMError(f"供应商连接已熔断，冷却截至 {retry_at}。可在冷却后从断点继续，或切换连接。",
                    code="CIRCUIT_OPEN", status=503, retryable=True, retry_at=retry_at)


def _check_cooldown(state):
    if state is not None and state.open_until > _clock():
        raise _circuit_error(_as_iso_retry_at(state.open_until))


def _check_circuit(key):
    if key is not None:
        with _state_lock:
            _check_cooldown(_circuits.get(key))


def _enter_circuit(key, *, claim=False):
    if key is None:
        return None, None
    with _state_lock:
        state = _circuits.get(key)
        _check_cooldown(state)
        if state is not None and state.probing:
            future = asyncio.get_running_loop().create_future()
            state.waiters.add(future)
            return None, (state, future)
        if not claim:
            return None, None
        if state is None:
            state = _circuits[key] = _Circuit()
        probing = state.open_until > 0
        if probing:
            state.probing = True
        return (key, state, state.epoch, probing), None


async def _wait_for_probe(waiter):
    state, future = waiter
    try:
        retry_at = await future
    finally:
        with _state_lock:
            state.waiters.discard(future)
    if retry_at is not None:
        raise _circuit_error(retry_at)


def _complete_probe_waiter(future, retry_at):
    if not future.done():
        future.set_result(retry_at)


def _wake_probe_waiters(state, retry_at=None):
    # Each future is delivered on its own loop. Waiting callers can cancel independently.
    for future in state.waiters:
        try:
            future.get_loop().call_soon_threadsafe(_complete_probe_waiter, future, retry_at)
        except RuntimeError:
            # A waiter may have closed its loop while a probe on another loop was in flight.
            pass
    state.waiters.clear()


def _settle_circuit(claim, error=None, *, cancelled=False):
    if claim is None:
        return
    key, state, epoch, probing = claim
    with _state_lock:
        if _circuits.get(key) is not state or state.epoch != epoch:
            return
        state.probing = False
        if cancelled or (error is not None and is_long_quota(error)):
            _wake_probe_waiters(state)
            return
        if error is None or error.code in {"EMPTY_RESPONSE", "INVALID_RESPONSE", "UNEXPECTED_TOOL_CALL", "TOOLS_UNSUPPORTED", "CONTENT_BLOCKED"}:
            state.failures = 0
            state.open_until = 0
            _wake_probe_waiters(state)
            return
        retry_at = None
        failed = error.code in {"NETWORK_ERROR", "TIMEOUT"} or (
            error.code.startswith("HTTP_") and (error.status == 408 or error.status >= 500)
        )
        if failed:
            state.failures += 1
            if state.failures >= 3 or probing:
                cooldown = _env_positive_int("NOVEL_FORGE_PROVIDER_CIRCUIT_COOLDOWN_MS", 60_000)
                state.open_until = _clock() + cooldown / 1000
                state.epoch += 1
                retry_at = error.circuit_retry_at = _as_iso_retry_at(state.open_until)
        elif not probing and (error.status == 429 or re.fullmatch(r"HTTP_4\d\d", error.code)):
            state.failures = 0
        _wake_probe_waiters(state, retry_at)


@dataclass
class _RateGate:
    last_started: float = -math.inf
    cooldown_until: float = 0
    lock_ref: weakref.ReferenceType | None = None

    def lock(self):
        lock = self.lock_ref() if self.lock_ref else None
        if lock is None:
            lock = asyncio.Lock()
            self.lock_ref = weakref.ref(lock)
        return lock


_rate_gates = weakref.WeakKeyDictionary()


@asynccontextmanager
async def _rate_slot(creds, transport, allow_parallel):
    if _is_local(creds.base_url):
        yield
        return
    loop = asyncio.get_running_loop()
    rate_key = creds.provider, creds.model.lower()
    with _state_lock:
        gate = _rate_gates.setdefault(loop, {}).setdefault(rate_key, _RateGate())
    minimum_gap = 0.25
    if transport == "vertex":
        minimum_gap = 2.5 if re.search(r"argon|pro", creds.model, re.I) else 0.9
    lock = gate.lock()
    held = False
    try:
        await lock.acquire()
        held = True
        while (remaining := max(gate.cooldown_until, gate.last_started + minimum_gap) - _clock()) > 0:
            await _sleep(remaining)
        gate.last_started = _clock()
        if allow_parallel:
            lock.release()
            held = False
        yield
    except LLMError as error:
        if error.code == "HTTP_429" and not is_long_quota(error):
            penalty = max(15, (error.retry_after_ms or 0) / 1000)
            gate.cooldown_until = max(gate.cooldown_until, _clock() + penalty)
        raise
    finally:
        if held:
            lock.release()


_CONTENT_BLOCK_RE = re.compile(
    r"blocked by gemini'?s filters|"
    r"因内容策略结束生成|"
    r"\bPROHIBITED_CONTENT\b|"
    r"\bcontent_filter\b|"
    r"content[ _-]?management[ _-]?policy|"
    r"safety[ _-]?filters?|"
    r"内容安全策略|敏感内容拦截|内容违规",
    re.I,
)


def _http_error(label, response, api_key, retry_rate_limit):
    raw_text = response.text
    if api_key:
        raw_text = raw_text.replace(api_key, "[已隐藏密钥]")
    snippet = raw_text[:300]
    status = response.status_code
    if status in (400, 403, 422, 451, 500, 502, 503) and _CONTENT_BLOCK_RE.search(raw_text[:2000]):
        return LLMError(
            f"{label} 触发上游内容安全拦截，已停止重试（不触发熔断）。建议微调当前章节章纲敏感词或切换模型后从断点继续：{snippet}",
            code="CONTENT_BLOCKED", status=status, retryable=False,
        )
    if status != 429:
        return LLMError(f"{label} API error {status}: {snippet}", code=f"HTTP_{status}",
                        status=status, retryable=status >= 500)

    try:
        payload = json.loads(raw_text)
    except (ValueError, RecursionError):
        payload = None
    hint = parse_reset_hint(headers=response.headers, text=raw_text[:8000], payload=payload)
    details = _object(_object(payload).get("error")) or _object(payload)
    metadata = dict(retry_after_ms=hint.retry_after_ms, quota_scope=hint.scope, quota_kind=hint.kind,
                    reset_source=hint.source, provider_code=_text(details.get("code")) or None)
    if hint.scope == "long":
        return LLMError(f"{label} 额度已耗尽 (429): {snippet}（可在监控页切换模型或提供商后继续续跑）",
                        code="QUOTA_EXCEEDED", status=429, retry_at=quota_retry_at(hint), **metadata)
    return LLMError(f"{label} 触发限流 (429): {snippet}",
                    code="HTTP_429" if retry_rate_limit else "OPENAI_RATE_LIMIT",
                    status=429, retryable=retry_rate_limit, **metadata)


def _rejects_output_limit(error):
    return error.status in (400, 422) and re.search(
        r"\b(?:max_(?:completion_|output_)?tokens|maxOutputTokens|(?:completion|output)[ -]tokens)\b",
        str(error), re.I,
    ) and re.search(
        r"\b(?:at most|between|exceed\w*|maximum|range|less than|too (?:large|high)|upper bound|up to)\b|不得超过|上限",
        str(error), re.I,
    )


def _rejects_thinking(text):
    if _CONTENT_BLOCK_RE.search(text):
        return False
    return bool(
        re.search(r"\b(?:thinking|reasoning)\w*", text, re.I)
        or re.fullmatch(r"\s*(?:unknown|invalid|unsupported)\s+(?:field|parameter|argument)\.?\s*", text, re.I)
    )


def _truncated_error(reason, text, budget, rejected_at, task):
    detail = "可见输出被截断" if text.strip() else "达到输出上限后没有可见正文，可能是思考用量占满预算"
    stage = f"【{task}】" if task else ""
    return LLMError(f"{stage}{detail}（{budget} tokens，{reason}）。可以调整模型或渠道后从断点继续。",
                    code="OUTPUT_TRUNCATED", status=502, finish_reason=reason, request_max_tokens=budget,
                    rejected_output_tokens=rejected_at if math.isfinite(rejected_at) else None)


def _encode_component(value):
    return quote(value, safe=_URI_COMPONENT_SAFE)


def build_vertex_endpoint(base_url, model=""):
    root = (base_url or get_provider_definition("vertex")["baseUrl"]).rstrip("/")
    if root.endswith(":generateContent"):
        return root
    if not root.endswith("/models"):
        root += "/models"
    return f"{root}/{_encode_component(strip_vertex_model_suffix(model))}:generateContent"


@asynccontextmanager
async def _http_client(client, creds, transport):
    if creds.provider == SIWC:
        yield None
        return
    if client is not None:
        yield client
        return
    options = {"trust_env": False, "timeout": None}
    if transport == "vertex" and not _is_private_or_local(creds.base_url):
        proxy = next((os.environ[var] for var in ("VERTEX_PROXY", "HTTPS_PROXY", "HTTP_PROXY") if os.environ.get(var)), "")
        if proxy:
            options["proxy"] = proxy
    try:
        owned = httpx.AsyncClient(**options)
    except (ImportError, ValueError):
        raise LLMError("代理配置无法使用，请检查代理地址与协议。", code="CONFIG_REQUIRED", status=400) from None
    async with owned:
        yield owned


async def _post(client, endpoint, body, headers):
    try:
        request = httpx.Request("POST", endpoint, json=body, headers=headers,
                                extensions={"timeout": httpx.Timeout(None).as_dict()})
        return await client.send(request, auth=None, follow_redirects=False)
    except httpx.TimeoutException:
        raise LLMError("LLM 网络请求超时。", code="TIMEOUT", status=408, retryable=True) from None
    except httpx.RequestError as error:
        if isinstance(error, (httpx.UnsupportedProtocol, httpx.LocalProtocolError)):
            raise LLMError("供应商的 API 地址或请求配置无效。", code="CONFIG_REQUIRED", status=400) from None
        raise LLMError(f"网络请求失败（{type(error).__name__}）。", code="NETWORK_ERROR", status=502, retryable=True) from None
    except httpx.InvalidURL:
        raise LLMError("供应商的 API 地址无效。", code="CONFIG_REQUIRED", status=400) from None


def _object(value):
    return value if isinstance(value, dict) else {}


def _records(value):
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _head(value):
    entries = _records(value)
    return entries[0] if entries else {}


def _text(value):
    return value if isinstance(value, str) else ""


def _stat(usage, name, fallback=0):
    value = usage.get(name)
    return value if type(value) in (int, float) and math.isfinite(value) else fallback


def _unexpected_tool_call(finish_reason):
    return str(finish_reason or "").strip().lower() in ("unexpected_tool_call", "malformed_function_call")


def _rejects_tools(status, text):
    return status in (400, 404, 422, 501) and bool(
        re.search(r"\b(?:tools?|tool_choice|toolConfig|functionDeclarations?|function_call(?:ing)?)\b", text or "", re.I)
    )


def _parse_tool_args(raw):
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except (ValueError, UnicodeError, RecursionError):
            pass
    return {}


def _strip_gemini_schema(schema):
    if isinstance(schema, list):
        return [_strip_gemini_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    cleaned = {}
    for key, value in schema.items():
        if key == "additionalProperties":
            continue
        cleaned[key] = _strip_gemini_schema(value)
    return cleaned


def _messages_estimate_text(messages, prompt):
    if not messages:
        return prompt
    parts = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, str) and content:
            parts.append(content)
        for call in msg.get("toolCalls") or []:
            if isinstance(call, dict):
                parts.append(str(call.get("name") or ""))
                raw = call.get("rawArguments")
                parts.append(raw if isinstance(raw, str) else json.dumps(call.get("arguments") or {}, ensure_ascii=False))
    return "\n".join(parts) if parts else prompt


def _build_openai_messages(system, prompt, messages, transport):
    out = [{"role": "system", "content": system}] if system else []
    if not messages:
        out.append({"role": "user", "content": prompt})
        return out
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role == "user":
            out.append({"role": "user", "content": str(msg.get("content") or "")})
        elif role == "assistant":
            state = msg.get("providerState")
            if (isinstance(state, dict) and state.get("transport") == transport
                    and isinstance(state.get("message"), dict)):
                raw = state["message"]
                replayed = {k: raw[k] for k in ("content", "tool_calls", "reasoning_content", "extra_content") if k in raw}
                replayed["role"] = "assistant"
                out.append(replayed)
                continue
            entry = {"role": "assistant", "content": msg.get("content") or ("" if not msg.get("toolCalls") else None)}
            calls = []
            for call in msg.get("toolCalls") or []:
                if not isinstance(call, dict) or not call.get("id") or not call.get("name"):
                    continue
                raw_args = call.get("rawArguments")
                if not isinstance(raw_args, str):
                    raw_args = json.dumps(call.get("arguments") or {}, ensure_ascii=False)
                calls.append({
                    "id": str(call["id"]),
                    "type": "function",
                    "function": {"name": str(call["name"]), "arguments": raw_args},
                })
            if calls:
                entry["tool_calls"] = calls
            out.append(entry)
        elif role == "tool":
            out.append({
                "role": "tool",
                "tool_call_id": str(msg.get("toolCallId") or ""),
                "content": str(msg.get("content") or ""),
            })
    return out


def _build_anthropic_messages(prompt, messages):
    if not messages:
        return [{"role": "user", "content": prompt}]
    out = []
    pending_tools = []

    def flush_tools():
        if pending_tools:
            out.append({"role": "user", "content": list(pending_tools)})
            pending_tools.clear()

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role == "tool":
            pending_tools.append({
                "type": "tool_result",
                "tool_use_id": str(msg.get("toolCallId") or ""),
                "content": str(msg.get("content") or ""),
            })
            continue
        flush_tools()
        if role == "user":
            out.append({"role": "user", "content": str(msg.get("content") or "")})
        elif role == "assistant":
            state = msg.get("providerState")
            if (isinstance(state, dict) and state.get("transport") == "anthropic"
                    and isinstance(state.get("content"), list) and state["content"]):
                out.append({"role": "assistant", "content": state["content"]})
                continue
            blocks = []
            if msg.get("content"):
                blocks.append({"type": "text", "text": str(msg["content"])})
            for call in msg.get("toolCalls") or []:
                if isinstance(call, dict) and call.get("id") and call.get("name"):
                    blocks.append({
                        "type": "tool_use",
                        "id": str(call["id"]),
                        "name": str(call["name"]),
                        "input": _parse_tool_args(call.get("arguments")),
                    })
            if blocks:
                out.append({"role": "assistant", "content": blocks})
    flush_tools()
    return out or [{"role": "user", "content": prompt}]


def _build_gemini_contents(prompt, messages, transport):
    if not messages:
        return [{"role": "user", "parts": [{"text": prompt}]}]
    out = []
    pending_tools = []

    def flush_tools():
        if pending_tools:
            out.append({"role": "user", "parts": list(pending_tools)})
            pending_tools.clear()

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role == "tool":
            pending_tools.append({
                "functionResponse": {
                    "name": str(msg.get("name") or "tool"),
                    "response": {"result": str(msg.get("content") or "")},
                },
            })
            continue
        flush_tools()
        if role == "user":
            out.append({"role": "user", "parts": [{"text": str(msg.get("content") or "")}]})
        elif role == "assistant":
            state = msg.get("providerState")
            if (isinstance(state, dict) and state.get("transport") == transport
                    and isinstance(state.get("parts"), list) and state["parts"]):
                out.append({"role": "model", "parts": state["parts"]})
                continue
            parts = []
            if msg.get("content"):
                parts.append({"text": str(msg["content"])})
            for call in msg.get("toolCalls") or []:
                if isinstance(call, dict) and call.get("name"):
                    parts.append({
                        "functionCall": {
                            "name": str(call["name"]),
                            "args": _parse_tool_args(call.get("arguments")),
                        },
                    })
            if parts:
                out.append({"role": "model", "parts": parts})
    flush_tools()
    return out or [{"role": "user", "parts": [{"text": prompt}]}]


def _reply(text, finish_reason, usage, prompt_key, completion_key, total_key, system, prompt, *,
           cache_hit_tokens=0, thoughts_tokens=None, zero_total_is_missing=False,
           tool_calls=None, assistant_message=None, include_turn=False):
    input_tokens = _stat(usage, prompt_key, estimate_tokens(system + prompt))
    output_tokens = _stat(usage, completion_key, estimate_tokens(text)) + (thoughts_tokens or 0)
    total = _stat(usage, total_key, input_tokens + output_tokens)
    if zero_total_is_missing and not total:
        total = input_tokens + output_tokens
    counted = {"promptTokens": input_tokens, "completionTokens": output_tokens, "totalTokens": total,
               "cacheHitTokens": cache_hit_tokens}
    if thoughts_tokens is not None:
        counted["thoughtsTokens"] = thoughts_tokens
    result = {"text": text, "finishReason": finish_reason, "cacheHitTokens": cache_hit_tokens, "usage": counted}
    if include_turn or tool_calls:
        result["toolCalls"] = list(tool_calls or [])
        result["assistantMessage"] = assistant_message or {
            "role": "assistant", "content": text, "toolCalls": list(tool_calls or []),
        }
    return result


async def _call_transport(client, transport, creds, system, prompt, temperature, max_tokens, *,
                          messages=None, tools=None, reasoning_effort=None, cache_key=None):
    provider, api_key = creds.provider, creds.api_key
    include_turn = messages is not None or tools is not None
    estimate_input = _messages_estimate_text(messages, prompt)
    if provider == SIWC:
        try:
            response = await call_responses(
                creds.siwc, creds.model, system, prompt, creds.siwc_epoch,
                messages=messages, tools=tools, reasoning_effort=reasoning_effort, cache_key=cache_key,
            )
        except SiwcError as error:
            if tools and error.code == "SIWC_CAPABILITY_UNSUPPORTED" and "（tools）" in str(error):
                raise LLMError("ChatGPT 当前模型不支持原生工具调用。", code="TOOLS_UNSUPPORTED", status=400) from None
            raise _siwc_llm_error(error) from None
        text = response["text"]
        finish = response["finishReason"]
        tool_calls = response.get("toolCalls") or []
        raw_output = response.get("rawOutput") or []
        if output_was_truncated(finish):
            raise LLMError("ChatGPT 输出被截断，请缩小任务范围或换模型后继续。",
                           code="OUTPUT_TRUNCATED", status=502, finish_reason=finish)
        usage = _object(response.get("usage"))
        assistant_msg = {
            "role": "assistant",
            "content": text,
            "toolCalls": tool_calls,
            "providerState": {"transport": "openai-responses-siwc", "output": raw_output},
        }
        reply = _reply(text, finish, usage, "input_tokens", "output_tokens", "total_tokens", system, estimate_input,
                       cache_hit_tokens=_stat(_object(usage.get("input_tokens_details")), "cached_tokens"),
                       tool_calls=tool_calls, assistant_message=assistant_msg, include_turn=include_turn)
        reply["usage"]["thoughtsTokens"] = _stat(_object(usage.get("output_tokens_details")), "reasoning_tokens")
        return reply
    root = creds.base_url.rstrip("/")
    if not root:
        raise LLMError("请先保存供应商的 API 地址。", code="CONFIG_REQUIRED", status=400)
    headers = {}
    label = get_provider_definition(provider)["label"]
    thinking_field = None
    thinking_key = (provider, creds.base_url, creds.model)
    with _state_lock:
        allow_thinking_field = thinking_key not in _UNSUPPORTED_THINKING_KEYS
    if transport in ("openai", "deepseek"):
        endpoint = f"{root}/chat/completions"
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        model = creds.model or ("deepseek-flash" if transport == "deepseek" else "gpt-4o-mini")
        wire_messages = _build_openai_messages(system, prompt, messages, transport)
        modern = provider == "openai" and re.match(r"^(?:gpt-[5-9](?:[.-]|$)|o[134](?:-|$))", model, re.I)
        fixed_temperature = provider == "moonshot" and re.match(r"^kimi-k[2-9](?:[.-]|$)", model, re.I)
        body = {"model": model, "messages": wire_messages, "max_completion_tokens" if modern else "max_tokens": max_tokens}
        if not (modern or fixed_temperature):
            body["temperature"] = temperature
        if tools:
            body["tools"] = [
                {"type": "function", "function": {
                    "name": str(t["name"]),
                    "description": str(t.get("description") or ""),
                    "parameters": t.get("parameters") or {"type": "object", "properties": {}},
                }}
                for t in tools if isinstance(t, dict) and t.get("name")
            ]
            body["tool_choice"] = "auto"
        if provider == "openai" and isinstance(cache_key, str) and cache_key.strip():
            body["prompt_cache_key"] = cache_key.strip()[:64]
        if transport == "deepseek":
            label = "DeepSeek"
            if allow_thinking_field:
                body["thinking"] = {"type": "enabled" if creds.thinking_mode == "enabled" else "disabled"}
                thinking_field = body, "thinking"
        elif transport == "openai":
            effort = reasoning_effort.strip().lower() if isinstance(reasoning_effort, str) and reasoning_effort.strip() else None
            if effort is None and is_openai_gemini_model(provider, model) and creds.thinking_mode == "enabled":
                effort = preferred_enabled_effort(model)
            if effort and allow_thinking_field:
                body["reasoning_effort"] = effort
                thinking_field = body, "reasoning_effort"
    elif transport == "anthropic":
        label = "Anthropic"
        endpoint = root + ("/messages" if root.endswith("/v1") else "/v1/messages")
        headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
        body = {"model": creds.model or "claude-3-5-sonnet-latest", "max_tokens": max_tokens,
                "temperature": temperature, "messages": _build_anthropic_messages(prompt, messages)}
        if system:
            body["system"] = system
        if tools:
            body["tools"] = [
                {
                    "name": str(t["name"]),
                    "description": str(t.get("description") or ""),
                    "input_schema": t.get("parameters") or {"type": "object", "properties": {}},
                }
                for t in tools if isinstance(t, dict) and t.get("name")
            ]
            body["tool_choice"] = {"type": "auto"}
    else:
        generation = {"temperature": temperature, "maxOutputTokens": max_tokens}
        body = {"contents": _build_gemini_contents(prompt, messages, transport), "generationConfig": generation}
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        if tools:
            body["tools"] = [{"functionDeclarations": [
                {
                    "name": str(t["name"]),
                    "description": str(t.get("description") or ""),
                    "parameters": _strip_gemini_schema(t.get("parameters") or {"type": "object", "properties": {}}),
                }
                for t in tools if isinstance(t, dict) and t.get("name")
            ]}]
            body["toolConfig"] = {"functionCallingConfig": {"mode": "AUTO"}}
        if transport == "vertex":
            label = "Vertex"
            endpoint = build_vertex_endpoint(root, creds.model)
            headers = {"x-goog-api-key": api_key}
            thinking = resolve_vertex_thinking_config(creds.model)
            writing_task = system.startswith(("[TASK:CHAPTER_WRITE]", "[TASK:REPAIR]"))
            if thinking and allow_thinking_field and (not writing_task or creds.thinking_mode == "enabled"):
                generation["thinkingConfig"] = thinking
                thinking_field = generation, "thinkingConfig"
            body["safetySettings"] = _VERTEX_SAFETY_SETTINGS
        else:
            label = "Gemini"
            model = _encode_component(creds.model or "gemini-2.5-flash")
            endpoint = f"{root}/models/{model}:generateContent?key={_encode_component(api_key)}"

    response = await _post(client, endpoint, body, headers)
    if thinking_field is not None and response.status_code == 400 and _rejects_thinking(response.text):
        with _state_lock:
            _UNSUPPORTED_THINKING_KEYS.add(thinking_key)
        fields, field = thinking_field
        del fields[field]
        response = await _post(client, endpoint, body, headers)
    if not response.is_success:
        if tools and _rejects_tools(response.status_code, response.text):
            raise LLMError(f"{label} 当前模型或接口不支持原生工具调用。", code="TOOLS_UNSUPPORTED", status=400)
        raise _http_error(label, response, api_key, provider != "openai")
    try:
        data = _object(response.json())
    except (ValueError, UnicodeError):
        raise LLMError(f"{label} 返回了无效的 JSON。", code="INVALID_RESPONSE", status=502) from None

    if transport in ("openai", "deepseek"):
        choice = _head(data.get("choices"))
        message = _object(choice.get("message"))
        text = _text(message.get("content"))
        finish = choice.get("finish_reason")
        raw_tool_calls = _records(message.get("tool_calls"))
        tool_calls = []
        if tools:
            for idx, raw_call in enumerate(raw_tool_calls):
                fn = _object(raw_call.get("function"))
                name = fn.get("name")
                if not isinstance(name, str) or not name:
                    continue
                call_id = raw_call.get("id") if isinstance(raw_call.get("id"), str) and raw_call.get("id") else f"call_{idx}"
                raw_args = fn.get("arguments")
                raw_str = raw_args if isinstance(raw_args, str) else json.dumps(raw_args or {}, ensure_ascii=False)
                tool_calls.append({
                    "id": call_id,
                    "name": name,
                    "arguments": _parse_tool_args(raw_args),
                    "rawArguments": raw_str,
                })
        if ((_unexpected_tool_call(finish) and (not tools or not tool_calls))
                or (not tools and raw_tool_calls and not text.strip())):
            raise LLMError(f"{label} 意外触发了未声明的工具调用（{finish or 'tool_calls'}），正在重试。",
                           code="UNEXPECTED_TOOL_CALL", status=502, retryable=True, finish_reason=finish)
        if str(finish or "").lower() == "content_filter" and not text.strip() and not tool_calls:
            raise LLMError(f"{label} 触发上游内容安全拦截（{finish}），建议微调当前章节章纲敏感词或切换模型后从断点继续。",
                           code="CONTENT_BLOCKED", status=422, retryable=False, finish_reason=finish)
        if not text.strip() and not tool_calls and max_tokens >= 500:
            raise LLMError("OpenAI API returned empty content", code="EMPTY_RESPONSE", status=502,
                           retryable=True, finish_reason=finish)
        usage = _object(data.get("usage"))
        cache_hit = (_stat(usage, "prompt_cache_hit_tokens")
                     or _stat(_object(usage.get("prompt_tokens_details")), "cached_tokens"))
        safe_msg = {k: message[k] for k in ("role", "content", "tool_calls", "reasoning_content", "extra_content")
                    if k in message}
        safe_msg["role"] = "assistant"
        assistant_msg = {
            "role": "assistant",
            "content": text,
            "toolCalls": tool_calls,
            "providerState": {"transport": transport, "message": safe_msg},
        }
        reply = _reply(text, finish, usage, "prompt_tokens", "completion_tokens", "total_tokens", system, estimate_input,
                       cache_hit_tokens=cache_hit, zero_total_is_missing=transport == "openai",
                       tool_calls=tool_calls, assistant_message=assistant_msg, include_turn=include_turn)
        reasoning_tokens = _object(usage.get("completion_tokens_details")).get("reasoning_tokens")
        if type(reasoning_tokens) in (int, float) and math.isfinite(reasoning_tokens) and reasoning_tokens > 0:
            reply["usage"]["thoughtsTokens"] = reasoning_tokens
        return reply

    if transport == "anthropic":
        content_blocks = _records(data.get("content"))
        text = "".join(_text(part.get("text")) for part in content_blocks)
        tool_calls = []
        if tools:
            for part in content_blocks:
                if part.get("type") == "tool_use" and isinstance(part.get("id"), str) and isinstance(part.get("name"), str):
                    args = _parse_tool_args(part.get("input"))
                    tool_calls.append({
                        "id": part["id"],
                        "name": part["name"],
                        "arguments": args,
                        "rawArguments": json.dumps(args, ensure_ascii=False),
                    })
        usage = _object(data.get("usage"))
        assistant_msg = {
            "role": "assistant",
            "content": text,
            "toolCalls": tool_calls,
            "providerState": {"transport": "anthropic", "content": content_blocks},
        }
        return _reply(text, data.get("stop_reason"), usage, "input_tokens", "output_tokens",
                      None, system, estimate_input,
                      cache_hit_tokens=_stat(usage, "cache_read_input_tokens"),
                      tool_calls=tool_calls, assistant_message=assistant_msg, include_turn=include_turn)

    candidate = _head(data.get("candidates"))
    parts = _records(_object(candidate.get("content")).get("parts"))
    text = "".join(_text(part.get("text")) for part in parts if not part.get("thought"))
    raw_fc_parts = [part for part in parts if isinstance(part.get("functionCall"), dict)]
    tool_calls = []
    if tools:
        for idx, part in enumerate(parts):
            fc = part.get("functionCall")
            if isinstance(fc, dict) and isinstance(fc.get("name"), str) and fc["name"]:
                args = _parse_tool_args(fc.get("args"))
                call_id = fc.get("id") if isinstance(fc.get("id"), str) and fc["id"] else f"gemini_call_{idx}"
                tool_calls.append({
                    "id": call_id,
                    "name": fc["name"],
                    "arguments": args,
                    "rawArguments": json.dumps(args, ensure_ascii=False),
                })
    prompt_block = _object(data.get("promptFeedback")).get("blockReason")
    finish = candidate.get("finishReason")
    if ((_unexpected_tool_call(finish) and (not tools or not tool_calls))
            or (not tools and raw_fc_parts and not text.strip())):
        raise LLMError(f"{label} 意外触发了未声明的工具调用（{finish or 'functionCall'}），正在重试。",
                       code="UNEXPECTED_TOOL_CALL", status=502, retryable=True, finish_reason=finish)
    if ((prompt_block or str(finish or "").upper() in {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "IMAGE_SAFETY"})
            and not text.strip() and not tool_calls):
        reason = str(prompt_block or finish)
        raise LLMError(f"{label} 触发上游内容安全拦截（{reason}），建议微调当前章节章纲敏感词或切换模型后从断点继续。",
                       code="CONTENT_BLOCKED", status=422, retryable=False, finish_reason=reason)
    if transport == "vertex" and not text.strip() and not tool_calls and finish == "MAX_TOKENS":
        raise LLMError("Vertex 返回空内容 (finishReason: MAX_TOKENS，思考消耗完输出预算，请调大 maxTokens)",
                       code="EMPTY_RESPONSE", status=502, retryable=True, finish_reason=finish)
    usage = _object(data.get("usageMetadata"))
    assistant_msg = {
        "role": "assistant",
        "content": text,
        "toolCalls": tool_calls,
        "providerState": {"transport": transport, "parts": parts},
    }
    return _reply(text, finish, usage, "promptTokenCount", "candidatesTokenCount", "totalTokenCount", system, estimate_input,
                  cache_hit_tokens=_stat(usage, "cachedContentTokenCount"),
                  thoughts_tokens=_stat(usage, "thoughtsTokenCount") if transport == "vertex" else None,
                  tool_calls=tool_calls, assistant_message=assistant_msg, include_turn=include_turn)


async def _provider_attempt(client, transport, creds, key, system, prompt, temperature, budget, timeout_ms, parallel, *,
                            messages=None, tools=None, reasoning_effort=None, cache_key=None, quota_key=None):
    while True:
        _, waiter = _enter_circuit(key)
        if waiter is not None:
            await _wait_for_probe(waiter)
            continue
        claim = None
        try:
            async with asyncio.timeout(timeout_ms / 1000), _rate_slot(creds, transport, parallel):
                _check_quota_block(quota_key)
                # Recheck after pacing: a parallel call may have claimed the probe in the meantime.
                claim, waiter = _enter_circuit(key, claim=True)
                if waiter is None:
                    result = await _call_transport(
                        client, transport, creds, system, prompt, temperature, budget,
                        messages=messages, tools=tools, reasoning_effort=reasoning_effort, cache_key=cache_key,
                    )
        except TimeoutError:
            error = _timeout_error(timeout_ms)
            _settle_circuit(claim, error)
            raise error from None
        except LLMError as error:
            _settle_circuit(claim, error)
            raise
        except BaseException:
            _settle_circuit(claim, cancelled=True)
            raise
        if waiter is not None:
            await _wait_for_probe(waiter)
            continue
        _settle_circuit(claim)
        return result


def _timeout_error(timeout_ms):
    return LLMError(f"LLM request timed out after {timeout_ms}ms", code="TIMEOUT", status=408, retryable=True)


def _backoff(creds, error, attempt):
    if error.retry_after_ms is not None:
        delay = error.retry_after_ms / 1000
        return delay + min(5.0, max(0.5, delay * 0.1))
    if _is_local(creds.base_url):
        return min(0.5, 0.1 * attempt)
    if error.code == "HTTP_429":
        steps = (10, 20, 35, 50, 60)
        return steps[min(attempt - 1, len(steps) - 1)] + random.randrange(2500) / 1000
    return 1.5 * 2 ** (attempt - 1) if attempt < 6 else 30


def _elapsed(started):
    return max(1, round((_clock() - started) * 1000))


async def call_llm(*, provider="mock", base_url="", api_key="", model="", thinking_mode="disabled",
                   system="", prompt="", temperature=0.7, max_tokens=DEFAULT_MAX_TOKENS,
                   timeout_ms=DEFAULT_TIMEOUT_MS, retries=None, max_retries=None, test_scenario=None,
                   use_saved_connection=False, allow_parallel=False, retry_empty_response=True,
                   apply_output_floor=True, store=None, client: httpx.AsyncClient | None = None,
                   siwc_context=None, siwc_profile=None, messages=None, tools=None,
                   reasoning_effort=None, cache_key=None, _bypass_quota_blocks=False):
    """Call a model, returning the same camelCase response fields as the Node gateway."""
    started = _clock()
    effective_timeout = max(1000, int(timeout_ms or DEFAULT_TIMEOUT_MS))
    if str(provider).lower() == SIWC:
        service = getattr(store, "openai_siwc", None)
        if service is None or (siwc_context not in ("provider-test", "chief-editor") and not siwc_all_stages()):
            raise LLMError("此阶段尚未启用 ChatGPT 订阅模型。", code="SIWC_DISABLED", status=403)
        if base_url not in ("", SIWC_RESOURCE) or api_key:
            raise LLMError("ChatGPT 订阅连接只使用官方地址与已授权账号。", code="SIWC_OAUTH_ONLY", status=400)
        try:
            service.check_selection(model, siwc_profile)
        except SiwcError as error:
            raise _siwc_llm_error(error) from None
        creds = _Credentials(SIWC, SIWC_RESOURCE, "", model, "disabled", service, service.epoch)
    else:
        creds = await _resolve_credentials(provider, base_url, api_key, model, thinking_mode, use_saved_connection, store)
    if creds.provider == "mock":
        result = await call_mock_llm(system=system, prompt=prompt, timeout_ms=effective_timeout,
                                     test_scenario=test_scenario)
        return {**result, "durationMs": _elapsed(started), "cacheHitTokens": 0,
                "usage": {**result["usage"], "cacheHitTokens": 0}}
    definition = get_provider_definition(creds.provider)
    if definition is None:
        raise LLMError(f"Unsupported LLM provider: {provider}", code="UNSUPPORTED_PROVIDER", status=400)

    transport = definition["transport"]
    attempts = max(1, int(retries if retries is not None else max_retries if max_retries is not None else DEFAULT_RETRIES) + 1)
    guard = _guard_output()
    budget = int(max_tokens)
    stage_match = re.match(r"^\[TASK:([A-Z_0-9]+)\]", system, re.I)
    stage = stage_match[1] if stage_match else None
    if guard and apply_output_floor:
        budget = max(budget, min(_env_positive_int("NOVEL_FORGE_MIN_OUTPUT_TOKENS", 0), DEFAULT_MAX_TOKENS))
    if transport == "vertex" and stage != "RETITLE":
        budget = max(1024, budget or DEFAULT_MAX_TOKENS)
    if transport == "openai" and guard and apply_output_floor and stage != "RETITLE":
        eff = reasoning_effort.strip().lower() if isinstance(reasoning_effort, str) else ""
        if eff in ("medium", "high") or (not eff and is_openai_gemini_model(creds.provider, creds.model) and creds.thinking_mode == "enabled"):
            budget = max(budget, 16_384)
    rejected_at = math.inf
    truncation = None
    tried = set()
    fallbacks = 0
    attempt = 1
    endpoint_key = _circuit_key(creds)
    key = endpoint_key if os.environ.get("NOVEL_FORGE_PROVIDER_CIRCUIT") != "0" else None
    quota_key = None
    if endpoint_key is not None and not _bypass_quota_blocks and os.environ.get("NOVEL_FORGE_QUOTA_FAST_FAIL") != "0":
        quota_key = endpoint_key, creds.model

    _check_quota_block(quota_key)
    _check_circuit(key)
    async with _http_client(client, creds, transport) as active_client:
        while attempt <= attempts:
            try:
                _check_quota_block(quota_key)
                _check_circuit(key)
                tried.add(budget)
                result = await _provider_attempt(
                    active_client, transport, creds, key, system, prompt,
                    temperature, budget, effective_timeout, allow_parallel,
                    messages=messages, tools=tools, reasoning_effort=reasoning_effort, cache_key=cache_key,
                    quota_key=quota_key,
                )
                finish = result.get("finishReason")
                if guard and output_was_truncated(finish):
                    raise _truncated_error(finish, result["text"], budget, rejected_at, stage)
                if guard and not result["text"].strip() and not result.get("toolCalls"):
                    raise LLMError("模型未返回可见内容。", code="EMPTY_RESPONSE", status=502,
                                   retryable=True, finish_reason=finish)
                return {**result, "durationMs": _elapsed(started)}
            except LLMError as error:
                error.provider, error.model = creds.provider, creds.model
                if is_long_quota(error):
                    _remember_quota_block(quota_key, error)
                    raise
                if error.no_replay or (error.code in {"HTTP_429", "SIWC_RATE_LIMIT"}
                                       and (error.retry_after_ms or 0) > 60_000):
                    raise
                if error.code == "CIRCUIT_OPEN" or (error.status == 429 and creds.provider == "openai"):
                    raise
                if creds.provider == SIWC:
                    # The preview endpoint offers no wire-level output budget to increase.
                    if not error.retryable or attempt == attempts:
                        raise
                    _check_circuit(key)
                    await _sleep(_backoff(creds, error, attempt))
                    attempt += 1
                    continue
                if guard and error.code in {"EMPTY_RESPONSE", "OUTPUT_TRUNCATED"} and output_was_truncated(error.finish_reason):
                    truncation = error if error.code == "OUTPUT_TRUNCATED" else _truncated_error(
                        error.finish_reason, "", budget, rejected_at, stage,
                    )
                    next_budget = next_output_budget(budget, rejected_at)
                    if not retry_empty_response or attempt == attempts or next_budget is None or next_budget in tried:
                        raise truncation from None
                    log.warning("%s 输出截断（%s），预算 %d → %d tokens。", creds.provider, error.finish_reason, budget, next_budget)
                    budget = next_budget
                    attempt += 1
                    continue
                if not retry_empty_response and (error.code == "EMPTY_RESPONSE" or output_was_truncated(error.finish_reason)):
                    raise
                if _rejects_output_limit(error):
                    rejected_at = min(rejected_at, budget)
                    next_budget = next_output_budget(truncation.request_max_tokens, rejected_at) if truncation else budget // 2
                    if fallbacks < 2 and budget >= 2048 and next_budget is not None and next_budget not in tried:
                        budget = next_budget
                        fallbacks += 1
                        continue
                    if truncation:
                        truncation.rejected_output_tokens = rejected_at
                        raise truncation from None
                if not error.retryable or attempt == attempts:
                    raise
                _check_circuit(key)
                await _sleep(_backoff(creds, error, attempt))
                attempt += 1


async def test_connection(*, provider="deepseek", base_url="", api_key="", model="", thinking_mode="disabled",
                          timeout_ms=15_000, store=None, client: httpx.AsyncClient | None = None):
    started = _clock()
    siwc = str(provider).lower() == SIWC
    creds = (_Credentials(SIWC, SIWC_RESOURCE, "", model, "disabled") if siwc else
             await _resolve_credentials(provider, base_url, api_key, model, thinking_mode, False, store))
    result = {"provider": creds.provider, "model": creds.model, "baseUrl": creds.base_url,
              "thinkingMode": creds.thinking_mode}
    try:
        if provider_requires_key(creds.provider) and not creds.api_key:
            raise LLMError(f"未配置 {creds.provider} API Key", code="CONFIG_REQUIRED", status=400)
        reply = await call_llm(provider=creds.provider, base_url=base_url if siwc else creds.base_url,
                               api_key=api_key if siwc else creds.api_key,
                               model=creds.model, thinking_mode=creds.thinking_mode, timeout_ms=timeout_ms,
                               system="你是连通性探测助手，请简短回复。", prompt="回复OK", temperature=0,
                               max_tokens=protected_output_budget(4096, 16_384), retries=0, store=store, client=client,
                               _bypass_quota_blocks=True,
                               **({"siwc_context": "provider-test"} if siwc else {}))
    except LLMError as error:
        return {**result, "ok": False, "latencyMs": _elapsed(started), "replyPreview": "",
                "usage": None, "error": str(error), **({"code": error.code} if siwc else {})}
    return {**result, "ok": True, "latencyMs": reply["durationMs"], "replyPreview": reply["text"].strip()[:80],
            "usage": reply["usage"], "error": None}


# Deterministic scenarios for the writing pipeline and acceptance tests.
_mock_attempts = {}
_MOCK_TASKS = {
    "topic_candidates": r"3\s*个候选方案|候选选题",
    "full_audit": r"全书审计|全书终审|FullBookAudit",
    "book_plan": r"全书总纲|BookPlan",
    "chapter_outlines": r"连续章纲|ChapterOutlines",
    "word_count_adjust": r"篇幅调整|扩写|精简",
    "fact_extraction": r"事实提取|FactExtraction",
    "verify": r"一致性审校|ConsistencyVerify",
    "repair": r"问题修复|ChapterRepair",
    "de_ai": r"去AI味|DeAI",
    "chapter_write": r"撰写第\s*\d+\s*章正文|ChapterWrite",
}
_MOCK_MARKERS = ("topic_candidates", "book_plan", "chapter_outlines", "arc_outlines", "arc_summary", "word_count_adjust", "fact_extraction",
                 "verify", "repair", "de_ai", "full_audit", "chapter_write", "retitle")
_BRONZE_KEY = "刻有“玄天七号”的青铜钥匙"


def reset_mock_state():
    with _state_lock:
        _mock_attempts.clear()


def _first_mock_attempt(kind, scenario, chapter=None):
    key = kind, scenario.get("runId") or "default", chapter
    with _state_lock:
        attempt = _mock_attempts.get(key, 0) + 1
        _mock_attempts[key] = attempt
        return attempt == 1


def _mock_field(text, pattern, default):
    match = re.search(pattern, text)
    return match[1].strip() if match and match[1] else default


def _mock_foreshadowing(scenario):
    return scenario.get("mode") in {"foreshadowing", "long-range-key"}


def _mock_json(value, *, compact=False):
    return json.dumps(value, ensure_ascii=False, indent=None if compact else 2,
                      separators=(",", ":") if compact else None)


def _mock_topic_candidates(text):
    genre = _mock_field(text, r"题材[：:]\s*([^\n]+)", "都市重生爽文")
    pov = _mock_field(text, r"视角[：:]\s*([^\n]+)", "第三人称限知")
    seed = _mock_field(text, r"参考素材标题[：:]\s*([^\n]+)", "暗夜执掌者")
    return {"candidates": [
        {
            "title": f"{seed[:8]}：逆命执掌者",
            "premise": "主角林渊在家族覆灭与背叛之夜重生回十年前，凭借前世金融与地下情报记忆，携手盟友苏清雪反手布局，一步步夺回失地并揭开跨越两代人的权谋真相。",
            "coreConflict": "林渊必须在十年前资金链断裂前反杀赵天豪与幕后财阀的联合围剿，同时守住家族核心机密。",
            "protagonistMotivation": "改写前世至亲惨死的结局，查明林家覆灭的真正幕后黑手。",
            "goldenThreeChapters": [
                "第1章：重生回股权转让鸿门宴当晚，当众撕毁协议并反手录音留证。",
                "第2章：夜返老宅暗格取回核心账本与神秘信物，锁定第一桶启动资金。",
                "第3章：次日开盘精准狙击仇家股票，对方资金链当场断裂，幕后黑手初露端倪。",
            ],
            "mainPlot": "前期立足破局与资本反杀，中期追查神秘组织与跨市博弈，后期收网洗冤并完成秩序重塑。",
            "recommendedPov": pov, "tags": [genre, "重生", "强反转", "长线伏笔"],
            "genreFit": 96, "novelty": 91, "longFormPotential": 95,
        },
        {
            "title": "倒计时十天：我提前买断整个时代的底牌",
            "premise": "主角意外获得未来十年的新闻与行情简报，在风口来临前连续截胡竞争对手的命脉项目，从底层逆袭为幕后操盘手。",
            "coreConflict": "信息差优势与暗中觊觎者的围追堵截。",
            "protagonistMotivation": "摆脱被资本收割的宿命，建立独立商业帝国。",
            "goldenThreeChapters": [
                "第1章：被公司裁员当天收到十年后首条加密简报。",
                "第2章：抵押房产全仓押注冷门专利，众人嘲笑疯癫。",
                "第3章：新政突发落地，专利估值暴涨百倍，前上司登门求合作。",
            ],
            "mainPlot": "从单点套利到全产业链布局，最终直面顶层财团规则制定者。",
            "recommendedPov": pov, "tags": [genre, "信息差", "商战"],
            "genreFit": 88, "novelty": 84, "longFormPotential": 86,
        },
        {
            "title": "双面档案：城西钟楼下的十年旧案",
            "premise": "一名普通调查记者在整理旧档案时发现自己竟然是十年前重大悬案的唯一幸存者，每翻开一页卷宗都引来致命追杀。",
            "coreConflict": "追寻真相与自身记忆空白之间的撕裂。",
            "protagonistMotivation": "拼凑完整记忆，为当年的无辜者讨回公道。",
            "goldenThreeChapters": [
                "第1章：匿名快递送来十年前的现场照片，背面写着主角的名字。",
                "第2章：档案室深夜失火，主角抢出半份残缺名单。",
                "第3章：名单上的第一个名字正是现任市局顾问。",
            ],
            "mainPlot": "逐一核对名单线索，层层剥开保护伞，最终在钟楼密室揭晓真相。",
            "recommendedPov": pov, "tags": [genre, "悬疑", "强主线"],
            "genreFit": 82, "novelty": 89, "longFormPotential": 83,
        },
    ]}


def _mock_book_plan(text, scenario, include_style_bible):
    total = int(_mock_field(text, r"总章数[：:]\s*(\d+)", "10")) or 10
    middle = (total + 1) // 2
    plan = {
        "title": _mock_field(text, r"书名[：:]\s*([^\n]+)", "逆天归来：暗夜执掌者"),
        "genre": _mock_field(text, r"题材[：:]\s*([^\n]+)", "都市重生爽文"),
        "pov": _mock_field(text, r"视角[：:]\s*([^\n]+)", "第三人称限知"),
        "logline": "主角林渊重回十年前，凭借前世记忆与神秘遗物布局破局，揭开跨时代阴谋并逆袭登顶。",
        "worldview": {"setting": "现代架空都市与暗流涌动的隐秘宗门/财阀势力交织",
                      "coreRules": ["实力与情报决定话语权", "秘密只在核心知情人之间流通"]},
    }
    if include_style_bible:
        plan["styleBible"] = {
            "narrativeVoice": "偏冷的近景叙述，交锋用短句，交代线索时放慢节奏。",
            "dialogueRules": ["林渊先问证据再表态。", "苏清雪直接点明风险，少用反问。", "赵天豪把威胁说成交易条件。"],
            "domainTaboos": ["年代尚未普及的即时通讯工具不可提前出现。"],
            "forbiddenCliches": ["瞳孔骤缩", "空气仿佛凝固", "心里掀起惊涛骇浪"],
        }
    plan["mainCharacters"] = [
        {"name": "林渊", "role": "主角", "traits": ["冷静", "果断", "重情义"], "goal": "查明前世覆灭真相并护住至亲"},
        {"name": "苏清雪", "role": "女主/盟友", "traits": ["聪慧", "外冷内热"], "goal": "摆脱家族联姻控制"},
        {"name": "赵天豪", "role": "前期反派", "traits": ["傲慢", "多疑"], "goal": "吞并林家产业"},
    ]
    plan["volumes"] = [
        {"volume": 1, "title": "第一卷：暗夜潜龙", "chapters": [1, middle], "summary": "重生归来，站稳脚跟并埋下关键线索。"},
        {"volume": 2, "title": "第二卷：风云破局", "chapters": [middle + 1, total], "summary": "全面反击，回收伏笔，揭晓终极秘密。"},
    ]
    if _mock_foreshadowing(scenario):
        clue = {"id": "fs_bronze_key", "clue": _BRONZE_KEY, "plantedChapter": 2, "reinforcedChapters": [6, 11],
                "payoffChapter": 18, "description": "第2章于老宅暗格发现青铜钥匙，第6章确认其来源，第11章定位钟楼机关，第18章开启密室终极真相。"}
    else:
        clue = {"id": "fs_1", "clue": "神秘黑色玉佩", "plantedChapter": 1, "reinforcedChapters": [3],
                "payoffChapter": total, "description": "贯穿全书的核心信物。"}
    plan["foreshadowingPlan"] = [clue]
    return plan


_MOCK_PLOT = {
    2: ("林渊返回老宅暗格，找到刻有“玄天七号”的青铜钥匙（重要伏笔）。",
        ["林渊在老宅地下室暗格发现青铜钥匙", "钥匙背面刻着“玄天七号”"]),
    6: ("黑市拍卖会上出现同源纹章，林渊确认随身青铜钥匙与玄天阁有关。",
        ["林渊取出青铜钥匙比对纹章", "苏清雪留意到钥匙却不知其真正用途"]),
    11: ("林渊破解古籍残卷，得知“玄天七号”青铜钥匙对应城西废弃钟楼的地下密室。",
         ["破译钟楼坐标", "青铜钥匙成为开启密室的唯一凭证"]),
    18: ("林渊持“玄天七号”青铜钥匙开启城西钟楼密室，伏笔正式回收，真相大白！",
         ["青铜钥匙插入钟楼暗锁", "密室开启，林家覆灭真相水落石出"]),
}


def _mock_outlines(text, scenario):
    start = int(_mock_field(text, r"起始章节[：:]\s*(\d+)", "1")) or 1
    end = int(_mock_field(text, r"结束章节[：:]\s*(\d+)", "10")) or 10
    foreshadowing = _mock_foreshadowing(scenario)
    outlines = []
    for chapter in range(start, end + 1):
        title = f"破局第{chapter}步"
        summary = f"第{chapter}章推进主线冲突，林渊应对来自赵家的试探并进一步扩大优势。"
        events = [f"林渊完成第{chapter}阶段布局", "信息增量揭示新线索"]
        if chapter == 1:
            title = "重生之夜"
            summary = "林渊重生回到命运转折夜，当场反制暗算，确立主角优势。"
            events = ["林渊醒来确认重生时间点", "识破赵天豪的鸿门宴"]
        elif foreshadowing and chapter in _MOCK_PLOT:
            summary, events = _MOCK_PLOT[chapter]
            if chapter == 18:
                title = "钟楼密钥终局"
        outlines.append({
            "chapterNumber": chapter, "title": f"第{chapter}章 {title}", "goal": f"推进第{chapter}章主线剧情",
            "summary": summary, "keyEvents": events, "hook": f"章末突然传来意外消息，指向第{chapter + 1}章的新危机。",
        })
    return outlines


def _mock_arc_skeleton(index):
    pairs = (
        ("investigation", "confrontation"), ("negotiation", "reveal"), ("pursuit", "acquisition"),
        ("deadline", "scheme"), ("relationship", "trial"), ("loss", "reward"),
    )
    return list(pairs[(index - 1) % len(pairs)])


def _mock_arc_outlines(text, scenario):
    index = int(_mock_field(text, r"当前单元[：:]\s*(\d+)", "1"))
    start = int(_mock_field(text, r"起始章节[：:]\s*(\d+)", "1"))
    total = int(_mock_field(text, r"全书总章数[：:]\s*(\d+)", str(start)))
    minimum = int(_mock_field(text, r"最少章数[：:]\s*(\d+)", "3"))
    maximum = int(_mock_field(text, r"最多章数[：:]\s*(\d+)", "6"))
    allowed = json.loads(_mock_field(text, r"allowedLengths[：:]\s*(\[[\d,\s]+\])", "[]"))
    lengths = [length for length in allowed if minimum <= length <= maximum] or range(minimum, maximum + 1)
    end = start + min(lengths, key=lambda length: abs(length - 5)) - 1
    rows = _mock_outlines(f"起始章节：{start}\n结束章节：{end}", scenario)
    if end == total:
        rows[-1]["goal"] = "清算最后的交易，交代旧案与各方选择的后果"
        rows[-1]["summary"] += " 本章揭开旧案、兑现未揭伏笔并收束冲突。"
        rows[-1]["hook"] = "主线收束，角色各自承担选择的后果。"
    return {"arcTitle": f"第{start}至{end}章调查与交锋", "arcGoal": f"确认第{end}章证据来源并逼出新的决策。",
            "skeleton": _mock_arc_skeleton(index),
            "villainChips": [{"villain": "赵天豪", "newChips": [f"第{start}章协商代理权"],
                              "exitPlan": "exit" if end == total else "retain"}], "chapters": rows}


def _mock_arc_summary(prompt, *, story_ledger=False):
    try:
        index = int(_mock_field(prompt, r"归档第\s*(\d+)\s*单元", "1"))
        rows, _ = json.JSONDecoder().raw_decode(prompt.split("【正文依据】", 1)[1].lstrip())
        first = rows[0]["chapterNumber"]
        last = rows[-1]["chapterNumber"]
        turns, beats, hooks = [], [], []
        for row in rows:
            n = row["chapterNumber"]
            body = row.get("content") or row.get("middle") or row.get("beginning") or ""
            tail = row.get("ending") or body
            if n in (first, (first + last) // 2, last):
                turns.append({"chapter": n, "text": row.get("title") or f"第{n}章获取证据"})
            fragments = [line.strip() for line in body.splitlines() if line.strip()]
            if fragments:
                beats.append({"chapter": n, "text": f"第{n}章的证据交接与当面拆招",
                              "quote": fragments[min(1, len(fragments) - 1)][:45]})
            if tail.strip() and len(hooks) < 3:
                hooks.append({"chapter": n, "text": f"第{n}章以具体物证带出行动",
                              "quote": tail.strip()[-45:]})
        summary = {"mainline": f"第{first}至{last}章完成调查与交接，双方掌握的证据与下一步行动发生变化。",
                   "turningPoints": turns, "usedBeats": beats, "usedHooks": hooks,
                   "themes": [f"第{first}章交割凭证"], "skeleton": _mock_arc_skeleton(index),
                   "shape": "循线取证后完成交接，双方重新选择行动。",
                   "villainStatus": f"赵天豪：第{last}章旧证据被迫交接，暂退幕后。"}
        if story_ledger:
            summary["driftChecks"] = [
                {"chapter": row["chapterNumber"], "kind": "value", "issue": "可用余额混入尚未到账的款项",
                 "fixHint": "保留已到账的余额，把这笔款单列为待到账。", "quote": _MOCK_UNPOSTED_DRIFT}
                for row in rows if any(_MOCK_UNPOSTED_DRIFT in (row.get(key) or "")
                                       for key in ("content", "beginning", "middle", "ending"))
            ][:4]
        return summary
    except (IndexError, KeyError, TypeError, ValueError):
        raise LLMError("Mock 单元归档缺少正文输入。", code="INVALID_RESPONSE", status=502) from None


_MOCK_CLUE_SCENES = {
    2: "{p}独自回到林家老宅的地下室，撬开墙角那块松动的青砖。暗格之中静静躺着一枚古朴的青铜钥匙，钥匙柄上清晰地铭刻着“玄天七号”四个篆字。{p}神色微凝，将青铜钥匙贴身收好，决定暂不向任何人透露此事。",
    6: "黑市拍卖会的灯光昏暗而压抑。当展台上出现一枚残缺的玄天纹章时，{p}下意识摸了摸贴身携带的那枚刻有“玄天七号”的青铜钥匙——两者的纹路竟然严丝合缝。坐在一旁的苏清雪察觉到{p}神色有异，低声询问，但{p}只是微微摇头，没有道破钥匙的秘密。",
    11: "灯下翻阅完刚到手的古籍残卷，{p}终于彻底看懂了“玄天七号”青铜钥匙的指向。残卷末端明确标注：玄天七号所对应的并非普通保险柜，而是城西废弃钟楼地底的三重机关密室。",
    18: "暴雨倾盆而下，城西废弃钟楼内一片死寂。{p}走上前，从怀中取出那枚自第2章起便贴身保管的“玄天七号”青铜钥匙，稳稳插入石壁暗锁。伴随沉重的齿轮转动声，密室石门缓缓开启，尘封十年的真相终于展现在眼前！",
}
_MOCK_FILLER = (
    "窗外的霓虹灯影在玻璃上流转，映照出{p}沉静而锐利的侧脸。每一步落子都经过了精密的算计，绝不容许半点差池。从股权结构到地下资金流向，{p}早已在脑海中推演过无数次，此刻不过是按图索骥。",
    "苏清雪站在走廊尽头，看着{p}从容离去的背影，心中掀起了惊涛骇浪。她原本以为这场博弈早已注定结局，却没想到局势会在短短半小时内彻底逆转，对方掌握的情报深度甚至超越了顶尖财阀的智囊团。",
    "桌上的加密通讯器微微震动，新的密报已经传回。{p}扫了一眼屏幕上的坐标，拿笔圈出相隔三条街的两座仓库。他把通知抄给城南码头和金融街的线人，约好两边的货车必须在换班之前同时出发。",
    "街角的风卷起几片落叶，远处的车灯由远及近。暗流之下的交锋才刚刚开始，谁能笑到最后，还要看接下来的每一步棋。夜色深处，几方势力的代表各自拨通了加密电话，整座城市的暗网悄然沸腾。",
    "会议室内的投影仪光束微微闪烁，一份份经过交叉比对的财务报表铺满长桌。{p}修长的手指点在关键数据上，语气平静却带着不容置疑的压迫感：“给你们十二个小时，把原本属于林家的东西悉数奉还。”",
    "茶水在白瓷杯中微微晃动，倒映出天花板上冰冷的吊灯。赵天豪的呼吸明显粗重了几分，他死死盯着屏幕上的实时行情曲线，怎么也想不明白对方究竟是如何提前三天预判了所有做空节点。",
)

_MOCK_CHAPTER_ENDINGS = (
    "门卫把出入单退回窗口，说司机的笔迹变了。{p}翻到纸背，发现上一张签收单的墨痕还印在上面。他把两张单子对着值班室的灯摆齐，沿着缺了半笔的姓氏，在交接名单上划去第一个人。",
    "船工把扳手搁在桥墩上，抱起那只漏水的铁桶。卸掉底部的夹层以后，桶比铅盒还轻。{p}叫住他，把沿河仓库的钥匙递过去，请他路过第三座石桥时捎一封信给守桥人。",
    "收音机接连报错了两个车站。修理铺老板断开电源，把绕在天线上的细铜线抽出来，铜线末端还拴着一枚烧焦的邮票。{p}把信封移到窗边烘干，邮戳下面慢慢显出寄件人留下的门牌。",
    "值班护士敲了敲保温瓶，催人先喝完热水再走。{p}接过杯子，从瓶套内侧摸到一块磨圆的骨牌。柜台下面排着一串同样的骨牌，唯独倒数第二个位置上压着一张去北站的车票。",
    "仓库顶上落下一撮干草。会计抬头望了望，把印泥盖好，按约定给卖鱼人留下一盏灯。窗外潮水退得很急，{p}拎起椅背上的外衣，从堤岸另一边的小路绕去取那盒底片。",
    "敲钟人把账本朝外翻了半页，匆匆抹掉页脚的铅笔字。{p}用手掌压住被夜风掀起的纸角，请他把上次忘了归档的钥匙也放进抽屉。随后两人分别锁门，赶在封街前离开。",
)

_MOCK_UNPOSTED_DRIFT = "账房却把尚未到账的款项计入可用余额。"
_MOCK_LEDGER_KNOWLEDGE = "苏清雪说：“我亲眼看见钟楼暗门。我会帮你守住它。”"
_MOCK_LEDGER_CLOCK = re.compile(r"(第\d+天)的(清晨)，(初冬)的风穿过码头，离潮门关闭还剩(\d+天)。")
_MOCK_LEDGER_BALANCE = re.compile(r"码头备用金(?:入账(\d+元)，目前余额为|的期初余额为)(\d+元)。")


def _mock_chapter(text, chapter, scenario, *, story_ledger=False):
    pov = _mock_field(text, r"叙事视角[：:]\s*([^\n]+)", "第三人称限知")
    words = int(_mock_field(text, r"(?:本章)?目标字数[：:]\s*(\d+)", "2000")) or 2000
    protag = "我" if "第一人称" in pov else "林渊"
    fault_mode = scenario.get("mode") == "fault-injection"
    fault_chapter = scenario.get("faultChapter")
    inject = fault_mode and chapter == (2 if fault_chapter is None else fault_chapter) and _first_mock_attempt(
        "fault", scenario, chapter,
    )
    if chapter == 1:
        paragraphs = [f"夜色如墨，冷风顺着半掩的窗缝灌入室内。{protag}猛地睁开双眼，胸口剧烈起伏，掌心还残留着前世坠楼时的冰冷触感。墙上的电子日历清晰地显示着十年前的日期——一切都还来得及。"]
        if fault_mode:
            paragraphs.append(f"门外突然传来急促的脚步声，几名黑衣打手破门而入。{protag}虽然反应极快，但在格挡铁棍时左臂被重重砸中，骨裂的剧痛瞬间袭来，左臂暂时失去了行动能力。")
    else:
        paragraphs = [f"第{chapter}章的序幕在清晨的薄雾中拉开。{protag}站在落地窗前，指尖轻轻敲击着桌面，将昨夜收集到的所有情报在脑海中重新推演了一遍。"]
    if _mock_foreshadowing(scenario) and chapter in _MOCK_CLUE_SCENES:
        paragraphs.append(_MOCK_CLUE_SCENES[chapter].format(p=protag))
    if inject:
        paragraphs.append(f"面对赵天豪派来的伏兵，{protag}冷哼一声，直接挥动完好无损的左臂猛然劈下，将面前实木桌案震得粉碎。【此处待补充具体打斗细节】")
    else:
        paragraphs.append(f"客厅内的气氛骤然紧绷，赵天豪冷笑着放下茶杯，试图用言语试探{protag}的底线。然而{protag}神色淡然，只用三言两语便将对方的陷阱原封不动地推了回去，反手亮出的证据更是让在场众人脸色剧变。")
    if story_ledger and scenario.get("mode") in ("story-ledger", "story-ledger-drift"):
        clock = f"第{chapter}天的清晨，初冬的风穿过码头，离潮门关闭还剩{max(0, 30 - chapter)}天。"
        balance = "码头备用金的期初余额为100元。" if chapter == 1 else f"码头备用金入账10元，目前余额为{90 + 10 * chapter}元。"
        paragraphs.append(clock + balance + _MOCK_LEDGER_KNOWLEDGE)
        if scenario.get("mode") == "story-ledger-drift" and chapter == scenario.get("driftChapter", 2):
            paragraphs.append(_MOCK_UNPOSTED_DRIFT)
    aim = max(200, math.floor(words * 0.97 + 0.5))
    ending = _MOCK_CHAPTER_ENDINGS[(chapter - 1) % len(_MOCK_CHAPTER_ENDINGS)].format(p=protag)
    length = sum(len(_CJK.findall(part)) for part in [*paragraphs, ending])
    for index in range(200):
        if length >= aim:
            break
        filler = _MOCK_FILLER[(index + chapter) % len(_MOCK_FILLER)].format(p=protag)
        paragraphs.append(filler)
        length += len(_CJK.findall(filler))
    return "\n\n".join([*paragraphs, ending])


def _mock_word_count_adjust(text, chapter):
    target = int(_mock_field(text, r"目标字数[：:]\s*(\d+)", "2000")) or 2000
    body = text.partition("【待调整正文】")[2].strip()
    if not body:
        return _mock_chapter(text, chapter, {})
    current = len(_CJK.findall(body))
    if current < 0.92 * target:
        extra = f"夜色更深，第{chapter}章的暗流仍在持续发酵，林渊将刚获得的线索逐一归档，确认下一阶段的行动坐标与时间窗口。"
        missing = max(0, math.floor(target * 0.97 + 0.5) - current)
        return body + ("\n\n" + extra) * min(120, math.ceil(missing / len(_CJK.findall(extra))))
    if current > 1.08 * target:
        paragraphs = re.split(r"\n{2,}", body)
        while len(paragraphs) > 2 and current > math.floor(target * 1.03 + 0.5):
            current -= len(_CJK.findall(paragraphs.pop()))
        return "\n\n".join(paragraphs)
    return body


def _mock_story_memory(body):
    memory = {}
    if match := _MOCK_LEDGER_CLOCK.search(body):
        memory["storyClock"] = {**dict(zip(("day", "timeOfDay", "season", "countdown"), match.groups())), "quote": match[0]}
    elif "在清晨的薄雾中拉开" in body:
        memory["storyClock"] = {"timeOfDay": "清晨", "quote": "在清晨的薄雾中拉开"}
    elif "夜色如墨，冷风顺着半掩的窗缝灌入室内" in body:
        memory["storyClock"] = {"timeOfDay": "夜晚", "quote": "夜色如墨，冷风顺着半掩的窗缝灌入室内"}
    if match := _MOCK_LEDGER_BALANCE.search(body):
        balance = {"label": "码头备用金", "value": match[2], "quote": match[0]}
        if match[1]:
            balance["change"] = "+" + match[1]
        memory["ledgerBalances"] = [balance]
    if _MOCK_LEDGER_KNOWLEDGE in body:
        memory["characterStates"] = {"苏清雪": {"seen": True, "stanceTier": "ally", "knownFacts": ["亲眼看见钟楼暗门"],
                                              "quote": _MOCK_LEDGER_KNOWLEDGE}}
    return memory


def _mock_facts(chapter, scenario, *, story_ledger=False, body=""):
    delta = {
        "characters": {"林渊": {"name": "林渊", "status": "active", "role": "主角",
                              "location": "老宅地下室" if chapter == 2 else "都市"}},
        "injuries": [],
        "timeline": [{"dayOrTime": f"第{chapter}章时间线", "event": f"第{chapter}章核心事件推进", "characters": ["林渊"]}],
        "items": {}, "secrets": [], "foreshadowing": [],
    }
    if scenario.get("mode") == "fault-injection" and chapter == 1:
        delta["injuries"].append({"character": "林渊", "description": "左臂骨裂，暂时失去行动能力",
                                  "severity": "high", "active": True})
    if story_ledger and (memory := _mock_story_memory(body)):
        delta["memory"] = memory
    if not _mock_foreshadowing(scenario):
        return delta
    if chapter == 2:
        key = "玄天七号青铜钥匙"
        delta["items"][key] = {"name": key, "owner": "林渊", "location": "林渊贴身保管", "status": "intact"}
        delta["secrets"].append({"secret": f"{key}的存在与来历", "holders": ["林渊"],
                                 "excludedCharacters": ["苏清雪", "赵天豪"]})
    clue = {
        2: {"plantedChapter": 2, "status": "planted"},
        6: {"status": "reinforced", "details": "与黑市玄天纹章同源"},
        11: {"status": "reinforced", "details": "对应城西废弃钟楼地下密室"},
        18: {"status": "paid_off", "payoffChapter": 18, "details": "成功开启钟楼密室并揭晓真相"},
    }.get(chapter)
    if clue:
        delta["foreshadowing"].append({"id": "fs_bronze_key", "clue": _BRONZE_KEY, **clue})
    return delta


def _mock_verify(text, chapter, scenario, *, story_ledger=False):
    before, marker, after = text.partition("【待审校正文】")
    body = (after if marker else before).partition("输出 JSON")[0]
    issues = []
    if re.search(r"【待补充|\[TODO\]|\bTBD\b|\bplaceholder\b", body, re.I):
        issues.append({"type": "placeholder", "severity": "error", "message": f"第{chapter}章包含未完成的占位符文本"})
    if "完好无损的左臂" in body and "左臂骨裂" in text:
        issues.append({"type": "injury_contradiction", "severity": "error",
                       "message": f"第{chapter}章存在伤势前后矛盾：林渊第1章左臂骨裂尚未痊愈，却在本章“挥动完好无损的左臂”",
                       "sourceChapter": 1})
    return {"passed": not issues, "chapterNumber": chapter, "issues": issues, "aiIssues": [],
            "factsDelta": None if issues else _mock_facts(chapter, scenario, story_ledger=story_ledger, body=body),
            "summary": f"发现 {len(issues)} 处一致性问题" if issues else f"第{chapter}章一致性审校通过"}


def _mock_full_audit(scenario, prompt):
    budgeted = "【localDiagnostics】" in prompt
    if scenario.get("mode") == "audit-fail-once" and _first_mock_attempt("audit", scenario):
        material = prompt.partition("【末三章结尾】")[2]
        try:
            endings, _ = json.JSONDecoder().raw_decode(material.lstrip())
        except ValueError:
            endings = []
        blocker = []
        if endings:
            finale = endings[-1]
            if match := re.search(r"[^。！？\n]{4,180}[。！？]\s*$", finale["ending"]):
                blocker = [{"type": "ending", "chapterNumber": finale["chapterNumber"], "quote": match[0].strip(),
                            "message": "模拟终审：末句补明人物下一步的安排。"}]
        return {
            "passed": False, "score": 58, "summary": "终审未通过：部分伏笔回收缺少明确章节证据引用。",
            **({"blockingIssues": blocker, "issueChapters": [row["chapterNumber"] for row in blocker]} if budgeted else {}),
            "mainConflictChapter": 1, "protagonistChoiceChapter": 2, "foreshadowingPayoffs": [], "evidence": [],
            "issues": ["第18章伏笔回收证据引用缺失"], "consistencySummary": "需补全终章证据链后复审",
            "recommendations": ["补全关键章节证据引用"],
        }
    return {
        "passed": True, "score": 92 if budgeted else 95, **({"blockingIssues": []} if budgeted else {}),
        "summary": "全书主线完整，主角选择后果明确，人物行为与伤势/物品/秘密边界一致，核心伏笔回收闭环。",
        "mainConflictChapter": 1, "protagonistChoiceChapter": 2,
        "foreshadowingPayoffs": [{"clue": "核心伏笔线索", "plantedChapter": 2, "payoffChapter": 18, "status": "paid_off"}],
        "evidence": [{"claim": "主线冲突确立", "chapter": 1, "quote": "重生回十年前并当场反制暗算"},
                     {"claim": "核心伏笔回收", "chapter": 18, "quote": "开启密室并揭晓最终真相"}],
        "consistencySummary": "人物状态、伤势、物品位置及秘密知情边界全书一致",
        "recommendations": ["可进一步丰富次要配角对话质感"],
    }


def build_mock_volume_summary(chapters):
    """Mirror the deterministic archive produced from the first, fifth and tenth chapters."""
    if len(chapters) != 10:
        raise ValueError("Mock volume summaries require ten chapters")
    turns = [{"chapter": chapter["chapterNumber"], "text": chapter.get("summary") or chapter.get("title") or ""}
             for chapter in (chapters[0], chapters[4], chapters[9])]
    return {"mainline": "；".join(f'第{point["chapter"]}章：{point["text"][:80]}' for point in turns),
            "turningPoints": turns}


def _mock_volume_prompt(prompt):
    try:
        evidence = json.loads(prompt.split("\n", 1)[1])
        return build_mock_volume_summary(evidence)
    except (IndexError, KeyError, TypeError, ValueError):
        raise LLMError("Mock 卷摘要缺少完整的十章输入。", code="INVALID_RESPONSE", status=502) from None


def _detect_mock_task(text):
    if re.search(r"\[TASK:VOLUME_SUMMARY\]", text, re.I):
        return "volume_summary"
    if re.search(r"\[TASK:DRIFT_PATCH\]", text, re.I):
        return "drift_patch"
    for task in _MOCK_MARKERS:
        if re.search(rf"\[TASK:{task}\]", text, re.I):
            return task
    return next((task for task, pattern in _MOCK_TASKS.items() if re.search(pattern, text, re.I)), "generic")


def _mock_drift_patch(prompt):
    material = prompt.partition("【偏差与局部窗口】")[2]
    try:
        drifts, _ = json.JSONDecoder().raw_decode(material.lstrip())
    except ValueError:
        return {"patches": []}
    patches = []
    for drift in drifts:
        quote = drift["quote"]
        if quote == _MOCK_UNPOSTED_DRIFT and quote in drift["window"]:
            patches.append({"driftId": drift["id"], "confirmed": True, "find": quote,
                            "replace": "账房把尚未到账的款项单列为待到账。", "reason": "待到账款项单列"})
        elif drift["issue"] == "模拟终审：末句补明人物下一步的安排。" and quote in drift["window"]:
            patches.append({"driftId": drift["id"], "confirmed": True, "find": quote,
                            "replace": quote + "这件事要在天亮前办妥。", "reason": "补明下一步的时间"})
        elif (drift["kind"] == "clock" and "倒计时缺少衔接" in drift["issue"]
              and "沿用" in drift["fixHint"] and quote in drift["window"]):
            patches.append({"driftId": drift["id"], "confirmed": True, "find": quote,
                            "replace": "账册上标出的交割日期没有变。" + quote, "reason": "补明倒计时的前文依据"})
    return {"patches": patches}


async def call_mock_llm(*, system="", prompt="", timeout_ms=DEFAULT_TIMEOUT_MS, test_scenario=None):
    scenario = {"mode": test_scenario} if isinstance(test_scenario, str) else _object(test_scenario)
    combined = f"{system}\n{prompt}"
    task = _detect_mock_task(combined)
    chapter_match = re.search(r"第\s*(\d+)\s*章", prompt) or re.search(r"第\s*(\d+)\s*章", system)
    if task == "chapter_write" and "不可变核心口径锁（初始值仅为历史基线）：\n" in prompt:
        chapter_match = re.search(r"^请撰写第(\d+)(?:/\d+)?章正文", prompt, re.M) or chapter_match
    chapter = int(chapter_match[1]) if chapter_match else 1
    mode = scenario.get("mode")
    story_ledger = STORY_LEDGER_MARKER in system
    if mode == "always-timeout":
        wait_ms = min(timeout_ms or 200, 150)
        await asyncio.sleep(wait_ms / 1000)
        raise LLMError(f"Simulated LLM timeout after {wait_ms}ms", code="TIMEOUT", status=408, retryable=True)
    if mode == "timeout-once" and task == "chapter_write":
        chapter_to_fail = scenario.get("failAtChapter")
        if chapter == (3 if chapter_to_fail is None else chapter_to_fail) and _first_mock_attempt("timeout", scenario, chapter):
            await asyncio.sleep(0.030)
            raise LLMError(f"Simulated timeout on chapter {chapter} (attempt 1)", code="TIMEOUT", status=408, retryable=True)

    await asyncio.sleep(0.008)
    if task == "topic_candidates":
        text = _mock_json(_mock_topic_candidates(combined))
    elif task == "book_plan":
        plan = _mock_book_plan(combined, scenario, system.endswith(STYLE_BIBLE_MARKER))
        v2 = PLAN_V2_MARKER in system
        if v2 or CANON_LOCKS_MARKER in system:
            plan["canonLocks"] = {
                "powerScale": "没有经验值或充值升级机制，人物依靠现有技能和情报推进。",
                "currencyAndEra": "现代都市使用人民币，按实际到账和资产估值分别记账。",
                "worldTaboos": ["重生仅属于主角的既有经历；世界里不存在穿书或任务系统。"],
                "keyAssets": ["青铜钥匙背刻玄天七号。"] if _mock_foreshadowing(scenario) else ["核心信物是黑色玉佩。"],
            }
        if v2:
            plan["planVersion"] = PLAN_VERSION_V2
            plan["canonLocks"].update(
                identityAliases=[{"character": "林渊", "scope": "旧案档案", "alias": "林渊"}],
                boundaries=[{"subject": "钟楼密室", "rule": "未经守门人许可不得进入", "changeWhen": "守门人当面授予进入许可"}],
                assetSets=[{"name": "旧案档案", "members": ["交接单", "证人名单", "钟楼图纸"]}],
            )
            plan["volumes"] = [row for row in plan["volumes"] if row["chapters"][0] <= row["chapters"][1]]
            plan["volumes"][0]["escalation"] = {
                "pressure": "旧案卷宗即将被转移", "costAtRisk": "调查暴露会失去老宅的立足点",
                "retiredTactics": [], "stageTransition": None,
            }
            if len(plan["volumes"]) > 1:
                plan["volumes"][1]["escalation"] = {
                    "pressure": "财阀切断证人联络并接管档案", "costAtRisk": "败诉将失去林家的产业与声誉",
                    "retiredTactics": [{"tactic": "私下复制卷宗", "whyFails": "卷宗已移交并启用双人交接"}],
                    "stageTransition": "核对交接单并取得证人同意后才能公开旧案",
                }
        text = _mock_json(plan)
    elif task == "chapter_outlines":
        text = _mock_json(_mock_outlines(combined, scenario))
    elif task == "arc_outlines":
        text = _mock_json(_mock_arc_outlines(combined, scenario))
    elif task == "arc_summary":
        text = _mock_json(_mock_arc_summary(prompt, story_ledger=story_ledger))
    elif task == "chapter_write":
        text = _mock_chapter(combined, chapter, scenario, story_ledger=story_ledger)
    elif task == "word_count_adjust":
        text = _mock_word_count_adjust(combined, chapter)
    elif task == "retitle":
        text = "关口生变"
    elif task == "fact_extraction":
        body = prompt.partition(f"【第{chapter}章正文】")[2].partition("请按以下 JSON 格式输出")[0] if story_ledger else ""
        text = _mock_json(_mock_facts(chapter, scenario, story_ledger=story_ledger, body=body))
    elif task == "verify":
        text = _mock_json(_mock_verify(combined, chapter, scenario, story_ledger=story_ledger))
    elif task == "repair":
        ledger_scenario = scenario if story_ledger and mode in ("story-ledger", "story-ledger-drift") else {}
        text = _mock_chapter(combined, chapter, ledger_scenario, story_ledger=story_ledger)
    elif task == "de_ai":
        _, marker, body = combined.partition("【待润色正文】")
        text = body.strip() if marker else combined
    elif task == "full_audit":
        text = _mock_json(_mock_full_audit(scenario, prompt))
    elif task == "volume_summary":
        text = _mock_json(_mock_volume_prompt(prompt))
    elif task == "drift_patch":
        text = _mock_json(_mock_drift_patch(prompt))
    elif prompt.strip() == "回复OK":
        text = "OK"
    else:
        text = _mock_json({"ok": True, "message": "Mock LLM response", "chapter": chapter}, compact=True)
    prompt_tokens = estimate_tokens(combined)
    completion_tokens = estimate_tokens(text)
    return {"text": text, "usage": {"promptTokens": prompt_tokens, "completionTokens": completion_tokens,
                                    "totalTokens": prompt_tokens + completion_tokens}}
