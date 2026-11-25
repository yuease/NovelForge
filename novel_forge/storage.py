import hashlib
import json
import logging
import math
import os
import re
import secrets
import sqlite3
import threading
import weakref
from contextlib import closing, contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile

from .arc_planning import legacy_outline_placeholder, prior_arc_summaries, saved_arc_plans
from .long_memory import empty_long_memory, normalize_arc_summary, normalize_memory_delta, normalize_volume_summary, replay_long_memory
from .openai_siwc import PROVIDER as SIWC, RESOURCE as SIWC_RESOURCE
from .providers import (
    PROVIDERS, ProviderCatalogError, get_provider_definition, is_masked_api_key, mask_api_key,
    normalize_model_ids, provider_requires_key, same_endpoint_origin,
)

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chapters (
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    number INTEGER NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (job_id, number)
);
CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY);
"""

_config_locks = weakref.WeakValueDictionary()
_config_locks_guard = threading.Lock()
_CJK = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")
_ASCII_WORD = re.compile(r"[a-zA-Z0-9_]+")
_SURROGATE = re.compile(r"[\ud800-\udfff]")
_COMMITTED = frozenset(("completed", "verified", "repaired", "needs_review"))


class StorageError(RuntimeError):
    def __init__(self, message, status=409, code="STORAGE_ERROR"):
        super().__init__(message)
        self.status = status
        self.code = code


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _json(value, *, indent=None):
    result = json.dumps(value, ensure_ascii=False, allow_nan=False, indent=indent,
                        separators=(",", ":") if indent is None else None)
    return _SURROGATE.sub(lambda match: f"\\u{ord(match[0]):04x}", result)


def _atomic_write_json(path, value):
    payload = _json(value, indent=2)
    temporary = None
    try:
        with NamedTemporaryFile(dir=path.parent, prefix=f"{path.name}.tmp.", delete=False,
                                mode="w", encoding="utf-8", newline="\n") as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _provider_defaults(provider):
    metadata = get_provider_definition(provider) or {}
    preset = metadata.get("models") or [""]
    model = "gemini-3.8-flash" if provider == "vertex" else preset[0]
    return metadata.get("baseUrl", ""), model, "disabled"


def _env_api_key(provider, base_url):
    metadata = get_provider_definition(provider) or {}
    variable = metadata.get("env")
    if variable and (provider == "openai-compatible" or same_endpoint_origin(base_url, metadata["baseUrl"])):
        return os.environ.get(variable, "")
    return ""


def _dict(value):
    return value if isinstance(value, dict) else {}


def _list(value):
    return value if isinstance(value, list) else []


def _provider_config(store, provider=None):
    provider = str(provider or store.get("activeProvider") or "deepseek").lower()
    default_url, default_model, thinking = _provider_defaults(provider)
    saved = _dict(_dict(store.get("providers")).get(provider))
    base_url = saved["baseUrl"] if saved.get("baseUrl") is not None else default_url
    manual = normalize_model_ids(saved.get("manualModels"))
    discovered = normalize_model_ids(saved.get("discoveredModels"))
    if discovered and default_model not in discovered:
        default_model = discovered[0]
    return {
        "provider": provider, "baseUrl": base_url, "model": saved.get("model") or default_model or (manual[0] if manual else ""),
        "savedModel": saved.get("model") or "", "manualModels": manual, "discoveredModels": discovered,
        "thinkingMode": saved.get("thinkingMode") or thinking,
        "apiKey": saved.get("apiKey") or _env_api_key(provider, base_url),
        "explicitlyConfigured": bool(saved.get("baseUrl")),
    }


def _public_provider(config):
    return {key: config[key] for key in ("provider", "baseUrl", "model", "thinkingMode")} | {
        "hasKey": bool(config["apiKey"]), "maskedKey": mask_api_key(config["apiKey"]),
    }


def count_chinese_words(text=""):
    if not text:
        return 0
    return len(_CJK.findall(text)) + len(_ASCII_WORD.findall(_CJK.sub(" ", text)))


def _number(value, default=0):
    try:
        number = float(value if value is not None and value != "" else 0)
        if math.isfinite(number):
            return int(number) if number.is_integer() else number
    except (TypeError, ValueError, OverflowError):
        pass
    return default


def _first_non_null(*values):
    return next((value for value in values if value is not None), None)


def _is_committed(chapter):
    return (chapter or {}).get("status") in _COMMITTED


def manuscript_fingerprint(chapters):
    version = []
    for chapter in chapters:
        number = chapter.get("chapterNumber")
        if isinstance(number, float):
            number = _number(number, None)
        version.append([number, chapter.get("title"), chapter.get("content")])
    return hashlib.sha256(_json(version).encode("utf-8")).hexdigest()


def volume_source_hash(chapters):
    return manuscript_fingerprint([
        {**chapter, "content": _json([chapter.get("content"), chapter.get("summary") or "",
                                     *(["needs_review"] if chapter.get("status") == "needs_review" else [])])}
        for chapter in chapters
    ])


def arc_context_version(facts, preceding, before):
    return (
        tuple((row.get("arc"), row["startChapter"], row["endChapter"], row.get("sourceHash"))
              for row in prior_arc_summaries(facts, before)),
        tuple((row.get("endChapter"), row.get("sourceHash")) for row in _list(facts.get("volumeSummaries"))
              if isinstance(row, dict) and _number(row.get("endChapter"), math.inf) < before),
        volume_source_hash(preceding[-5:]),
    )


def revision_manuscript_state(job, chapters, active=False):
    if active or job.get("evaluationStatus") == "running" or job.get("status") not in ("completed", "failed"):
        return "busy"
    expected = _number(job.get("totalChapters"))
    if expected < 1 or len(chapters) != expected or any(
        type(chapter.get("chapterNumber")) not in (int, float) or chapter["chapterNumber"] != index or
        (chapter.get("status") is not None and not _is_committed(chapter))
        for index, chapter in enumerate(chapters, 1)
    ):
        return "incomplete"
    return "ready"


def _archive_touches(archive, chapters):
    row = _dict(archive)
    start, end = _number(row.get("startChapter")), _number(row.get("endChapter"))
    return any(start <= chapter <= end for chapter in chapters)


def _invalidate_archives(facts, chapters):
    return {
        **facts, **{kind: [row for row in _list(facts.get(kind)) if not _archive_touches(row, chapters)]
                   for kind in ("volumeSummaries", "arcSummaries") if kind in facts},
    }


def _job_record(data, config, now):
    target = _number(data.get("targetWords")) or 30000
    words = _number(data.get("wordsPerChapter")) or 2000
    total = _number(data.get("totalChapters")) or max(1, math.floor(target / words + 0.5))
    raw = _dict(data.get("llmConfig"))
    provider = config["provider"]
    saved = raw.get("useSavedConnection") is True
    base_url = "" if saved else str(_first_non_null(raw.get("baseUrl"), data.get("baseUrl"), "")).strip()
    model = str(_first_non_null(raw.get("model"), data.get("model"), "")).strip()
    if not model or (provider != "mock" and model == "mock-novel-v1"):
        model = "mock-novel-v1" if provider == "mock" else config["model"]
    retries = raw.get("maxRetries") if "maxRetries" in raw else data.get("maxRetries", 5)
    llm = {
        "provider": provider, "baseUrl": base_url or ("" if provider == "mock" else config["baseUrl"]),
        **({"useSavedConnection": True} if saved else {"apiKey": _first_non_null(raw.get("apiKey"), data.get("apiKey"), "")}),
        "model": model, "thinkingMode": _first_non_null(raw.get("thinkingMode"), data.get("thinkingMode"),
                                                       config["thinkingMode"], "disabled"),
        "temperature": _first_non_null(raw.get("temperature"), data.get("temperature"), 0.7),
        "timeoutMs": _number(_first_non_null(raw.get("timeoutMs"), data.get("timeoutMs"), 300000)) or 300000,
        "maxRetries": _number(retries, None),
    }
    if raw.get("lightModel"):
        llm["lightModel"] = str(raw["lightModel"]).strip()
    if isinstance(raw.get("autoResumeOnRateLimit"), bool):
        llm["autoResumeOnRateLimit"] = raw["autoResumeOnRateLimit"]

    topic = data.get("topicPackage")
    meme_level = data.get("memeLevel")
    job = {
        "id": data.get("id") or f"job_{int(datetime.fromisoformat(now).timestamp() * 1000)}_{secrets.token_hex(3)}",
        "title": data.get("title") or "未命名小说项目", "premise": data.get("premise") or "",
        "genre": data.get("genre") or "都市爽文", "pov": data.get("pov") or "第三人称限知",
        "targetWords": target, "wordsPerChapter": words, "totalChapters": total,
        "completedChapters": 0, "totalWords": 0, "status": data.get("status") or "pending",
        "stage": data.get("stage") or "init", "progress": 0, "llmConfig": llm,
        "topicPackage": topic, "inspirationMode": data.get("inspirationMode") or _dict(topic).get("inspirationMode")
        or ("user_premise" if data.get("premise") else "auto"),
        "testScenario": data.get("testScenario"), "hotspotContext": data.get("hotspotContext"),
        "styleGuide": data.get("styleGuide") or "", "memeLevel": meme_level if meme_level in ("none", "light", "rich") else "none",
        "memeSourceMode": "include_archive_elements" if data.get("memeSourceMode") == "include_archive_elements" else "active_only",
        "maxRepairRetries": _number(data.get("maxRepairRetries", 2), None),
        "enableDeAi": data.get("enableDeAi") is not False,
        "autoResumeOnRateLimit": data.get("autoResumeOnRateLimit", raw.get("autoResumeOnRateLimit")) is not False,
        "autoRetryState": None, "enableEvaluation": bool(data.get("enableEvaluation")), "evalConfig": data.get("evalConfig"),
        "bookPlan": None, "outlines": [], "chapterIndex": {}, "canonicalFacts": _empty_facts(), "checkpoint": None,
        "tokenUsage": {"promptTokens": 0, "completionTokens": 0, "totalTokens": 0},
        "stageStats": data.get("stageStats") or {}, "logs": [], "repairLogs": [], "auditReport": None,
        "evaluationStatus": None, "evaluationProgress": "", "evaluationReport": None, "error": None,
        "createdAt": now, "updatedAt": now,
    }
    if isinstance(data.get("degradeOnVerifyExhausted"), bool):
        job["degradeOnVerifyExhausted"] = data["degradeOnVerifyExhausted"]
    cooldown = _first_non_null(data.get("autoResumeCooldownMs"), raw.get("autoResumeCooldownMs"))
    if cooldown is not None:
        job["autoResumeCooldownMs"] = _number(cooldown, None)
    return job


def _chapter_record(number, data, now):
    content = _first_non_null(data.get("content"), data.get("text"), "")
    return {
        "chapterNumber": number, "title": data.get("title") or f"第{number}章", "content": content,
        "summary": data.get("summary") or "",
        "wordCount": data["wordCount"] if data.get("wordCount") is not None else count_chinese_words(content),
        "status": data.get("status") or "completed", "verification": data.get("verification"),
        "repairCount": _first_non_null(data.get("repairCount"), 0),
        "updatedAt": now, "createdAt": data.get("createdAt") or now,
    }


def _chapter_index_entry(chapter):
    return {key: chapter[key] for key in (
        "chapterNumber", "title", "wordCount", "status", "repairCount", "updatedAt",
    )}


@contextmanager
def claim_data_dir(directory: Path):
    """Allow a single process to schedule runs and update a data directory."""
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".novel-forge.lock").open("a+b") as handle:
        if handle.seek(0, 2) == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError(f"Novel Forge is already using {directory}") from exc
        yield


def read_legacy_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        log.warning("Could not import %s: %s", path.name, exc)
        return None


class Store:
    def __init__(self, directory: Path):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "novel-forge.sqlite3"
        self.config_path = self.directory / "config.json"
        with _config_locks_guard:
            lock_key = os.path.normcase(str(self.config_path))
            self._config_lock = _config_locks.get(lock_key) or threading.RLock()
            _config_locks[lock_key] = self._config_lock
        with self._connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)
            db.execute("BEGIN IMMEDIATE")
            with db:
                self.import_legacy(db, self.directory / "jobs")

    @contextmanager
    def _connection(self):
        with closing(sqlite3.connect(self.path, timeout=5, isolation_level=None)) as db:
            db.execute("PRAGMA foreign_keys=ON")
            yield db

    @contextmanager
    def _write(self):
        # Read the latest data only after reserving the single SQLite writer.
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            with db:
                yield db

    def _read_config(self):
        with self._config_lock:
            try:
                return _dict(json.loads(self.config_path.read_text(encoding="utf-8-sig")))
            except (OSError, ValueError):
                return {"activeProvider": "deepseek", "providers": {}}

    def get_provider_config(self, provider=None):
        return _provider_config(self._read_config(), provider)

    def get_model_catalog_configs(self):
        saved = self._read_config()
        return {"activeProvider": saved.get("activeProvider"),
                "configs": {provider: _provider_config(saved, provider) for provider in PROVIDERS}}

    def get_public_provider_config(self, provider=None):
        if provider == SIWC:
            return self._public_siwc_config()
        return _public_provider(self.get_provider_config(provider))

    def _public_siwc_config(self):
        service = getattr(self, "openai_siwc", None)
        status = service.catalog() if service else {}
        return {"provider": SIWC, "baseUrl": SIWC_RESOURCE, "model": status.get("selectedModel", ""),
                "hasKey": False, "maskedKey": "", "thinkingMode": "disabled",
                "configured": status.get("configured", False), "oauthState": status.get("state", "disabled")}

    def get_all_public_provider_configs(self):
        saved = self._read_config()
        providers = {key: _public_provider(_provider_config(saved, key)) for key in PROVIDERS if key not in ("mock", SIWC)}
        service = getattr(self, "openai_siwc", None)
        if service and service.status()["enabled"]:
            providers[SIWC] = self._public_siwc_config()
        return {"activeProvider": saved.get("activeProvider") or ("deepseek" if providers["deepseek"]["hasKey"] else "mock"),
                "map": providers}

    def save_provider_config(self, patch=None):
        patch = patch or {}
        with self._config_lock:
            store = self._read_config()
            provider = str(patch.get("provider") or store.get("activeProvider") or "deepseek").lower()
            if provider == SIWC:
                raise ProviderCatalogError("ChatGPT 连接只能通过登录管理。", 400, "SIWC_OAUTH_ONLY")
            providers = _dict(store.get("providers"))
            if provider == "mock":
                updated = {**store, "activeProvider": "mock", "providers": providers}
                _atomic_write_json(self.config_path, updated)
                return _public_provider(_provider_config(updated, "deepseek"))

            default_url, default_model, default_thinking = _provider_defaults(provider)
            saved = _dict(providers.get(provider))
            current = _provider_config(store, provider)
            expected = patch.get("expectedConnection")
            if expected is not None and any(expected.get(field) != current[field] for field in ("baseUrl", "apiKey")):
                raise ProviderCatalogError("连接配置已变更，请重新同步模型。", 409, "CONNECTION_CHANGED")
            base_url = (str(patch.get("baseUrl") or "").strip() or default_url) if "baseUrl" in patch else current["baseUrl"]
            key = patch.get("apiKey")
            key = key.strip() if isinstance(key, str) and not is_masked_api_key(key) else ""
            validate = patch.get("validateConnection")
            if validate and current["apiKey"] and not key and not same_endpoint_origin(current["baseUrl"], base_url):
                raise ProviderCatalogError("更换服务地址时，请确认该地址对应的 API Key。", 400, "KEY_REQUIRED")
            next_key = "" if patch.get("clearApiKey") is True else key or saved.get("apiKey") or ""
            effective_key = next_key or _env_api_key(provider, base_url)
            if validate and provider_requires_key(provider) and not effective_key:
                raise ProviderCatalogError("请填写此供应商的 API Key。", 400, "KEY_REQUIRED")
            updated_at = _now()
            updated_provider = {
                **saved, "baseUrl": base_url,
                "model": (str(patch.get("model") or "").strip() or default_model)
                if "model" in patch else saved.get("model") or "",
                "thinkingMode": patch["thinkingMode"] if "thinkingMode" in patch else saved.get("thinkingMode") or default_thinking,
                "apiKey": next_key, "updatedAt": updated_at,
            }
            if "manualModels" in patch:
                updated_provider["manualModels"] = normalize_model_ids(patch["manualModels"])
            if "discoveredModels" in patch:
                updated_provider["discoveredModels"] = normalize_model_ids(patch["discoveredModels"])
            elif base_url != current["baseUrl"] or effective_key != current["apiKey"]:
                updated_provider["discoveredModels"] = []
            updated = {**store, "providers": {**providers, provider: updated_provider}, "updatedAt": updated_at}
            if not patch.get("keepActiveProvider"):
                updated["activeProvider"] = provider
            _atomic_write_json(self.config_path, updated)
            return _public_provider(_provider_config(updated, provider))

    def import_legacy(self, db: sqlite3.Connection, legacy: Path):
        if db.execute("SELECT 1 FROM metadata WHERE key = 'legacy_imported'").fetchone():
            return
        for path in sorted(legacy.glob("*.json")):
            job = read_legacy_json(path)
            if not isinstance(job, dict) or job.get("id") != path.stem:
                continue
            job_id = job["id"]
            db.execute("INSERT OR IGNORE INTO jobs VALUES (?, ?)", (job_id, json.dumps(job)))
            for chapter_path in sorted((legacy / job_id / "chapters").glob("*.json")):
                chapter = read_legacy_json(chapter_path)
                if isinstance(chapter, dict) and isinstance(chapter.get("chapterNumber"), int):
                    db.execute(
                        "INSERT OR IGNORE INTO chapters VALUES (?, ?, ?)",
                        (job_id, chapter["chapterNumber"], json.dumps(chapter)),
                    )
        db.execute("INSERT INTO metadata VALUES ('legacy_imported')")

    def list_jobs(self):
        with self._connection() as db:
            rows = db.execute("SELECT payload FROM jobs ORDER BY json_extract(payload, '$.updatedAt') DESC")
            return [public_job(json.loads(row[0])) for row in rows]

    def get_job(self, job_id: str):
        record = self.get_job_record(job_id)
        return public_job(record) if record is not None else None

    @staticmethod
    def _get_job(db, job_id, *, required=False):
        row = db.execute("SELECT payload FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None and required:
            raise StorageError(f"Job not found: {job_id}", 404, "JOB_NOT_FOUND")
        return json.loads(row[0]) if row is not None else None

    @staticmethod
    def _write_job(db, job):
        db.execute("""INSERT INTO jobs (id, payload) VALUES (?, ?)
                      ON CONFLICT(id) DO UPDATE SET payload=excluded.payload""",
                   (job["id"], _json(job)))

    def get_job_record(self, job_id):
        """Internal lookup used by workers. Public callers use get_job()."""
        with self._connection() as db:
            return self._get_job(db, job_id)

    def create_job(self, data=None):
        data = data or {}
        provider = str(_dict(data.get("llmConfig")).get("provider") or data.get("provider") or "mock").lower()
        job = _job_record(data, self.get_provider_config(provider), _now())
        with self._write() as db:
            self._write_job(db, job)
        return job

    def create_imported_manuscript(self, parsed, *, title=""):
        """Save a parsed manuscript and all of its chapters as one finished work."""
        now = _now()
        chapters = [_chapter_record(row["chapterNumber"], row, now) for row in parsed["chapters"]]
        count = len(chapters)
        total_words = parsed["totalWords"]
        job = _job_record({
            "title": title or parsed["inferredTitle"],
            "genre": "未指定", "pov": "沿用原稿", "styleGuide": "保持原稿文风与人物口吻。",
            "targetWords": total_words, "wordsPerChapter": max(1, (total_words + count // 2) // count),
            "totalChapters": count, "status": "completed", "stage": "completed",
            "llmConfig": {"useSavedConnection": True},
        }, self.get_provider_config(), now)
        job.update({
            "progress": 100, "completedChapters": count, "totalWords": total_words,
            "importedChapterCount": count,
            "chapterIndex": {str(row["chapterNumber"]): _chapter_index_entry(row) for row in chapters},
        })
        with self._write() as db:
            self._write_job(db, job)
            for row in chapters:
                self._write_chapter(db, job["id"], row)
        return job

    def update_job(self, job_id, patch):
        with self._write() as db:
            job = self._get_job(db, job_id, required=True)
            changes = patch(deepcopy(job)) if callable(patch) else patch
            updated = {**job, **(changes or {}), "id": job["id"], "updatedAt": _now()}
            if "createdAt" in job:
                updated["createdAt"] = job["createdAt"]
            else:
                updated.pop("createdAt", None)
            self._write_job(db, updated)
        return updated

    def job_detail(self, job_id: str):
        with self._connection() as db:
            db.execute("BEGIN")
            job = self._get_job(db, job_id)
            return {"job": public_job(job), "chapters": self._get_chapters(db, job_id)} if job is not None else None

    @staticmethod
    def _get_chapters(db, job_id):
        rows = db.execute("SELECT payload FROM chapters WHERE job_id = ? ORDER BY number", (job_id,))
        return [json.loads(row[0]) for row in rows]

    def get_chapters(self, job_id, *, start_chapter=None, end_chapter=None):
        with self._connection() as db:
            if start_chapter is None and end_chapter is None:
                return self._get_chapters(db, job_id)
            rows = db.execute(
                "SELECT payload FROM chapters WHERE job_id=? AND number BETWEEN ? AND ? ORDER BY number",
                (job_id, start_chapter if start_chapter is not None else 1,
                 end_chapter if end_chapter is not None else 2**63 - 1),
            )
            return [json.loads(row[0]) for row in rows]

    @staticmethod
    def _get_chapter(db, job_id, number):
        row = db.execute("SELECT payload FROM chapters WHERE job_id = ? AND number = ?", (job_id, number)).fetchone()
        return json.loads(row[0]) if row else None

    def get_chapter(self, job_id: str, number: int):
        with self._connection() as db:
            return self._get_chapter(db, job_id, number)

    @staticmethod
    def _write_chapter(db, job_id, chapter):
        db.execute("""INSERT INTO chapters (job_id, number, payload) VALUES (?, ?, ?)
                      ON CONFLICT(job_id, number) DO UPDATE SET payload=excluded.payload""",
                   (job_id, chapter["chapterNumber"], _json(chapter)))

    def _save_chapter(self, job_id, number, data, facts_delta, long_memory, *, meme_title=None, story_ledger=False):
        number = _number(number, None)
        if type(number) is not int or number < 1:
            raise ValueError("Chapter number must be a positive integer")
        with self._write() as db:
            job = self._get_job(db, job_id, required=True)
            now = _now()
            chapter = _chapter_record(number, data, now)
            index = dict(_dict(job.get("chapterIndex")))
            index[str(number)] = _chapter_index_entry(chapter)
            updated = {**job, "chapterIndex": index, "updatedAt": now,
                       "completedChapters": sum(_is_committed(item) for item in index.values()),
                       "totalWords": sum(_number(item.get("wordCount")) for item in index.values())}
            if meme_title and _is_committed(chapter):
                updated["usedMemeTitles"] = _unique_strings([*_list(job.get("usedMemeTitles")), meme_title])
            if facts_delta is not None and chapter["status"] != "needs_review":
                updated["canonicalFacts"] = _merge_chapter_facts(job.get("canonicalFacts"), number, facts_delta,
                                                               long_memory, chapter["content"], now, story_ledger=story_ledger,
                                                               book_plan=job.get("bookPlan"))
            current = _dict(job.get("canonicalFacts"))
            archives = [*_list(current.get("volumeSummaries")), *_list(current.get("arcSummaries"))]
            if any(_archive_touches(archive, [number]) for archive in archives):
                previous = self._get_chapter(db, job_id, number) or {}
                if (any(previous.get(key) != chapter[key] for key in ("title", "content", "summary"))
                        or (previous.get("status") == "needs_review") != (chapter["status"] == "needs_review")
                        or (_is_committed(previous) and not _is_committed(chapter))):
                    updated["canonicalFacts"] = _invalidate_archives(_dict(updated.get("canonicalFacts")), [number])
            self._write_chapter(db, job_id, chapter)
            self._write_job(db, updated)
        return chapter, _canonical_facts(updated.get("canonicalFacts"))

    def save_chapter(self, job_id, number, data, *, facts_delta=None, long_memory=False, story_ledger=False):
        chapter, _ = self._save_chapter(job_id, number, data, facts_delta, long_memory, story_ledger=story_ledger)
        return chapter

    def save_verified_chapter(self, job_id, number, data, facts_delta=None, *, long_memory=False, meme_title=None,
                              story_ledger=False):
        chapter, facts = self._save_chapter(job_id, number, data, facts_delta, long_memory, meme_title=meme_title,
                                           story_ledger=story_ledger)
        return {"chapter": chapter, "canonicalFacts": facts}

    def get_canonical_facts(self, job_id):
        with self._connection() as db:
            return _canonical_facts(self._get_job(db, job_id, required=True).get("canonicalFacts"))

    def commit_chapter_facts(self, job_id, number, facts_delta=None, *, long_memory=False, chapter_text=None,
                             story_ledger=False):
        number = _number(number, None)
        if type(number) is not int or number < 1:
            raise ValueError("Chapter number must be a positive integer")
        with self._write() as db:
            job = self._get_job(db, job_id, required=True)
            now = _now()
            facts = _merge_chapter_facts(job.get("canonicalFacts"), number, facts_delta or {}, long_memory, chapter_text, now,
                                         story_ledger=story_ledger, book_plan=job.get("bookPlan"))
            self._write_job(db, {**job, "canonicalFacts": facts, "updatedAt": now})
        return facts

    def delete_uncommitted_facts(self, job_id, from_chapter):
        with self._write() as db:
            job = self._get_job(db, job_id, required=True)
            facts = _rollback_facts(job.get("canonicalFacts"), _number(from_chapter), book_plan=job.get("bookPlan"))
            self._write_job(db, {**job, "canonicalFacts": facts, "updatedAt": _now()})
        return facts

    def save_volume_summary(self, job_id, end_chapter, source_hash, summary):
        """Commit an archive only while the verified inputs still match its hash."""
        if type(end_chapter) is not int or end_chapter < 10 or end_chapter % 10:
            return False
        start = end_chapter - 9
        normalized = normalize_volume_summary(summary, start, end_chapter)
        if normalized is None:
            return False
        with self._write() as db:
            job = self._get_job(db, job_id, required=True)
            rows = db.execute("SELECT payload FROM chapters WHERE job_id=? AND number BETWEEN ? AND ? ORDER BY number",
                              (job_id, start, end_chapter))
            chapters = [json.loads(row[0]) for row in rows]
            if (len(chapters) != 10 or any(not _is_committed(chapter) or chapter.get("chapterNumber") != number
                                           for number, chapter in enumerate(chapters, start))
                    or volume_source_hash(chapters) != source_hash):
                return False
            facts = _canonical_facts(job.get("canonicalFacts"))
            archives = _list(facts["volumeSummaries"])
            if any(volume["endChapter"] == end_chapter and volume["sourceHash"] == source_hash for volume in archives):
                return True
            review = [chapter["chapterNumber"] for chapter in chapters if chapter["status"] == "needs_review"]
            record = {"volume": end_chapter // 10, "startChapter": start, "endChapter": end_chapter,
                      "sourceHash": source_hash, **normalized,
                      **({"needsReviewChapters": review} if review else {})}
            facts["volumeSummaries"] = sorted([volume for volume in archives if volume["endChapter"] != end_chapter] + [record],
                                               key=lambda volume: volume["endChapter"])
            self._write_job(db, {**job, "canonicalFacts": facts, "updatedAt": _now()})
            return True

    def save_checkpoint(self, job_id, checkpoint, *, outlines=None):
        with self._write() as db:
            job = self._get_job(db, job_id, required=True)
            saved = {**checkpoint, "savedAt": _now()}
            if outlines is not None:
                job["outlines"] = outlines
            self._write_job(db, {**job, "checkpoint": saved, "updatedAt": saved["savedAt"]})
        return saved

    def save_arc_plan(self, job_id, arc, outlines, *, total, expected_context, checkpoint):
        """Save one full arc and its cursor as a single job update."""
        start, end = arc["startChapter"], arc["endChapter"]
        if not 1 <= start <= end <= total or [row["chapterNumber"] for row in outlines] != list(range(start, end + 1)):
            return False
        with self._write() as db:
            job = self._get_job(db, job_id, required=True)
            if job.get("totalChapters") != total:
                return False
            preceding = [row for row in self._get_chapters(db, job_id)
                         if row["chapterNumber"] < start and _is_committed(row)]
            facts = _dict(job.get("canonicalFacts"))
            if arc_context_version(facts, preceding, start) != expected_context:
                return False
            plans = saved_arc_plans(job)
            if any(record["arcIndex"] == arc["arcIndex"] or
                   record["startChapter"] <= end and start <= record["endChapter"] for record in plans):
                return False
            previous = {}
            for value in _list(job.get("outlines")):
                row = _dict(value)
                number = _number(row.get("chapterNumber"))
                if type(number) is int and 1 <= number <= total:
                    previous.setdefault(number, row)
            for number in range(start, end + 1):
                if number in previous and not legacy_outline_placeholder(previous[number], number):
                    return False
                chapter = self._get_chapter(db, job_id, number)
                if chapter and _is_committed(chapter):
                    return False
            previous.update((row["chapterNumber"], row) for row in outlines)
            now = _now()
            self._write_job(db, {**job, "arcPlans": [*plans, arc], "outlines": [previous[n] for n in sorted(previous)],
                                 "checkpoint": {**checkpoint, "savedAt": now}, "updatedAt": now})
            return True

    def save_arc_summary(self, job_id, arc, source_hash, summary, *, story_ledger=False):
        start, end = arc["startChapter"], arc["endChapter"]
        index = arc["arcIndex"]
        with self._write() as db:
            job = self._get_job(db, job_id, required=True)
            if not any((row["arcIndex"], row["startChapter"], row["endChapter"]) == (index, start, end)
                       for row in saved_arc_plans(job)):
                return False
            rows = db.execute("SELECT payload FROM chapters WHERE job_id=? AND number BETWEEN ? AND ? ORDER BY number",
                              (job_id, start, end))
            chapters = [json.loads(row[0]) for row in rows]
            if ([row["chapterNumber"] for row in chapters] != list(range(start, end + 1))
                    or not all(_is_committed(row) for row in chapters) or volume_source_hash(chapters) != source_hash):
                return False
            facts = _canonical_facts(job.get("canonicalFacts"))
            archives = _list(facts["arcSummaries"])
            if any(_dict(row).get("arc") == index and row.get("sourceHash") == source_hash for row in archives):
                return True
            normalized = normalize_arc_summary(summary, chapters, story_ledger=story_ledger)
            if normalized is None:
                return False
            review = [row["chapterNumber"] for row in chapters if row.get("status") == "needs_review"]
            record = {"arc": index, "startChapter": start, "endChapter": end, "sourceHash": source_hash, **normalized,
                      **({"needsReviewChapters": review} if review else {})}
            facts["arcSummaries"] = sorted([row for row in archives if isinstance(row, dict) and row.get("arc") != index] + [record],
                                           key=lambda row: _number(row.get("endChapter")))
            self._write_job(db, {**job, "canonicalFacts": facts, "updatedAt": _now()})
            return True

    def append_job_log(self, job_id, entry):
        with self._write() as db:
            job = self._get_job(db, job_id)
            if job is None:
                return None
            item = {"ts": _now(), "level": entry.get("level") or "info", "stage": entry.get("stage") or job.get("stage"),
                    "message": entry.get("message") or "", "meta": entry.get("meta", None)}
            logs = [*_list(job.get("logs")), item][-500:]
            self._write_job(db, {**job, "logs": logs, "updatedAt": item["ts"]})
            return item

    def append_repair_log(self, job_id, entry):
        with self._write() as db:
            job = self._get_job(db, job_id)
            if job is None:
                return None
            item = {"ts": _now(), "attempt": _first_non_null(entry.get("attempt"), 1),
                    "issues": entry.get("issues") or [], "action": entry.get("action") or "auto_repair",
                    "resolved": bool(entry.get("resolved"))}
            if "chapterNumber" in entry:
                item["chapterNumber"] = entry["chapterNumber"]
            self._write_job(db, {**job, "repairLogs": [*_list(job.get("repairLogs")), item], "updatedAt": item["ts"]})
            return item

    def apply_manuscript_revision(self, job_id, revision, is_active=False):
        revision_id = revision.get("revisionId")
        if not isinstance(revision_id, str) or not revision_id or not isinstance(revision.get("changes"), list):
            raise StorageError("候选稿含有重复、缺失或无效章节。", 400, "REVISION_INVALID_CHAPTER")
        with self._write() as db:
            job = self._get_job(db, job_id)
            if job is None:
                raise StorageError("找不到作品。", 404, "JOB_NOT_FOUND")
            if job.get("appliedRevisionId") == revision_id:
                return
            chapters = self._get_chapters(db, job_id)
            active = is_active(job_id) if callable(is_active) else is_active
            readiness = revision_manuscript_state(job, chapters, active)
            if readiness == "busy":
                raise StorageError("请在写作任务结束且评估空闲时应用修订。", 409, "REVISION_JOB_BUSY")
            if readiness == "incomplete":
                raise StorageError("已保存章节未达到全书目标或章节编号不连续。", 409, "REVISION_CHAPTERS")
            if manuscript_fingerprint(chapters) != revision.get("sourceVersion"):
                raise StorageError("正文在修订期间发生变化，候选稿已保留，请基于当前正文重新评估。", 409, "REVISION_SOURCE_CHANGED")

            prior = {chapter["chapterNumber"]: chapter for chapter in chapters}
            index = dict(_dict(job.get("chapterIndex")))
            now = _now()
            changed, seen = [], set()
            for edit in revision["changes"]:
                number = _dict(edit).get("chapterNumber")
                title, content = _dict(edit).get("title"), _dict(edit).get("content")
                if (type(number) is not int or number not in prior or number in seen or not isinstance(title, str)
                        or not title.strip() or not isinstance(content, str) or not content.strip()):
                    raise StorageError("候选稿含有重复、缺失或无效章节。", 400, "REVISION_INVALID_CHAPTER")
                seen.add(number)
                previous = prior[number]
                pending = previous.get("status") == "needs_review"
                chapter = {
                    **previous, "title": title, "content": content, "wordCount": count_chinese_words(content),
                    "status": "needs_review" if pending else "completed", "updatedAt": now,
                }
                if not pending or "verification" in previous:
                    chapter["verification"] = previous.get("verification") if pending else None
                chapter.pop("aiIssues", None)
                index[str(number)] = {**_dict(index.get(str(number))), **{key: chapter[key] for key in (
                    "chapterNumber", "title", "wordCount", "status", "updatedAt",
                )}, "repairCount": _first_non_null(chapter.get("repairCount"), 0)}
                if previous.get("title") != title or previous.get("content") != content:
                    changed.append(number)
                self._write_chapter(db, job_id, chapter)
            updated = {
                **job, "chapterIndex": index, "updatedAt": now, "appliedRevisionId": revision_id,
                "totalWords": sum(_number(item.get("wordCount")) for item in index.values()),
                "evaluationStatus": None, "evaluationReport": None, "evaluationCheckpoint": None,
                "evaluationProgress": "已应用修订稿，可以重新评估。",
            }
            if job.get("canonicalFacts") is not None and changed:
                updated["canonicalFacts"] = _invalidate_archives(job["canonicalFacts"], changed)
            self._write_job(db, updated)


def public_job(job: dict):
    config = job.get("llmConfig") or {}
    key = config.get("apiKey")
    if isinstance(key, str) and key:
        job = {**job, "llmConfig": {**config, "apiKey": mask_api_key(key)}}
    checkpoint = job.get("evaluationCheckpoint")
    if isinstance(checkpoint, dict):
        total = checkpoint.get("totalBatches", 0)
        completed = min(total, sum(
            bool(batch.get("batchRange")) and not re.match(r"^JSON\s*解析失败", str(batch.get("timelineSummary", "")))
            for batch in checkpoint.get("batchResults") or []
            if isinstance(batch, dict)
        ))
        job["evaluationCheckpoint"] = {
            "mode": checkpoint.get("mode"),
            "stage": "flash" if completed < total else checkpoint.get("stage"),
            "updatedAt": checkpoint.get("updatedAt"),
            "totalBatches": total,
            "selection": checkpoint.get("selection"),
            "completedBatches": completed,
        }
    return job




def _empty_facts():
    return {
        "characters": {}, "injuries": [], "timeline": [], "items": {}, "secrets": [],
        "locations": {}, "foreshadowing": [], "relations": [], "factHistory": [],
        **empty_long_memory(), "volumeSummaries": [], "arcSummaries": [],
    }


def _canonical_facts(saved):
    return {**_empty_facts(), **_dict(saved)}


def _unique_strings(values):
    return list(dict.fromkeys(value for value in values if isinstance(value, str) and value.strip()))


def _named_entries(value, *keys):
    if isinstance(value, list):
        return [(next((item.get(key) for key in keys if item.get(key)), None), item)
                for item in value if isinstance(item, dict)]
    return _dict(value).items()


def _matching_index(entries, match):
    return next((i for i, entry in enumerate(entries) if match(entry)), None)


_INJURY_PART = re.compile(r"左前臂|右前臂|左上臂|右上臂|左臂|右臂|左肩|右肩|左手|右手|左腿|右腿|双腿|胸口")


def _injury_part(entry):
    for field in ("bodyPart", "description"):
        matches = {re.sub(r"[前上]臂$", "臂", part) for part in _INJURY_PART.findall(str(entry.get(field) or ""))}
        if matches:
            return next(iter(matches)) if len(matches) == 1 else None
    return None


def _apply_injuries(injuries, changes, number):
    for ordinal, change in enumerate(_list(changes), 1):
        change = _dict(change)
        character = change.get("character")
        if not isinstance(character, str) or not character.strip():
            continue
        identifier = change.get("id") if isinstance(change.get("id"), str) else None
        description = change.get("description")
        part = _injury_part(change)
        active = change.get("active") is not False
        candidates = [(i, row) for i, row in enumerate(injuries)
                      if row.get("character") == character and _number(row.get("sourceChapter")) <= number]
        index = next((i for i, row in candidates if identifier and row.get("id") == identifier), None)
        candidates = [(i, row) for i, row in candidates if row.get("active") is not False]
        if index is None and isinstance(description, str) and description.strip():
            index = next((i for i, row in candidates if row.get("description") == description), None)
        if index is None and part:
            matches = [i for i, row in candidates if _injury_part(row) == part]
            if len(matches) == 1:
                index = matches[0]
            elif matches and not identifier:
                continue
        if index is None and (not active or not isinstance(description, str) or not description.strip()):
            continue

        previous = injuries[index] if index is not None else {}
        if "active" not in change and previous:
            active = previous.get("active") is not False
        if previous.get("id"):
            identifier = previous["id"]
        elif not identifier or any(row.get("id") == identifier for row in injuries):
            identifier = f"inj_{number}_{ordinal}"
            while any(row.get("id") == identifier for row in injuries):
                identifier += "_new"
        record = {
            **previous, "id": identifier,
            "character": character, "severity": change.get("severity") or previous.get("severity") or "medium",
            "active": active, "sourceChapter": _first_non_null(previous.get("sourceChapter"), number),
            "updatedAtChapter": number, "healedChapter": None if active else (
                _first_non_null(previous.get("healedChapter"), number) if "active" not in change else number
            ),
        }
        if active and isinstance(description, str) and description.strip():
            record["description"] = description
        if part:
            record["bodyPart"] = part
        if index is None:
            injuries.append(record)
        else:
            injuries[index] = record


def _replay_injuries(base, history):
    injuries = [dict(entry) for entry in _list(base) if isinstance(entry, dict)]
    for row in sorted(_list(history), key=lambda entry: _number(_dict(entry).get("chapter"))):
        if _dict(row).get("injuryMergeVersion") == 1:
            _apply_injuries(injuries, _dict(row.get("delta")).get("injuries"), _number(row.get("chapter")))
    return injuries


def _merge_chapter_facts(saved, number, delta, long_memory, chapter_text, timestamp, *, story_ledger=False, book_plan=None):
    facts = _canonical_facts(saved)
    for name, info in _named_entries(delta.get("characters"), "name"):
        if not name:
            continue
        previous = _dict(facts["characters"].get(name))
        info = _dict(info)
        facts["characters"][name] = {
            **previous, **info, "name": name,
            "traits": _unique_strings([*_list(previous.get("traits")), *_list(info.get("traits"))]),
            "knownSecrets": _unique_strings([*_list(previous.get("knownSecrets")), *_list(info.get("knownSecrets"))]),
            "sourceChapter": _first_non_null(previous.get("sourceChapter"), number), "updatedAtChapter": number,
        }

    has_injury_changes = bool(_list(delta.get("injuries")))
    if has_injury_changes and "injuryReplayBase" not in facts:
        # Old books may have verified injuries without matching history entries.
        facts["injuryReplayBase"] = [dict(entry) for entry in _list(facts["injuries"]) if isinstance(entry, dict)]

    if isinstance(delta.get("timeline"), list):
        facts["timeline"] = [item for item in facts["timeline"] if _number(item.get("sourceChapter")) != number]
        for event in delta["timeline"]:
            if not event:
                continue
            detail = _dict(event)
            facts["timeline"].append({
                "dayOrTime": detail.get("dayOrTime") or detail.get("time") or f"第{number}章时间点",
                "event": detail.get("event") or detail.get("description") or
                ("[object Object]" if isinstance(event, dict) else str(event)),
                "characters": detail.get("characters") or [],
                "sourceChapter": _first_non_null(detail.get("sourceChapter"), number),
            })

    for name, info in _named_entries(delta.get("items"), "name"):
        if name:
            previous = _dict(facts["items"].get(name))
            facts["items"][name] = {**previous, **_dict(info), "name": name,
                                    "sourceChapter": _first_non_null(previous.get("sourceChapter"), number),
                                    "updatedAtChapter": number}

    for secret in _list(delta.get("secrets")):
        if not _dict(secret).get("secret"):
            continue
        index = _matching_index(facts["secrets"], lambda item:
                                item.get("secret") == secret["secret"] or
                                (secret.get("id") and item.get("id") == secret["id"]))
        fields = ("holders", "revealedTo", "excludedCharacters")
        if index is not None:
            previous = facts["secrets"][index]
            facts["secrets"][index] = {
                **previous, **secret, **{key: _unique_strings([*_list(previous.get(key)), *_list(secret.get(key))]) for key in fields},
                "updatedAtChapter": number,
            }
        else:
            facts["secrets"].append({
                "id": secret.get("id") or f'sec_{number}_{len(facts["secrets"]) + 1}', "secret": secret["secret"],
                **{key: secret.get(key) or [] for key in fields},
                "sourceChapter": _first_non_null(secret.get("sourceChapter"), number),
            })

    for entity, info in _named_entries(delta.get("locations"), "entity", "character", "name"):
        if entity:
            facts["locations"][entity] = {
                **({"location": info} if isinstance(info, str) else _dict(info)), "entity": entity, "sourceChapter": number,
            }

    for item in _list(delta.get("foreshadowing")):
        if not _dict(item).get("clue"):
            continue
        index = _matching_index(facts["foreshadowing"], lambda previous:
                                previous.get("clue") == item["clue"] or
                                (item.get("id") and previous.get("id") == item["id"]))
        if index is not None:
            previous = facts["foreshadowing"][index]
            payoff = _first_non_null(item.get("payoffChapter"), number) if item.get("status") == "paid_off" else previous.get("payoffChapter")
            facts["foreshadowing"][index] = {
                **previous, **item, "relatedChapters": list(dict.fromkeys([*_list(previous.get("relatedChapters")), number])),
                "payoffChapter": payoff,
            }
        else:
            facts["foreshadowing"].append({
                "id": item.get("id") or f'fs_{number}_{len(facts["foreshadowing"]) + 1}', "clue": item["clue"],
                "plantedChapter": _first_non_null(item.get("plantedChapter"), number), "status": item.get("status") or "planted",
                "payoffChapter": item.get("payoffChapter") or (number if long_memory and item.get("status") == "paid_off" else None),
                "relatedChapters": [number], "details": item.get("details") or "",
                "sourceChapter": _first_non_null(item.get("sourceChapter"), number),
            })

    for relation in _list(delta.get("relations")):
        if not (_dict(relation).get("from") and relation.get("to")):
            continue
        index = _matching_index(facts["relations"], lambda previous:
                                previous.get("from") == relation["from"] and previous.get("to") == relation["to"])
        record = {**relation, "sourceChapter": _first_non_null(relation.get("sourceChapter"), number)}
        if index is not None:
            facts["relations"][index] = record
        else:
            facts["relations"].append(record)

    history = [item for item in _list(facts["factHistory"]) if _number(_dict(item).get("chapter")) != number]
    memory = normalize_memory_delta(
        delta.get("memory"), number, chapter_text, story_ledger=story_ledger, long_memory=long_memory,
        book_plan=book_plan, history=history,
    ) if long_memory or story_ledger else None
    recorded = {key: value for key, value in delta.items() if key != "memory"}
    if memory:
        recorded["memory"] = memory
    history.append({"chapter": number, "timestamp": timestamp, "delta": recorded,
                    **({"injuryMergeVersion": 1} if has_injury_changes else {})})
    facts["factHistory"] = history
    if "injuryReplayBase" in facts:
        facts["injuries"] = _replay_injuries(facts["injuryReplayBase"], history)
    for key in ("storyClock", "ledgerBalances"):
        facts.pop(key, None)
    facts.update(replay_long_memory(history, book_plan=book_plan))
    return facts


def _rollback_facts(saved, from_chapter, *, book_plan=None):
    previous, result = _canonical_facts(saved), _empty_facts()
    for kind in ("characters", "items", "locations"):
        result[kind] = {key: value for key, value in _dict(previous[kind]).items()
                        if _number(value.get("sourceChapter")) < from_chapter}
    for kind in ("injuries", "timeline", "secrets", "relations"):
        result[kind] = [value for value in _list(previous[kind]) if _number(value.get("sourceChapter")) < from_chapter]
    for entry in _list(previous["foreshadowing"]):
        if _number(_first_non_null(entry.get("plantedChapter"), entry.get("sourceChapter"))) >= from_chapter:
            continue
        record = {**entry, "relatedChapters": [number for number in _list(entry.get("relatedChapters"))
                                             if _number(number) < from_chapter]}
        if entry.get("payoffChapter") and _number(entry["payoffChapter"]) >= from_chapter:
            record.update(payoffChapter=None, status="planted")
        result["foreshadowing"].append(record)
    history = [item for item in _list(previous["factHistory"]) if _number(item.get("chapter")) < from_chapter]
    result["factHistory"] = history
    if "injuryReplayBase" in previous:
        base = [entry for entry in _list(previous["injuryReplayBase"])
                if _number(_dict(entry).get("sourceChapter")) < from_chapter]
        result["injuryReplayBase"] = base
        result["injuries"] = _replay_injuries(base, history)
    result.update(replay_long_memory(history, book_plan=book_plan))
    result["volumeSummaries"] = [volume for volume in _list(previous["volumeSummaries"])
                                 if _number(volume.get("endChapter"), math.inf) < from_chapter]
    result["arcSummaries"] = [arc for arc in _list(previous["arcSummaries"])
                              if _number(_dict(arc).get("endChapter"), math.inf) < from_chapter]
    return result
