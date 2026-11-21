import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from novel_forge import providers
from novel_forge.providers import (
    ProviderCatalogError, get_provider_definition, is_masked_api_key, list_model_catalog, mask_api_key,
    normalize_model_ids, provider_requires_key, same_endpoint_origin, sync_provider_models, update_model_settings,
)
from novel_forge.storage import Store


FIXTURE_KEY = "test-only-secret-654321"


class ProviderMetadataTests(unittest.TestCase):
    def test_metadata_and_origin_helpers_keep_node_conventions(self):
        self.assertFalse(provider_requires_key("mock"))
        self.assertFalse(provider_requires_key("ollama"))
        self.assertTrue(provider_requires_key("vertex-new"))
        self.assertTrue(provider_requires_key("not-listed"))
        self.assertIsNone(get_provider_definition("not-listed"))
        self.assertTrue(same_endpoint_origin("https://EXAMPLE.test:443/a", "https://example.test/b"))
        self.assertFalse(same_endpoint_origin("https://example.test:444", "https://example.test"))
        self.assertFalse(same_endpoint_origin("invalid", "invalid"))
        self.assertNotIn("/projects/", providers.PROVIDERS["vertex"]["baseUrl"])
        self.assertNotEqual(providers.PROVIDERS["vertex"]["env"], providers.PROVIDERS["vertex-new"]["env"])
        self.assertEqual(normalize_model_ids(["\ufeff text\u3000", "text", "image space", "bad\0id", 23, "x" * 257]), ["text"])
        self.assertEqual(normalize_model_ids(["😀" * 128, "😀" * 129]), ["😀" * 128])
        self.assertEqual(mask_api_key("abcd1234wxyz"), "abcd****wxyz")
        self.assertEqual(mask_api_key(" short "), "****")
        self.assertTrue(is_masked_api_key("abcd...wxyz"))
        self.assertFalse(is_masked_api_key("legitimate-key"))


class ModelDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = Store(Path(temporary.name))

    async def catalog_entry(self, provider):
        return next(entry for entry in (await list_model_catalog(self.store))["providers"] if entry["providerId"] == provider)

    async def test_legacy_compatible_listing_preserves_saved_model_and_does_not_copy_key(self):
        self.store.config_path.write_text(json.dumps({"activeProvider": "qwen", "extension": 42, "providers": {
            "qwen": {"apiKey": FIXTURE_KEY, "baseUrl": "http://fixture.test/local/v1", "model": "legacy-qwen", "future": {"keep": True}},
        }}), encoding="utf-8")
        old = await self.catalog_entry("qwen")
        self.assertEqual(old["modelSource"], "preset")
        addresses = []

        async def handler(request):
            self.assertEqual(request.headers["Authorization"], "Bearer " + FIXTURE_KEY)
            self.assertNotIn("x-fixture-default", request.headers)
            addresses.append(request.url)
            if len(addresses) == 1:
                return httpx.Response(200, json={"has_more": True, "last_id": "cursor-one", "data": [
                    {"id": "qwen-new"}, {"id": "qwen-embedding-1"}, {"id": "custom-img", "architecture": {"output_modalities": ["image"]}},
                    {"id": "no-modality", "architecture": {"output_modalities": []}},
                ]})
            self.assertEqual(request.url.params["after"], "cursor-one")
            return httpx.Response(200, json={"data": [{"id": "qwen-next"}, {"id": "qwen-new"}]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), headers={"x-fixture-default": "no"}) as client:
            entry = await sync_provider_models(self.store, "qwen", client=client)
        self.assertTrue(all(url.path == "/local/v1/models" for url in addresses))
        self.assertEqual(entry["models"], ["qwen-new", "qwen-next", "legacy-qwen"])
        self.assertEqual(entry["selectedModel"], "legacy-qwen")
        revised = await update_model_settings(self.store, "qwen", {"selectedModel": "qwen-new", "apiKey": old["apiKeyMasked"]})
        self.assertNotIn(FIXTURE_KEY, json.dumps(revised))
        self.assertEqual(revised["selectedModel"], "qwen-new")
        self.assertEqual(revised["apiKeyMasked"], "test****4321")
        saved = json.loads(self.store.config_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["extension"], 42)
        self.assertEqual(saved["providers"]["qwen"]["future"], {"keep": True})
        self.assertEqual(saved["providers"]["qwen"]["apiKey"], FIXTURE_KEY)
        job = self.store.create_job({"llmConfig": {"provider": "qwen", "model": "qwen-new", "useSavedConnection": True,
                                                 "apiKey": "old-task-key", "baseUrl": "http://old.test/v1"}})
        self.assertNotIn("apiKey", job["llmConfig"])
        self.assertEqual(job["llmConfig"]["baseUrl"], "http://fixture.test/local/v1")

    async def test_gemini_paginates_and_rejects_non_text_models(self):
        await update_model_settings(self.store, "gemini", {"baseUrl": "https://fixture.test/v1beta", "apiKey": FIXTURE_KEY})
        cursors = []

        async def handler(request):
            self.assertEqual(request.url.path, "/v1beta/models")
            self.assertEqual(request.headers["x-goog-api-key"], FIXTURE_KEY)
            self.assertNotIn("key", request.url.params)
            cursors.append(request.url.params.get("pageToken"))
            rows = [
                {"name": "models/gemini-text-first", "supportedGenerationMethods": ["generateContent"]},
                {"name": "models/text-embedding-1", "supportedGenerationMethods": ["embedContent"]},
                {"name": "models/gemini-image", "supportedGenerationMethods": ["generateContent"], "outputModalities": ["IMAGE"]},
            ] if len(cursors) == 1 else [{"name": "models/gemini-text-last", "supportedGenerationMethods": ["generateContent"]}]
            return httpx.Response(200, json={"models": rows, **({"nextPageToken": "two"} if len(cursors) == 1 else {})})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            entry = await sync_provider_models(self.store, "gemini", client=client)
        self.assertEqual(cursors, [None, "two"])
        self.assertEqual(entry["models"], ["gemini-text-first", "gemini-text-last"])

    async def test_vertex_official_uses_manual_models_and_gateway_preserves_independent_profiles(self):
        official = "https://europe-west1-aiplatform.googleapis.com/v1/projects/example/locations/global/publishers/google/models"
        await update_model_settings(self.store, "vertex", {"baseUrl": official, "apiKey": FIXTURE_KEY,
                                                         "models": ["gemini-official-manual"]})
        gateway = "https://vertex-gateway.test/custom/v1/publishers/google/models"
        await update_model_settings(self.store, "vertex-new", {"baseUrl": gateway, "apiKey": "second-vertex-secret",
                                                             "models": ["gemini-saved"], "selectedModel": "gemini-saved"})
        called = []

        async def handler(request):
            self.assertEqual(request.url.host, "vertex-gateway.test")
            self.assertEqual(request.url.path, "/custom/v1/publishers/google/models")
            self.assertEqual(request.headers["x-goog-api-key"], "second-vertex-secret")
            self.assertNotIn("key", request.url.params)
            called.append(request.url.params.get("pageToken"))
            if len(called) == 1:
                return httpx.Response(200, json={"nextPageToken": "second", "publisherModels": [
                    {"name": "publishers/google/models/gemini-good"}, {"name": "publishers/google/models/gemini-flash-image"},
                ]})
            if len(called) == 2:
                return httpx.Response(200, json={"models": [{"name": "models/gemini-next", "outputModalities": ["TEXT"]},
                                                          {"name": "models/gemini-embed", "supportedGenerationMethods": ["embedContent"]}]})
            return httpx.Response(502)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(ProviderCatalogError) as caught:
                await sync_provider_models(self.store, "vertex", client=client)
            self.assertEqual(caught.exception.code, "MODEL_DISCOVERY_UNSUPPORTED")
            self.assertEqual(called, [])
            entry = await sync_provider_models(self.store, "vertex-new", client=client)
            self.assertEqual(entry["models"], ["gemini-saved", "gemini-good", "gemini-next"])
            self.assertEqual(entry["selectedModel"], "gemini-saved")
            with self.assertRaisesRegex(ProviderCatalogError, "HTTP 502"):
                await sync_provider_models(self.store, "vertex-new", client=client)
        self.assertEqual(called, [None, "second", None])
        self.assertEqual(await self.catalog_entry("vertex-new"), entry)
        self.assertEqual((await self.catalog_entry("vertex"))["models"], ["gemini-official-manual"])
        self.assertEqual(self.store.get_provider_config("vertex")["apiKey"], FIXTURE_KEY)
        self.assertNotEqual(self.store.get_provider_config("vertex-new")["apiKey"], FIXTURE_KEY)

    async def test_keyless_catalog_and_bound_vertex_preserve_fallbacks(self):
        await update_model_settings(self.store, "openai-compatible", {"baseUrl": "http://fixture.test/v1",
                                                                   "models": ["model-local"], "selectedModel": "model-local"})
        await update_model_settings(self.store, "vertex", {
            "baseUrl": "https://fixture.test/gateway/v1/models/gemini-page:generateContent",
            "apiKey": FIXTURE_KEY, "models": ["gemini-page"],
        })
        calls = 0

        async def handler(request):
            nonlocal calls
            calls += 1
            self.assertNotIn("Authorization", request.headers)
            return httpx.Response(302, headers={"location": "https://other.test/models"})

        with patch.dict("os.environ", {"OPENAI_COMPATIBLE_API_KEY": ""}):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
                with self.assertRaisesRegex(ProviderCatalogError, "HTTP 302"):
                    await sync_provider_models(self.store, "openai-compatible", client=client)
                with self.assertRaises(ProviderCatalogError) as caught:
                    await sync_provider_models(self.store, "vertex", client=client)
                self.assertEqual(caught.exception.code, "MODEL_DISCOVERY_UNSUPPORTED")
        self.assertEqual(calls, 1)
        entry = await self.catalog_entry("openai-compatible")
        self.assertTrue(entry["configured"])
        self.assertEqual(entry["models"], ["model-local"])

    async def test_concurrent_connection_update_rejects_stale_results(self):
        self.store.save_provider_config({"provider": "openai", "apiKey": FIXTURE_KEY, "baseUrl": "https://origin.test/v1",
                                        "discoveredModels": ["gpt-old"]})
        calls = 0

        async def handler(request):
            nonlocal calls
            calls += 1
            self.assertEqual(request.headers["Authorization"], "Bearer " + FIXTURE_KEY)
            await update_model_settings(self.store, "openai", {"apiKey": "new-fixture-key", "baseUrl": "https://new.test/v1"})
            return httpx.Response(200, json={"data": [{"id": "gpt-stale"}]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(ProviderCatalogError) as caught:
                await sync_provider_models(self.store, "openai", client=client)
        self.assertEqual(caught.exception.code, "CONNECTION_CHANGED")
        self.assertEqual(calls, 1)
        current = self.store.get_provider_config("openai")
        self.assertEqual(current["baseUrl"], "https://new.test/v1")
        self.assertEqual(current["discoveredModels"], [])
        with self.assertRaises(ProviderCatalogError) as caught:
            await update_model_settings(self.store, "openai", {"baseUrl": "https://third.test/v1", "apiKey": "****"})
        self.assertEqual(caught.exception.code, "KEY_REQUIRED")

    async def test_anthropic_qwen_and_discovery_budget(self):
        self.store.save_provider_config({"provider": "anthropic", "apiKey": FIXTURE_KEY})
        self.store.save_provider_config({"provider": "qwen", "apiKey": FIXTURE_KEY,
                                        "baseUrl": "https://dashscope.aliyuncs.com/compatible-mode/v1"})
        calls = []

        async def handler(request):
            calls.append(request.url)
            if request.url.host == "api.anthropic.com":
                self.assertEqual(request.url.path, "/v1/models")
                self.assertEqual(request.headers["anthropic-version"], "2023-06-01")
                later = "after_id" in request.url.params
                return httpx.Response(200, json={"has_more": not later, "last_id": "a1",
                                                 "data": [{"id": "claude-b" if later else "claude-a"}]})
            self.assertEqual(request.url.path, "/api/v1/models")
            self.assertEqual(request.url.params["capabilities"], "TG")
            number = int(request.url.params["page_no"])
            return httpx.Response(200, json={"output": {"page_no": number, "page_size": 1, "total": 2,
                                                       "models": [{"model": f"qwen-{number}"}]}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            self.assertEqual((await sync_provider_models(self.store, "anthropic", client=client))["models"], ["claude-a", "claude-b"])
            self.assertEqual((await sync_provider_models(self.store, "qwen", client=client))["models"], ["qwen-1", "qwen-2"])
        self.assertEqual(len(calls), 4)

        async def slow(_request):
            await asyncio.sleep(1)
            return httpx.Response(200, json={"data": [{"id": "claude-unreachable"}]})

        with patch.object(providers, "DISCOVERY_TIMEOUT", 0.02):
            async with httpx.AsyncClient(transport=httpx.MockTransport(slow)) as client:
                with self.assertRaises(ProviderCatalogError) as caught:
                    await sync_provider_models(self.store, "anthropic", client=client)
        self.assertEqual(caught.exception.code, "MODEL_SYNC_FAILED")
        self.assertEqual((await self.catalog_entry("anthropic"))["models"], ["claude-a", "claude-b"])
