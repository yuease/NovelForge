"""Provider metadata and model discovery without a dependency on the web app."""

import asyncio
import re
from collections.abc import Mapping
from typing import Protocol

import httpx

from .openai_siwc import PROVIDER as SIWC, RESOURCE as SIWC_RESOURCE
from .thinking_policy import thinking_capability


PROVIDERS = {
    "mock": {
        "label": "Mock · 模拟模型", "description": "无需密钥，适合试跑流程。",
        "transport": "mock", "baseUrl": "", "models": ["mock-novel-v1"],
    },
    "openai": {
        "label": "OpenAI", "description": "GPT 系列官方 API。",
        "transport": "openai", "baseUrl": "https://api.openai.com/v1", "env": "OPENAI_API_KEY",
        "models": ["gpt-4o-mini", "gpt-4.1-mini", "gpt-4.1", "gpt-5-mini"],
    },
    SIWC: {
        "label": "ChatGPT 订阅", "description": "使用 ChatGPT 登录并调用获准的订阅模型。",
        "transport": "openai-responses-siwc", "baseUrl": SIWC_RESOURCE, "models": [], "requiresKey": False,
    },
    "anthropic": {
        "label": "Anthropic", "description": "Claude 系列官方 API。",
        "transport": "anthropic", "baseUrl": "https://api.anthropic.com", "env": "ANTHROPIC_API_KEY",
        "models": ["claude-sonnet-4-6", "claude-haiku-4-5", "claude-opus-4-6"],
    },
    "gemini": {
        "label": "Google Gemini", "description": "使用 Google AI Studio 的 API 密钥。",
        "transport": "gemini", "baseUrl": "https://generativelanguage.googleapis.com/v1beta", "env": "GEMINI_API_KEY",
        "models": ["gemini-2.5-flash", "gemini-2.5-pro", "gemini-2.5-flash-lite"],
    },
    "deepseek": {
        "label": "DeepSeek", "description": "深度求索官方 API。",
        "transport": "deepseek", "baseUrl": "https://api.deepseek.com", "env": "DEEPSEEK_API_KEY",
        "models": ["deepseek-flash", "deepseek-pro", "deepseek-chat", "deepseek-reasoner"],
    },
    "qwen": {
        "label": "阿里云百炼 · 通义千问", "description": "默认中国内地接入地址，可改为你的百炼工作空间地址。",
        "transport": "openai", "baseUrl": "https://dashscope.aliyuncs.com/compatible-mode/v1", "env": "DASHSCOPE_API_KEY",
        "models": ["qwen-plus", "qwen-max", "qwen-turbo", "qwen-flash"],
    },
    "moonshot": {
        "label": "月之暗面 · Kimi", "description": "月之暗面开放平台。",
        "transport": "openai", "baseUrl": "https://api.moonshot.cn/v1", "env": "MOONSHOT_API_KEY",
        "models": ["kimi-k2.6", "kimi-k2.5", "moonshot-v1-32k"],
    },
    "zhipu": {
        "label": "智谱 · GLM", "description": "智谱开放平台，使用平台预设模型。",
        "transport": "openai", "baseUrl": "https://open.bigmodel.cn/api/paas/v4", "env": "ZHIPUAI_API_KEY",
        "discover": False, "models": ["glm-4.7", "glm-4.7-flash", "glm-4.7-flashx"],
    },
    "siliconflow": {
        "label": "硅基流动", "description": "从硅基流动获取平台可用模型。",
        "transport": "openai", "baseUrl": "https://api.siliconflow.cn/v1", "env": "SILICONFLOW_API_KEY",
        "models": ["deepseek-ai/DeepSeek-V4-Flash", "Qwen/Qwen3-32B"],
    },
    "openrouter": {
        "label": "OpenRouter", "description": "通过一个接入点使用多家平台的模型。",
        "transport": "openai", "baseUrl": "https://openrouter.ai/api/v1", "env": "OPENROUTER_API_KEY",
        "models": ["openai/gpt-4o-mini", "anthropic/claude-sonnet-4.6", "google/gemini-2.5-flash"],
    },
    "ollama": {
        "label": "Ollama · 本地模型", "description": "同步本机 Ollama 已下载的模型。",
        "transport": "openai", "baseUrl": "http://127.0.0.1:11434/v1", "env": "OLLAMA_API_KEY",
        "requiresKey": False, "models": [],
    },
    "openai-compatible": {
        "label": "OpenAI 兼容接口", "description": "自建网关、代理或其他兼容服务。",
        "transport": "openai", "baseUrl": "", "env": "OPENAI_COMPATIBLE_API_KEY",
        "requiresKey": False, "models": [],
    },
    "vertex": {
        "label": "Google Vertex AI", "description": "官方连接使用预设和已登记模型，支持模型列表的兼容网关可同步。",
        "transport": "vertex", "baseUrl": "https://aiplatform.googleapis.com/v1/publishers/google/models",
        "env": "VERTEX_API_KEY", "models": ["gemini-2.5-flash", "gemini-2.5-pro"],
    },
    "vertex-new": {
        "label": "Google Vertex AI · 新项目", "description": "独立的 Vertex 项目连接，模型按该项目的可用范围登记。",
        "transport": "vertex", "baseUrl": "https://aiplatform.googleapis.com/v1/publishers/google/models",
        "env": "VERTEX_NEW_API_KEY", "models": [],
    },
}

_MODEL_EXCLUSION = re.compile(
    r"(?:^|[/:-])(?:embedding|embed|rerank|moderation|whisper|tts|asr|realtime|dall-e|gpt-image|sora)"
    r"(?:s$|$|[-_.:0-9])", re.I,
)
_GEMINI_NON_TEXT = re.compile(
    r"(?:^|[-_.])(?:image|live|audio|tts|embedding|translate|transcribe|robotics|omni)(?:$|[-_.@])", re.I,
)
_JS_SPACE = r"\u0009-\u000d\u0020\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
_MODEL_SPACE = re.compile(rf"[{_JS_SPACE}\x00-\x1f]")
_EDGE_SPACE = re.compile(rf"^[{_JS_SPACE}]+|[{_JS_SPACE}]+$")
DISCOVERY_TIMEOUT = 12.0
MAX_DISCOVERY_PAGES = 12
_UNSET = object()


class ProviderCatalogError(RuntimeError):
    def __init__(self, message: str, status: int = 400, code: str = "MODEL_CATALOG_ERROR"):
        super().__init__(message)
        self.status = status
        self.code = code


class ProviderConfigStore(Protocol):
    def get_provider_config(self, provider: str | None = None) -> dict: ...
    def get_model_catalog_configs(self) -> dict: ...
    def save_provider_config(self, patch: dict) -> dict: ...


def get_provider_definition(provider):
    return PROVIDERS.get(provider) if isinstance(provider, str) else None


def provider_requires_key(provider):
    definition = get_provider_definition(provider) or {}
    return definition.get("transport") != "mock" and definition.get("requiresKey") is not False


def same_endpoint_origin(left, right):
    def origin(value):
        if not isinstance(value, str) or not value:
            return None
        url = httpx.URL(_EDGE_SPACE.sub("", value))
        if not url.host or url.scheme not in ("http", "https", "ws", "wss", "ftp"):
            return None
        default = {"http": 80, "https": 443, "ws": 80, "wss": 443, "ftp": 21}
        return url.scheme, url.raw_host, url.port if url.port is not None else default[url.scheme]

    try:
        left_origin = origin(left)
        return left_origin is not None and left_origin == origin(right)
    except (httpx.InvalidURL, ValueError):
        return False


def normalize_model_ids(models):
    if not isinstance(models, list):
        return []
    result = {}
    for model in models:
        if not isinstance(model, str):
            continue
        model = _EDGE_SPACE.sub("", model)
        if model and not _MODEL_SPACE.search(model) and len(model.encode("utf-16-le", "surrogatepass")) <= 512:
            result[model] = None
            if len(result) == 5000:
                break
    return list(result)


def is_masked_api_key(key):
    return isinstance(key, str) and bool("****" in key or "..." in key or re.fullmatch(r"\*+", key))


def mask_api_key(key):
    if not isinstance(key, str):
        return ""
    key = _EDGE_SPACE.sub("", key)
    raw = key.encode("utf-16-le", "surrogatepass")
    if len(raw) <= 16:
        return "****" if key else ""
    return (raw[:8] + "****".encode("utf-16-le") + raw[-8:]).decode("utf-16-le", "surrogatepass")


def validate_base_url(raw):
    if isinstance(raw, str) and raw.strip():
        try:
            url = httpx.URL(raw.strip())
            if url.scheme in ("http", "https") and url.host and not url.userinfo and "?" not in str(url) and "#" not in str(url):
                if url.port == (443 if url.scheme == "https" else 80):
                    url = url.copy_with(port=None)
                return str(url).rstrip("/")
        except (httpx.InvalidURL, ValueError):
            pass
    raise ProviderCatalogError("请填写完整的 HTTP 或 HTTPS API 地址，地址中不能包含账号、参数或片段。")


def _require_provider(provider):
    definition = get_provider_definition(provider)
    if definition is None:
        raise ProviderCatalogError("未找到该模型供应商。", 404, "UNKNOWN_PROVIDER")
    return definition


def _configured(provider, config):
    return provider == "mock" or bool(config["baseUrl"] and
        (config["apiKey"] if provider_requires_key(provider) else config["explicitlyConfigured"]))


def _siwc_entry(service):
    status = service.catalog()
    models = list(status["modelLabels"])
    preferred = status["selectedModel"]
    return {
        "providerId": SIWC, "label": PROVIDERS[SIWC]["label"], "description": PROVIDERS[SIWC]["description"],
        "authKind": "oauth", "configured": status["configured"], "oauthState": status["state"],
        "accountLabel": status["accountLabel"], "profileId": status["profileId"],
        "catalogVersion": status["catalogVersion"], "allStages": status["allStages"],
        "baseUrl": SIWC_RESOURCE, "defaultBaseUrl": SIWC_RESOURCE, "apiKeyMasked": "", "requiresKey": False,
        "discoverySupported": True, "modelSource": "discovered", "models": models,
        "modelLabels": status["modelLabels"], "selectedModel": preferred if preferred in models else next(iter(models), ""),
    }


def _discovery_capability(definition, base_url):
    if definition["transport"] != "vertex":
        return definition["transport"] != "mock" and definition.get("discover") is not False, None, None
    try:
        url = httpx.URL(validate_base_url(base_url))
    except ProviderCatalogError:
        return False, None, None
    if re.fullmatch(r"(?:[a-z0-9-]+-)?aiplatform\.googleapis\.com\.?", url.host, re.I):
        return False, None, "vertex-oauth"
    if re.search(r":generateContent$", url.path, re.I):
        return False, None, None
    if url.path == "/" or re.search(r"/v1(?:alpha\d*|beta\d*)?$", url.path, re.I):
        url = url.copy_with(raw_path=url.raw_path.rstrip(b"/") + b"/models")
    elif not url.path.endswith("/models"):
        return False, None, None
    return True, url.copy_set_param("pageSize", "100"), None


def _model_choices(definition, config, block):
    manual = config["manualModels"]
    manual_only = block == "vertex-oauth" and bool(manual)
    discovered = not manual_only and bool(config["discoveredModels"])
    fallback = [] if manual_only else config["discoveredModels"] if discovered else definition["models"]
    models = normalize_model_ids([*manual, *fallback, config["savedModel"]])
    selected = config["model"] if config["model"] in models else models[0] if models else ""
    return {"models": models, "selectedModel": selected,
            "modelSource": "discovered" if discovered else "manual" if manual else "preset"}


def _public_entry(provider, definition, config):
    configured = _configured(provider, config)
    supported, _, block = _discovery_capability(definition, config["baseUrl"])
    return {
        "providerId": provider, "label": definition["label"], "description": definition["description"],
        "configured": configured, "baseUrl": config["baseUrl"] if configured or config["explicitlyConfigured"] else definition["baseUrl"],
        "defaultBaseUrl": definition["baseUrl"], "apiKeyMasked": mask_api_key(config["apiKey"]),
        "requiresKey": provider_requires_key(provider), **_model_choices(definition, config, block),
        "discoverySupported": supported, **({"discoveryBlock": block} if block else {}),
    }


async def list_model_catalog(store: ProviderConfigStore):
    snapshot = await asyncio.to_thread(store.get_model_catalog_configs)
    entries = [_public_entry(key, definition, snapshot["configs"][key])
               for key, definition in PROVIDERS.items() if key != SIWC]
    service = getattr(store, "openai_siwc", None)
    if service is not None and service.status()["enabled"]:
        entries.insert(2, _siwc_entry(service))
    active = next((entry["providerId"] for entry in entries if entry["providerId"] == snapshot["activeProvider"]
                   and entry["configured"] and entry["models"]), "mock")
    return {"activeProvider": active, "providers": entries}


def _update_model_settings(store, provider, definition, payload):
    if provider == "mock":
        store.save_provider_config({"provider": provider})
        return _public_entry(provider, definition, store.get_provider_config(provider))
    if not isinstance(payload, Mapping):
        raise ProviderCatalogError("连接配置格式有误。")
    current = store.get_provider_config(provider)
    patch = {"provider": provider, "validateConnection": True,
             "expectedConnection": {key: current[key] for key in ("baseUrl", "apiKey")}}
    if "baseUrl" in payload:
        raw = payload["baseUrl"]
        patch["baseUrl"] = validate_base_url(definition["baseUrl"] if isinstance(raw, str) and not raw.strip() else raw)
    elif not current["explicitlyConfigured"] and not current["apiKey"]:
        patch["baseUrl"] = validate_base_url(definition["baseUrl"])
    if "apiKey" in payload:
        if not isinstance(payload["apiKey"], str):
            raise ProviderCatalogError("API Key 格式有误。")
        patch["apiKey"] = payload["apiKey"]
    if "models" in payload:
        models = payload["models"]
        if not isinstance(models, list) or any(not normalize_model_ids([model]) for model in models):
            raise ProviderCatalogError("模型编号不能包含空格。")
        patch["manualModels"] = normalize_model_ids(models)
    if "selectedModel" in payload and payload["selectedModel"] != "":
        _, _, block = _discovery_capability(definition, current["baseUrl"])
        offered = _model_choices(definition, {**current, "manualModels": patch.get("manualModels", current["manualModels"])}, block)
        if payload["selectedModel"] not in offered["models"]:
            raise ProviderCatalogError("请从供应商的模型列表选择默认模型。")
        patch["model"] = payload["selectedModel"]
    store.save_provider_config(patch)
    return _public_entry(provider, definition, store.get_provider_config(provider))


async def update_model_settings(store: ProviderConfigStore, provider: str, payload=_UNSET):
    definition = _require_provider(provider)
    if provider == SIWC:
        service = getattr(store, "openai_siwc", None)
        if service is None or not isinstance(payload, dict) or set(payload) != {"selectedModel"}:
            raise ProviderCatalogError("ChatGPT 连接只能通过登录管理。", 400, "SIWC_OAUTH_ONLY")
        if not isinstance(payload["selectedModel"], str):
            raise ProviderCatalogError("请从 ChatGPT 的模型目录选择模型。")
        await service.select_default(payload["selectedModel"])
        return _siwc_entry(service)
    return await asyncio.to_thread(_update_model_settings, store, provider, definition, {} if payload is _UNSET else payload)


def _models(value):
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _permits_text(modalities):
    return not isinstance(modalities, list) or any(isinstance(mode, str) and mode.lower() == "text" for mode in modalities)


def _model_request(provider, definition, config, vertex_endpoint):
    base = validate_base_url(config["baseUrl"])
    root = httpx.URL(base)
    key = config["apiKey"]
    transport = definition["transport"]
    if transport == "vertex":
        return "vertex", vertex_endpoint, {"x-goog-api-key": key}
    if transport == "gemini":
        return "gemini", httpx.URL(base + "/models?pageSize=100"), {"x-goog-api-key": key}
    if transport == "anthropic":
        suffix = "/models" if root.path.endswith("/v1") else "/v1/models"
        return "anthropic", httpx.URL(base + suffix + "?limit=100"), {
            "x-api-key": key, "anthropic-version": "2023-06-01",
        }
    headers = {"Authorization": "Bearer " + key} if key else {}
    if provider == "qwen" and re.search(r"(?:^|\.)aliyuncs\.com$", root.host, re.I):
        return "qwen", root.join("/api/v1/models?capabilities=TG&page_no=1&page_size=50"), headers
    endpoint = httpx.URL(base + "/models")
    if provider == "openrouter":
        endpoint = endpoint.copy_set_param("output_modalities", "text")
    if provider == "siliconflow":
        endpoint = endpoint.copy_set_param("type", "chat")
    return "compatible", endpoint, headers


def _output_modalities(model):
    architecture = model.get("architecture")
    declared = architecture.get("output_modalities") if isinstance(architecture, dict) else None
    return declared if isinstance(declared, list) or declared else model.get("output_modalities")


def _read_page(kind, body):
    if kind in ("gemini", "vertex"):
        entries = body.get("models")
        if kind == "vertex":
            entries = next((body.get(key) for key in ("publisherModels", "models", "data") if body.get(key) is not None), None)
        ids = []
        for entry in _models(entries):
            name = entry.get("name")
            if kind == "vertex" and name is None:
                name = entry.get("id")
            if not isinstance(name, str):
                continue
            methods = entry.get("supportedGenerationMethods")
            modalities = next((entry[key] for key in ("outputModalities", "output_modalities") if entry.get(key) is not None), None)
            if not _permits_text(modalities if modalities is not None else _output_modalities(entry)):
                continue
            if kind == "gemini":
                name = name.removeprefix("models/")
                if isinstance(methods, list) and "generateContent" in methods and not _GEMINI_NON_TEXT.search(name):
                    ids.append(name)
                continue
            name = re.sub(r"^(?:publishers/google/)?models/", "", name)
            if not re.fullmatch(r"gemini-[\w.-]+(?:@[\w.-]+)?", name, re.I | re.ASCII) or _GEMINI_NON_TEXT.search(name):
                continue
            if isinstance(methods, list) and "generateContent" not in methods:
                continue
            ids.append(name)
        token = body.get("nextPageToken")
        return ids, ("pageToken", token) if isinstance(token, str) and token else None

    if kind == "qwen":
        page = body.get("output") or {}
        ids = [model.get("model") for model in _models(page.get("models"))
               if _permits_text((model.get("inference_metadata") or {}).get("response_modality"))]
        number, size, total = (int(page.get(key) or 0) for key in ("page_no", "page_size", "total"))
        return ids, ("page_no", str(number + 1)) if number * size < total else None

    entries = _models(body.get("data"))
    ids = [model.get("id") for model in entries if kind == "anthropic" or _permits_text(_output_modalities(model))]
    if kind == "compatible":
        for field, parameter in (("nextPageToken", "pageToken"), ("next_page_token", "page_token"), ("next_cursor", "cursor")):
            token = body.get(field)
            if isinstance(token, str) and token:
                return ids, (parameter, token)
    token = body.get("last_id")
    parameter = "after_id" if kind == "anthropic" else "after"
    return ids, (parameter, token) if body.get("has_more") and isinstance(token, str) and token else None


async def _fetch_models(provider, definition, config, endpoint, client):
    kind, url, headers = _model_request(provider, definition, config, endpoint)
    ids, pages = [], set()
    try:
        async with asyncio.timeout(DISCOVERY_TIMEOUT):
            for _ in range(MAX_DISCOVERY_PAGES):
                pages.add(url)
                # Build an independent request so a caller's default auth/headers cannot be sent to another profile.
                response = await client.send(httpx.Request("GET", url, headers=headers), auth=None, follow_redirects=False)
                if not response.is_success:
                    raise ProviderCatalogError(
                        f"供应商的模型列表请求失败（HTTP {response.status_code}）。可检查连接，或使用已保存的模型。",
                        502, "MODEL_SYNC_FAILED",
                    )
                models, cursor = _read_page(kind, response.json())
                ids.extend(models)
                if cursor is None:
                    break
                url = url.copy_set_param(*cursor)
                if url in pages:
                    break
    except (httpx.HTTPError, TimeoutError, ValueError, TypeError, AttributeError) as exc:
        raise ProviderCatalogError("暂时无法读取供应商的模型列表，可继续使用已保存的模型。", 502, "MODEL_SYNC_FAILED") from exc

    ids = normalize_model_ids([model for model in ids if isinstance(model, str) and not _MODEL_EXCLUSION.search(model)])
    if not ids:
        raise ProviderCatalogError("供应商未返回可用于正文生成的文本模型。", 422, "EMPTY_MODEL_CATALOG")
    return ids


async def sync_provider_models(store: ProviderConfigStore, provider: str, *, client: httpx.AsyncClient | None = None):
    definition = _require_provider(provider)
    if provider == SIWC:
        service = getattr(store, "openai_siwc", None)
        if service is None:
            raise ProviderCatalogError("当前工作台未启用本机 ChatGPT 登录。", 403, "SIWC_DISABLED")
        await service.discover()
        return _siwc_entry(service)
    config = await asyncio.to_thread(store.get_provider_config, provider)
    supported, endpoint, block = _discovery_capability(definition, config["baseUrl"])
    if not supported:
        message = ("Google Vertex AI 官方模型列表需要 OAuth 2.0，当前 API 密钥无法同步。请在「模型管理」登记要用的模型 ID，再回到任务中选择。"
                   if block == "vertex-oauth" else "该供应商使用预设或手动保存的模型。")
        raise ProviderCatalogError(message, 400, "MODEL_DISCOVERY_UNSUPPORTED")
    if not _configured(provider, config):
        raise ProviderCatalogError("请先保存供应商连接。")
    if client is None:
        async with httpx.AsyncClient(timeout=DISCOVERY_TIMEOUT, trust_env=False) as owned:
            models = await _fetch_models(provider, definition, config, endpoint, owned)
    else:
        models = await _fetch_models(provider, definition, config, endpoint, client)
    await asyncio.to_thread(store.save_provider_config, {
        "provider": provider, "discoveredModels": models, "keepActiveProvider": True,
        "expectedConnection": {key: config[key] for key in ("baseUrl", "apiKey")},
    })
    return _public_entry(provider, definition, await asyncio.to_thread(store.get_provider_config, provider))
