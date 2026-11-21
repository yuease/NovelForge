import asyncio
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import httpx

from novel_forge import hotspots, memes
from novel_forge.llm import DEFAULT_MAX_TOKENS, LLMError, reset_provider_circuit_state
from novel_forge.storage import Store


NOW = "2026-11-17T12:00:00Z"
FRESH = "2026-11-16T10:10:00Z"
LOCAL = "http://127.0.0.1:7083"


def _board(items):
    return {"success": True, "data": {"cards": [{"component": "novel_list", "content": [{"content": items}]}]}}


def _remote_meme(slug, title=None, *, updated_at="2026-11-17T11:00:00Z"):
    return {"slug": slug, "title": title or slug, "updated_at": updated_at,
            "summary": "反差与吐槽结构示例", "tags": ["都市", "反差"],
            "spreadRecords": [{"creator": "甲", "url": "https://example.test/first", "publishedAt": FRESH},
                              {"creator": "乙", "url": "https://example.test/second", "publishedAt": FRESH}]}


class HotspotTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        reset_provider_circuit_state()
        self.addCleanup(reset_provider_circuit_state)

    async def client(self, handler):
        return await self.enterAsyncContext(httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    async def test_nested_baidu_30_items_and_failure_preserves_durable_cache(self):
        valid = True
        entries = [{
            "index": i, "title": "绍宋" if i == 1 else f"榜单小说{i}",
            "desc": [f"作者：作家{i}", "历史 | 完结"], "hotScore": {"value": str(103_555 - (i - 1) * 500)},
            "url": f"https://example.test/{i}",
        } for i in range(1, 32)]

        def handle(request):
            self.assertIn("NovelForge", request.headers["User-Agent"])
            return httpx.Response(200, json=_board(entries if valid else [{"desc": ["无标题"]}]))

        feed = hotspots.HotspotsFeed(self.directory, client=await self.client(handle))
        first = await feed.refresh_hotspots_feed(source_url=LOCAL + "/board", source_name="测试小说榜", now=NOW)
        self.assertTrue(first["ok"])
        self.assertEqual(len(first["liveItems"]), 30)
        item = first["liveItems"][0]
        self.assertEqual((item["title"], item["author"], item["genre"], item["hotScore"]), ("绍宋", "作家1", "历史", 103555))
        valid = False
        failed = await feed.refresh_hotspots_feed(source_url=LOCAL + "/board", now=NOW)
        self.assertFalse(failed["ok"])
        self.assertTrue(failed["usingCache"])
        self.assertEqual(failed["liveItems"], first["liveItems"])
        self.assertEqual(failed["feedMeta"]["feedVersion"], 1)
        self.assertEqual(failed["feedMeta"]["lastSuccessAt"], first["feedMeta"]["lastSuccessAt"])
        from_disk = await hotspots.HotspotsFeed(self.directory).get_hotspots(query="绍宋")
        self.assertEqual(from_disk["liveItems"], [item])
        self.assertTrue(from_disk["feedMeta"]["usingCache"])
        self.assertTrue(from_disk["fallbackTemplates"])

    async def test_default_realtime_fallback_and_corrupt_cache_recovery(self):
        (self.directory / "hotspots_cache.json").write_text(
            '{"liveItems":[{"title":"热点选题1","summary":"[object Object]"}]}', encoding="utf-8",
        )
        urls = []

        def handle(request):
            urls.append(str(request.url))
            return httpx.Response(200, json=_board([] if len(urls) == 1 else [{"word": "新实时条目", "hot": 100}]))

        feed = hotspots.HotspotsFeed(self.directory, client=await self.client(handle))
        self.assertEqual((await feed.get_hotspots())["liveItems"], [])
        refreshed = await feed.refresh_hotspots_feed(now=NOW)
        self.assertTrue(refreshed["ok"])
        self.assertEqual(urls, [hotspots.DEFAULT_FEED_URL, hotspots.FALLBACK_FEED_URL])
        self.assertEqual(refreshed["liveItems"][0]["title"], "新实时条目")

    async def test_author_lock_live_cache_scoring_protected_budget_and_manual_fallback(self):
        feed = hotspots.HotspotsFeed(self.directory)
        await feed.refresh_hotspots_feed(custom_items=[{"title": "热门长篇", "genre": "都市", "desc": "内容"}], now=NOW)
        meme_store = memes.MemeStore(self.directory)
        await meme_store.sync_memes(custom_items=[_remote_meme("test", "结构测试梗")], now=NOW)
        prompts = []
        config = {"provider": "openai", "baseUrl": LOCAL, "apiKey": "sample-key", "maxRetries": 0}

        def handle(request):
            body = json.loads(request.content)
            prompts.append((body["max_tokens"], body["messages"][1]["content"]))
            text = json.dumps({"candidates": [
                {"title": "低分候选", "premise": "低分梗概", "genreFit": 40, "novelty": 50, "longFormPotential": 60},
                {"title": "高分候选", "premise": "高分梗概", "genreFit": 98, "novelty": 96, "longFormPotential": 90},
                {"title": "中等候选", "premise": "中等梗概", "genreFit": 80, "novelty": 80, "longFormPotential": 80},
            ]}, ensure_ascii=False)
            return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})

        client = await self.client(handle)
        options = {"llm_config": config, "feed": feed, "meme_store": meme_store, "client": client, "now": NOW,
                   "meme_level": "rich", "genre": "都市"}
        locked = await hotspots.select_or_generate_concept(**options, premise="作者写好的梗概")
        self.assertEqual(locked["inspirationMode"], "user_premise")
        self.assertEqual(prompts, [])
        with patch.dict(os.environ, {"NOVEL_FORGE_OUTPUT_GUARD": "1", "NOVEL_FORGE_MIN_OUTPUT_TOKENS": ""}):
            generated = await hotspots.select_or_generate_concept(**options)
        self.assertEqual((generated["inspirationMode"], generated["title"]), ("live_feed", "高分候选"))
        self.assertEqual([candidate["title"] for candidate in generated["candidates"]], ["高分候选", "中等候选", "低分候选"])
        self.assertEqual(generated["sourceRefs"][0]["title"], "热门长篇")
        self.assertEqual(prompts[0][0], DEFAULT_MAX_TOKENS)
        self.assertIn("结构测试梗", prompts[0][1])
        with patch.dict(os.environ, {"NOVEL_FORGE_OUTPUT_GUARD": "0"}):
            reopened = await hotspots.select_or_generate_concept(**{**options, "feed": hotspots.HotspotsFeed(self.directory)})
        self.assertEqual(reopened["inspirationMode"], "cached_feed")
        self.assertEqual(prompts[1][0], 10_000)
        freeform = await hotspots.select_or_generate_concept(prefer_freeform=True, now=NOW)
        self.assertEqual(freeform["inspirationMode"], "model_freeform")
        self.assertEqual(len(freeform["candidates"]), 3)
        manual = await hotspots.generate_concept(keywords="雾港钥匙")
        self.assertIn("雾港钥匙", manual["premise"])
        self.assertIn("pacingTemplate", manual)

    async def test_topic_local_parse_fallback_and_circuit_does_not_fallback(self):
        config = {"provider": "openai", "baseUrl": LOCAL, "maxRetries": 0}
        client = await self.client(lambda _: httpx.Response(200, json={"choices": [{"message": {"content": "{bad json"}}]}))
        result = await hotspots.select_or_generate_concept(llm_config=config, now=NOW, client=client)
        self.assertEqual(result["inspirationMode"], "fallback_templates")
        self.assertEqual(len(result["candidates"]), 1)
        calls = 0

        def unavailable(_):
            nonlocal calls
            calls += 1
            return httpx.Response(503)

        with patch.dict(os.environ, {"NOVEL_FORGE_PROVIDER_CIRCUIT": "1"}):
            client = await self.client(unavailable)
            with self.assertRaises(LLMError) as caught:
                await hotspots.select_or_generate_concept(llm_config={**config, "maxRetries": 5}, client=client, now=NOW)
            self.assertEqual(caught.exception.code, "CIRCUIT_OPEN")
            self.assertEqual(calls, 3)

    async def test_manual_concept_uses_saved_connection_and_preserves_template_metadata(self):
        store = Store(self.directory)
        store.save_provider_config({"provider": "openai", "baseUrl": LOCAL, "apiKey": "fixture-secret", "model": "fixture"})

        def handle(request):
            self.assertEqual(request.headers.get("authorization"), "Bearer fixture-secret")
            self.assertIn("新的题材词", json.loads(request.content)["messages"][1]["content"])
            text = json.dumps({"title": "实测题目", "premise": "实测梗概"}, ensure_ascii=False)
            return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})

        selected = hotspots.HOTSPOT_TROPES[1]
        result = await hotspots.generate_concept(trope_id=selected["id"], keywords="新的题材词",
                                                 llm_config={"provider": "openai"}, store=store,
                                                 client=await self.client(handle))
        self.assertEqual(result["title"], "实测题目")
        self.assertEqual(result["sourceTropeId"], selected["id"])
        self.assertEqual(result["pacingTemplate"]["id"], selected["pacingTemplate"])


class MemeAdmissionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))

    async def client(self, handler):
        return await self.enterAsyncContext(httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    async def test_strict_dual_admission_ignores_updated_at_dedupes_sources_and_future_dates(self):
        entry = _remote_meme("new")
        self.assertEqual(memes.evaluate_meme_admission(entry, now=NOW)["status"], "active")
        self.assertEqual(memes.evaluate_meme_admission({
            **entry, "earliestSpreadAt": "2025-01-01T00:00:00Z",
        }, now=NOW)["status"], "rejected_old")
        self.assertEqual(memes.evaluate_meme_admission({**entry, "spreadRecords": []}, now=NOW)["status"], "pending_verification")
        future = "2026-11-17T12:01:00Z"
        self.assertEqual(memes.evaluate_meme_admission({
            **entry, "spreadRecords": [{**item, "publishedAt": future} for item in entry["spreadRecords"]],
            "hotListRecords": [{"publishedAt": future}],
        }, now=NOW)["status"], "pending_verification")
        single_family = [
            {"creator": "甲", "sourceMaterialId": "A", "publishedAt": FRESH},
            {"creator": "甲", "sourceMaterialId": "other", "publishedAt": FRESH},
            {"creator": "搬运甲", "repostOf": "A", "sourceMaterialId": "B", "publishedAt": FRESH},
            {"creator": "搬运乙", "repostOf": "B", "publishedAt": FRESH},
            {"url": "https://example.test/uncredited", "publishedAt": FRESH},
        ]
        merged = memes.evaluate_meme_admission({**entry, "spreadRecords": single_family}, now=NOW)
        self.assertEqual((merged["status"], merged["independentCreatorsCount"]), ("pending_verification", 1))
        board = memes.evaluate_meme_admission({
            **entry, "spreadRecords": single_family, "hotListRecords": [{"date": FRESH, "url": "https://example.test/hot"}],
        }, now=NOW)
        self.assertEqual(board["status"], "active")
        self.assertTrue(board["hasHotListRecord"])
        exact = {**entry, "earliestSpreadAt": "2026-11-10T12:00:00Z"}
        self.assertEqual(memes.evaluate_meme_admission(exact, now=NOW)["status"], "active")
        self.assertEqual(memes.evaluate_meme_admission({
            **exact, "wasActive": True,
        }, now="2026-11-17T12:00:01Z")["status"], "expired")

    async def test_multi_page_overlap_429_alias_merge_and_cache_after_network_failure(self):
        first = True
        addresses = []
        rejected = False

        def handle(request):
            nonlocal rejected
            addresses.append((request.url.path, dict(request.url.params)))
            self.assertEqual(request.headers["user-agent"], memes.CNMEME_USER_AGENT)
            if not rejected:
                rejected = True
                return httpx.Response(429, headers={"Retry-After": "0"})
            offset = int(request.url.params["offset"])
            if first:
                return httpx.Response(200, json={"items": [
                    _remote_meme("one", "同一个梗", updated_at="2026-11-17T11:54:00Z"),
                ], "has_more": False})
            if offset == 0:
                return httpx.Response(200, json={"items": [
                    _remote_meme("alias", "同一个梗", updated_at="2026-11-17T11:59:00Z"),
                    _remote_meme("shifted", "窗口内闪回", updated_at="2026-11-17T11:32:00Z"),
                ], "has_more": True})
            return httpx.Response(200, json={"items": [
                _remote_meme("old_page", "窗口外", updated_at="2026-11-17T11:15:00Z"),
            ], "has_more": True})

        store = memes.MemeStore(self.directory, client=await self.client(handle))
        options = {"base_url": LOCAL, "limit": 2, "verify_details": False, "backoff_base_ms": 1, "now": NOW}
        created = await store.sync_memes(**options)
        self.assertEqual(created["syncMeta"]["retryCount"], 1)
        first = False
        synced = await store.sync_memes(**options)
        self.assertTrue(synced["ok"])
        index = {entry["slug"]: entry for entry in synced["items"]}
        self.assertEqual(set(index), {"one", "shifted"})
        self.assertEqual(index["one"]["mergedSlugs"], ["one", "alias"])
        self.assertEqual(index["one"]["independentCreatorsCount"], 2)
        self.assertEqual([params["offset"] for _, params in addresses], ["0", "0", "0", "2"])
        self.assertEqual(synced["syncMeta"]["lastSyncedUpdatedAt"], "2026-11-17T11:59:00.000Z")
        cached = await store.sync_memes(now="2026-11-24T12:00:00Z", simulate_failure=True)
        self.assertFalse(cached["ok"])
        self.assertTrue(cached["usingCache"])
        self.assertTrue(all(item["status"] == "expired" for item in cached["items"]))
        reopened = await memes.MemeStore(self.directory).get_memes(status="active", now="2026-11-24T12:00:00Z")
        self.assertEqual(reopened["items"], [])
        self.assertEqual(reopened["syncMeta"]["lastSyncedUpdatedAt"], synced["syncMeta"]["lastSyncedUpdatedAt"])

    async def test_detail_fetched_on_demand_and_source_dates_parsed(self):
        paths = []

        def handler(request):
            paths.append(request.url.path)
            if request.url.path.endswith("/memes"):
                # An update timestamp alone only advances the sync cursor.
                return httpx.Response(200, json={"items": [{
                    "slug": "detail", "title": "详情候选", "updated_at": "2026-11-17T11:00:00Z",
                }]})
            return httpx.Response(200, json={"item": {"content_html": "原帖日期 2026年11月16日： https://example.test/news.",
                                                      "hotListRecords": [{"date": FRESH}]}})

        store = memes.MemeStore(self.directory, client=await self.client(handler))
        first = await store.sync_memes(base_url=LOCAL, now=NOW)
        item = first["items"][0]
        self.assertEqual(item["status"], "active")
        self.assertIn("https://example.test/news", item["heatEvidenceUrls"])
        self.assertTrue(item["earliestSpreadAt"].startswith("2026-11-16"))
        await store.sync_memes(base_url=LOCAL, now=NOW)
        self.assertEqual(paths, ["/api/v1/public/memes", "/api/v1/public/memes/detail", "/api/v1/public/memes"])

    async def test_readonly_archive_import_alias_lock_pagination_and_opt_in_samples(self):
        path = self.directory / "历史梗档案.db"
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("CREATE TABLE meta(key TEXT, value TEXT)")
            db.execute("INSERT INTO meta VALUES(?, ?)", ("built_at", "2026-11-05 12:48:06"))
            db.execute("CREATE TABLE memes(title TEXT, slug TEXT, tags TEXT, usage TEXT, origin TEXT, other TEXT, summary TEXT, aliases TEXT)")
            db.executemany("INSERT INTO memes VALUES(?, ?, ?, ?, ?, ?, ?, ?)", [
                ("旧梗甲", "old", "反差;都市", "反转用法", "2025年8月9日 https://example.test/old", "", "句式模板与反差", '["旧名"]'),
                ("旧梗乙", "old2", "反转|都市", "反转用法", "", "", "另一种反差", ""),
                ("历史重名", "live", "反差", "", "", "", "旧梗", ""),
            ])
        original = path.read_bytes()
        store = memes.MemeStore(self.directory)
        await store.sync_memes(custom_items=[_remote_meme("live", "新梗")], now=NOW)
        with patch.dict(os.environ, {memes.ARCHIVE_PATH_ENV: str(path)}):
            imported = await store.import_memes_from_sqlite(now=NOW)
        self.assertEqual((imported["imported"], imported["skippedNonArchive"], imported["activeCount"]), (2, 1, 1))
        self.assertEqual(path.read_bytes(), original)
        updated = await store.sync_memes(custom_items=[_remote_meme("changed", "旧名")], now=NOW)
        self.assertEqual({item["slug"]: item["status"] for item in updated["items"]},
                         {"live": "active", "old": "rejected_old", "old2": "rejected_old"})
        page = await store.get_memes(status="rejected_old", tag="都市", q="反差", now=NOW, limit=1, offset=1)
        self.assertEqual((page["total"], len(page["items"])), (2, 1))
        samples = await store.get_meme_inspiration_samples(meme_source_mode="include_archive_elements", now=NOW)
        self.assertEqual([item["isArchivePattern"] for item in samples], [False])
        later = "2026-11-24T12:00:00Z"
        self.assertEqual(await store.get_meme_inspiration_samples(now=later), [])
        patterns = await store.get_meme_inspiration_samples(meme_source_mode="include_archive_elements", now=later)
        self.assertEqual([item["isArchivePattern"] for item in patterns], [True, True])

    async def test_cancelled_sync_keeps_cursor_and_all_reopened_entries(self):
        store = memes.MemeStore(self.directory)
        await store.sync_memes(custom_items=[_remote_meme("original")], now=NOW)
        entered = asyncio.Event()

        async def wait(_):
            entered.set()
            await asyncio.Future()

        client = await self.client(wait)
        task = asyncio.create_task(store.sync_memes(client=client, now=NOW))
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        current = await memes.MemeStore(self.directory).get_memes(now=NOW)
        self.assertEqual([entry["slug"] for entry in current["items"]], ["original"])
        self.assertEqual(current["syncMeta"]["lastSyncedUpdatedAt"], "2026-11-17T11:00:00.000Z")

    async def test_invalid_list_response_preserves_previous_success(self):
        store = memes.MemeStore(self.directory)
        initial = await store.sync_memes(custom_items=[_remote_meme("original")], now=NOW)
        client = await self.client(lambda _: httpx.Response(200, json={"unrelated": "no items"}))
        failed = await store.sync_memes(client=client, now=NOW)
        self.assertFalse(failed["ok"])
        self.assertEqual(failed["syncMeta"]["lastSuccessAt"], initial["syncMeta"]["lastSuccessAt"])
        self.assertEqual(failed["items"], initial["items"])
