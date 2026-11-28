import json
import os
import re
import unittest
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from novel_forge import llm
from novel_forge.app import create_app
from novel_forge.openai_siwc import PROVIDER, OpenAISiwc, SiwcError, responses_error
from novel_forge.pipeline import PipelineRuntime
from novel_forge.rate_limits import humanize_wait, parse_duration, parse_reset_hint, retry_at_iso
from novel_forge.storage import Store


NOW = datetime(2026, 11, 28, tzinfo=timezone.utc)
LOCAL = "http://127.0.0.1:8472/v1"
REQUEST = {"provider": "deepseek", "base_url": LOCAL, "api_key": "test-key", "model": "writer"}


def chat(text="正文"):
    return httpx.Response(200, json={"choices": [{"message": {"content": text}, "finish_reason": "stop"}]})


def stream(*events, headers=None):
    return httpx.Response(
        200, headers={"Content-Type": "text/event-stream", **(headers or {})},
        content="".join(f"data: {json.dumps(event)}\n\n" for event in events).encode(),
    )


class ResetHintTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {"NOVEL_FORGE_LONG_QUOTA_SECONDS": "300"}))

    def test_durations(self):
        for value, seconds in (
            ("15 hours, 3 minutes", 54180), ("15h 3m", 54180), ("in 15 hrs", 54000),
            ("90 minutes", 5400), ("7m12s", 432), ("54000s", 54000), ("1.5 hours", 5400),
            ("2 days 4 hours", 187200), ("200ms", 0.2), ("15小时3分钟", 54180),
            ("2天4小时", 187200), ("90分钟", 5400), ("54000秒", 54000),
            ("-15h", None), ("15h -3m", None), ("NaN", None), ("inf", None), ("0s", None),
        ):
            with self.subTest(value=value):
                self.assertEqual(parse_duration(value), seconds)
        self.assertEqual([humanize_wait(s) for s in (54180, 5400, 187200)],
                         ["约 15 小时 3 分", "约 90 分钟", "约 2 天 4 小时"])

    def test_long_reset_sources(self):
        for options, seconds, source in (
            ({"text": "resets in 15 hours, 3 minutes"}, 54180, "text:duration"),
            ({"text": "limit resets in 15h 3m"}, 54180, "text:duration"),
            ({"text": "try again in 15 hrs"}, 54000, "text:duration"),
            ({"text": "resets in 90 minutes"}, 5400, "text:duration"),
            ({"headers": {"Retry-After": "54000"}}, 54000, "header:retry-after"),
            ({"headers": {"Retry-After": "Sat, 28 Nov 2026 10:00:00 GMT"}}, 36000, "header:retry-after"),
            ({"headers": {"rEtRy-AfTeR-mS": "54000000"}}, 54000, "header:retry-after-ms"),
            ({"payload": {"error": {"details": [{"retryDelay": "54000s"}]}}}, 54000, "json:retryDelay"),
            ({"payload": {"error": {"retry_after_ms": 54000000}}}, 54000, "json:field"),
            ({"payload": {"resets_at": NOW.timestamp() + 54000}}, 54000, "json:field"),
            ({"payload": {"reset_at": "2026-11-28T15:00:00Z"}}, 54000, "json:field"),
            ({"text": "额度已用完，15小时3分钟后重置"}, 54180, "text:zh"),
            ({"text": "x" * 1200 + "resets in 15 hours"}, 54000, "text:duration"),
            ({"text": "resets at 2026-11-28T17:03:00+02:00"}, 54180, "text:absolute"),
            ({"text": "resets at 2026-11-28 15:03 UTC"}, 54180, "text:absolute"),
        ):
            with self.subTest(options=options):
                hint = parse_reset_hint(**options, now=NOW)
                self.assertEqual((hint.scope, hint.kind, hint.seconds, hint.source), ("long", "usage", seconds, source))
        self.assertEqual(retry_at_iso(54180, NOW), "2026-11-28T15:03:00Z")

    def test_priority_threshold_and_invalid_times(self):
        healthy = {"x-ratelimit-reset-requests": "15h", "X-RateLimit-Remaining-Requests": "50"}
        hint = parse_reset_hint(headers={
            **healthy, "x-ratelimit-reset-tokens": "30s", "x-ratelimit-remaining-tokens": "0",
        }, now=NOW)
        self.assertEqual((hint.scope, hint.seconds), ("short", 30))
        hint = parse_reset_hint(headers={
            **healthy, "Retry-After": "30", "x-ratelimit-reset-tokens": "45s",
        }, payload={"retryAfter": 54000}, now=NOW)
        self.assertEqual((hint.scope, hint.seconds, hint.source), ("short", 30, "header:retry-after"))
        hint = parse_reset_hint(headers={
            "x-ratelimit-reset-requests": "30s", "x-ratelimit-remaining-requests": "0",
            "anthropic-ratelimit-tokens-reset": "2026-11-28T15:00:00Z",
            "anthropic-ratelimit-tokens-remaining": "0",
        }, now=NOW)
        self.assertEqual((hint.scope, hint.seconds), ("long", 54000))
        for options, scope, seconds in (
            ({"text": "try again in 12s"}, "short", 12),
            ({"text": "RPM limit"}, "short", None),
            ({"text": "TPM quota", "headers": {"Retry-After": "30"}}, "short", 30),
            ({"text": "resets in 5 minutes"}, "short", 300),
            ({"text": "resets in 301 seconds"}, "long", 301),
            ({"text": "daily per day limit"}, "long", None),
            ({"text": "reached your usage limit; Retry in 15s"}, "short", 15),
            ({"text": "resets at 2026-11-28T15:00:00"}, "unknown", None),
            ({"text": "daily limit resets at 2026-11-28T15:00:00"}, "long", None),
            ({"text": "x" * 8000 + " resets in 15 hours"}, "unknown", None),
            ({"headers": {"Retry-After": "Fri, 27 Nov 2026 10:00:00 GMT"}}, "unknown", None),
            ({"headers": {"Retry-After": "NaN"}, "payload": {"retry_after": -3}}, "unknown", None),
        ):
            with self.subTest(options=options):
                hint = parse_reset_hint(**options, now=NOW)
                self.assertEqual((hint.scope, hint.seconds), (scope, seconds))
        with patch.dict(os.environ, {"NOVEL_FORGE_LONG_QUOTA_SECONDS": "60"}):
            self.assertEqual(parse_reset_hint(text="resets in 61s", now=NOW).scope, "long")

    def test_billing_overrides_short_header_without_retry_at(self):
        headers = {"Retry-After": "2"}
        payload = {"error": {"code": "insufficient_quota"}}
        hint = parse_reset_hint(headers=headers, payload=payload, now=NOW)
        self.assertEqual((hint.scope, hint.kind, hint.seconds), ("long", "billing", 2))
        error = llm._http_error("Test", httpx.Response(429, headers=headers, json=payload), "", True)
        self.assertEqual((error.code, error.retry_after_ms, error.provider_code), ("QUOTA_EXCEEDED", 2000, "insufficient_quota"))
        self.assertIsNone(error.retry_at)
        error = responses_error(429, payload, headers=headers, now_utc=NOW)
        self.assertEqual((error.code, error.quota_kind, error.retry_at), ("SIWC_USAGE_LIMIT", "billing", None))


class TransportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {
            "NOVEL_FORGE_LONG_QUOTA_SECONDS": "300", "NOVEL_FORGE_QUOTA_FAST_FAIL": "1",
            "NOVEL_FORGE_PROVIDER_CIRCUIT": "1", "NOVEL_FORGE_HOST": "127.0.0.1", "NOVEL_FORGE_OPENAI_SIWC": "1",
        }))
        llm.reset_quota_blocks()
        llm.reset_provider_circuit_state()
        self.addCleanup(llm.reset_quota_blocks)
        self.addCleanup(llm.reset_provider_circuit_state)

    async def client(self, handler):
        return await self.enterAsyncContext(httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    async def test_long_http_quota_fails_after_one_request(self):
        for options, expected in (
            ({"text": "limit resets in 15h 3m"}, 54_180_000),
            ({"headers": {"Retry-After": "54000"}}, 54_000_000),
            ({"text": "resets in 90 minutes"}, 5_400_000),
        ):
            with self.subTest(options=options):
                handler = AsyncMock(side_effect=lambda _: httpx.Response(429, **options))
                client = await self.client(handler)
                llm.reset_quota_blocks()
                with patch.object(llm, "_sleep", new_callable=AsyncMock) as sleep, self.assertRaises(llm.LLMError) as caught:
                    await llm.call_llm(**REQUEST, retries=5, client=client)
                error = caught.exception
                self.assertEqual((error.code, error.retryable, error.retry_after_ms),
                                 ("QUOTA_EXCEEDED", False, expected))
                self.assertTrue(error.retry_at.endswith("Z"))
                handler.assert_awaited_once()
                sleep.assert_not_awaited()

    async def test_fast_fail_same_model_connection_test_and_expiry(self):
        handler = AsyncMock(side_effect=[httpx.Response(429, headers={"Retry-After": "54000"}), chat(), chat(), chat(), chat()])
        client = await self.client(handler)
        with self.assertRaises(llm.LLMError) as first:
            await llm.call_llm(**REQUEST, client=client)
        with self.assertRaises(llm.LLMError) as blocked:
            await llm.call_llm(**REQUEST, client=client)
        self.assertEqual((blocked.exception.code, blocked.exception.retry_at),
                         ("QUOTA_EXCEEDED", first.exception.retry_at))
        handler.assert_awaited_once()
        self.assertEqual((await llm.call_llm(**{**REQUEST, "model": "another"}, client=client))["text"], "正文")
        self.assertTrue((await llm.test_connection(**REQUEST, client=client))["ok"])
        with self.assertRaises(llm.LLMError):
            await llm.call_llm(**REQUEST, client=client)
        self.assertEqual(handler.await_count, 3)
        with patch.dict(os.environ, {"NOVEL_FORGE_QUOTA_FAST_FAIL": "0"}):
            self.assertEqual((await llm.call_llm(**REQUEST, client=client))["text"], "正文")
        with patch.object(llm, "seconds_until", return_value=-1):
            self.assertEqual((await llm.call_llm(**REQUEST, client=client))["text"], "正文")
        self.assertEqual(handler.await_count, 5)

    async def test_short_http_backoff_and_outer_handoff(self):
        handler = AsyncMock(side_effect=[httpx.Response(429, headers={"Retry-After": "120"}),
                                        httpx.Response(429, headers={"Retry-After": "2"}), chat()])
        client = await self.client(handler)
        with patch.object(llm, "_sleep", new_callable=AsyncMock) as sleep:
            with self.assertRaises(llm.LLMError) as caught:
                await llm.call_llm(**REQUEST, retries=5, client=client)
            self.assertEqual((caught.exception.code, caught.exception.retry_after_ms), ("HTTP_429", 120_000))
            handler.assert_awaited_once()
            sleep.assert_not_awaited()
            self.assertEqual((await llm.call_llm(**REQUEST, retries=1, client=client))["text"], "正文")
            sleep.assert_awaited_once_with(2.5)

    async def test_siwc_http_and_sse_preserve_quota_and_replay_policy(self):
        failure = {"type": "response.failed", "response": {"error": {"code": "rate_limit_exceeded"}}}
        fragment = {"type": "response.output_text.delta", "delta": "partial"}
        handler = AsyncMock(side_effect=[
            httpx.Response(429, headers={"Retry-After": "54000"}),
            stream(fragment, failure, headers={"Retry-After": "54000"}),
            stream(fragment, failure, headers={"Retry-After": "30"}),
            httpx.Response(429, headers={"Retry-After": "30"}),
        ])
        client = await self.client(handler)
        service = OpenAISiwc(Path(self.enterContext(TemporaryDirectory())), client)
        store = SimpleNamespace(openai_siwc=service)
        with patch.object(service, "check_selection"), patch.object(service, "bearer", return_value=("bearer", 0)):
            for expected_code, no_replay, long_quota in (
                ("SIWC_USAGE_LIMIT", False, True), ("SIWC_USAGE_LIMIT", True, True),
                ("SIWC_RATE_LIMIT", True, False), ("SIWC_RATE_LIMIT", False, False),
            ):
                with self.subTest(expected_code=expected_code, no_replay=no_replay):
                    llm.reset_quota_blocks()
                    count = handler.await_count
                    with self.assertRaises(llm.LLMError) as caught:
                        await llm.call_llm(provider=PROVIDER, model="writer", store=store, siwc_context="chief-editor",
                                           retries=0 if expected_code == "SIWC_RATE_LIMIT" and not no_replay else 5)
                    error = caught.exception
                    self.assertEqual((error.code, error.no_replay, error.retryable, error.reset_source),
                                     (expected_code, no_replay, not (no_replay or long_quota), "header:retry-after"))
                    self.assertEqual(error.retry_after_ms, 54_000_000 if long_quota else 30_000)
                    self.assertEqual(error.retry_at is not None, long_quota)
                    self.assertEqual(handler.await_count, count + 1)
        self.assertTrue(service.quota_limited)

    async def test_siwc_sse_capability_failure_cannot_trigger_parameter_fallback(self):
        handler = AsyncMock(side_effect=lambda _: stream({
            "type": "error", "code": "subscription_sharing_unsupported_capability",
        }))
        service = OpenAISiwc(Path(self.enterContext(TemporaryDirectory())), await self.client(handler))
        with patch.object(service, "check_selection"), patch.object(service, "bearer", return_value=("bearer", 0)):
            with self.assertRaises(llm.LLMError) as caught:
                await llm.call_llm(provider=PROVIDER, model="writer", store=SimpleNamespace(openai_siwc=service),
                                   siwc_context="chief-editor", reasoning_effort="low", retries=5)
        self.assertEqual(caught.exception.code, "SIWC_CAPABILITY_UNSUPPORTED")
        self.assertTrue(caught.exception.no_replay)
        handler.assert_awaited_once()


class PipelineQuotaTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(TemporaryDirectory()))
        self.store = Store(self.directory)
        self.enterContext(patch.dict(os.environ, {
            "NOVEL_FORGE_LONG_QUOTA_SECONDS": "300", "NOVEL_FORGE_RATE_STREAK_LIMIT": "4",
            "NOVEL_FORGE_ROLLING_ARC_OUTLINES": "0", "NOVEL_FORGE_AUTO_RECOVER": "0",
            "NOVEL_FORGE_BACKGROUND_FEEDS": "0",
        }))
        llm.reset_quota_blocks()
        self.addCleanup(llm.reset_quota_blocks)

    def book(self, chapters=2, **fields):
        return self.store.create_job({
            "title": "铜钥夜航", "premise": "主角追踪旧城钟楼的青铜钥匙", "wordsPerChapter": 850,
            "targetWords": 850 * chapters, "totalChapters": chapters,
            "llmConfig": {"provider": "deepseek", "baseUrl": LOCAL, "model": "writer", "apiKey": "test-key",
                          "maxRetries": 0},
            **fields,
        })["id"]

    async def runtime(self, write_failure):
        writes = Counter()

        async def handler(request):
            body = json.loads(request.content)
            system, prompt = [message["content"] for message in body["messages"]]
            if system.startswith("[TASK:CHAPTER_WRITE]"):
                match = re.search(r"第\s*(\d+)\s*章", prompt)
                number = int(match[1])
                writes[number] += 1
                failure = write_failure(number, writes[number])
                if failure is not None:
                    return failure
            return chat((await llm.call_mock_llm(system=system, prompt=prompt))["text"])

        client = await self.enterAsyncContext(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        runtime = PipelineRuntime(self.store, client=client)
        self.addAsyncCleanup(runtime.close)
        return runtime, writes

    async def test_long_quota_checkpoints_without_cooldown(self):
        runtime, writes = await self.runtime(
            lambda chapter, _: httpx.Response(429, text="resets in 15h 3m") if chapter == 2 else None,
        )
        job_id = self.book()
        with patch.object(runtime, "_cooldown", new_callable=AsyncMock) as cooldown:
            await runtime.start_job(job_id)
            job = await runtime.wait_for_job(job_id)
        cooldown.assert_not_awaited()
        self.assertEqual((job["status"], job["checkpoint"]["nextChapter"], job["completedChapters"]),
                         ("interrupted", 2, 1))
        self.assertEqual(writes, {1: 1, 2: 1})
        self.assertIsNone(job["autoRetryState"])
        self.assertEqual(job["error"], {
            "code": "QUOTA_EXCEEDED", "message": job["error"]["message"], "chapterNumber": 2, "retryable": True,
            "resumePolicy": "manual", "quotaScope": "long", "quotaKind": "usage", "resetSource": "text:duration",
            "retryAt": job["error"]["retryAt"], "provider": "deepseek", "model": "writer",
        })
        self.assertIn("切换模型", job["error"]["message"])
        self.assertIn("15 小时", job["error"]["message"])

    async def test_restart_skips_manual_errors(self):
        runtime = PipelineRuntime(self.store)
        runtime.runner = AsyncMock()
        for error in (
            {"code": "QUOTA_EXCEEDED"}, {"code": "SIWC_USAGE_LIMIT"}, {"code": "QUOTA_SUSPECTED"},
            {"code": "HTTP_429", "resumePolicy": "manual"},
        ):
            self.store.update_job(self.book(), {"status": "interrupted", "error": error})
        automatic = self.book()
        self.store.update_job(automatic, {"status": "interrupted", "error": {"code": "HTTP_429"}})
        self.assertEqual(len(await runtime.recover_interrupted_jobs(auto_resume=True)), 5)
        await runtime.wait_for_job(automatic)
        runtime.runner.assert_awaited_once_with(automatic)

    async def test_short_429_cools_down_on_server_schedule(self):
        runtime, writes = await self.runtime(
            lambda chapter, count: httpx.Response(429, headers={"Retry-After": "30"})
            if (chapter, count) == (2, 1) else None,
        )
        job_id = self.book()
        states = []

        async def sleep(delay):
            if delay >= 5:
                state = self.store.get_job_record(job_id)["autoRetryState"]
                states.append((delay, state["retryAfterSource"], state["cooldownMs"]))

        with patch("novel_forge.pipeline.asyncio.sleep", side_effect=sleep):
            await runtime.start_job(job_id)
            job = await runtime.wait_for_job(job_id)
        self.assertEqual(states, [(33, "server", 33_000)])
        self.assertEqual((job["status"], writes, job["autoRetryState"]), ("completed", {1: 1, 2: 2}, None))

    async def test_four_consecutive_429s_stop_after_progress_resets_streak(self):
        runtime, writes = await self.runtime(
            lambda chapter, count: httpx.Response(429) if chapter == 2 or (chapter == 1 and count <= 3) else None,
        )
        job_id = self.book(autoResumeCooldownMs=5)
        with patch.object(runtime, "_cooldown", wraps=runtime._cooldown) as cooldown:
            await runtime.start_job(job_id)
            job = await runtime.wait_for_job(job_id)
        self.assertEqual([call.args[-1] for call in cooldown.await_args_list], [1, 2, 3, 1, 2, 3])
        self.assertEqual((writes, job["status"], job["completedChapters"]), ({1: 4, 2: 4}, "interrupted", 1))
        self.assertEqual((job["error"]["code"], job["error"]["resumePolicy"]), ("QUOTA_SUSPECTED", "manual"))
        self.assertIn("切换模型", job["error"]["message"])
        self.assertNotIn("retryAt", job["error"])
        self.assertIsNone(job["autoRetryState"])

    async def test_resume_advisory_and_service_error(self):
        app = create_app(self.directory)
        await self.enterAsyncContext(app.router.lifespan_context(app))
        app.state.pipeline.runner = AsyncMock()
        client = await self.enterAsyncContext(httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=LOCAL.removesuffix("/v1"),
        ))
        retry_at = retry_at_iso(3600)
        for change, pending in ((None, True), ({"model": "writer"}, True), ({"model": "another"}, False),
                                ({"provider": "openai"}, False), (None, False)):
            expired = change is None and not pending
            job_id = self.book()
            self.store.update_job(job_id, {"status": "interrupted", "error": {
                "code": "QUOTA_EXCEEDED", "retryAt": "2000-01-01T00:00:00Z" if expired else retry_at,
            }})
            response = await client.post(f"/api/jobs/{job_id}/resume", json={"llmConfig": change})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["job"]["status"], "running")
            self.assertEqual(response.json().get("quotaPending"), {"retryAt": retry_at} if pending else None)
        for error in (llm.LLMError("额度", code="QUOTA_EXCEEDED", status=429, retry_at=retry_at),
                      SiwcError("SIWC_USAGE_LIMIT", 429, retry_at=retry_at)):
            with patch.object(app.state.pipeline, "resume_job", side_effect=error):
                response = await client.post(f"/api/jobs/{job_id}/resume")
            self.assertEqual(response.status_code, 429)
            self.assertEqual(response.json(), {"error": str(error), "code": error.code, "retryAt": retry_at})
