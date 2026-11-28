"""Sign in with ChatGPT for a single local NovelForge account."""

import asyncio
import base64
import hashlib
import ipaddress
import json
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlencode, urlsplit
from uuid import uuid4

import httpx
import jwt

from .rate_limits import parse_reset_hint, quota_retry_at
from .siwc_vault import SiwcVault


PROVIDER = "openai-siwc"
ISSUER = "https://auth.openai.com"
RESOURCE = "https://api.openai.com/v1"
DISCOVERY = ISSUER + "/.well-known/openid-configuration"
ENDPOINTS = {
    "authorization_endpoint": ISSUER + "/api/accounts/authorize",
    "token_endpoint": ISSUER + "/api/accounts/oauth/token",
    "revocation_endpoint": ISSUER + "/api/accounts/oauth/revoke",
    "jwks_uri": ISSUER + "/.well-known/jwks.json",
}
PREFIX = "/api/auth/openai-siwc"
COOKIE = "novelforge_siwc_flow"
SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
ACCESS_SCOPES = frozenset(("resource.invoke", "chatgpt.tokens.use.direct"))
TTL = 600
ERROR_MESSAGES = {
    "SIWC_LOGIN_REQUIRED": "ChatGPT 授权已失效或已断开，请到「模型管理」重新登录。",
    "SIWC_PERMISSION_REQUIRED": "尚未授权使用 ChatGPT 计划，请到「模型管理」补授权或重新登录。",
    "SIWC_USAGE_LIMIT": "已达到 ChatGPT 计划或应用的用量上限，请在 ChatGPT 设置中查看用量。",
    "SIWC_RATE_LIMIT": "ChatGPT 请求暂时受限，请稍后重试并查看 ChatGPT 用量。",
}
_TERMINAL_REFRESH = {
    "invalid_grant", "invalid_refresh_token", "token_expired", "refresh_token_expired",
    "refresh_token_invalidated", "refresh_token_reused",
}
_RETRYABLE_ACCOUNT = {"subscription_sharing_user_unavailable", "subscription_sharing_usage_unavailable"}
_QUOTA = "subscription_sharing_usage_limit_exceeded"


class SiwcError(RuntimeError):
    def __init__(self, code, status, message=None, *, retryable=False, retry_after_ms=None, retry_at=None,
                 quota_scope=None, quota_kind=None, reset_source=None, no_replay=False):
        super().__init__(message or ERROR_MESSAGES[code])
        self.status, self.code, self.retryable = status, code, retryable and not no_replay
        self.retry_after_ms, self.retry_at = retry_after_ms, retry_at
        self.quota_scope, self.quota_kind, self.reset_source = quota_scope, quota_kind, reset_source
        self.no_replay = no_replay


def enabled():
    return (os.environ.get("NOVEL_FORGE_OPENAI_SIWC", "1") != "0"
            and os.environ.get("NOVEL_FORGE_HOST", "127.0.0.1") == "127.0.0.1")


def all_stages():
    return enabled() and os.environ.get("NOVEL_FORGE_OPENAI_SIWC_ALL_STAGES") == "1"


def is_loopback(host):
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host or "").is_loopback
    except ValueError:
        return False


def listen_port(server):
    configured = os.environ.get("PORT")
    if configured and configured != "0":
        return int(configured)
    if server and is_loopback(server[0]) and type(server[1]) is int and 0 < server[1] <= 65535:
        return server[1]
    return 3457


def allowed_return_origin(value, port):
    if not isinstance(value, str) or not re.fullmatch(
        r"http://(?:127\.0\.0\.1|localhost|\[::1\]):[0-9]{1,5}", value,
    ):
        raise SiwcError("SIWC_RETURN_ORIGIN", 400, "登录只能返回本机工作台。")
    if urlsplit(value).port not in {port, 5173, 5174}:
        raise SiwcError("SIWC_RETURN_ORIGIN", 400, "登录返回端口不在本机工作台白名单内。")
    return value


def _decode(response):
    try:
        result = response.json()
    except (ValueError, UnicodeError, RecursionError):
        result = None
    return result if isinstance(result, dict) else {}


def responses_error(status, payload, *, headers=None, text="", now_utc=None):
    error = payload.get("error") if isinstance(payload.get("error"), dict) else payload
    code = error.get("code")
    if not isinstance(code, str):
        code = ""
    hint = parse_reset_hint(headers=headers, text=text, payload=payload, now=now_utc)
    metadata = dict(retry_after_ms=hint.retry_after_ms, quota_kind=hint.kind, reset_source=hint.source)
    if code == _QUOTA or hint.scope == "long":
        return SiwcError("SIWC_USAGE_LIMIT", 429, retry_at=quota_retry_at(hint, now_utc),
                         quota_scope="long", **metadata)
    if status == 429 or code in ("rate_limit_exceeded", "rate_limit_error"):
        return SiwcError("SIWC_RATE_LIMIT", 429, retryable=True, quota_scope=hint.scope, **metadata)
    if code in ("chatpass_v2_scope_not_authorized", "chatpass_v2_invalid_authorization_context"):
        return SiwcError("SIWC_PERMISSION_REQUIRED", 403)
    if status == 401 or code in ("invalid_token", "subscription_sharing_invalid_user"):
        return SiwcError("SIWC_LOGIN_REQUIRED", 401)
    if code == "subscription_sharing_user_not_eligible":
        return SiwcError("SIWC_NOT_ELIGIBLE", 403, "此 ChatGPT 账号、工作区或策略不支持计划用量。")
    if code == "subscription_sharing_route_not_supported":
        return SiwcError("SIWC_ROUTE_UNSUPPORTED", 403, "当前官方路由不可用于 ChatGPT 计划。")
    if code == "subscription_sharing_unsupported_capability":
        param = error.get("param")
        suffix = f"（{param}）" if param in ("model", "input", "instructions", "tools", "service_tier") else ""
        return SiwcError("SIWC_CAPABILITY_UNSUPPORTED", 400, "当前模型或请求能力不受支持" + suffix + "。")
    if code in _RETRYABLE_ACCOUNT:
        return SiwcError("SIWC_ACCOUNT_UNAVAILABLE", 503, "暂时无法核对 ChatGPT 计划用量，请稍后重试。",
                         retryable=True)
    if status >= 500 or code == "server_error":
        return SiwcError(f"HTTP_{status if status >= 500 else 502}", status if status >= 500 else 502,
                         "ChatGPT 服务暂时不可用，请稍后重试。", retryable=True)
    if status == 403:
        return SiwcError("SIWC_FORBIDDEN", 403, "ChatGPT 拒绝此请求，请检查账号、授权和区域政策。")
    return SiwcError("SIWC_REQUEST_FAILED", status if status >= 400 else 502, "ChatGPT 请求未完成。")


@dataclass
class Attempt:
    origin: str
    redirect: str
    state: str
    nonce: str
    verifier: str
    cookie: str
    client_id: str
    subject: str
    host_id: str
    consent: bool
    expires: float
    epoch: int


class OpenAISiwc:
    def __init__(self, directory, client=None):
        self.vault = SiwcVault(directory)
        self.client = client
        self.lock = asyncio.Lock()
        self.tickets, self.pending = {}, {}
        self._saved = None
        self._discovery = None
        self._jwks = None
        self.models = {}
        self.catalog_version = 0
        self.epoch = 0
        self.rotate = 0
        self.quota_limited = False
        self._streams = set()

    def _state(self):
        if self._saved is None:
            try:
                self._saved = self.vault.read()
                self.models = dict(self._saved.get("profile", {}).get("models") or {})
            except (ValueError, OSError):
                raise SiwcError("SIWC_VAULT", 500, "无法读取本机 ChatGPT 凭据。") from None
        return self._saved

    def _profile(self):
        return self._state().get("profile", {})

    def _save(self, state):
        try:
            self.vault.write(state)
        except (ValueError, OSError):
            raise SiwcError("SIWC_VAULT", 500, "无法安全保存本机 ChatGPT 凭据。") from None
        self._saved = state

    def status(self):
        available = enabled()
        status = {"enabled": available, "configured": False, "state": "signed_out",
                  "accountLabel": "", "profileId": "", "quotaLimited": False}
        if not available:
            return {**status, "state": "disabled"}
        try:
            profile = self._profile()
        except SiwcError:
            return {**status, "state": "storage_error"}
        configured = bool(profile.get("access_token") and self._permitted(profile)
                          and (profile.get("expires_at", 0) > time.time() or profile.get("refresh_token")))
        state = ("connected" if configured else "permission_required" if profile.get("id_token")
                 and not self._permitted(profile) else "reauthorize" if profile.get("access_token")
                 else profile.get("session_state", "signed_out"))
        return {**status, "configured": configured, "state": state, "accountLabel": profile.get("label", ""),
                "profileId": profile.get("profileId", ""), "quotaLimited": self.quota_limited}

    def catalog(self):
        status = self.status()
        return {**status, "catalogVersion": self.catalog_version, "allStages": all_stages(),
                "modelLabels": dict(self.models) if status["configured"] else {},
                "selectedModel": self._profile().get("selectedModel", "") if status["configured"] else ""}

    async def select_default(self, model):
        async with self.lock:
            self.check_selection(model)
            self._save({**self._state(), "profile": {**self._profile(), "selectedModel": model}})

    def check_selection(self, model, profile_id=None):
        self._require_enabled()
        status = self.status()
        if status["state"] == "permission_required":
            raise SiwcError("SIWC_PERMISSION_REQUIRED", 403)
        if not status["configured"] or (profile_id is not None and profile_id != status["profileId"]):
            raise SiwcError("SIWC_LOGIN_REQUIRED", 401)
        if not model or model not in self.models:
            raise SiwcError("SIWC_MODEL_UNAVAILABLE", 400, "请先在模型管理同步并选择此 ChatGPT 账号提供的模型。")
        return status["profileId"]

    def _require_enabled(self):
        if not enabled():
            raise SiwcError("SIWC_DISABLED", 403, "当前工作台未启用本机 ChatGPT 登录。")

    @staticmethod
    def _permitted(profile):
        return ACCESS_SCOPES <= set(profile.get("scopes", ()))

    def _prune(self):
        now = time.monotonic()
        for entries in (self.tickets, self.pending):
            for key in tuple(entries):
                if entries[key].expires <= now:
                    del entries[key]

    def start(self, origin, port, mode):
        self._require_enabled()
        self._prune()
        if len(self.tickets) + len(self.pending) >= 16:
            raise SiwcError("SIWC_BUSY", 429, "登录尝试过多，请等待已有尝试结束。")
        state = self._state()
        if not state.get("hostId"):
            state = {**state, "hostId": f"urn:uuid:{uuid4()}"}
            self._save(state)
        profile = self._profile() if mode != "switch" else {}
        callback = f"http://127.0.0.1:{port}{PREFIX}/callback"
        ticket = secrets.token_urlsafe(32)
        self.tickets[ticket] = Attempt(
            origin, callback, secrets.token_urlsafe(32), secrets.token_urlsafe(32), secrets.token_urlsafe(48),
            secrets.token_urlsafe(32), profile.get("client_id", ""), profile.get("subject", ""), state["hostId"],
            mode == "consent", time.monotonic() + TTL, self.epoch,
        )
        return f"http://127.0.0.1:{port}{PREFIX}/begin?{urlencode({'ticket': ticket})}"

    async def begin(self, ticket, port):
        self._require_enabled()
        self._prune()
        attempt = self.tickets.pop(ticket, None)
        if (attempt is None or attempt.epoch != self.epoch
                or attempt.redirect != f"http://127.0.0.1:{port}{PREFIX}/callback"):
            raise SiwcError("SIWC_TRANSACTION", 400, "登录链接无效或已过期，请重新发起登录。")
        try:
            meta = await self.metadata()
        except SiwcError:
            return attempt, None
        self.require_epoch(attempt.epoch)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(attempt.verifier.encode()).digest()).decode().rstrip("=")
        params = {
            "response_type": "code", "client_id": attempt.client_id or "dynamic_agent_client",
            "redirect_uri": attempt.redirect, "scope": SCOPES, "resource": RESOURCE,
            "state": attempt.state, "nonce": attempt.nonce, "code_challenge": challenge,
            "code_challenge_method": "S256", "ext_agent_host_id": attempt.host_id,
        }
        if not attempt.client_id:
            params["agent_name_hint"] = "NovelForge"
        if attempt.consent:
            params["prompt"] = "consent"
        self.pending[attempt.state] = attempt
        return attempt, meta["authorization_endpoint"] + "?" + urlencode(params)

    def consume(self, state, cookie, port):
        self._require_enabled()
        self._prune()
        attempt = self.pending.get(state)
        if (not attempt or attempt.epoch != self.epoch or not cookie or not cookie.isascii()
                or not secrets.compare_digest(attempt.cookie, cookie)
                or attempt.redirect != f"http://127.0.0.1:{port}{PREFIX}/callback"):
            raise SiwcError("SIWC_TRANSACTION", 400, "登录事务无效或已过期，请重新发起登录。")
        return self.pending.pop(state)

    @asynccontextmanager
    async def http(self, url):
        if self.client is not None:
            yield self.client
            return
        proxy = os.environ.get("OPENAI_PROXY") or os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
        host = urlsplit(url).hostname
        no_proxy = os.environ.get("NO_PROXY", os.environ.get("no_proxy", ""))
        if any(host == pattern or host.endswith("." + pattern) or pattern == "*"
               for value in no_proxy.split(",") if (pattern := value.strip().lower().removeprefix("."))):
            proxy = None
        try:
            client = httpx.AsyncClient(trust_env=False, proxy=proxy, timeout=httpx.Timeout(15.0))
        except (ImportError, ValueError):
            raise SiwcError("SIWC_PROXY", 400, "ChatGPT 代理设置无效。") from None
        async with client:
            yield client

    async def _send(self, method, url, **kwargs):
        try:
            async with asyncio.timeout(15), self.http(url) as client:
                return await client.send(httpx.Request(method, url, **kwargs), auth=None, follow_redirects=False)
        except (httpx.TimeoutException, TimeoutError):
            raise SiwcError("TIMEOUT", 408, "ChatGPT 请求超时。", retryable=True) from None
        except httpx.RequestError:
            raise SiwcError("NETWORK_ERROR", 502, "无法连接 ChatGPT。", retryable=True) from None

    async def metadata(self):
        if self._discovery is None:
            response = await self._send("GET", DISCOVERY)
            meta = _decode(response)
            if (not response.is_success or meta.get("issuer") != ISSUER
                    or any(meta.get(key) != value for key, value in ENDPOINTS.items())):
                raise SiwcError("SIWC_DISCOVERY", 502, "无法验证 OpenAI 登录端点。")
            self._discovery = meta
        return self._discovery

    async def _identity(self, id_token, client_id, nonce, access_token):
        try:
            header = jwt.get_unverified_header(id_token)
            kid = header.get("kid")
            if header.get("alg") != "RS256" or not isinstance(kid, str) or not kid:
                raise ValueError
            keys = self._jwks or {}
            if kid not in keys:
                meta = await self.metadata()
                response = await self._send("GET", meta["jwks_uri"])
                if not response.is_success:
                    raise ValueError
                keys = {entry["kid"]: entry for entry in _decode(response).get("keys", [])
                        if isinstance(entry, dict) and isinstance(entry.get("kid"), str)
                        and entry.get("kty") == "RSA" and entry.get("use", "sig") == "sig"
                        and entry.get("alg", "RS256") == "RS256"}
                self._jwks = keys
            key = jwt.PyJWK.from_dict(keys[kid], algorithm="RS256").key
            claims = jwt.decode(id_token, key, algorithms=["RS256"], audience=client_id, issuer=ISSUER,
                                leeway=30, options={"require": ["iss", "aud", "sub", "exp", "nonce"]})
            audience = claims["aud"]
            if (((isinstance(audience, list) and len(audience) > 1) or "azp" in claims)
                    and claims.get("azp") != client_id):
                raise ValueError
            if claims["nonce"] != nonce or not isinstance(claims["sub"], str) or not claims["sub"].strip():
                raise ValueError
            if "at_hash" in claims:
                digest = hashlib.sha256(access_token.encode("ascii")).digest()
                if claims["at_hash"] != base64.urlsafe_b64encode(digest[:16]).decode().rstrip("="):
                    raise ValueError
            return claims
        except (jwt.PyJWTError, KeyError, TypeError, ValueError, AttributeError):
            raise SiwcError("SIWC_INVALID_IDENTITY", 400, "无法验证 OpenAI 返回的登录身份。") from None

    def _tokens(self, data, previous=None):
        access, lifetime = data.get("access_token"), data.get("expires_in")
        token_type = data.get("token_type")
        if (not isinstance(access, str) or not re.fullmatch(r"[A-Za-z0-9._~+/-]+=*", access)
                or not isinstance(token_type, str) or token_type.lower() != "bearer"
                or type(lifetime) is not int or lifetime <= 0):
            raise SiwcError("SIWC_TOKEN_RESPONSE", 502, "OpenAI 未返回可使用的登录凭据。")
        scope = data.get("scope")
        if scope is None and previous is not None:
            scopes = previous["scopes"]
        elif isinstance(scope, str):
            scopes = scope.split()
        else:
            raise SiwcError("SIWC_TOKEN_RESPONSE", 502, "OpenAI 未返回已授予的权限。")
        refresh = data.get("refresh_token")
        if (previous is not None or "offline_access" in scopes) and (not isinstance(refresh, str) or not refresh):
            raise SiwcError("SIWC_TOKEN_RESPONSE", 502, "OpenAI 未返回可续期凭据。")
        return {"access_token": access, "refresh_token": refresh if isinstance(refresh, str) else "",
                "scopes": scopes, "expires_at": time.time() + min(lifetime, 3600),
                "earliest_refresh_at": self._refresh_time(data.get("earliest_refresh_at"))}

    @staticmethod
    def _refresh_time(value):
        try:
            if isinstance(value, str):
                return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
            if type(value) in (float, int) and 0 < value < 100_000_000_000:
                return float(value)
        except (ValueError, OverflowError, OSError):
            pass
        return 0

    async def exchange(self, attempt, code, issued):
        client_id = issued or attempt.client_id
        if (not client_id or not re.fullmatch(r"oaiapp_[A-Za-z0-9_-]+", client_id)
                or attempt.client_id and client_id != attempt.client_id or not code or len(code) > 4096):
            raise SiwcError("SIWC_TRANSACTION", 400, "授权返回的注册信息无效。")
        meta = await self.metadata()
        response = await self._send("POST", meta["token_endpoint"], data={
            "grant_type": "authorization_code", "client_id": client_id, "code": code,
            "code_verifier": attempt.verifier, "redirect_uri": attempt.redirect, "resource": RESOURCE,
        })
        data = _decode(response)
        if not response.is_success:
            raise SiwcError("SIWC_EXCHANGE", 400, "OpenAI 未接受授权码，请重新发起登录。")
        tokens = self._tokens(data)
        id_token = data.get("id_token")
        claims = await self._identity(id_token, client_id, attempt.nonce, tokens["access_token"])
        if attempt.subject and attempt.subject != claims["sub"]:
            raise SiwcError("SIWC_INVALID_IDENTITY", 400, "返回的 ChatGPT 账号与待续期的账号不一致。")
        profile = {
            **tokens, "client_id": client_id, "subject": claims["sub"], "id_token": id_token,
            "profileId": hashlib.sha256((client_id + ":" + claims["sub"]).encode()).hexdigest()[:32],
            "label": next((claims[key][:150] for key in ("email", "name")
                           if isinstance(claims.get(key), str) and claims[key]), "ChatGPT"),
        }
        async with self.lock:
            self.require_epoch(attempt.epoch)
            self._save({**self._state(), "profile": profile})
            await self._reset_session()
        return self.status()

    async def _reset_session(self):
        self.epoch += 1
        self.rotate += 1
        self.models = {}
        self.catalog_version += 1
        self.quota_limited = False
        if self._streams:
            await asyncio.gather(*(response.aclose() for response in tuple(self._streams)), return_exceptions=True)

    async def _clear_session(self, profile, state):
        self._save({**self._state(), "profile": {
            key: profile[key] for key in ("client_id", "subject", "profileId", "label") if key in profile
        } | {"session_state": state}})
        await self._reset_session()

    async def sign_out(self):
        self._require_enabled()
        async with self.lock:
            profile = self._profile()
            self.tickets.clear()
            self.pending.clear()
            await self._clear_session(profile, "signed_out")
            refresh = profile.get("refresh_token")
            if not refresh:
                return True
            for attempt in range(2):
                try:
                    meta = await self.metadata()
                    response = await self._send("POST", meta["revocation_endpoint"], data={
                        "token": refresh, "token_type_hint": "refresh_token", "client_id": profile["client_id"],
                    })
                    if response.status_code == 200:
                        return True
                    if response.status_code < 500:
                        break
                except SiwcError as error:
                    if not error.retryable:
                        break
                if not attempt:
                    await asyncio.sleep(0.25)
            return False

    async def bearer(self, epoch, *, failed_version=None):
        async with self.lock:
            self._require_enabled()
            profile = self._profile()
            if epoch != self.epoch or not profile.get("access_token"):
                raise SiwcError("SIWC_LOGIN_REQUIRED", 401)
            if not self._permitted(profile):
                raise SiwcError("SIWC_PERMISSION_REQUIRED", 403)
            now = time.time()
            expires = profile["expires_at"]
            earliest = profile.get("earliest_refresh_at", 0)
            forced = failed_version is not None and failed_version == self.rotate
            if (expires > now and not forced and (expires > now + 60 or earliest > now
                                                 or not profile.get("refresh_token"))):
                return profile["access_token"], self.rotate
            if not profile.get("refresh_token") or earliest > now:
                raise SiwcError("SIWC_LOGIN_REQUIRED", 401)
            meta = await self.metadata()
            response = await self._send("POST", meta["token_endpoint"], data={
                "grant_type": "refresh_token", "client_id": profile["client_id"],
                "refresh_token": profile["refresh_token"], "resource": RESOURCE,
            })
            data = _decode(response)
            if not response.is_success:
                error = data.get("error")
                if isinstance(error, str) and error in _TERMINAL_REFRESH:
                    await self._clear_session(profile, "reauthorize")
                    raise SiwcError("SIWC_LOGIN_REQUIRED", 401)
                if error == "invalid_client":
                    raise SiwcError("SIWC_INVALID_CLIENT", 400, "ChatGPT 注册已失效，请切换账号并重新注册。")
                text = response.text.replace(profile["refresh_token"], "[已隐藏密钥]")
                raise responses_error(response.status_code, data, headers=response.headers, text=text)
            updated = {**profile, **self._tokens(data, profile)}
            self._save({**self._state(), "profile": updated})
            self.rotate += 1
            if not self._permitted(updated):
                raise SiwcError("SIWC_PERMISSION_REQUIRED", 403)
            return updated["access_token"], self.rotate

    @asynccontextmanager
    async def authorized(self, method, endpoint, *, epoch, body=None):
        if (method, endpoint) not in {("GET", RESOURCE + "/models"), ("POST", RESOURCE + "/responses")}:
            raise ValueError("Unsupported SIWC endpoint")
        failed_version = None
        for attempt in range(2):
            bearer, version = await self.bearer(epoch, failed_version=failed_version)
            headers = {"Authorization": "Bearer " + bearer}
            if body is not None:
                headers["Accept"] = "text/event-stream"
            accepted = False
            try:
                async with self.http(endpoint) as client:
                    request = httpx.Request(method, endpoint, headers=headers, json=body)
                    if body is not None:
                        request.extensions["timeout"] = httpx.Timeout(None, connect=15).as_dict()
                    response = await client.send(request, stream=True, auth=None, follow_redirects=False)
                    self._streams.add(response)
                    try:
                        self.require_epoch(epoch)
                        if not response.is_success:
                            chunks = bytearray()
                            async for chunk in response.aiter_bytes():
                                chunks.extend(chunk[:65_536 - len(chunks)])
                                if len(chunks) >= 65_536:
                                    break
                            text = chunks.decode("utf-8", errors="replace").replace(bearer, "[已隐藏密钥]")
                            try:
                                payload = json.loads(chunks)
                            except (ValueError, UnicodeError, RecursionError):
                                payload = {}
                            error = responses_error(
                                response.status_code, payload if isinstance(payload, dict) else {},
                                headers=response.headers, text=text,
                            )
                            if error.code == "SIWC_LOGIN_REQUIRED" and response.status_code == 401 and not attempt:
                                failed_version = version
                                continue
                            self.record_error(error, epoch)
                            raise error
                        accepted = True
                        yield response
                        return
                    finally:
                        self._streams.discard(response)
                        await response.aclose()
            except httpx.TimeoutException:
                raise SiwcError("TIMEOUT", 408, "ChatGPT 请求超时。", retryable=True, no_replay=accepted) from None
            except (httpx.RequestError, httpx.StreamError):
                self.require_epoch(epoch)
                raise SiwcError("NETWORK_ERROR", 502, "ChatGPT 连接中断。", retryable=True, no_replay=accepted) from None

    def require_epoch(self, epoch):
        self._require_enabled()
        if epoch != self.epoch:
            raise SiwcError("SIWC_LOGIN_REQUIRED", 401)

    def record_error(self, error, epoch):
        if error.code == "SIWC_USAGE_LIMIT" and epoch == self.epoch:
            self.quota_limited = True

    async def discover(self):
        epoch = self.epoch
        if self.status()["state"] == "permission_required":
            raise SiwcError("SIWC_PERMISSION_REQUIRED", 403)
        try:
            async with asyncio.timeout(20), self.authorized("GET", RESOURCE + "/models", epoch=epoch) as response:
                await response.aread()
                entries = _decode(response).get("models")
                if not isinstance(entries, list):
                    raise SiwcError("SIWC_MODEL_CATALOG", 502, "ChatGPT 返回的模型目录格式无效。")
        except TimeoutError:
            raise SiwcError("TIMEOUT", 408, "读取 ChatGPT 模型目录超时。") from None
        models = {}
        for item in entries:
            if not isinstance(item, dict) or item.get("visibility") != "list":
                continue
            slug, label = item.get("slug"), item.get("display_name")
            modalities = item.get("output_modalities")
            if isinstance(modalities, list) and "text" not in modalities:
                continue
            if isinstance(slug, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}", slug):
                models.setdefault(slug, label[:150] if isinstance(label, str) and label.strip() else slug)
        async with self.lock:
            self.require_epoch(epoch)
            self._save({**self._state(), "profile": {**self._profile(), "models": models}})
            self.models = models
            self.catalog_version += 1
