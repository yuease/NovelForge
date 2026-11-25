import asyncio
import base64
import hashlib
import json
import logging
import os
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from novel_forge.__main__ import RedactingAccessFilter
from novel_forge.app import create_app
from novel_forge.editor_desk import EditorDesk
from novel_forge.llm import LLMError, call_llm, reset_provider_circuit_state
from novel_forge.openai_siwc import COOKIE, DISCOVERY, ENDPOINTS, ISSUER, PREFIX, RESOURCE, SCOPES
from novel_forge.revision_checks import fingerprint
from novel_forge.workflow import WorkerError


PORT = 43957
BASE = f"http://127.0.0.1:{PORT}"
UI = f"http://localhost:{PORT}"
MODEL = "gpt-fiction"


def events(*items):
    return b"".join(b"data: " + json.dumps(item, ensure_ascii=False).encode() + b"\r\n\r\n" for item in items)


def completed(text, *, output=None):
    resp = {
        "status": "completed",
        "usage": {"input_tokens": 11, "output_tokens": 12, "total_tokens": 23,
                  "output_tokens_details": {"reasoning_tokens": 4},
                  "input_tokens_details": {"cached_tokens": 3}},
    }
    if output is not None:
        resp["output"] = output
    ev_list = []
    if text:
        ev_list.extend([
            {"type": "response.output_text.delta", "delta": text},
            {"type": "response.output_text.done", "text": text},
        ])
    ev_list.append({"type": "response.completed", "response": resp})
    return events(*ev_list)


class SegmentedStream(httpx.AsyncByteStream):
    def __init__(self, contents):
        self.contents = contents
        self.closed = False

    async def __aiter__(self):
        for index in range(0, len(self.contents), 13):
            yield self.contents[index:index + 13]

    async def aclose(self):
        self.closed = True


class SiwcTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.public_key = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(cls.key.public_key()))
        cls.public_key.update(kid="test-key", alg="RS256", use="sig")

    async def asyncSetUp(self):
        self.directory = Path(self.enterContext(TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, {
            "PORT": "0", "NOVEL_FORGE_HOST": "127.0.0.1", "NOVEL_FORGE_OPENAI_SIWC": "1",
            "NOVEL_FORGE_OPENAI_SIWC_ALL_STAGES": "0", "NOVEL_FORGE_BACKGROUND_FEEDS": "0",
            "NOVEL_FORGE_AUTO_RECOVER": "0",
        }))
        reset_provider_circuit_state()
        self.addCleanup(reset_provider_circuit_state)
        self.received = []
        self.streams = []
        self.access = "first-access-token"
        self.refresh = "first-refresh-token"
        self.subject = "test-subject"
        self.token_scopes = SCOPES
        self.token_lifetime = 3600
        self.signed_nonce = None
        self.allow_refresh = True
        self.reject_old_token = False
        self.revocation_status = 200
        self.allow_native_tools = False
        self.stream_content_type = "text/event-stream"
        self.stream_contents = completed("收到。")
        self.upstream = await self.enterAsyncContext(httpx.AsyncClient(transport=httpx.MockTransport(self.upstream_request)))
        self.app = create_app(self.directory, client=self.upstream)
        await self.enterAsyncContext(self.app.router.lifespan_context(self.app))
        self.http = await self.enterAsyncContext(httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app, client=("127.0.0.1", 51870)),
            base_url=BASE, follow_redirects=False,
        ))
        self.store = self.app.state.store
        self.service = self.store.openai_siwc

    def tokens(self, *, client_id="oaiapp_test", nonce=None):
        body = {"access_token": self.access, "refresh_token": self.refresh, "token_type": "Bearer",
                "scope": self.token_scopes, "expires_in": self.token_lifetime}
        if nonce is not None:
            body["id_token"] = jwt.encode({
                "iss": ISSUER, "aud": client_id, "sub": self.subject, "exp": int(time.time()) + 300,
                "nonce": nonce, "email": "account@example.test",
            }, self.key, algorithm="RS256", headers={"kid": "test-key"})
        return body

    async def upstream_request(self, request):
        path = str(request.url)
        self.received.append(request)
        if path == DISCOVERY:
            return httpx.Response(200, json={"issuer": ISSUER, **ENDPOINTS})
        if path == ENDPOINTS["jwks_uri"]:
            return httpx.Response(200, json={"keys": [self.public_key]})
        if path == ENDPOINTS["token_endpoint"]:
            form = parse_qs(request.content.decode())
            if form["grant_type"] == ["authorization_code"]:
                self.assertNotIn("client_secret", form)
                self.assertNotIn("ext_agent_host_id", form)
                return httpx.Response(200, json=self.tokens(client_id=form["client_id"][0], nonce=self.signed_nonce))
            self.assertEqual(form["refresh_token"], [self.refresh])
            self.assertNotIn("scope", form)
            if not self.allow_refresh:
                return httpx.Response(400, json={"error": "refresh_token_reused"})
            await asyncio.sleep(0.01)
            self.access, self.refresh = "rotated-access-token", "rotated-refresh-token"
            self.token_lifetime = 3600
            return httpx.Response(200, json=self.tokens())
        if path == ENDPOINTS["revocation_endpoint"]:
            self.assertEqual(parse_qs(request.content.decode())["token_type_hint"], ["refresh_token"])
            return httpx.Response(self.revocation_status)
        if request.headers.get("Authorization") == "Bearer first-access-token" and self.reject_old_token:
            return httpx.Response(401, json={"detail": "credential rejected"})
        self.assertEqual(request.headers["Authorization"], "Bearer " + self.access)
        self.assertNotIn("chatgpt-account-id", request.headers)
        if path == RESOURCE + "/models":
            return httpx.Response(200, json={"models": [
                {"slug": MODEL, "display_name": "Writing Model", "visibility": "list"},
                {"slug": "hidden", "display_name": "Hidden", "visibility": "private"},
                {"slug": "image", "visibility": "list", "output_modalities": ["image"]},
            ]})
        if path == RESOURCE + "/responses":
            body = json.loads(request.content)
            self.assertIs(body["store"], False)
            self.assertIs(body["stream"], True)
            self.assertEqual(body["model"], MODEL)
            self.assertTrue(isinstance(body["input"], list))
            forbidden = {"max_output_tokens", "temperature", "previous_response_id"}
            if not self.allow_native_tools:
                forbidden |= {"reasoning", "tools"}
            self.assertFalse(forbidden & body.keys())
            raw = self.stream_contents(body) if callable(self.stream_contents) else self.stream_contents
            if isinstance(raw, httpx.Response):
                return raw
            stream = raw if isinstance(raw, httpx.AsyncByteStream) else SegmentedStream(raw)
            self.streams.append(stream)
            headers = {"Content-Type": self.stream_content_type} if self.stream_content_type is not None else {}
            return httpx.Response(200, stream=stream, headers=headers)
        self.fail(f"Unexpected upstream endpoint {path}")

    async def begin(self, mode="login"):
        started = await self.http.post(UI + PREFIX + "/start", json={"mode": mode, "returnOrigin": UI}, headers={
            "Origin": UI, "X-Forwarded-Host": "malicious.example", "X-Forwarded-Port": "7777",
        })
        self.assertEqual(started.status_code, 200, started.text)
        self.assertNotIn(COOKIE, started.cookies)
        url = started.json()["url"]
        self.assertTrue(url.startswith(BASE + PREFIX + "/begin?ticket="))
        self.assertEqual((await self.http.get(url.replace(BASE, UI))).status_code, 403)
        begun = await self.http.get(url)
        self.assertEqual(begun.status_code, 302, begun.text)
        self.assertIn("HttpOnly", begun.headers["set-cookie"])
        self.assertIn("SameSite=lax", begun.headers["set-cookie"])
        self.assertEqual((await self.http.get(url)).status_code, 400)
        auth = parse_qs(urlsplit(begun.headers["location"]).query)
        self.signed_nonce = auth["nonce"][0]
        self.assertEqual(auth["redirect_uri"], [BASE + PREFIX + "/callback"])
        self.assertEqual(auth["scope"], [SCOPES])
        self.assertTrue(auth["ext_agent_host_id"][0].startswith("urn:uuid:"))
        return auth

    async def finish(self, auth, **params):
        return await self.http.get(BASE + PREFIX + "/callback", params={
            "state": auth["state"][0], "code": "one-time-code",
            **({"client_id": "oaiapp_test"} if auth["client_id"] == ["dynamic_agent_client"] else {}),
            **params,
        }, headers={"Sec-Fetch-Mode": "navigate", "Sec-Fetch-Site": "cross-site"})

    async def connect(self):
        auth = await self.begin()
        response = await self.finish(auth)
        self.assertEqual(response.headers.get("location"), UI + "/#/models?siwc=connected")
        result = await self.http.post("/api/model-catalog/openai-siwc/discover")
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["provider"]["models"], [MODEL])
        return auth

    async def test_dcr_pkce_cookie_return_and_logout_are_bound_to_local_host(self):
        before = await self.http.post(UI + PREFIX + "/start", headers={"Origin": "https://example.test"}, json={})
        self.assertEqual(before.status_code, 403)
        self.assertEqual((await self.http.post(UI + PREFIX + "/start", headers={
            "Sec-Fetch-Site": "cross-site",
        }, json={})).status_code, 403)
        self.assertEqual((await self.http.post(UI + PREFIX + "/start", json={
            "returnOrigin": "http://localhost:9999",
        })).status_code, 400)
        auth = await self.begin()
        self.assertEqual(auth["client_id"], ["dynamic_agent_client"])
        self.assertEqual(auth["agent_name_hint"], ["NovelForge"])
        self.assertEqual(auth["code_challenge_method"], ["S256"])
        self.assertEqual((await self.finish(auth, state="wrong")).status_code, 400)
        cookie = self.http.cookies.get(COOKIE)
        self.http.cookies.clear()
        self.assertEqual((await self.finish(auth)).status_code, 400)
        self.http.cookies.set(COOKIE, cookie, domain="127.0.0.1", path=PREFIX)
        final = await self.finish(auth, scope="bogus")
        self.assertEqual(final.status_code, 303)
        self.assertEqual(final.headers["location"], UI + "/#/models?siwc=connected")
        self.assertEqual((await self.finish(auth)).status_code, 400)
        token_request = next(req for req in self.received if str(req.url) == ENDPOINTS["token_endpoint"])
        form = parse_qs(token_request.content.decode())
        self.assertEqual(form["client_id"], ["oaiapp_test"])
        self.assertEqual(form["resource"], [RESOURCE])
        challenge = base64.urlsafe_b64encode(hashlib.sha256(form["code_verifier"][0].encode()).digest())
        self.assertEqual(challenge.decode().rstrip("="), auth["code_challenge"][0])
        self.assertEqual(form["redirect_uri"], auth["redirect_uri"])
        self.assertTrue(self.service.vault.path.is_relative_to(self.directory))
        disk = self.service.vault.path.read_text(encoding="utf-8")
        if os.name == "nt":
            self.assertIn("dpapi:", disk)
            self.assertNotIn(self.access, disk)
            self.assertNotIn(self.refresh, disk)
        else:
            self.assertEqual(self.service.vault.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.service.vault.read()["profile"]["access_token"], self.access)
        signed_out = await self.http.post("/api/auth/openai-siwc/sign-out")
        self.assertTrue(signed_out.json()["revocationConfirmed"])
        self.assertNotIn("refresh_token", self.service.vault.read()["profile"])
        again = await self.begin()
        self.assertEqual(again["client_id"], ["oaiapp_test"])
        self.assertNotIn("agent_name_hint", again)
        self.assertEqual(again["ext_agent_host_id"], auth["ext_agent_host_id"])

    async def test_denial_bad_identity_and_missing_scope_do_not_enable_inference(self):
        auth = await self.begin()
        denied = await self.finish(auth, error="access_denied")
        self.assertTrue(denied.headers["location"].endswith("siwc=denied"))
        self.assertFalse(any(req.url == ENDPOINTS["token_endpoint"] for req in self.received))
        auth = await self.begin()
        self.signed_nonce = "not-the-issued-nonce"
        self.assertTrue((await self.finish(auth)).headers["location"].endswith("siwc=failed"))
        self.assertFalse(self.service.status()["configured"])
        auth = await self.begin()
        self.token_scopes = SCOPES.replace("chatgpt.tokens.use.direct", "")
        self.assertTrue((await self.finish(auth)).headers["location"].endswith("siwc=permission_required"))
        with self.assertRaisesRegex(LLMError, "补授权"):
            await call_llm(provider="openai-siwc", model=MODEL, store=self.store, siwc_context="chief-editor")
        consent = await self.begin("consent")
        self.assertEqual(consent["prompt"], ["consent"])

    async def test_refresh_rotation_serializes_and_terminal_error_preserves_registration(self):
        self.token_lifetime = 30
        auth = await self.begin()
        await self.finish(auth)
        result = await asyncio.gather(*(self.service.bearer(self.service.epoch) for _ in range(5)))
        self.assertEqual({token for token, _ in result}, {"rotated-access-token"})
        forms = [req.content for req in self.received if str(req.url) == ENDPOINTS["token_endpoint"]]
        self.assertEqual(sum(b"grant_type=refresh_token" in item for item in forms), 1)
        self.assertEqual(self.service.vault.read()["profile"]["refresh_token"], "rotated-refresh-token")
        self.allow_refresh = False
        with self.assertRaisesRegex(RuntimeError, "重新登录"):
            await self.service.bearer(self.service.epoch, failed_version=self.service.rotate)
        self.assertEqual(self.service.status()["state"], "reauthorize")
        self.assertEqual(self.service.vault.read()["profile"]["client_id"], "oaiapp_test")

    async def test_expired_ticket_and_account_switch_cannot_reuse_stale_session(self):
        await self.connect()
        version = self.service.epoch
        profile = self.service.status()["profileId"]
        start = await self.http.post(UI + PREFIX + "/start", json={"returnOrigin": UI})
        url = start.json()["url"]
        next(iter(self.service.tickets.values())).expires = time.monotonic() - 1
        self.assertEqual((await self.http.get(url)).status_code, 400)
        auth = await self.begin("switch")
        self.assertEqual(auth["client_id"], ["dynamic_agent_client"])
        self.subject = "new-subject"
        self.assertEqual((await self.finish(auth, client_id="oaiapp_other")).status_code, 303)
        self.assertNotEqual(self.service.status()["profileId"], profile)
        self.assertEqual(self.service.catalog()["modelLabels"], {})
        with self.assertRaisesRegex(RuntimeError, "重新登录"):
            await self.service.bearer(version)
        with self.assertRaises(LLMError) as caught:
            await call_llm(provider="openai-siwc", model=MODEL, store=self.store,
                           siwc_context="chief-editor", siwc_profile=profile)
        self.assertEqual(caught.exception.code, "SIWC_LOGIN_REQUIRED")
        self.revocation_status = 503
        logout = (await self.http.post("/api/auth/openai-siwc/sign-out")).json()
        self.assertFalse(logout["revocationConfirmed"])
        self.assertEqual(logout["state"], "signed_out")
        self.assertNotIn("access_token", self.service.vault.read()["profile"])

    async def test_catalog_and_public_api_hide_all_internal_credentials(self):
        await self.connect()
        for route in ("/api/model-catalog", "/api/config/providers?provider=openai-siwc", PREFIX + "/status", "/api/jobs"):
            body = (await self.http.get(route)).text
            self.assertNotIn(self.access, body)
            self.assertNotIn(self.refresh, body)
            self.assertNotIn("oaiapp_test", body)
        catalog = (await self.http.get("/api/model-catalog")).json()
        entry = next(value for value in catalog["providers"] if value["providerId"] == "openai-siwc")
        self.assertEqual(entry["modelLabels"], {MODEL: "Writing Model"})
        self.assertFalse(entry["allStages"])
        self.assertNotEqual(catalog["activeProvider"], "openai-siwc")
        for route in ("/api/config/providers", "/api/model-catalog/openai-siwc"):
            payload = {"provider": "openai-siwc", "apiKey": "fake"} if route.endswith("providers") else {"apiKey": "fake"}
            self.assertEqual((await self.http.put(route, json=payload)).status_code, 400)
        await self.http.post("/api/providers/test", json={"provider": "openai-siwc", "model": MODEL})
        self.assertTrue(self.streams[-1].closed)

    async def test_sse_completed_uses_visible_text_only_and_counts_thought_tokens_once(self):
        await self.connect()
        self.reject_old_token = True
        result = await call_llm(provider="openai-siwc", model=MODEL, system="系统指令", prompt="测试",
                                store=self.store, siwc_context="chief-editor", retries=0)
        self.assertEqual(result["text"], "收到。")
        self.assertEqual(result["usage"], {
            "promptTokens": 11, "completionTokens": 12, "totalTokens": 23,
            "cacheHitTokens": 3, "thoughtsTokens": 4,
        })
        self.assertEqual(sum(req.url == RESOURCE + "/responses" for req in self.received), 2)
        self.assertTrue(all(stream.closed for stream in self.streams))

    async def test_sse_errors_discard_partial_text_and_do_not_repeat_quota_or_truncation(self):
        await self.connect()
        fragment = {"type": "response.output_text.delta", "delta": '{"broken":'}
        for cases, expected in (
            ([fragment], "SIWC_STREAM_INTERRUPTED"),
            ([fragment, {"type": "response.incomplete", "response": {
                "incomplete_details": {"reason": "max_output_tokens"}}}], "OUTPUT_TRUNCATED"),
            ([fragment, {"type": "response.failed", "response": {
                "error": {"code": "subscription_sharing_usage_limit_exceeded"}}}], "SIWC_USAGE_LIMIT"),
            ([fragment, {"type": "error", "code": "chatpass_v2_scope_not_authorized"}], "SIWC_PERMISSION_REQUIRED"),
        ):
            self.stream_contents = events(*cases)
            count = len(self.streams)
            with self.assertRaises(LLMError) as caught:
                await call_llm(provider="openai-siwc", model=MODEL, store=self.store, siwc_context="chief-editor")
            self.assertEqual(caught.exception.code, expected)
            self.assertEqual(len(self.streams), count + 1)
            self.assertTrue(self.streams[-1].closed)
        self.stream_contents = completed("省略响应头也能正常接收。")
        self.stream_content_type = None
        ok = await call_llm(provider="openai-siwc", model=MODEL, store=self.store, siwc_context="chief-editor")
        self.assertEqual(ok["text"], "省略响应头也能正常接收。")
        self.stream_content_type = "application/json"
        with self.assertRaises(LLMError) as caught:
            await call_llm(provider="openai-siwc", model=MODEL, store=self.store, siwc_context="chief-editor")
        self.assertEqual(caught.exception.code, "SIWC_INVALID_STREAM")
        self.stream_content_type = "text/event-stream"

    async def test_cancelled_stream_closes_and_does_not_leak_partial_output(self):
        await self.connect()
        waiting = asyncio.Event()

        class InFlight(SegmentedStream):
            async def __aiter__(self):
                yield self.contents
                waiting.set()
                await asyncio.Event().wait()

        self.stream_contents = InFlight(events({"type": "response.output_text.delta", "delta": "半截正文"}))
        request = asyncio.create_task(call_llm(
            provider="openai-siwc", model=MODEL, store=self.store, siwc_context="chief-editor",
        ))
        await asyncio.wait_for(waiting.wait(), 5)
        request.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await request
        self.assertTrue(self.stream_contents.closed)

    async def test_chief_editor_runs_real_evidence_tools_with_mock_responses(self):
        await self.connect()
        self.allow_native_tools = True
        created = await self.http.post("/api/jobs/import-txt", json={"text": "她借了三十元。", "title": "原稿"})
        job_id = created.json()["job"]["id"]
        endpoint = f"/api/jobs/{job_id}/editor-desk"
        selection = await self.http.put(endpoint + "/settings", json={"model": {"provider": "openai-siwc", "model": MODEL}})
        self.assertEqual(selection.status_code, 200, selection.text)
        self.assertEqual(selection.json()["model"]["siwcProfileId"], self.service.status()["profileId"])
        rows = self.store.get_chapters(job_id)
        revisions = self.app.state.revisions
        revisions.store.create("run", job_id, {"chapters": rows, "report": {}, "title": "原稿"}, {
            "workflowVersion": 2, "sourceVersion": fingerprint(rows), "chapterCount": 1, "isMock": False,
        })
        revisions.commit("run", status="ready", ledgerVersion=1, candidateTurn=0, hasCandidate=True, products=[
            ("v2-ledger", 1, {"rules": []}),
            ("v2-result", 0, {"candidate": rows, "candidateHash": fingerprint(rows), "ledgerVersion": 1,
                              "outcomes": [], "findings": []}),
        ])
        seen_bodies = []
        round1_output = [
            {"type": "reasoning", "id": "rs_1", "encrypted_content": "opaque-encrypted-cot-1", "summary": []},
            {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "read",
             "arguments": json.dumps({"version": "original", "chapter": 1})},
            {"type": "function_call", "id": "fc_2", "call_id": "call_2", "name": "search",
             "arguments": json.dumps({"version": "candidate", "query": "三十元"})},
        ]
        round2_output = [
            {"type": "function_call", "id": "fc_3", "call_id": "call_3", "name": "submit_revision_plan",
             "arguments": json.dumps({"chapters": [1], "note": "按作者建议修改金额。"}, ensure_ascii=False)},
        ]

        def reply(body):
            step = len(seen_bodies)
            seen_bodies.append(body)
            self.assertTrue(body.get("tools"))
            self.assertEqual(body.get("reasoning"), {"effort": "medium"})
            self.assertEqual(body.get("include"), ["reasoning.encrypted_content"])
            self.assertTrue(body.get("prompt_cache_key", "").startswith("nf-editor-"))
            if step == 0:
                return completed("", output=round1_output)
            if step == 1:
                # Prefix stability + encrypted reasoning replay + tool outputs intact
                self.assertEqual(body["input"][0], seen_bodies[0]["input"][0])
                self.assertEqual(body["instructions"], seen_bodies[0]["instructions"])
                self.assertEqual(body["tools"], seen_bodies[0]["tools"])
                self.assertIn(round1_output[0], body["input"])
                tool_outputs = [item for item in body["input"] if item.get("type") == "function_call_output"]
                self.assertEqual(len(tool_outputs), 2)
                self.assertIn("借了三十元", tool_outputs[0]["output"])
                return completed("", output=round2_output)
            plan_outputs = [item for item in body["input"]
                            if item.get("type") == "function_call_output" and item.get("call_id") == "call_3"]
            self.assertEqual(len(plan_outputs), 1)
            self.assertTrue(json.loads(plan_outputs[0]["output"])["ok"])
            return completed("原稿写的是三十元（E1、E2），已经帮你拟好返修单。")

        self.stream_contents = reply
        response = await self.http.post(endpoint + "/chat", json={"message": "第一章借了多少钱？"})
        self.assertEqual(response.status_code, 200, response.text)
        answer = response.json()["turn"]["answer"]
        self.assertEqual(answer["reply"], "原稿写的是三十元（E1、E2），已经帮你拟好返修单。")
        self.assertEqual([item["id"] for item in answer["evidence"]], ["E1", "E2"])
        self.assertEqual(answer["evidence"][0]["data"]["正文"], "她借了三十元。")
        self.assertIn(".", answer["plan"]["token"])
        rejected = await self.http.post(endpoint + "/dispatch", json={"token": answer["plan"]["token"] + "0"})
        self.assertGreaterEqual(rejected.status_code, 400)
        self.assertEqual(revisions.store.get("run")["candidateTurn"], 0)

        # Verify optional reasoning/include/prompt_cache_key 400 rejection gracefully retries without them.
        seen_fallback = []

        def reject_optional_once(body):
            seen_fallback.append(body)
            if "reasoning" in body:
                return httpx.Response(400, json={"error": {"code": "subscription_sharing_unsupported_capability"}})
            return completed("已自动降级参数并正常回复。")

        self.stream_contents = reject_optional_once
        ok_fallback = await self.http.post(endpoint + "/chat", json={"message": "再说一遍。"})
        self.assertEqual(ok_fallback.status_code, 200, ok_fallback.text)
        self.assertEqual(len(seen_fallback), 2)
        self.assertIn("reasoning", seen_fallback[0])
        self.assertNotIn("reasoning", seen_fallback[1])
        self.assertNotIn("include", seen_fallback[1])
        self.assertNotIn("prompt_cache_key", seen_fallback[1])

        self.stream_contents = events({"type": "response.failed", "response": {
            "error": {"code": "subscription_sharing_usage_limit_exceeded"},
        }})
        limited = await self.http.post(endpoint + "/chat", json={"message": "请再核对一遍。"})
        self.assertEqual(limited.status_code, 429, limited.text)
        self.assertEqual(limited.json()["code"], "SIWC_USAGE_LIMIT")
        self.assertIn("settings/usage", EditorDesk.failed_diagnosis(WorkerError("quota", 429, "SIWC_USAGE_LIMIT")))
        self.assertIn("重新登录", EditorDesk.failed_diagnosis(WorkerError("auth", 401, "SIWC_LOGIN_REQUIRED")))
        self.allow_native_tools = False
        with self.assertRaises(LLMError) as caught:
            await call_llm(provider="openai-siwc", model=MODEL, store=self.store)
        self.assertEqual(caught.exception.code, "SIWC_DISABLED")
        with patch.dict(os.environ, {"NOVEL_FORGE_OPENAI_SIWC": "0"}):
            self.assertFalse(any(entry["providerId"] == "openai-siwc" for entry in
                                 (await self.http.get("/api/model-catalog")).json()["providers"]))
            disabled = await self.http.post(endpoint + "/chat", json={"message": "继续诊断。"})
            self.assertEqual(disabled.json()["code"], "SIWC_DISABLED")


class SiwcAccessLoggingTests(unittest.TestCase):
    def test_redacts_ticket_code_and_state_on_all_response_statuses(self):
        for route in ("begin", "callback"):
            for status in (200, 302, 303, 400, 500):
                record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, "%s %s %s %s %s",
                                           ("peer", "GET", f"{PREFIX}/{route}?code=secret&state=secret", "1.1", status), None)
                self.assertTrue(RedactingAccessFilter().filter(record))
                self.assertEqual(record.args[2], f"{PREFIX}/{route}?[redacted]")
                self.assertNotIn("secret", record.getMessage())
