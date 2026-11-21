"""Incremental CNMeme Wiki cache and evidence-based meme admission."""

import asyncio
import json
import math
import os
import re
import sqlite3
from contextlib import asynccontextmanager, closing
from copy import deepcopy
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import quote

import httpx

from .storage import _atomic_write_json


CNMEME_BASE_URL = "https://cnmeme.wiki"
CNMEME_USER_AGENT = "CNMemeWikiPlugin/1.0"
DEFAULT_OVERLAP_MS = 30 * 60 * 1000
DEFAULT_AUTO_SYNC_MS = 6 * 60 * 60 * 1000
DEFAULT_MAX_AGE_DAYS = 7
ARCHIVE_PATH_ENV = "NOVEL_FORGE_MEME_ARCHIVE_SQLITE"
_ARCHIVED_BUILT_AT = "2026-11-05T12:48:06+08:00"
_ARCHIVE_REASON = "冷启动历史档案（2026-11-05 已收录老梗）"
_DAY_MS = 86_400_000
_URLS = re.compile(r"""https?://[^\s"'<>)\]]+""", re.I)
_ISO_DATE = re.compile(
    r"\b(20[0-9]{2})[-/]([0-9]{1,2})[-/]([0-9]{1,2})"
    r"(?:[T\s]([0-9]{1,2}):([0-9]{2})(?::([0-9]{2}))?(Z|[+-][0-9]{2}:?[0-9]{2})?)?\b",
    re.ASCII,
)
_CN_DATE = re.compile(r"(20[0-9]{2})\s*年\s*([0-9]{1,2})\s*月\s*([0-9]{1,2})\s*日")
_TERM_PUNCTUATION = re.compile(r"""[\s\-_·.,，。！？!?:：'"“”‘’()（）\[\]【】]+""")


def _record(value):
    return value if isinstance(value, dict) else {}


def _list(value):
    return value if isinstance(value, list) else []


def _strings(values):
    return [value for value in _list(values) if isinstance(value, str) and value]


def _unique(values):
    return list(dict.fromkeys(value for value in values if value))


def _timestamp_ms(value):
    if value is None or value == "":
        return None
    try:
        if type(value) in (int, float):
            return value if math.isfinite(value) and -62_135_596_800_000 <= value <= 253_402_300_799_999 else None
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, date):
            parsed = datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
        else:
            text = str(value).strip().replace("/", "-")
            parsed = datetime.fromisoformat(text)
            if len(text) == 10:
                parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp() * 1000)
    except (ValueError, OSError, OverflowError):
        return None


def _now_ms(now):
    specified = _timestamp_ms(now)
    return int(datetime.now(timezone.utc).timestamp() * 1000) if specified is None else specified


def _iso(milliseconds):
    return datetime.fromtimestamp(milliseconds / 1000, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def extract_provenance_from_text(text=""):
    text = str(text or "")
    urls = _unique(match[0].rstrip(".,;!?") for match in _URLS.finditer(text))
    dates = []
    for match in _ISO_DATE.finditer(text):
        year, month, day, hours, minutes, seconds, zone = match.groups()
        value = f"{year}-{int(month):02d}-{int(day):02d}"
        if hours is not None:
            value += f"T{int(hours):02d}:{minutes}:{seconds or '00'}{zone or ''}"
        stamp = _timestamp_ms(value)
        if stamp is not None:
            dates.append(stamp)
    for match in _CN_DATE.finditer(text):
        year, month, day = match.groups()
        stamp = _timestamp_ms(f"{year}-{int(month):02d}-{int(day):02d}")
        if stamp is not None:
            dates.append(stamp)
    return {"urls": urls, "dates": dates}


def _normalize_term(text):
    return _TERM_PUNCTUATION.sub("", str(text or "").lower()).strip()


def _independent_creators(records):
    """Link reposts and sources, and count credited original creators only once."""
    parents = {}

    def root(key):
        parents.setdefault(key, key)
        while parents[key] != key:
            parents[key] = parents[parents[key]]
            key = parents[key]
        return key

    credited = set()
    for entry in records:
        material = entry.get("sourceMaterialId")
        repost = entry.get("repostOf")
        if repost:
            reference = ("source", str(repost))
            if material:
                parents[root(("source", str(material)))] = root(reference)
            continue
        creator = _normalize_term(entry.get("creator"))
        if not creator or entry.get("isRepost"):
            continue
        key = ("creator", creator)
        credited.add(key)
        if material:
            parents[root(("source", str(material)))] = root(key)
    return len({root(key) for key in credited})


def evaluate_meme_admission(entry, *, now=None, max_age_days=DEFAULT_MAX_AGE_DAYS):
    now_ms = _now_ms(now)
    max_age = max_age_days * _DAY_MS
    evidence = _strings(entry.get("heatEvidenceUrls"))
    spread = [item for item in _list(entry.get("spreadRecords")) if isinstance(item, dict)]
    hot = [item for item in _list(entry.get("hotListRecords")) if isinstance(item, dict)]
    video = _record(entry.get("sourceVideo"))
    bvid = video.get("bvid")
    if video and any(video.get(key) for key in ("published_at", "creator_name", "bvid")) and not (
        bvid and any(item.get("sourceMaterialId") == bvid or str(bvid) in str(item.get("url") or "") for item in spread)
    ):
        spread.append({"creator": video.get("creator_name"), "url": f"https://www.bilibili.com/video/{bvid}" if bvid else "",
                       "publishedAt": video.get("published_at"), "sourceMaterialId": bvid})
    times = []
    earliest_saved = _timestamp_ms(entry.get("earliestSpreadAt"))
    if earliest_saved is not None and earliest_saved <= now_ms:
        times.append(earliest_saved)
    has_hot_list = bool(entry.get("hasHotListRecord"))
    sources = []
    for item, dedicated_hot_list in [(item, False) for item in spread] + [(item, True) for item in hot]:
        if isinstance(item.get("url"), str):
            evidence.append(item["url"])
        stamp = _timestamp_ms(item.get("publishedAt") or item.get("published_at") or item.get("date"))
        if stamp is not None and stamp > now_ms:
            continue
        if stamp is not None:
            times.append(stamp)
        if dedicated_hot_list or item.get("isHotList") or item.get("type") == "hot_list":
            if stamp is None or now_ms - stamp <= max_age:
                has_hot_list = True
        else:
            sources.append(item)
    for provenance in (entry.get("summary"), entry.get("contentHtml") or entry.get("content_html")):
        extracted = extract_provenance_from_text(provenance)
        evidence.extend(extracted["urls"])
        times.extend(stamp for stamp in extracted["dates"] if stamp <= now_ms)

    earliest = min(times) if times else None
    earliest_iso = _iso(earliest) if earliest is not None else None
    creators = _independent_creators(sources)
    was_active = entry.get("status") == "active" or bool(entry.get("wasActive"))
    result = {"earliestSpreadAt": earliest_iso, "independentCreatorsCount": creators,
              "hasHotListRecord": has_hot_list, "heatEvidenceUrls": _unique(evidence),
              "verifiedAt": entry.get("verifiedAt"), "wasActive": was_active}
    if entry.get("historicalArchive") is True:
        return {**result, "status": "rejected_old", "statusReason": entry.get("statusReason") or _ARCHIVE_REASON,
                "earliestSpreadAt": entry.get("earliestSpreadAt") or entry.get("archivedBuiltAt") or _ARCHIVED_BUILT_AT,
                "independentCreatorsCount": 0, "hasHotListRecord": False, "wasActive": False}
    if earliest is None:
        return {**result, "status": "pending_verification", "verifiedAt": None,
                "statusReason": "缺少可核实的公开最早传播时间（不使用词条编辑时间 updated_at），暂置待核验。"}
    if now_ms - earliest > max_age:
        reason = (f"已超过 {max_age_days} 天，已自动淘汰。" if was_active
                  else f"距今已超过 {max_age_days} 天，老梗不予入库。")
        return {**result, "status": "expired" if was_active else "rejected_old",
                "statusReason": f"最早传播时间（{earliest_iso[:10]}）{reason}"}
    if not has_hot_list and creators < 2:
        return {**result, "status": "pending_verification", "verifiedAt": None,
                "statusReason": f"满足 {max_age_days} 天内传播要求，但独立创作者数({creators})不足2且无热榜记录，暂置待核验。"}
    hot_note = "，含热榜记录" if has_hot_list else ""
    return {**result, "status": "active", "verifiedAt": entry.get("verifiedAt") or _iso(now_ms), "wasActive": True,
            "statusReason": f"已核验：{max_age_days}天内新梗（最早传播 {earliest_iso[:10]}），独立创作者 {creators} 位{hot_note}。"}


class _JsonCache:
    """One owner per cache file. Keep readers on the last durable snapshot."""

    def __init__(self, path, empty, validate):
        self.path = Path(path)
        self.lock = asyncio.Lock()
        self._empty = empty
        self._validate = validate
        self.snapshot = None

    def _read(self):
        try:
            return self._validate(json.loads(self.path.read_text(encoding="utf-8-sig")))
        except (OSError, ValueError):
            return None

    async def load(self):
        if self.snapshot is None:
            saved = await asyncio.to_thread(self._read)
            if self.snapshot is None:
                self.snapshot = saved if saved is not None else self._empty()
        return self.snapshot

    def _write(self, snapshot):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(self.path, snapshot)

    async def save(self, snapshot):
        pending = asyncio.create_task(asyncio.to_thread(self._write, snapshot))
        try:
            await asyncio.shield(pending)
        except asyncio.CancelledError:
            await pending
            self.snapshot = snapshot
            raise
        self.snapshot = snapshot


@asynccontextmanager
async def _http_client(client):
    if client is None:
        async with httpx.AsyncClient(timeout=15, trust_env=False) as owned:
            yield owned
    else:
        yield client


def _empty_store():
    return {
        "syncMeta": {
            "lastSuccessAt": None, "lastAttemptAt": None, "lastSyncedUpdatedAt": None, "overlapMs": DEFAULT_OVERLAP_MS,
            "autoRefreshIntervalMs": DEFAULT_AUTO_SYNC_MS, "usingCache": False, "lastError": None, "retryCount": 0,
            "lastSyncAdded": 0, "lastSyncUpdated": 0, "totalEntries": 0,
        },
        "entries": {},
    }


def _stored_memes(saved):
    if not isinstance(saved, dict) or not isinstance(saved.get("entries"), dict):
        return None
    return {"syncMeta": {**_empty_store()["syncMeta"], **_record(saved.get("syncMeta"))},
            "entries": {slug: entry for slug, entry in saved["entries"].items() if isinstance(entry, dict)}}


def _terms(entry):
    return _unique(_normalize_term(item) for item in [entry.get("title"), *_strings(entry.get("aliases"))])


def _meme_url(slug):
    return f"{CNMEME_BASE_URL}/wiki/{quote(slug, safe='')}"


def _merge_entry(existing, incoming, detail, attempt_at):
    old = existing or {}
    merged = {**incoming, **detail} if detail else incoming
    slug = old.get("slug") or merged.get("slug") or incoming["slug"]
    title = old.get("title") or merged.get("title") or slug
    aliases = _unique([*_strings(old.get("aliases")), *_strings(merged.get("aliases")),
                       merged.get("title") if merged.get("title") != old.get("title") else None])

    def provided(*keys, default=None):
        return next((merged[key] for key in keys if merged.get(key) is not None), default)

    def records(key):
        unique = {}
        for item in [*_list(old.get(key)), *_list(merged.get(key))]:
            if isinstance(item, dict):
                unique.setdefault(json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")), item)
        return list(unique.values())

    return {
        "slug": slug, "title": title, "summary": merged.get("summary") or old.get("summary") or "",
        "tags": _unique([*_strings(old.get("tags")), *_strings(merged.get("tags"))]), "aliases": aliases,
        "mergedSlugs": _unique([*(_strings(old.get("mergedSlugs")) or [slug]), merged.get("slug"), incoming["slug"]]),
        "sourceUrl": _meme_url(slug), "updatedAt": merged.get("updated_at") or merged.get("updatedAt") or old.get("updatedAt") or attempt_at,
        "sourceVideo": provided("source_video", "sourceVideo", default=old.get("sourceVideo")),
        "contentHtml": provided("content_html", "contentHtml", default=old.get("contentHtml", "")),
        "detailFetchedAt": attempt_at if detail else old.get("detailFetchedAt"),
        "spreadRecords": records("spreadRecords"), "hotListRecords": records("hotListRecords"),
        "earliestSpreadAt": merged.get("earliestSpreadAt") or old.get("earliestSpreadAt"),
        "heatEvidenceUrls": _unique([*_strings(old.get("heatEvidenceUrls")), *_strings(merged.get("heatEvidenceUrls"))]),
        "hasHotListRecord": bool(old.get("hasHotListRecord") or merged.get("hasHotListRecord")),
        "verifiedAt": old.get("verifiedAt"), "status": old.get("status") or "pending_verification",
        "statusReason": old.get("statusReason") or "",
        "wasActive": bool(old.get("wasActive") or old.get("status") == "active"),
        "historicalArchive": bool(old.get("historicalArchive") or merged.get("historicalArchive")),
        "archivedBuiltAt": old.get("archivedBuiltAt") or merged.get("archivedBuiltAt"),
        **{field: provided(field, default=old.get(field, "")) for field in ("usageText", "originText", "otherText")},
    }


def _reverify(entries, now, max_age_days=DEFAULT_MAX_AGE_DAYS):
    return {slug: {**entry, **evaluate_meme_admission(entry, now=now, max_age_days=max_age_days)}
            for slug, entry in entries.items()}


class _MemeFetchError(RuntimeError):
    pass


async def _fetch_page(client, url, *, max_retries, backoff_base_ms, retries):
    for attempt in range(max_retries + 1):
        retry_after = None
        try:
            response = await client.get(url, headers={"User-Agent": CNMEME_USER_AGENT, "Accept": "application/json"})
            if response.is_success:
                return response.json()
            error = _MemeFetchError(f"CNMemeWiki HTTP {response.status_code} from {url}")
            retryable = response.status_code == 429 or response.status_code >= 500
            retry_after = response.headers.get("Retry-After")
        except httpx.RequestError as exc:
            error = _MemeFetchError(f"CNMemeWiki 请求失败：{type(exc).__name__}")
            retryable = True
        if not retryable or attempt >= max_retries:
            raise error
        delay = backoff_base_ms * 2**attempt
        if retry_after:
            try:
                seconds = float(retry_after)
                if math.isfinite(seconds):
                    delay = max(10, min(5000, seconds * 1000))
            except ValueError:
                pass
        retries[0] += 1
        await asyncio.sleep(min(delay, 4 * backoff_base_ms) / 1000)


def _aliases_for(entries):
    aliases, slugs = {}, {}
    for slug, entry in entries.items():
        for term in _terms(entry):
            aliases.setdefault(term, slug)
        for alias in _strings(entry.get("mergedSlugs")):
            slugs.setdefault(alias, slug)
    return aliases, slugs


def _updated_ms(entry):
    return _timestamp_ms(entry.get("updated_at") or entry.get("updatedAt"))


def _public_values(snapshot):
    return {"syncMeta": deepcopy(snapshot["syncMeta"]), "items": deepcopy(list(snapshot["entries"].values()))}


def _parse_list_field(raw):
    if isinstance(raw, list):
        values = raw
    elif not raw:
        return []
    else:
        text = str(raw).strip()
        if text.startswith("["):
            try:
                parsed = json.loads(text)
                if isinstance(parsed, list):
                    return _parse_list_field(parsed)
            except ValueError:
                pass
        values = re.split(r"[,，、|;；\n]+", text)
    return [value for item in values if (value := str(item).strip())]


def _import_archive(path, existing):
    archive_date = _ARCHIVED_BUILT_AT
    imported = skipped = 0
    entries = dict(existing)
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        try:
            built = db.execute("SELECT value FROM meta WHERE key = ?", ("built_at",)).fetchone()
            if built and built["value"]:
                stamp = str(built["value"]).strip()
                if stamp:
                    archive_date = stamp if "T" in stamp else stamp.replace(" ", "T", 1) + "+08:00"
        except sqlite3.OperationalError:
            pass
        rows = db.execute("SELECT title, slug, tags, usage, origin, other, summary, aliases FROM memes")
        for row in rows:
            slug = str(row["slug"] or "").strip()
            if not slug:
                continue
            if slug in entries and entries[slug].get("historicalArchive") is not True:
                skipped += 1
                continue
            origin = str(row["origin"] or "").strip()
            evidence = extract_provenance_from_text(origin)
            earliest = _iso(min(evidence["dates"])) if evidence["dates"] else archive_date
            entries[slug] = {
                "slug": slug, "title": str(row["title"] or slug).strip(), "summary": str(row["summary"] or "").strip(),
                "tags": _parse_list_field(row["tags"]), "aliases": _parse_list_field(row["aliases"]),
                "mergedSlugs": [slug], "sourceUrl": _meme_url(slug), "updatedAt": archive_date, "sourceVideo": None,
                "contentHtml": "", "detailFetchedAt": None, "spreadRecords": [], "hotListRecords": [],
                "earliestSpreadAt": earliest, "heatEvidenceUrls": evidence["urls"],
                "independentCreatorsCount": 0, "hasHotListRecord": False, "verifiedAt": None, "status": "rejected_old",
                "statusReason": _ARCHIVE_REASON, "wasActive": False, "historicalArchive": True,
                "archivedBuiltAt": archive_date, "usageText": str(row["usage"] or "").strip(),
                "originText": origin, "otherText": str(row["other"] or "").strip(),
            }
            imported += 1
    return entries, imported, skipped, archive_date


class MemeStore:
    def __init__(self, data_dir, *, client=None):
        self._cache = _JsonCache(Path(data_dir) / "memes_store.json", _empty_store, _stored_memes)
        self.client = client

    async def sync_memes(self, *, base_url=CNMEME_BASE_URL, now=None, overlap_ms=DEFAULT_OVERLAP_MS,
                         max_pages=10, limit=30, verify_details=True, custom_items=None, simulate_failure=False,
                         backoff_base_ms=150, max_retries=3, client=None):
        attempt = _now_ms(now)
        attempt_at = _iso(attempt)
        options = {"max_retries": max_retries, "backoff_base_ms": backoff_base_ms, "retries": [0]}
        root = base_url.rstrip("/")
        async with self._cache.lock:
            original = await self._cache.load()
            meta = {**original["syncMeta"], "lastAttemptAt": attempt_at, "overlapMs": overlap_ms}
            entries = dict(original["entries"])
            cursor = _timestamp_ms(meta["lastSyncedUpdatedAt"])
            last_updated = cursor or 0
            added = updated = 0
            try:
                if simulate_failure:
                    raise _MemeFetchError("Simulated CNMemeWiki sync failure")
                async with _http_client(client if client is not None else self.client) as active_client:
                    if custom_items is not None:
                        collected = _list(custom_items)
                    else:
                        collected = await self._list_pages(active_client, root, max_pages, limit, cursor, overlap_ms, options)
                    aliases, slugs = await asyncio.to_thread(_aliases_for, entries)
                    for item in collected:
                        if not isinstance(item, dict) or not isinstance(item.get("slug"), str) or not item["slug"]:
                            continue
                        updated_at = _updated_ms(item)
                        if updated_at is not None:
                            last_updated = max(last_updated, updated_at)
                        slug = item["slug"]
                        canonical = slug if slug in entries else slugs.get(slug) or next(
                            (aliases[term] for term in _terms(item) if term in aliases), None,
                        )
                        existing = entries.get(canonical)
                        detail = None
                        if verify_details and custom_items is None and (
                            not existing or not existing.get("detailFetchedAt")
                            or not existing.get("earliestSpreadAt") and not _record(item.get("source_video")).get("published_at")
                            or existing.get("updatedAt") != (item.get("updated_at") or item.get("updatedAt"))
                        ):
                            detail_url = f"{root}/api/v1/public/memes/{quote(slug, safe='')}"
                            try:
                                detail_response = await _fetch_page(
                                    active_client, detail_url, **{**options, "max_retries": min(1, max_retries)},
                                )
                                detail = _record(_record(detail_response).get("item"))
                            except (ValueError, _MemeFetchError):
                                pass
                        merged = _merge_entry(existing, item, detail, attempt_at)
                        entries[merged["slug"]] = merged
                        for term in _terms(merged):
                            aliases.setdefault(term, merged["slug"])
                        for alias in merged["mergedSlugs"]:
                            slugs.setdefault(alias, merged["slug"])
                        if existing is None:
                            added += 1
                        else:
                            updated += 1

                verified = await asyncio.to_thread(_reverify, entries, attempt)
                successful = {"entries": verified, "syncMeta": {
                    **meta, "lastSuccessAt": attempt_at,
                    "lastSyncedUpdatedAt": _iso(last_updated) if last_updated > 0 else meta["lastSyncedUpdatedAt"],
                    "usingCache": False, "lastError": None, "retryCount": options["retries"][0],
                    "lastSyncAdded": added, "lastSyncUpdated": updated, "totalEntries": len(verified),
                }}
                await self._cache.save(successful)
                return {"ok": True, **_public_values(successful)}
            except (ValueError, OSError, _MemeFetchError, httpx.HTTPError) as error:
                verified = await asyncio.to_thread(_reverify, original["entries"], attempt)
                has_cache = bool(verified)
                fallback = {"entries": verified, "syncMeta": {
                    **meta, "usingCache": has_cache, "lastError": str(error), "retryCount": options["retries"][0],
                    "totalEntries": len(verified),
                }}
                try:
                    await self._cache.save(fallback)
                except OSError:
                    # Preserve the last durable version if the disk is currently unavailable.
                    pass
                return {"ok": False, "usingCache": has_cache, "error": str(error), **_public_values(fallback)}

    @staticmethod
    async def _list_pages(client, root, max_pages, limit, cursor, overlap, options):
        collected = []
        cutoff = None if cursor is None else cursor - overlap
        for page in range(max_pages):
            endpoint = f"{root}/api/v1/public/memes?q=&tag=&limit={limit}&offset={page * limit}"
            payload = await _fetch_page(client, endpoint, **options)
            if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
                raise _MemeFetchError("CNMemeWiki 列表格式不正确")
            items = payload["items"]
            if not items:
                break
            reached = False
            for item in items:
                if not isinstance(item, dict):
                    continue
                stamp = _updated_ms(item)
                if cutoff is not None and stamp is not None and stamp < cutoff:
                    reached = True
                    break
                collected.append(item)
            if reached or not payload.get("has_more"):
                break
        return collected

    async def reverify_cached_memes(self, *, now=None, max_age_days=DEFAULT_MAX_AGE_DAYS):
        async with self._cache.lock:
            saved = await self._cache.load()
            entries = await asyncio.to_thread(_reverify, saved["entries"], _now_ms(now), max_age_days)
            if entries != saved["entries"]:
                saved = {"entries": entries, "syncMeta": {**saved["syncMeta"], "totalEntries": len(entries)}}
                await self._cache.save(saved)
            return deepcopy(saved)

    async def get_memes(self, *, status="", q="", tag="", now=None, limit=None, offset=0):
        saved = await self.reverify_cached_memes(now=now)
        query = str(q or "").strip().lower()
        tag = str(tag or "").strip().lower()
        matches = []
        for entry in saved["entries"].values():
            if status and status != "all" and entry.get("status") != status:
                continue
            tags = _strings(entry.get("tags"))
            if tag and not any(tag in value.lower() for value in tags):
                continue
            search = [entry.get("title"), entry.get("summary"), *_strings(entry.get("aliases")), *tags]
            if query and not any(isinstance(value, str) and query in value.lower() for value in search):
                continue
            matches.append(entry)
        matches.sort(key=lambda item: _updated_ms(item) or 0, reverse=True)
        start = max(0, int(offset or 0))
        end = start + int(limit) if limit is not None and int(limit) > 0 else None
        return {"syncMeta": saved["syncMeta"], "items": matches[start:end], "total": len(matches)}

    async def import_memes_from_sqlite(self, sqlite_path=None, *, now=None):
        sqlite_path = sqlite_path or os.environ.get(ARCHIVE_PATH_ENV)
        if not sqlite_path:
            raise ValueError(f"请提供历史热梗 SQLite 路径或设置 {ARCHIVE_PATH_ENV}")
        async with self._cache.lock:
            saved = await self._cache.load()
            entries, imported, skipped, archive_date = await asyncio.to_thread(_import_archive, sqlite_path, saved["entries"])
            await self._cache.save({"entries": entries, "syncMeta": {**saved["syncMeta"], "totalEntries": len(entries)}})
        return {"imported": imported, "skippedNonArchive": skipped, "totalEntries": len(entries),
                "activeCount": sum(entry.get("status") == "active" for entry in entries.values()),
                "archivedBuiltAt": archive_date, "importedAt": _iso(_now_ms(now))}

    async def get_active_memes(self, *, now=None):
        saved = await self._cache.load()
        entries = await asyncio.to_thread(_reverify, saved["entries"], _now_ms(now))
        active = [entry for entry in entries.values() if entry["status"] == "active"]
        return deepcopy(sorted(active, key=lambda entry: _updated_ms(entry) or 0, reverse=True))

    async def get_meme_inspiration_samples(self, *, genre="", meme_source_mode="active_only", limit=4, now=None):
        active = await self.get_active_memes(now=now)
        if active:
            return [{
                "title": item.get("title"), "summary": item.get("summary") or "", "tags": _strings(item.get("tags")),
                "sourceUrl": item.get("sourceUrl") or "", "isArchivePattern": False,
            } for item in active[:limit]]
        if meme_source_mode != "include_archive_elements" or limit <= 0:
            return []
        saved = await self._cache.load()
        keywords = ("反差", "黑色幽默", "沟通错位", "句式模板", "打脸", "反转", "吐槽", "谐音", "热梗")
        genre = str(genre or "").strip()
        ranked = []
        for entry in saved["entries"].values():
            if entry.get("historicalArchive") is not True:
                continue
            tags = _strings(entry.get("tags"))
            summary = str(entry.get("summary") or entry.get("usageText") or "").strip()
            if not summary:
                continue
            score = 2 * sum(word in summary or any(word in tag for tag in tags) for word in keywords)
            if genre and any(tag in genre or genre[:2] in tag for tag in tags):
                score += 3
            if score:
                ranked.append((score, {
                    "title": entry.get("title"), "summary": summary[:140], "tags": tags[:5],
                    "sourceUrl": entry.get("sourceUrl") or "", "isArchivePattern": True,
                }))
            if len(ranked) >= limit * 10:
                break
        return [entry for _, entry in sorted(ranked, key=lambda item: item[0], reverse=True)[:limit]]
