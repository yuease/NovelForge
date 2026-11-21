import asyncio
import json
import os
import tempfile
import unittest
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

import httpx

from novel_forge import llm
from novel_forge.storage import Store


FIXTURE = Path(__file__).with_name("fixtures") / "test_python_llm.json"
LOCAL = "http://127.0.0.1:6123"
OPTIONS = {"provider": "openai", "base_url": LOCAL + "/one", "model": "fixture"}
GATEWAY_ENV = {
    "NOVEL_FORGE_PROVIDER_CIRCUIT": "1", "NOVEL_FORGE_PROVIDER_CIRCUIT_COOLDOWN_MS": "1000",
    "NOVEL_FORGE_OUTPUT_GUARD": "1", "NOVEL_FORGE_MIN_OUTPUT_TOKENS": "",
}


def chat(content="可见正文", finish_reason="stop", usage=None):
    return httpx.Response(200, json={
        "choices": [{"message": {"content": content}, "finish_reason": finish_reason}], "usage": usage,
    })


def google(content="可见正文", finish_reason="STOP"):
    parts = [{"text": "内部思考", "thought": True}]
    if content:
        parts.append({"text": content})
    return httpx.Response(200, json={
        "candidates": [{"content": {"parts": parts}, "finishReason": finish_reason}],
        "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 25, "thoughtsTokenCount": 300,
                          "totalTokenCount": 425},
    })


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, GATEWAY_ENV))
        llm.reset_provider_circuit_state()
        llm.reset_mock_state()
        self.addCleanup(llm.reset_provider_circuit_state)
        self.addCleanup(llm.reset_mock_state)

    async def client(self, handler):
        return await self.enterAsyncContext(httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    async def test_deepseek_connection_and_saved_credentials_are_bound_to_origin(self):
        store = Store(Path(self.enterContext(tempfile.TemporaryDirectory())))
        secret = "sample-deepseek-key"
        store.save_provider_config({"provider": "deepseek", "baseUrl": LOCAL, "apiKey": secret,
                                    "model": "deepseek-v4-pro", "thinkingMode": "enabled"})
        calls = []

        def handle(request):
            calls.append((request, json.loads(request.content)))
            return chat("OK", usage={"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16,
                                    "prompt_cache_hit_tokens": 8})

        client = await self.client(handle)
        probe = await llm.test_connection(store=store, client=client)
        self.assertTrue(probe["ok"])
        self.assertEqual(probe["replyPreview"], "OK")
        self.assertNotIn(secret, str(probe))
        request, body = calls[0]
        self.assertEqual(request.url.path, "/chat/completions")
        self.assertEqual(request.headers["authorization"], f"Bearer {secret}")
        self.assertEqual(body["model"], "deepseek-v4-pro")
        self.assertEqual(body["max_tokens"], 16_384)
        self.assertEqual(body["thinking"], {"type": "disabled"})
        self.assertEqual(body["temperature"], 0)
        self.assertEqual(probe["usage"], {"promptTokens": 12, "completionTokens": 4, "totalTokens": 16, "cacheHitTokens": 8})

        masked = "****"
        await llm.call_llm(provider="deepseek", base_url=LOCAL + "/v2", api_key=masked,
                           thinking_mode=None, store=store, client=client)
        self.assertEqual(calls[-1][0].headers["authorization"], f"Bearer {secret}")
        self.assertEqual(calls[-1][1]["thinking"], {"type": "enabled"})
        await llm.call_llm(provider="deepseek", base_url="http://127.0.0.1:6124", api_key=masked,
                           store=store, client=client)
        self.assertNotIn("authorization", calls[-1][0].headers)
        await llm.call_llm(provider="deepseek", base_url="http://127.0.0.1:6124", api_key="ignored",
                           model="new-model", use_saved_connection=True, store=store, client=client)
        self.assertEqual(calls[-1][0].url.port, 6123)
        self.assertEqual(calls[-1][0].headers["authorization"], f"Bearer {secret}")
        self.assertEqual(calls[-1][1]["model"], "new-model")
        missing = await llm.test_connection(provider="deepseek", base_url="http://127.0.0.1:6124",
                                            store=store, client=client)
        self.assertFalse(missing["ok"])
        self.assertEqual(len(calls), 4)

    async def test_openai_compatible_and_anthropic_contracts(self):
        calls = []

        def handle(request):
            calls.append((request, json.loads(request.content)))
            if request.url.path.endswith("/messages"):
                return httpx.Response(200, json={"content": [{"text": "甲"}, {"type": "tool_use"}, {"text": "乙"}],
                                                 "stop_reason": "end_turn", "usage": {"input_tokens": 6, "output_tokens": 4}})
            return chat()

        client = await self.client(handle)
        for provider, model in (("openai", "gpt-5-mini"), ("openai", "o3-mini"), ("openai-compatible", "gpt-5-mini"),
                                ("moonshot", "kimi-k2.6"), ("anthropic", "fixture")):
            result = await llm.call_llm(provider=provider, model=model, system="设定", prompt="正文",
                                        api_key="test-key", base_url=LOCAL + "/v1/", max_tokens=1000,
                                        temperature=0.5, client=client)
        for _, body in calls[:2]:
            self.assertEqual(body["max_completion_tokens"], 1000)
            self.assertNotIn("temperature", body)
            self.assertNotIn("max_tokens", body)
        self.assertEqual(calls[0][0].url.path, "/v1/chat/completions")
        self.assertEqual(calls[2][1]["max_tokens"], 1000)
        self.assertEqual(calls[2][1]["temperature"], 0.5)
        self.assertNotIn("temperature", calls[3][1])
        request, body = calls[-1]
        self.assertEqual(request.url.path, "/v1/messages")
        self.assertEqual(request.headers["x-api-key"], "test-key")
        self.assertEqual(request.headers["anthropic-version"], "2023-06-01")
        self.assertEqual(body["system"], "设定")
        self.assertEqual(body["messages"], [{"role": "user", "content": "正文"}])
        self.assertEqual(result["text"], "甲乙")
        self.assertEqual(result["finishReason"], "end_turn")
        self.assertEqual(result["usage"]["totalTokens"], 10)

    async def test_vertex_routes_profiles_separately_and_retries_only_thinking_rejection(self):
        store = Store(Path(self.enterContext(tempfile.TemporaryDirectory())))
        calls = []
        for provider, path in (("vertex", "/old/models"), ("vertex-new", "/new/models")):
            store.save_provider_config({"provider": provider, "baseUrl": LOCAL + path, "apiKey": provider + "-key"})

        def handle(request):
            body = json.loads(request.content)
            calls.append((request, body))
            if request.url.path.endswith("gemini-4-carbon:generateContent"):
                return httpx.Response(403, text="forbidden")
            if body["generationConfig"].get("thinkingConfig") == {"thinkingLevel": "MINIMAL"}:
                return httpx.Response(400, text="Thinking level is unsupported for this model")
            return google()

        client = await self.client(handle)
        first = await llm.call_llm(provider="vertex", model="gemini-4-argon-search", max_tokens=400, store=store, client=client)
        self.assertEqual(first["text"], "可见正文")
        self.assertEqual(first["usage"]["thoughtsTokens"], 300)
        self.assertEqual(first["usage"]["completionTokens"], 325)
        self.assertEqual(first["usage"]["totalTokens"], 425)
        request, body = calls[0]
        self.assertEqual(request.url.path, "/old/models/gemini-4-argon:generateContent")
        self.assertEqual(request.headers["x-goog-api-key"], "vertex-key")
        self.assertNotIn("authorization", request.headers)
        self.assertEqual(request.url.query, b"")
        self.assertEqual(body["generationConfig"]["thinkingConfig"], {"thinkingLevel": "LOW"})
        self.assertEqual(body["generationConfig"]["maxOutputTokens"], 1024)
        self.assertEqual([rule["threshold"] for rule in body["safetySettings"]], ["BLOCK_NONE"] * 4)
        self.assertEqual(llm.resolve_vertex_thinking_config("gemini-3.8-flash-build"), {"thinkingBudget": 0})
        self.assertIsNone(llm.resolve_vertex_thinking_config("gemini-3.1-pro-preview"))
        self.assertNotIn("/projects/", llm.build_vertex_endpoint("", "gemini-4-argon"))
        direct = LOCAL + "/model:generateContent"
        self.assertEqual(llm.build_vertex_endpoint(direct, "other"), direct)

        await llm.call_llm(provider="vertex-new", model="gemini-3.5-flash", retries=0, store=store, client=client)
        self.assertEqual(len(calls), 3)
        for request, body in calls[1:]:
            self.assertEqual(request.url.path, "/new/models/gemini-3.5-flash:generateContent")
            self.assertEqual(request.headers["x-goog-api-key"], "vertex-new-key")
        self.assertNotIn("thinkingConfig", calls[-1][1]["generationConfig"])
        with self.assertRaises(llm.LLMError) as caught:
            await llm.call_llm(provider="vertex", model="gemini-4-carbon", retries=3, store=store, client=client)
        self.assertEqual((caught.exception.code, caught.exception.status, caught.exception.retryable), ("HTTP_403", 403, False))
        self.assertEqual(len(calls), 4)

    async def test_owned_client_proxy_selection_and_external_client_ownership(self):
        options = []
        real_client = httpx.AsyncClient

        def create_client(**kwargs):
            options.append(kwargs)
            return real_client(transport=httpx.MockTransport(lambda request: (
                google() if request.url.path.endswith(":generateContent") else chat()
            )))

        with patch.dict(os.environ, {"HTTP_PROXY": "http://http.invalid:8888", "HTTPS_PROXY": "http://https.invalid:8888",
                                     "VERTEX_PROXY": "http://vertex.invalid:8888"}):
            with patch.object(llm.httpx, "AsyncClient", side_effect=create_client):
                await llm.call_llm(provider="vertex", base_url="https://vertex.example", model="alpha")
                await llm.call_llm(provider="vertex-new", base_url=LOCAL, model="alpha")
                await llm.call_llm(provider="vertex", base_url="http://localhost:5999", model="alpha")
                await llm.call_llm(provider="openai", base_url="https://custom.example", model="alpha")
                with patch.dict(os.environ, {"VERTEX_PROXY": ""}):
                    await llm.call_llm(provider="vertex", base_url="https://vertex.example", model="beta")
                with patch.dict(os.environ, {"VERTEX_PROXY": "", "HTTPS_PROXY": ""}):
                    await llm.call_llm(provider="vertex", base_url="https://vertex.example", model="gamma")
                async with real_client(transport=httpx.MockTransport(lambda _: google())) as injected:
                    await llm.call_llm(provider="vertex", base_url=LOCAL, client=injected)
                    self.assertFalse(injected.is_closed)
        self.assertEqual(len(options), 6)
        self.assertTrue(all(option["trust_env"] is False for option in options))
        self.assertEqual([option.get("proxy") for option in options], [
            "http://vertex.invalid:8888", None, None, None, "http://https.invalid:8888", "http://http.invalid:8888",
        ])

    async def test_thinking_only_increases_budget_without_exposing_thought(self):
        calls = []

        def handle(request):
            body = json.loads(request.content)
            budget = body["generationConfig"]["maxOutputTokens"]
            calls.append(budget)
            if request.url.host == "localhost":
                self.assertEqual(request.url.params["key"], "fake+key/?")
            return google("可见正文" if budget > 8192 else "", "STOP" if budget > 8192 else "MAX_TOKENS")

        client = await self.client(handle)
        for provider, host in (("vertex", LOCAL), ("gemini", "http://localhost:6123")):
            result = await llm.call_llm(provider=provider, base_url=host, model="gemini-2.5-flash", api_key="fake+key/?",
                                        max_tokens=8192, retries=1, client=client)
            self.assertEqual(result["text"], "可见正文")
        self.assertEqual(calls, [8192, 16384, 8192, 16384])
        calls.clear()
        with self.assertRaises(llm.LLMError) as caught:
            await llm.call_llm(provider="vertex", base_url=LOCAL, max_tokens=256, retries=1, client=client)
        self.assertEqual(caught.exception.code, "OUTPUT_TRUNCATED")
        self.assertEqual(calls, [1024, 2048])
        with patch.dict(os.environ, {"NOVEL_FORGE_OUTPUT_GUARD": "0"}):
            result = await llm.call_llm(provider="gemini", base_url=LOCAL, max_tokens=8192, retries=0, client=client)
            self.assertEqual(result["text"], "")
            result = await llm.call_llm(provider="vertex", base_url=LOCAL, max_tokens=16384, retries=0, client=client)
            self.assertEqual(result["text"], "可见正文")

    async def test_truncation_guard_budget_floor_rejection_and_probe(self):
        tried = []
        rejected_at = float("inf")
        okay_at = set()

        def handle(request):
            budget = json.loads(request.content)["max_tokens"]
            tried.append(budget)
            if budget >= rejected_at:
                return httpx.Response(400, text="max_tokens exceeds the maximum")
            return chat("OK") if budget in okay_at else chat('{"partial":', "length")

        client = await self.client(handle)
        for max_tokens, extra in ((65_536, {"max_retries": 5}), (8192, {"retry_empty_response": False}),
                                  (8192, {"retries": 0})):
            with self.assertRaises(llm.LLMError) as caught:
                await llm.call_llm(**OPTIONS, max_tokens=max_tokens, client=client, **extra)
            self.assertEqual(caught.exception.code, "OUTPUT_TRUNCATED")
            self.assertEqual(caught.exception.request_max_tokens, max_tokens)
            self.assertEqual(tried, [max_tokens])
            tried.clear()
        rejected_at = 16_384
        okay_at = {12_288}
        result = await llm.call_llm(**OPTIONS, max_tokens=8192, retries=1, client=client)
        self.assertEqual(result["text"], "OK")
        self.assertEqual(tried, [8192, 16_384, 12_288])
        tried.clear()

        rejected_at = 12_288
        with self.assertRaises(llm.LLMError) as caught:
            await llm.call_llm(**OPTIONS, max_tokens=8192, max_retries=5, client=client)
        self.assertEqual((caught.exception.code, caught.exception.request_max_tokens, caught.exception.rejected_output_tokens),
                         ("OUTPUT_TRUNCATED", 12160, 12288))
        self.assertEqual(tried, [8192, 16384, 12288, 10240, 11264, 11776, 12032, 12160])
        tried.clear()
        okay_at = {8192}
        with patch.dict(os.environ, {"NOVEL_FORGE_MIN_OUTPUT_TOKENS": "8192"}):
            await llm.call_llm(**OPTIONS, max_tokens=4096, retries=0, client=client)
            self.assertEqual(tried, [8192])
            tried.clear()
            probe = await llm.test_connection(**OPTIONS, api_key="fixture", client=client)
            self.assertTrue(probe["ok"])
            self.assertEqual(tried, [16_384, 8192])
            tried.clear()
            with patch.dict(os.environ, {"NOVEL_FORGE_OUTPUT_GUARD": "0"}):
                result = await llm.call_llm(**OPTIONS, max_tokens=4096, client=client)
                self.assertEqual(result["finishReason"], "length")
                self.assertEqual(tried, [4096])
                self.assertEqual(llm.protected_output_budget(4096, 16_384), 4096)

    async def test_rate_limit_http_and_network_errors_control_retry(self):
        calls = []
        steps = iter(())

        def handle(request):
            calls.append(request)
            action = next(steps)
            if isinstance(action, Exception):
                raise action
            return action

        client = await self.client(handle)
        for provider, text, code in (
            ("openai", "rate limit reached", "OPENAI_RATE_LIMIT"),
            ("deepseek", "insufficient_quota", "QUOTA_EXCEEDED"),
        ):
            steps = iter([httpx.Response(429, text=text, headers={"Retry-After": "2"})])
            with self.assertRaises(llm.LLMError) as caught:
                await llm.call_llm(provider=provider, base_url=LOCAL, retries=5, client=client)
            self.assertEqual((caught.exception.code, caught.exception.status, caught.exception.retryable,
                              caught.exception.retry_after_ms), (code, 429, False, 2000))
        self.assertEqual(len(calls), 2)
        steps = iter([httpx.Response(429, text="try again in 1s", headers={"Retry-After": "0"}), chat()])
        response = await llm.call_llm(provider="moonshot", base_url=LOCAL, max_retries=1, client=client)
        self.assertEqual(response["text"], "可见正文")
        self.assertEqual(len(calls), 4)

        steps = iter([httpx.RemoteProtocolError("peer disconnected"), chat()])
        response = await llm.call_llm(**OPTIONS, retries=1, client=client)
        self.assertEqual(response["text"], "可见正文")
        self.assertEqual(len(calls), 6)
        steps = iter([httpx.Response(400, text="invalid argument fixture")])
        with self.assertRaises(llm.LLMError) as caught:
            await llm.call_llm(**OPTIONS, api_key="fixture", client=client)
        self.assertEqual(caught.exception.code, "HTTP_400")
        self.assertNotIn("fixture", str(caught.exception))
        self.assertEqual(len(calls), 7)
        steps = iter([httpx.Response(200, text="{invalid json")])
        with self.assertRaises(llm.LLMError) as caught:
            await llm.call_llm(**OPTIONS, client=client)
        self.assertEqual(caught.exception.code, "INVALID_RESPONSE")
        self.assertEqual(len(calls), 8)

    async def test_circuit_separates_provider_and_endpoint_and_checks_between_retries(self):
        calls = 0
        healthy = False

        def handle(_):
            nonlocal calls
            calls += 1
            return chat() if healthy else httpx.Response(503, text="busy")

        client = await self.client(handle)
        with self.assertRaises(llm.LLMError) as caught:
            await llm.call_llm(**OPTIONS, max_retries=5, client=client)
        self.assertEqual(caught.exception.code, "CIRCUIT_OPEN")
        self.assertTrue(caught.exception.retry_at.endswith("Z"))
        self.assertEqual(calls, 3)
        with self.assertRaises(llm.LLMError) as caught:
            await llm.call_llm(provider="openai", base_url=OPTIONS["base_url"] + "/", model="different", client=client)
        self.assertEqual(caught.exception.code, "CIRCUIT_OPEN")
        self.assertEqual(calls, 3)
        healthy = True
        await llm.call_llm(provider="openai", base_url=LOCAL + "/two", retries=0, client=client)
        await llm.call_llm(provider="deepseek", base_url=OPTIONS["base_url"], retries=0, client=client)
        with patch.dict(os.environ, {"NOVEL_FORGE_PROVIDER_CIRCUIT": "0"}):
            await llm.call_llm(**OPTIONS, retries=0, client=client)
        self.assertEqual(calls, 6)

    async def test_circuit_stale_success_does_not_close_new_circuit(self):
        now = 100.0
        calls = 0
        action = lambda: httpx.Response(503, text="busy")

        async def handle(_):
            nonlocal calls
            calls += 1
            value = action()
            return await value if asyncio.iscoroutine(value) else value

        async def hold():
            nonlocal action
            ready = asyncio.Event()
            response = asyncio.get_running_loop().create_future()

            async def wait():
                ready.set()
                return await response

            action = wait
            candidate = asyncio.create_task(call())
            await asyncio.wait_for(ready.wait(), 1)
            return candidate, response

        async def error(code, task=None):
            with self.assertRaises(llm.LLMError) as caught:
                await (task if task is not None else call())
            self.assertEqual(caught.exception.code, code)
            return caught.exception

        client = await self.client(handle)

        async def call():
            return await llm.call_llm(**OPTIONS, retries=0, client=client)

        with patch.object(llm, "_clock", lambda: now):
            stale, response = await hold()
            action = lambda: httpx.Response(503, text="busy")
            for _ in range(3):
                await error("HTTP_503")
            response.set_result(chat())
            await stale
            await error("CIRCUIT_OPEN")
            self.assertEqual(calls, 4)

    async def half_open_circuit(self):
        clock = self.enterContext(patch.object(llm, "_clock", return_value=100.0))
        requests = asyncio.Queue()
        sent = []

        async def handle(request):
            label = json.loads(request.content)["messages"][-1]["content"]
            sent.append(label)
            if label == "seed":
                return httpx.Response(503, text="busy")
            if label == "other-loop":
                return chat(label)
            response = asyncio.get_running_loop().create_future()
            requests.put_nowait((label, response))
            return await response

        client = await self.client(handle)

        async def call(label):
            return await llm.call_llm(**OPTIONS, retries=0, allow_parallel=True, prompt=label, client=client)

        for _ in range(3):
            with self.assertRaises(llm.LLMError) as caught:
                await call("seed")
            self.assertEqual(caught.exception.code, "HTTP_503")
        clock.return_value += 1.001
        sent.clear()
        return call, requests, sent

    async def test_circuit_probe_success_releases_all_waiters(self):
        call, requests, sent = await self.half_open_circuit()
        probe = asyncio.create_task(call("probe"))
        _, response = await asyncio.wait_for(requests.get(), 1)
        waiters = [asyncio.create_task(call(label)) for label in ("first", "second")]
        await asyncio.sleep(0)
        self.assertEqual(sent, ["probe"])
        self.assertTrue(all(not waiter.done() for waiter in waiters))
        response.set_result(chat("probe"))
        for _ in waiters:
            label, response = await asyncio.wait_for(requests.get(), 1)
            response.set_result(chat(label))
        replies = await asyncio.wait_for(asyncio.gather(probe, *waiters), 1)
        self.assertEqual([reply["text"] for reply in replies], ["probe", "first", "second"])

    async def test_circuit_probe_failure_fails_waiters_without_transport(self):
        for failure, code in (
            (httpx.Response(503, text="still down"), "HTTP_503"),
            (httpx.ReadTimeout("probe timed out"), "TIMEOUT"),
            (httpx.ConnectError("probe disconnected"), "NETWORK_ERROR"),
        ):
            with self.subTest(code=code):
                llm.reset_provider_circuit_state()
                call, requests, sent = await self.half_open_circuit()
                probe = asyncio.create_task(call("probe"))
                _, response = await asyncio.wait_for(requests.get(), 1)
                waiters = [asyncio.create_task(call(label)) for label in ("first", "second")]
                await asyncio.sleep(0)
                self.assertEqual(sent, ["probe"])
                self.assertTrue(all(not waiter.done() for waiter in waiters))
                if isinstance(failure, Exception):
                    response.set_exception(failure)
                else:
                    response.set_result(failure)
                errors = await asyncio.wait_for(asyncio.gather(probe, *waiters, return_exceptions=True), 1)
                self.assertEqual([error.code for error in errors], [code, "CIRCUIT_OPEN", "CIRCUIT_OPEN"])
                self.assertTrue(errors[0].circuit_retry_at)
                self.assertEqual([error.retry_at for error in errors[1:]], [errors[0].circuit_retry_at] * 2)
                self.assertEqual(sent, ["probe"])

    async def test_cancelled_circuit_probe_hands_off_to_one_waiter(self):
        call, requests, sent = await self.half_open_circuit()
        probe = asyncio.create_task(call("probe"))
        _, response = await asyncio.wait_for(requests.get(), 1)
        waiters = [asyncio.create_task(call(label)) for label in ("first", "second")]
        await asyncio.sleep(0)
        self.assertEqual(sent, ["probe"])
        probe.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await probe
        self.assertTrue(response.cancelled())
        label, response = await asyncio.wait_for(requests.get(), 1)
        self.assertEqual(sent, ["probe", label])
        self.assertTrue(all(not waiter.done() for waiter in waiters))
        response.set_result(chat(label))
        label, response = await asyncio.wait_for(requests.get(), 1)
        response.set_result(chat(label))
        replies = await asyncio.wait_for(asyncio.gather(*waiters), 1)
        self.assertEqual([reply["text"] for reply in replies], ["first", "second"])

    async def test_circuit_waiter_cancellation_and_timeout_do_not_cancel_probe(self):
        call, requests, sent = await self.half_open_circuit()
        probe = asyncio.create_task(call("probe"))
        _, response = await asyncio.wait_for(requests.get(), 1)
        waiter = asyncio.create_task(call("cancelled"))
        await asyncio.sleep(0)
        self.assertFalse(waiter.done())
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        with self.assertRaises(TimeoutError):
            async with asyncio.timeout(0.01):
                await call("timed out")
        self.assertFalse(response.done())
        self.assertEqual(sent, ["probe"])
        response.set_result(chat("probe"))
        self.assertEqual((await asyncio.wait_for(probe, 1))["text"], "probe")

    async def test_circuit_probe_wakes_waiter_in_another_event_loop(self):
        call, requests, sent = await self.half_open_circuit()
        probe = asyncio.create_task(call("probe"))
        _, response = await asyncio.wait_for(requests.get(), 1)
        loop = asyncio.get_running_loop()
        registered = asyncio.Event()
        original_wait = llm._wait_for_probe

        async def report_wait(waiter):
            loop.call_soon_threadsafe(registered.set)
            return await original_wait(waiter)

        async def other_loop_call():
            async with asyncio.timeout(2):
                return await call("other-loop")

        with patch.object(llm, "_wait_for_probe", report_wait):
            waiter = asyncio.create_task(asyncio.to_thread(asyncio.run, other_loop_call()))
            await asyncio.wait_for(registered.wait(), 1)
            self.assertEqual(sent, ["probe"])
            response.set_result(chat("probe"))
            replies = await asyncio.wait_for(asyncio.gather(probe, waiter), 1)
        self.assertEqual([reply["text"] for reply in replies], ["probe", "other-loop"])

    async def test_circuit_excludes_429_empty_truncated_and_cancelled_calls(self):
        actions = iter([
            *[lambda: httpx.Response(429) for _ in range(3)],
            *[lambda: chat("", "length") for _ in range(3)],
            *[lambda: chat("") for _ in range(3)],
        ])
        client = await self.client(lambda _: next(actions)())
        for code in ("OPENAI_RATE_LIMIT", "OUTPUT_TRUNCATED", "EMPTY_RESPONSE"):
            for _ in range(3):
                with self.assertRaises(llm.LLMError) as caught:
                    await llm.call_llm(**OPTIONS, retries=0, client=client)
                self.assertEqual(caught.exception.code, code)

        logged = asyncio.Event()

        async def stalled(_):
            logged.set()
            await asyncio.Future()

        blocking_client = await self.client(stalled)
        task = asyncio.create_task(llm.call_llm(**OPTIONS, retries=0, client=blocking_client))
        await asyncio.wait_for(logged.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        with self.assertRaises(llm.LLMError) as caught:
            await llm.call_llm(**OPTIONS, retries=0, timeout_ms=20, client=blocking_client)
        self.assertEqual((caught.exception.code, caught.exception.status), ("TIMEOUT", 408))
        self.assertFalse(asyncio.current_task().cancelling())

        def timeout(_):
            raise httpx.ReadTimeout("fixture socket timed out")

        timing_client = await self.client(timeout)
        for _ in range(2):
            with self.assertRaises(llm.LLMError) as caught:
                await llm.call_llm(**OPTIONS, retries=0, client=timing_client)
            self.assertEqual(caught.exception.code, "TIMEOUT")
        self.assertIsNotNone(caught.exception.circuit_retry_at)
        with self.assertRaises(llm.LLMError) as caught:
            await llm.call_llm(**OPTIONS, retries=0, client=client)
        self.assertEqual(caught.exception.code, "CIRCUIT_OPEN")

    async def test_pacing_with_parallel_starts_serial_completion_and_429_cooldown(self):
        now = 100.0
        seen = []
        entered = asyncio.Event()
        release = asyncio.Event()
        waiting = asyncio.Event()

        async def sleep(seconds):
            nonlocal now
            now += seconds
            await asyncio.sleep(0)

        async def handle(request):
            data = json.loads(request.content)
            model = request.url.path.split("/")[-1] if "generationConfig" in data else data["model"]
            prompt = data["contents"][0]["parts"][0]["text"] if "generationConfig" in data else data["messages"][-1]["content"]
            seen.append((now, model, prompt))
            if prompt == "hold":
                entered.set()
                await release.wait()
            if prompt == "rate-limit":
                return httpx.Response(429, headers={"Retry-After": "2"})
            return google() if "generationConfig" in data else chat()

        client = await self.client(handle)

        async def call(model, prompt, *, provider="deepseek", allow_parallel=False):
            if prompt in {"queued", "cancelled queued"}:
                waiting.set()
            return await llm.call_llm(provider=provider, base_url="https://models.example.test", model=model, prompt=prompt,
                                      allow_parallel=allow_parallel, client=client, retries=0)

        with patch.object(llm, "_clock", lambda: now), patch.object(llm, "_sleep", sleep):
            for allow_parallel, model in ((False, "serial"), (True, "parallel")):
                entered.clear()
                release.clear()
                waiting.clear()
                first = asyncio.create_task(call(model, "hold", allow_parallel=allow_parallel))
                await asyncio.wait_for(entered.wait(), 1)
                previous_start = now
                if not allow_parallel:
                    cancelled = asyncio.create_task(call(model, "cancelled queued", allow_parallel=False))
                    await asyncio.wait_for(waiting.wait(), 1)
                    cancelled.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await cancelled
                    self.assertFalse(any(prompt == "cancelled queued" for _, _, prompt in seen))
                    waiting.clear()
                queued = asyncio.create_task(call(model, "queued", allow_parallel=allow_parallel))
                await asyncio.wait_for(waiting.wait(), 1)
                if allow_parallel:
                    await asyncio.wait_for(queued, 1)
                    self.assertFalse(first.done())
                else:
                    self.assertFalse(queued.done())
                    self.assertEqual(seen[-1][-1], "hold")
                release.set()
                await asyncio.wait_for(asyncio.gather(first, queued), 1)
                self.assertAlmostEqual(seen[-1][0] - previous_start, 0.25)

            with self.assertRaises(llm.LLMError) as caught:
                await call("serial", "rate-limit")
            self.assertEqual(caught.exception.code, "HTTP_429")
            rate_time = now
            await call("unique", "unrelated")
            self.assertEqual(now, rate_time)
            await call("serial", "after cooldown")
            self.assertEqual(now - rate_time, 15)
            for model, gap in (("gemini-4-argon", 2.5), ("gemini-2.5-flash", 0.9)):
                await call(model, "one", provider="vertex")
                before = now
                await call(model, "two", provider="vertex")
                self.assertAlmostEqual(now - before, gap)

    async def test_mock_modes_state_namespacing_and_volume_summary(self):
        scenario = {"mode": "fault-injection", "runId": "fault"}

        async def mock(task, prompt="", scenario=None):
            return (await llm.call_llm(system=f"[TASK:{task}]", prompt=prompt, test_scenario=scenario))["text"]

        first = await mock("CHAPTER_WRITE", "第1章\n本章目标字数：700", scenario)
        self.assertIn("骨裂", first)
        bad = await mock("CHAPTER_WRITE", "第2章\n本章目标字数：700", scenario)
        self.assertIn("【此处待补充", bad)
        verdict = json.loads(await mock("VERIFY", "第2章 左臂骨裂\n【待审校正文】" + bad + "\n[TODO]", scenario))
        self.assertEqual([issue["type"] for issue in verdict["issues"]], ["placeholder", "injury_contradiction"])
        repaired = await mock("REPAIR", "第2章\n本章目标字数：700", scenario)
        self.assertNotIn("【此处待补充", repaired)
        self.assertNotIn("【此处待补充", await mock("CHAPTER_WRITE", "第2章", scenario))
        for chapter, status in ((2, "planted"), (6, "reinforced"), (11, "reinforced"), (18, "paid_off")):
            for name in ("foreshadowing", "long-range-key"):
                self.assertIn("玄天七号", await mock("CHAPTER_WRITE", f"第{chapter}章", name))
                facts = json.loads(await mock("FACT_EXTRACTION", f"第{chapter}章", name))
                self.assertEqual(facts["foreshadowing"][0]["status"], status)
        timeout_scenario = {"mode": "timeout-once", "runId": "timeout", "failAtChapter": 5}
        with self.assertRaises(llm.LLMError) as caught:
            await mock("CHAPTER_WRITE", "第5章", timeout_scenario)
        self.assertEqual(caught.exception.code, "TIMEOUT")
        self.assertIn("第5章", await mock("CHAPTER_WRITE", "第5章", timeout_scenario))
        with self.assertRaises(llm.LLMError) as caught:
            await llm.call_mock_llm(test_scenario="always-timeout", timeout_ms=1)
        self.assertEqual(caught.exception.code, "TIMEOUT")
        for run_id in ("a", "b"):
            audit_scenario = {"mode": "audit-fail-once", "runId": run_id}
            self.assertFalse(json.loads(await mock("FULL_AUDIT", scenario=audit_scenario))["passed"])
            self.assertTrue(json.loads(await mock("FULL_AUDIT", scenario=audit_scenario))["passed"])
        style_plan = await llm.call_mock_llm(system="[TASK:BOOK_PLAN]\n" + llm.STYLE_BIBLE_MARKER, prompt="总章数：18")
        self.assertIn("styleBible", json.loads(style_plan["text"]))
        self.assertNotIn("styleBible", json.loads(await mock("BOOK_PLAN")))

        chapters = [{"chapterNumber": index, "title": f"章题{index}", "summary": f"进展{index}" if index % 2 else ""}
                    for index in range(11, 21)]
        volume = json.loads(await mock("VOLUME_SUMMARY", "给第11–20章归档。\n" + json.dumps(chapters)))
        self.assertEqual(volume, llm.build_mock_volume_summary(chapters))
        self.assertEqual([point["chapter"] for point in volume["turningPoints"]], [11, 15, 20])
        self.assertIn("第20章：章题20", volume["mainline"])
        llm.reset_mock_state()
        self.assertIn("【此处待补充", await mock("CHAPTER_WRITE", "第2章", scenario))

    async def test_mock_outputs_and_token_estimates_are_deterministic(self):
        samples = ["", "   ", "中文 mixed  words 👋", "字㐀附加\ufeffend", "古\xa0言 e-mail", "abc\x1fdef"]
        cases = [
            {"system": "[TASK:TOPIC_CANDIDATES]", "prompt": "候选选题\n题材：悬疑\n视角：第一人称\n参考素材标题：旧钟楼"},
            {"system": f"[TASK:BOOK_PLAN]\n{llm.STYLE_BIBLE_MARKER}",
             "prompt": "书名：雾港\n总章数：18", "testScenario": "long-range-key"},
            {"system": "[TASK:CHAPTER_OUTLINES]", "prompt": "起始章节：1\n结束章节：18", "testScenario": "long-range-key"},
            {"system": "[TASK:CHAPTER_WRITE]", "prompt": "第2章\n叙事视角：第一人称\n本章目标字数：800",
             "testScenario": "long-range-key"},
            {"system": "[TASK:FACT_EXTRACTION]", "prompt": "第18章", "testScenario": "long-range-key"},
            {"system": "[TASK:VERIFY]", "prompt": "第2章\n【待审校正文】占位[TODO]"},
            {"system": "[TASK:REPAIR]", "prompt": "第2章\n目标字数：800"},
            {"system": "[TASK:WORD_COUNT_ADJUST]", "prompt": "第2章\n目标字数：1200\n【待调整正文】风吹过。"},
            {"system": "[TASK:DE_AI]", "prompt": "【待润色正文】第一段。\n\n第二段。"},
            {"system": "[TASK:FULL_AUDIT]", "prompt": "终审"},
            {"system": "", "prompt": "回复OK"},
            {"system": "", "prompt": "无特定任务"},
        ]
        reference = json.loads(FIXTURE.read_text(encoding="utf-8"))["test_mock_outputs_and_token_estimates_are_deterministic"]
        self.assertEqual([llm.estimate_tokens(text) for text in samples], reference["tokens"])
        self.assertEqual(len(cases), len(reference["replies"]))
        for case, expected in zip(cases, reference["replies"]):
            with self.subTest(task=case["system"]):
                actual = await llm.call_llm(system=case["system"], prompt=case["prompt"], test_scenario=case.get("testScenario"))
                self.assertEqual(sha256(actual["text"].encode()).hexdigest(), expected["textSha256"])
                self.assertEqual(actual["usage"], expected["usage"])
