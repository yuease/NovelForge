"""Cached public bestseller feeds and topic generation."""

import asyncio
import json
import logging
import math
from copy import deepcopy
from pathlib import Path

import httpx

from .llm import DEFAULT_MAX_TOKENS, LLMError, _resolve_credentials, call_llm, output_was_truncated, protected_output_budget
from .memes import _JsonCache, _http_client, _iso, _now_ms
from .providers import provider_requires_key
from .verifier import _llm_options, parse_json_from_llm


HOTSPOT_PLATFORMS = [
    "番茄小说",
    "红果短剧",
    "抖音短剧"
]

DEFAULT_FEED_URL = "https://top.baidu.com/api/board?platform=wise&tab=novel"

FALLBACK_FEED_URL = "https://top.baidu.com/api/board?platform=wise&tab=realtime"

HOOK_FORMULAS = [
    {
        "id": "hook_rebirth_slap",
        "name": "重生退婚/当场打脸公式",
        "platform": "番茄小说",
        "formula": "前世惨死/被背叛 -> 睁眼回到命运转折点 -> 当众撕破伪装反杀 -> 亮出底牌震慑全场",
        "example": "婚礼现场醒来，面对未婚妻的当众退婚，主角反手亮出百亿黑卡与家族覆灭证据。"
    },
    {
        "id": "hook_countdown_system",
        "name": "倒计时+高收益系统公式",
        "platform": "红果短剧",
        "formula": "绝境倒计时（10分钟内破产/死亡） -> 觉醒神级反差系统 -> 越不被看好越暴击返现 -> 强卡点断章",
        "example": "被赶出豪门只剩100元，激活「消费十倍返现」系统，第一分钟买下对方最想拍的古董。"
    },
    {
        "id": "hook_identity_reveal",
        "name": "隐瞒身份掉马反转公式",
        "platform": "抖音短剧",
        "formula": "主角隐藏顶级大佬身份低调生活 -> 反派狗眼看人低极限施压 -> 关键人物突然下跪/敬礼 -> 身份揭晓全场窒息",
        "example": "送外卖的青年被保安羞辱，下一秒全城首富带队冲出大门鞠躬喊先生。"
    },
    {
        "id": "hook_rule_horror",
        "name": "规则怪谈+唯一免疫公式",
        "platform": "番茄小说",
        "formula": "全员被拉入诡异规则副本 -> 别人只能战战兢兢遵守 -> 主角看见隐藏红字提示/拥有反向修改权 -> 反向收割诡异",
        "example": "规则说“午夜不能照镜子”，主角却看到镜子背面写着“照镜子可继承前任房主遗产”。"
    }
]

PACING_TEMPLATES = [
    {
        "id": "pace_tomato_fast",
        "name": "番茄黄金三章快节奏",
        "description": "第1章开篇即冲突（300字内入戏），第2章获得金手指/埋下核心伏笔，第3章完成首轮小高潮打脸并抛出更大悬念。",
        "beats": [
            "0-300字：危机/屈辱开局",
            "300-1000字：转折/金手指觉醒",
            "1000-2000字：首次反击与钩子"
        ]
    },
    {
        "id": "pace_hongguo_drama",
        "name": "红果短剧强卡点节奏",
        "description": "每600~800字一次情绪反转，对话占比40%以上，每章结尾必须卡在“真相即将揭晓”或“反派即将遭殃”的瞬间。",
        "beats": [
            "开场3秒钩子",
            "中段极限拉扯",
            "尾声高能卡点"
        ]
    },
    {
        "id": "pace_suspense_spiral",
        "name": "长线伏笔螺旋递进",
        "description": "主线爽点与暗线伏笔双线并行，每3~5章回收一个中小伏笔，同时抛出更高维度的真相碎片。",
        "beats": [
            "明线：阶段性破局",
            "暗线：信物/秘密线索递增",
            "大节点：全线收网"
        ]
    }
]

HOTSPOT_TROPES = [
    {
        "id": "trope_rebirth_tycoon",
        "title": "重生回到背叛夜，我反手买断仇家命脉",
        "platform": "番茄小说",
        "genre": "都市重生爽文",
        "heatScore": 98,
        "recommendedPov": "第三人称限知",
        "tags": [
            "重生",
            "商战复仇",
            "黄金三章",
            "强打脸"
        ],
        "hookFormula": "前世家破人亡 -> 重生回股权转让前夜 -> 截胡关键技术与人脉 -> 布局反杀",
        "pacingTemplate": "pace_tomato_fast",
        "samplePremise": "林渊前世被未婚妻与合伙人联手做局倾家荡产，睁眼重回十年前签署股权转让协议的当晚。他当场撕毁协议，凭借未来十年的金融记忆与老宅暗格中的神秘信物，反手做空仇家集团，开启逆袭之路。",
        "goldenThreeChapters": [
            "第1章：重生归来，鸿门宴上当众撕毁卖身契，反手录音留证逼退恶客。",
            "第2章：连夜赶回老宅密室取回先祖留下的核心账本与青铜信物，锁定第一桶金。",
            "第3章：次日开盘精准狙击仇家股票，对方资金链当场断裂，幕后黑手浮出水面。"
        ]
    },
]


_DEFAULT_SOURCE = "百度热搜小说榜"
_REFRESH_INTERVAL_MS = 30 * 60 * 1000


def _empty_feed():
    return {
        "feedMeta": {
            "sourceName": _DEFAULT_SOURCE, "sourceUrl": DEFAULT_FEED_URL, "boardDate": _iso(_now_ms(None))[:10],
            "lastSuccessAt": None, "lastAttemptAt": None, "cachedAt": None, "usingCache": False,
            "lastError": None, "feedVersion": 0, "itemCount": 0, "autoRefreshIntervalMs": _REFRESH_INTERVAL_MS,
        },
        "liveItems": [],
    }


def _text(value):
    return value.strip() if isinstance(value, str) else ""


def _array(value):
    return value if isinstance(value, list) else []


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _title(item):
    for key in ("title", "word", "name"):
        if item.get(key) is not None:
            value = _text(item[key])
            return value if value != "[object Object]" else ""
    return ""


def _read_feed(cached):
    cached = _mapping(cached)
    rows = cached.get("liveItems")
    if not isinstance(rows, list) or any(
        not isinstance(item, dict) or not _title(item)
        or (item.get("title") == "热点选题1" and "[object Object]" in str(item.get("summary", "")))
        for item in rows
    ):
        return None
    return {"feedMeta": {**_empty_feed()["feedMeta"], **_mapping(cached.get("feedMeta")), "usingCache": bool(rows)},
            "liveItems": rows}


def _flatten(nodes, depth=0):
    if depth > 5:
        return
    for item in _array(nodes):
        if not isinstance(item, dict):
            continue
        if _title(item):
            yield item
        else:
            nested = next((item[field] for field in ("content", "list", "items") if isinstance(item.get(field), list)), [])
            yield from _flatten(nested, depth + 1)


def extract_baidu_board_items(payload):
    data = _mapping(payload)
    cards = _mapping(data.get("data")).get("cards", data.get("cards"))
    return [item for card in _array(cards)
            for item in _flatten(_mapping(card).get("content", _mapping(card).get("topContent")))]


def _feed_items(payload):
    if isinstance(payload, list):
        return payload
    payload = _mapping(payload)
    for container in (payload, _mapping(payload.get("data"))):
        if isinstance(container.get("items"), list):
            return container["items"]
    return extract_baidu_board_items(payload)


def _number(value, default=0):
    try:
        number = float(value)
        if math.isfinite(number) and number:
            return int(number) if number.is_integer() else number
    except (TypeError, ValueError, OverflowError):
        pass
    return default


def _normalize_items(raw, source_name, source_url, board_date, attempt_at):
    normalized = []
    for item in _flatten(raw):
        index = len(normalized)
        title = _title(item)
        rank = _number(item.get("rank", item.get("index")), index + 1)
        descriptions = [_text(text) for text in _array(item.get("desc")) if _text(text)]
        author = _text(item.get("author"))
        display = _array(item.get("show"))
        if not author and display:
            author = _text(display[0])
        if not author:
            author = next((line[3:].strip() for line in descriptions if line.startswith(("作者：", "作者:"))), "")
        genre = _text(item.get("genre") or item.get("type") or item.get("category"))
        if not genre:
            genre = next((line.split("|")[0].strip() for line in descriptions if not line.startswith(("作者：", "作者:"))
                          and ("|" in line or len(line) <= 16)), "")
        genre = genre or "热门网文"
        raw_score = item.get("hotScore")
        score = _number(_mapping(raw_score).get("value", raw_score if raw_score is not None else item.get("hot")))
        tags = [tag for tag in _array(item.get("tags")) if _text(tag)] or [genre, "榜单热议"]
        summary = (_text(item.get("summary")) or " · ".join(descriptions) or _text(item.get("desc"))
                   or _text(item.get("content")) or f"{title}（{genre}）")
        normalized.append({
            "id": str(item.get("id") or f"live_{board_date}_{rank}_{index}"), "title": title, "author": author or "未知作者",
            "genre": genre, "rank": rank, "hotScore": score if score > 0 else max(60, 100 - index * 2),
            "sourceName": source_name, "sourceUrl": item.get("url") or item.get("sourceUrl") or source_url,
            "boardDate": board_date, "fetchedAt": attempt_at, "tags": tags, "summary": summary,
        })
        if len(normalized) == 30:
            break
    if not normalized:
        raise ValueError("解析榜单失败：未提取到有效标题条目")
    return normalized


class HotspotsFeed:
    def __init__(self, data_dir, *, client=None):
        self._cache = _JsonCache(Path(data_dir) / "hotspots_cache.json", _empty_feed, _read_feed)
        self.client = client

    async def refresh_hotspots_feed(self, *, source_url=DEFAULT_FEED_URL, custom_items=None, board_date=None,
                                    source_name=_DEFAULT_SOURCE, timeout_ms=10_000, now=None, client=None):
        attempt = _iso(_now_ms(now))
        date = board_date or attempt[:10]
        async with self._cache.lock:
            old = await self._cache.load()
            try:
                if custom_items is not None:
                    raw = custom_items
                else:
                    async with asyncio.timeout(timeout_ms / 1000):
                        async with _http_client(client if client is not None else self.client) as active:
                            response = await active.get(source_url, headers={
                                "User-Agent": "Mozilla/5.0 (compatible; NovelForge/1.0)",
                                "Accept": "application/json, text/plain, */*",
                            })
                            response.raise_for_status()
                            raw = _feed_items(response.json())
                            if not raw and source_url == DEFAULT_FEED_URL:
                                backup = await active.get(FALLBACK_FEED_URL)
                                if backup.is_success:
                                    raw = extract_baidu_board_items(backup.json())
                items = _normalize_items(raw, source_name, source_url, date, attempt)
                new = {"feedMeta": {
                    **old["feedMeta"], "sourceName": source_name, "sourceUrl": source_url, "boardDate": date,
                    "lastSuccessAt": attempt, "lastAttemptAt": attempt, "cachedAt": attempt, "usingCache": False,
                    "lastError": None, "feedVersion": _number(old["feedMeta"].get("feedVersion")) + 1, "itemCount": len(items),
                }, "liveItems": items}
                await self._cache.save(new)
                return {"ok": True, **deepcopy(new)}
            except (OSError, ValueError, httpx.HTTPError) as error:
                message = str(error) or "热点榜单请求超时"
                has_cache = bool(old["liveItems"])
                fallback = {"feedMeta": {**old["feedMeta"], "lastAttemptAt": attempt, "lastError": message, "usingCache": has_cache},
                            "liveItems": old["liveItems"]}
                if has_cache:
                    try:
                        await self._cache.save(fallback)
                    except OSError:
                        pass
                else:
                    self._cache.snapshot = fallback
                return {"ok": False, "usingCache": has_cache, "error": message, **deepcopy(fallback)}

    async def get_hotspots(self, *, platform="", genre="", query=""):
        saved = await self._cache.load()
        tropes, live = HOTSPOT_TROPES, saved["liveItems"]
        if platform and platform != "全部":
            tropes = [entry for entry in tropes if entry["platform"] == platform]
        if genre and genre != "全部":
            def in_genre(entry):
                return genre in (entry.get("genre") or "") or any(genre in tag for tag in _array(entry.get("tags")))
            tropes = [entry for entry in tropes if in_genre(entry)]
            live = [entry for entry in live if in_genre(entry)] or live
        query = query.strip().lower()
        if query:
            def matches(entry, summary):
                return any(query in str(value or "").lower() for value in (
                    entry.get("title"), entry.get("genre"), entry.get(summary), *_array(entry.get("tags")),
                ))
            tropes = [entry for entry in tropes if matches(entry, "samplePremise")]
            live = [entry for entry in live if matches(entry, "summary")]
        return deepcopy({
            "feedMeta": saved["feedMeta"], "liveItems": live, "fallbackTemplates": HOTSPOT_TROPES,
            "platforms": HOTSPOT_PLATFORMS, "tropes": tropes, "hookFormulas": HOOK_FORMULAS,
            "pacingTemplates": PACING_TEMPLATES,
        })


def _pick_sources(items, genre, limit=3):
    genre = genre.strip()
    ranked = sorted(items, key=lambda entry: bool(genre) and (
        (bool(entry.get("genre")) and (genre in entry["genre"] or entry["genre"] in genre))
        or any(genre in tag or tag in genre for tag in _array(entry.get("tags")))
    ), reverse=True)
    return ranked[:limit]


async def select_or_generate_concept(*, premise="", title="", genre="都市重生爽文", pov="第三人称限知",
                                     llm_config=None, prefer_freeform=False, meme_level="none",
                                     meme_source_mode="active_only", feed=None, meme_store=None, store=None,
                                     client=None, now=None):
    created = _iso(_now_ms(now))
    if premise.strip():
        return {
            "locked": True, "inspirationMode": "user_premise", "title": title.strip() or "作者自定创意项目",
            "premise": premise.strip(), "genre": genre, "pov": pov,
            "coreConflict": "围绕作者核心设定展开的主线冲突与破局挑战", "protagonistMotivation": "遵循作者设定的主角核心诉求",
            "goldenThreeChapters": [
                "第1章：按作者设定直接切入开篇冲突与主角处境",
                "第2章：展开关键线索或能力优势，确立短期目标",
                "第3章：完成首轮正面交锋并留下长线悬念",
            ],
            "mainPlot": premise.strip(), "sourceRefs": [], "candidates": [],
            "selectionReason": "作者已提供完整创意设定，系统直接锁定作者设定立项。", "createdAt": created,
        }

    mode, materials = "model_freeform", []
    if not prefer_freeform:
        cached = await feed.get_hotspots() if feed else {}
        live = cached.get("liveItems") or []
        if live:
            mode = "cached_feed" if cached["feedMeta"]["usingCache"] else "live_feed"
            materials = _pick_sources(live, genre)
        else:
            mode = "fallback_templates"
            materials = [{
                **{key: entry[key] for key in ("id", "title", "genre", "tags")},
                "hotScore": entry["heatScore"], "sourceName": "内置热点模板库", "sourceUrl": "",
                "boardDate": created[:10], "summary": entry["samplePremise"],
            } for entry in _pick_sources(HOTSPOT_TROPES, genre)]

    samples = []
    if meme_store is not None:
        if meme_level != "none":
            samples = await meme_store.get_meme_inspiration_samples(
                genre=genre, meme_source_mode=meme_source_mode, limit=3, now=now,
            )
        else:
            samples = (await meme_store.get_active_memes(now=now))[:2]
    supplement = ""
    if samples:
        lines = []
        for sample in samples:
            source = f"（来源：{sample['sourceUrl']}）" if sample.get("sourceUrl") else ""
            lines.append(f"- {sample.get('title')}：{sample.get('summary') or ''}{source}")
        supplement = "\n补充网感/热梗元素参考（仅借鉴反差或笑点结构，不生硬堆砌）：\n" + "\n".join(lines)
    hint = {
        "rich": "\n风格加权：方案需具备高浓度网感、强认知反差、高传播金句和强喜剧爽点。",
        "light": "\n风格加权：方案需兼具强剧情冲突与适度幽默吐槽/嘴替爽感。",
    }.get(meme_level, "")
    seed = materials[0] if materials else HOTSPOT_TROPES[0]
    request = f"""[TASK:TOPIC_CANDIDATES]
请基于以下信息生成 3 个高概念长篇网文候选选题方案（3个候选方案）：
题材：{genre}
视角：{pov}
灵感来源模式：{mode}
参考素材标题：{seed['title']}
参考素材列表：
{json.dumps(materials, ensure_ascii=False, indent=2)}{supplement}{hint}

要求每个候选方案包含：
title, premise, coreConflict, protagonistMotivation, goldenThreeChapters(3条数组), mainPlot, recommendedPov, tags, genreFit(0-100), novelty(0-100), longFormPotential(0-100)。
严格输出 JSON：{{ "candidates": [...] }}"""
    config = _llm_options(llm_config) if _mapping(llm_config).get("provider") else {"provider": "mock"}
    config["max_tokens"] = protected_output_budget(10_000, DEFAULT_MAX_TOKENS)
    candidates = []
    try:
        reply = await call_llm(
            **config, prompt=request, store=store, client=client, temperature=0.8,
            system="[TASK:TOPIC_CANDIDATES] 你是金牌网文选题策划总监，擅长从热点榜单提炼长篇连载高概念方案。只输出合法 JSON。",
        )
        parsed = None if output_was_truncated(reply.get("finishReason")) else parse_json_from_llm(reply.get("text"))
        candidates = [item for item in _array(_mapping(parsed).get("candidates"))
                      if isinstance(item, dict) and _text(item.get("title")) and _text(item.get("premise"))]
    except LLMError as error:
        if error.code in ("ABORTED", "CIRCUIT_OPEN"):
            raise
        logging.getLogger(__name__).warning("选题模型调用失败，使用本地方案（%s）", error.code)
    if not candidates:
        candidates = [{
            "title": seed["title"][:10] + "：破局重生录", "premise": seed.get("summary") or HOTSPOT_TROPES[0]["samplePremise"],
            "coreConflict": "主角必须在势力围剿与时间倒计时下完成逆袭反杀。", "protagonistMotivation": "守护至亲并揭开隐藏多年的真相。",
            "goldenThreeChapters": HOTSPOT_TROPES[0]["goldenThreeChapters"], "mainPlot": "前期破局立威，中期多方博弈，后期收网揭秘。",
            "recommendedPov": pov, "tags": seed.get("tags") or [genre], "genreFit": 92, "novelty": 88, "longFormPotential": 90,
        }]

    scored = []
    for index, item in enumerate(candidates):
        total = (_number(item.get("genreFit"), 85) * 0.4 + _number(item.get("novelty"), 80) * 0.25
                 + _number(item.get("longFormPotential"), 85) * 0.35)
        scored.append({**item, "_index": index, "totalScore": math.floor(total + 0.5)})
    scored.sort(key=lambda item: item["totalScore"], reverse=True)
    winner = scored[0]
    explanation = (
        f"来源模式【{mode}】，综合题材匹配度({winner.get('genreFit', 90)})、差异度({winner.get('novelty', 88)})"
        f"与长篇展开空间({winner.get('longFormPotential', 90)})，以综合分 {winner['totalScore']} "
        f"从 {len(scored)} 个候选方案中自动选出《{winner['title']}》。"
    )
    refs = [{key: entry[key] for key in ("id", "title", "sourceName", "sourceUrl", "boardDate", "rank") if key in entry}
            for entry in materials]
    return deepcopy({
        "locked": True, "inspirationMode": mode, "title": title.strip() or winner["title"], "premise": winner["premise"],
        "genre": genre, "pov": winner.get("recommendedPov") or pov, "coreConflict": winner.get("coreConflict"),
        "protagonistMotivation": winner.get("protagonistMotivation"), "goldenThreeChapters": winner.get("goldenThreeChapters") or [],
        "mainPlot": winner.get("mainPlot") or winner["premise"], "tags": winner.get("tags") or [genre],
        "sourceRefs": refs, "candidates": scored, "selectionReason": explanation, "createdAt": created,
    })


async def generate_concept(*, trope_id="", platform="番茄小说", genre="", keywords="", llm_config=None, store=None, client=None):
    trope = next((entry for entry in HOTSPOT_TROPES if entry["id"] == trope_id), None) or next(
        (entry for entry in HOTSPOT_TROPES if entry["platform"] == platform), HOTSPOT_TROPES[0],
    )
    pacing = next((item for item in PACING_TEMPLATES if item["id"] == trope["pacingTemplate"]), PACING_TEMPLATES[0])
    config = _llm_options(llm_config)
    provider = config.get("provider")
    if provider and provider != "mock":
        creds = await _resolve_credentials(
            provider, config.get("base_url", ""), config.get("api_key", ""), config.get("model", ""),
            config.get("thinking_mode"), config.get("use_saved_connection", False), store,
        )
        if creds.api_key or not provider_requires_key(provider):
            try:
                reply = await call_llm(
                    **config, store=store, client=client, temperature=0.85,
                    system="你是番茄小说与红果短剧爆款策划总监，请输出合法 JSON，包含 title, premise, genre, pov, goldenThreeChapters(数组,3条)。",
                    prompt=(f"基于平台【{platform or trope['platform']}】、题材【{genre or trope['genre']}】、爆款套路【{trope['title']}】、"
                            f"关键词【{keywords or '、'.join(trope['tags'])}】，生成一个全新的高概念爆款故事设定与黄金三章钩子。"),
                )
                parsed = None if output_was_truncated(reply.get("finishReason")) else parse_json_from_llm(reply.get("text"))
                if isinstance(parsed, dict) and _text(parsed.get("title")) and _text(parsed.get("premise")):
                    return deepcopy({**parsed, "hookFormula": trope["hookFormula"], "pacingTemplate": pacing, "sourceTropeId": trope["id"]})
            except LLMError as error:
                if error.code in ("ABORTED", "CIRCUIT_OPEN"):
                    raise
                logging.getLogger(__name__).warning("灵感生成失败，使用本地模板（%s）", error.code)

    suffix = f"（融入核心元素：{keywords.strip()}）" if keywords.strip() else ""
    return deepcopy({
        "title": f"{trope['title'].split('，')[0]}：{keywords.strip()}" if keywords else trope["title"],
        "genre": genre or trope["genre"], "pov": trope.get("recommendedPov") or "第三人称限知",
        "platform": platform or trope["platform"], "premise": trope["samplePremise"] + suffix,
        "goldenThreeChapters": trope["goldenThreeChapters"], "hookFormula": trope["hookFormula"],
        "pacingTemplate": pacing, "sourceTropeId": trope["id"],
    })
