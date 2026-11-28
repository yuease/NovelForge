import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from novel_forge import llm
from novel_forge.pipeline import PipelineRuntime, read_writing_options
from novel_forge.prompt_budget import (
    BUDGET_PROFILES,
    normalize_stage_budgets,
    resolve_stage_budgets,
)
from novel_forge.storage import Store, count_chinese_words
from novel_forge.thinking_policy import (
    is_openai_gemini_model,
    normalize_stage_thinking,
    resolve_stage_thinking,
    thinking_capability,
)

LOCAL = "http://127.0.0.1:2048/v1"


def chat_ok(content, *, reasoning_tokens=0):
    usage = {"prompt_tokens": 100, "completion_tokens": max(20, len(content)), "total_tokens": 120 + len(content)}
    if reasoning_tokens:
        usage["completion_tokens_details"] = {"reasoning_tokens": reasoning_tokens}
    return httpx.Response(200, json={
        "choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
        "usage": usage,
    })


class StagePolicyUnitTests(unittest.TestCase):
    def test_openai_gemini_capability_and_argon_calibration(self):
        self.assertTrue(is_openai_gemini_model("openai", "gemini-4-argon"))
        self.assertTrue(is_openai_gemini_model("openai", "gemini-3.8-flash"))
        self.assertTrue(is_openai_gemini_model("openai-compatible", "gemini-4-hydrogen"))
        self.assertFalse(is_openai_gemini_model("openai", "gpt-4o-mini"))
        self.assertEqual(thinking_capability("openai", "gemini-4-argon", LOCAL), "toggle")
        self.assertEqual(thinking_capability("openai", "gemini-3.8-flash", LOCAL), "toggle")

        argon = {"provider": "openai", "model": "gemini-4-argon", "thinkingMode": "auto", "thinkingModeSource": "auto"}
        heavy = {"reasoningHeavy": True}
        light = {"reasoningHeavy": False}
        self.assertEqual(resolve_stage_thinking("book_plan", argon), {"thinkingMode": "enabled", "reasoningEffort": "high"})
        self.assertEqual(resolve_stage_thinking("chapter_outlines", argon), {"thinkingMode": "enabled", "reasoningEffort": "high"})
        self.assertEqual(resolve_stage_thinking("full_audit", argon), {"thinkingMode": "enabled", "reasoningEffort": "high"})
        self.assertEqual(resolve_stage_thinking("chapter_writing", argon, profile=heavy), {"thinkingMode": "enabled", "reasoningEffort": "high"})
        self.assertEqual(resolve_stage_thinking("chapter_writing", argon, profile=light), {"thinkingMode": "disabled", "reasoningEffort": "minimal"})

        flash = {"provider": "openai", "model": "gemini-3.8-flash", "thinkingMode": "auto", "thinkingModeSource": "auto"}
        self.assertEqual(resolve_stage_thinking("book_plan", flash), {"thinkingMode": "enabled", "reasoningEffort": "medium"})
        self.assertEqual(resolve_stage_thinking("chapter_verify", flash), {"thinkingMode": "disabled", "reasoningEffort": "low"})
        self.assertEqual(resolve_stage_thinking("drift_patch", flash), {"thinkingMode": "disabled", "reasoningEffort": "low"})
        self.assertEqual(resolve_stage_thinking("fact_extraction", flash), {"thinkingMode": "disabled", "reasoningEffort": "minimal"})
        self.assertEqual(resolve_stage_thinking("arc_archive", flash), {"thinkingMode": "disabled", "reasoningEffort": "minimal"})
        self.assertEqual(resolve_stage_thinking("volume_archive", flash), {"thinkingMode": "disabled", "reasoningEffort": "minimal"})
        self.assertEqual(resolve_stage_thinking("chapter_retitle", flash), {"thinkingMode": "disabled", "reasoningEffort": "minimal"})

    def test_explicit_stage_thinking_overrides_and_user_disable(self):
        cfg = {
            "provider": "openai",
            "model": "gemini-4-argon",
            "thinkingMode": "disabled",
            "thinkingModeSource": "user",
            "stageThinking": {"chapter_writing": "medium", "full_audit": "high", "archive": "low"},
        }
        # medium on gemini-4-argon auto-maps to high so thinking actually activates.
        self.assertEqual(resolve_stage_thinking("chapter_writing", cfg), {"thinkingMode": "enabled", "reasoningEffort": "high"})
        self.assertEqual(resolve_stage_thinking("full_audit", cfg), {"thinkingMode": "enabled", "reasoningEffort": "high"})
        self.assertEqual(resolve_stage_thinking("fact_extraction", cfg), {"thinkingMode": "disabled", "reasoningEffort": "low"})
        # Unspecified stage respects explicit user disable.
        self.assertEqual(resolve_stage_thinking("book_plan", cfg), {"thinkingMode": "disabled", "reasoningEffort": "minimal"})

        ds = {
            "provider": "deepseek",
            "model": "deepseek-chat",
            "thinkingMode": "auto",
            "stageThinking": {"book_plan": "medium", "chapter_writing": "off"},
        }
        self.assertEqual(resolve_stage_thinking("book_plan", ds), {"thinkingMode": "enabled", "reasoningEffort": None})
        self.assertEqual(resolve_stage_thinking("chapter_writing", ds, profile={"reasoningHeavy": True}),
                         {"thinkingMode": "disabled", "reasoningEffort": None})
        self.assertEqual(normalize_stage_thinking({"book_plan": "HIGH", "archive": "auto", "bad": "high"}),
                         {"book_plan": "high"})

    def test_stage_budget_profiles_and_precedence(self):
        clean_env = {
            "NOVEL_FORGE_CHAPTER_INPUT_BUDGET": "",
            "NOVEL_FORGE_ARC_INPUT_BUDGET": "",
            "NOVEL_FORGE_REVIEW_INPUT_BUDGET": "",
            "NOVEL_FORGE_REPAIR_INPUT_BUDGET": "",
            "NOVEL_FORGE_AUDIT_INPUT_BUDGET": "",
        }
        with patch.dict(os.environ, clean_env):
            self.assertEqual(resolve_stage_budgets({}), BUDGET_PROFILES["balanced"])
            self.assertEqual(
                resolve_stage_budgets({"llmConfig": {"budgetProfile": "long_book"}}),
                BUDGET_PROFILES["long_book"],
            )
            custom = resolve_stage_budgets({
                "llmConfig": {
                    "budgetProfile": "long_book",
                    "stageBudgets": {"chapter": 20480, "audit": 48000, "repair": 0},
                }
            })
            self.assertEqual(custom["chapter"], 20480)
            self.assertEqual(custom["arc"], 24576)
            self.assertEqual(custom["audit"], 48000)
            self.assertIsNone(custom["repair"])

        # Explicit job override beats env var; env var beats preset.
        with patch.dict(os.environ, {**clean_env, "NOVEL_FORGE_AUDIT_INPUT_BUDGET": "30000"}):
            from_env = resolve_stage_budgets({"llmConfig": {"budgetProfile": "long_book"}})
            self.assertEqual(from_env["audit"], 30000)
            from_job = resolve_stage_budgets({"llmConfig": {"stageBudgets": {"audit": 45000}}})
            self.assertEqual(from_job["audit"], 45000)

        with self.assertRaises(llm.LLMError):
            normalize_stage_budgets({"audit": 250_000})

    def test_private_and_tailscale_ip_detection(self):
        self.assertTrue(llm._is_private_or_local("http://100.115.248.109:2048/v1"))
        self.assertTrue(llm._is_private_or_local("http://192.168.1.50:8080/v1"))
        self.assertTrue(llm._is_private_or_local("http://10.0.0.5:2048/v1"))
        self.assertTrue(llm._is_private_or_local("http://mybox.tail123.ts.net:2048/v1"))
        self.assertFalse(llm._is_private_or_local("https://api.openai.com/v1"))
        self.assertFalse(llm._is_private_or_local("https://us-central1-aiplatform.googleapis.com/v1"))


class StagePolicyIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = Store(Path(self.enterContext(tempfile.TemporaryDirectory())))
        llm.reset_thinking_fallback_cache()
        llm.reset_provider_circuit_state()
        self.addCleanup(llm.reset_thinking_fallback_cache)
        self.addCleanup(llm.reset_provider_circuit_state)

    async def test_openai_reasoning_effort_output_floor_and_400_fallback(self):
        sent = []

        async def handler(request):
            body = json.loads(request.content)
            sent.append(body)
            if len(sent) == 1:
                return chat_ok("正文完成。", reasoning_tokens=888)
            if len(sent) == 2:
                return httpx.Response(400, json={"error": {"message": "Unknown parameter: reasoning_effort"}})
            return chat_ok("降级后正文。")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            # 1) High reasoning effort protects max_tokens floor (>= 16384) and records thoughtsTokens
            r1 = await llm.call_llm(
                provider="openai", base_url=LOCAL, api_key="k", model="gemini-4-argon",
                system="[TASK:CHAPTER_WRITE]\n写作", prompt="正文", max_tokens=2048,
                reasoning_effort="high", retries=0, client=client,
            )
            self.assertEqual(sent[0]["reasoning_effort"], "high")
            self.assertGreaterEqual(sent[0]["max_tokens"], 16_384)
            self.assertEqual(r1["usage"]["thoughtsTokens"], 888)

            # 2) HTTP 400 on reasoning_effort falls back cleanly and caches unsupported key
            r2 = await llm.call_llm(
                provider="openai", base_url=LOCAL, api_key="k", model="gemini-3.8-flash",
                system="[TASK:VERIFY]\n审校", prompt="审校", max_tokens=2048,
                reasoning_effort="low", retries=0, client=client,
            )
            self.assertEqual(sent[1]["reasoning_effort"], "low")
            self.assertNotIn("reasoning_effort", sent[2])
            self.assertEqual(r2["text"], "降级后正文。")

            # 3) Cached unsupported model skips reasoning_effort immediately
            await llm.call_llm(
                provider="openai", base_url=LOCAL, api_key="k", model="gemini-3.8-flash",
                system="[TASK:VERIFY]\n审校", prompt="再次审校", max_tokens=2048,
                reasoning_effort="low", retries=0, client=client,
            )
            self.assertNotIn("reasoning_effort", sent[3])

    async def test_pipeline_sends_calibrated_stage_reasoning_effort_and_persists_budgets(self):
        prose = "贞观三年的秋雨打湿了洛阳漕运司的青石台阶。" * 25
        words = count_chinese_words(prose)
        job = self.store.create_job({
            "title": "大唐算账",
            "premise": "穿越初唐靠历史考据、十六两制换算和复式账目查清漕运亏空",
            "genre": "历史架空",
            "totalChapters": 2,
            "wordsPerChapter": words,
            "targetWords": words * 2,
            "llmConfig": {
                "provider": "openai",
                "baseUrl": LOCAL,
                "apiKey": "k",
                "model": "gemini-4-argon",
                "lightModel": "gemini-3.8-flash",
                "thinkingMode": "auto",
                "budgetProfile": "long_book",
                "stageBudgets": {"audit": 44000},
                "maxRetries": 0,
            },
        })
        opts = read_writing_options(job)
        self.assertEqual(opts["input_budget"], 24_576)
        self.assertEqual(opts["arc_input_budget"], 24_576)
        self.assertEqual(opts["verify_input_budget"], 18_432)
        self.assertEqual(opts["repair_input_budget"], 16_384)
        self.assertEqual(opts["audit_input_budget"], 44_000)

        calls = []

        async def handler(request):
            wire = json.loads(request.content)
            system, prompt = (m["content"] for m in wire["messages"])
            task = re.match(r"\[TASK:(\w+)\]", system)[1]
            calls.append((task, wire["model"], wire.get("reasoning_effort"), wire.get("max_tokens")))
            if task == "CHAPTER_WRITE":
                return chat_ok(prose)
            mock_res = await llm.call_mock_llm(system=system, prompt=prompt)
            return chat_ok(mock_res["text"])

        clean_env = {
            "NOVEL_FORGE_ROLLING_ARC_OUTLINES": "0",
            "NOVEL_FORGE_STORY_LEDGER": "1",
            "NOVEL_FORGE_LONG_MEMORY": "1",
            "NOVEL_FORGE_DRAFT_QUALITY": "0",
            "NOVEL_FORGE_WRITE_AI_GATE": "0",
            "NOVEL_FORGE_CHAPTER_INPUT_BUDGET": "",
            "NOVEL_FORGE_ARC_INPUT_BUDGET": "",
            "NOVEL_FORGE_REVIEW_INPUT_BUDGET": "",
            "NOVEL_FORGE_REPAIR_INPUT_BUDGET": "",
            "NOVEL_FORGE_AUDIT_INPUT_BUDGET": "",
        }
        with patch.dict(os.environ, clean_env):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                runtime = PipelineRuntime(self.store, client=client)
                await runtime._consume(job["id"])

        by_task = {}
        for task, model, effort, max_tok in calls:
            by_task.setdefault(task, []).append((model, effort, max_tok))

        self.assertEqual(by_task["BOOK_PLAN"][0][:2], ("gemini-4-argon", "high"))
        self.assertEqual(by_task["CHAPTER_OUTLINES"][0][:2], ("gemini-4-argon", "high"))
        self.assertEqual(by_task["CHAPTER_WRITE"][0][:2], ("gemini-4-argon", "high"))
        self.assertGreaterEqual(by_task["CHAPTER_WRITE"][0][2], 16_384)
        self.assertEqual(by_task["VERIFY"][0][:2], ("gemini-3.8-flash", "low"))
        self.assertEqual(by_task["FULL_AUDIT"][0][:2], ("gemini-4-argon", "high"))
        if "FACT_EXTRACTION" in by_task:
            self.assertEqual(by_task["FACT_EXTRACTION"][0][:2], ("gemini-3.8-flash", "minimal"))

        # Updating config persists changes cleanly
        updated = await PipelineRuntime(self.store).update_job_llm_config(job["id"], {
            "budgetProfile": "balanced",
            "stageBudgets": {},
            "stageThinking": {"chapter_verify": "medium"},
        })
        self.assertEqual(updated["llmConfig"]["budgetProfile"], "balanced")
        self.assertNotIn("stageBudgets", updated["llmConfig"])
        self.assertEqual(updated["llmConfig"]["stageThinking"], {"chapter_verify": "medium"})
