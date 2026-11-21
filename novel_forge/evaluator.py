"""Persisted two-stage manuscript evaluation and focused chapter polishing."""

import asyncio
import hashlib
import json
import math
import re
import threading
from datetime import datetime, timezone
from decimal import Decimal
from string import Template

from .llm import DEFAULT_MAX_TOKENS, LLMError, call_llm, next_output_budget, output_was_truncated
from .storage import _json, count_chinese_words
from .verifier import _llm_options, _notify, _strict_json


AI_FLAVOR_CATEGORIES = [
    { "id": "over_explaining", "label": "写完又解释一遍", "desc": "动作对白已显露意图，旁白又补一句解释" },
    { "id": "formulaic_reaction", "label": "人物反应套公式", "desc": "高频重复瞳孔一缩、指节发白、动作一顿等反应词" },
    { "id": "shared_voice", "label": "人物共用一张嘴", "desc": "不同角色说话腔调与解释动机方式高度同质" },
    { "id": "static_situation", "label": "情节动了处境没变", "desc": "连续多章只换下一条线索，筹码与选择未发生实质变化" },
    { "id": "repetitive_hooks", "label": "章末同一种钩子", "desc": "章尾高频使用对讲机响、系统弹窗、还有一件事" },
    { "id": "inconsistent_details", "label": "细节很多却前后打架", "desc": "金额、日期、门牌或系统规则跨章前后矛盾" }
]

SIX_DIMENSION_DEFS = [
    { "id": "sellingPointAndOpening", "label": "1. 卖点与开篇" },
    { "id": "worldSettingAndCausality", "label": "2. 设定与因果" },
    { "id": "characterAndChoices", "label": "3. 人物与选择" },
    { "id": "plotAndPacing", "label": "4. 主线与节奏" },
    { "id": "foreshadowingAndEnding", "label": "5. 伏笔与结局" },
    { "id": "styleAndReadability", "label": "6. 文风与阅读体验" }
]

_REACTION_CLICHE_PATTERNS = [
    { "phrase": "瞳孔微缩|瞳孔一缩|瞳孔骤缩", "label": "瞳孔(一/微/骤)缩" },
    { "phrase": "指节发白|指节泛白|指节绷得发白", "label": "指节发白" },
    { "phrase": "动作一顿|手里的动作顿|停在半空|悬在半空", "label": "动作一顿/停在半空" },
    { "phrase": "呼吸一滞|呼吸停了半拍|心跳漏了一拍", "label": "呼吸一滞/心漏半拍" },
    { "phrase": "喉结动了动|喉结滚了|吞了口唾沫", "label": "喉结滚动/咽唾沫" },
    { "phrase": "嘴角勾起|唇角微勾|扯了扯嘴角|嘴角抽了抽", "label": "嘴角勾/抽" },
    { "phrase": "空气仿佛凝固|死一般的寂静|鸦雀无声", "label": "空气凝固/鸦雀无声" },
    { "phrase": "后背发凉|脊背发寒|胃里一阵翻涌", "label": "后背发凉/发寒" }
]

_BATCH_SYSTEM = """[TASK:EVAL_FLASH_BATCH]
你是资深长篇网文初审编辑，正在按“每10章一组”进行精细化 AI 味排查与跨章设定台账提取。
不要给模糊的总分（禁止输出“AI味80分”这种空话），必须逐条定位具体章节和原文原句。

请重点检查以下 6 类 AI 味问题：
1. over_explaining（写完又解释一遍）：动作或对白已经表现出态度/欺骗/犹豫，旁白却接着直白解释“她在撒谎/早有预谋/意思是……”。
2. formulaic_reaction（人物反应像套公式）：高频机械重复“瞳孔一缩/指节发白/动作一顿/喉结滚动”等套话，大小情绪同一副反应。
3. shared_voice（人物共用一张嘴）：去掉说话人名字后谁都能说，不同身份角色都用同一套完整冷静的逻辑把动机讲透。
4. static_situation（情节动了处境没变）：人物连环查线索换信息，但几章下来手里的筹码、危机与两难选择几乎没有变化。
5. repetitive_hooks（每章同一种方式吊胃口）：临收尾反复靠对讲机突然响、系统面板警告、门外脚步声或“还有一件事”强行断章。
6. inconsistent_details（细节很多却前后打架）：金额、日期、门牌号、容积、伤势、物品位置或系统规则在本组内或与总纲前后冲突。

必须严格输出合法 JSON。"""

_BATCH_PROMPT = Template("""书名：《${book_title}》 | 题材：${genre} | 视角：${pov}
总纲核心设定参考：${worldview}
本地统计信号提示（本组供参考核实）：${signal_hint}

请审读第 ${start_chapter}~${end_chapter} 章全文：
${chapter_texts}

请严格输出以下 JSON 结构（只输出 JSON）：
{
  "batchRange": "${start_chapter}-${end_chapter}",
  "timelineSummary": "本10章的时间跨度与关键日期节点（简明准确）",
  "situationDelta": "本10章结束后，主角处境、手中筹码或核心危机发生了哪些实质变化（若第某几章原地踏步请直说）",
  "endingHookSummary": [
    { "chapterNumber": ${start_chapter}, "hookType": "本集章末悬念写法一句话概括" }
  ],
  "extractedSettings": [
    "本组出现的硬设定/门牌/金额/扩容/伤势/规则数值（供跨章核对用，每条带章号）"
  ],
  "aiFlavorIssues": [
    {
      "chapterNumber": ${start_chapter},
      "category": "over_explaining|formulaic_reaction|shared_voice|static_situation|repetitive_hooks|inconsistent_details",
      "quote": "文中真实存在的原文原句（15~60字）",
      "impact": "为什么这一处影响阅读或暴露AI味",
      "priority": "P0|P1|P2",
      "suggestion": "具体删改建议（若可直接删掉解释句请明说删哪句）"
    }
  ]
}""")

_PRO_SYSTEM = """[TASK:EVAL_PRO_REVIEW]
你是挑剔、犀利且懂番茄/红果商业网文与长篇结构的主编，正在执行投稿前精读复审。
请务必先回答读者最关心的三个问题：
1) 开头想不想追；2) 中段读不读得下去；3) 结尾值不值得。

随后请完成两项深度评审：
A. 「盲测读者读前三章」：假设你完全没看过全书大纲，作为挑剔读者一口气读完前 3 章，诚实回答两句话：「你为什么想继续读？」和「你在哪一段开始想跳读？」（附章号与原文原句）。
B. 「六个维度 1～5 分评估」：
   - sellingPointAndOpening（卖点与开篇）
   - worldSettingAndCausality（设定与因果）
   - characterAndChoices（人物与选择）
   - plotAndPacing（主线与节奏）
   - foreshadowingAndEnding（伏笔与结局）
   - styleAndReadability（文风与阅读体验）
   每项必须给出 1～5 分（可保留 0.5 分如 3.5）、一个具体章号 chapterNumber 和一条原文/情节证据 evidence，不得空泛套话。
C. 「跨章终检 + AI味 P0/P1/P2 优先改稿清单」：结合全书每 10 章初审台账与跨章规则，指出跨章设定冲突与最影响阅读的 AI 味条目（带章号、原句、原因、优先级、修改建议）。

必须严格输出合法 JSON。"""

_PRO_PROMPT = Template("""书名：《${book_title}》 | 题材：${genre} | 视角：${pov} | 共 ${total_chapters} 章 | 总字数：${total_words}
【全书总纲】${book_plan}
【全书自动化重复统计】${signals}
【全书分段初审台账（每10章一组）】${batches}

【精读原文样本（前10章 + 中段转折 + 结尾）】
${sample_text}

请严格输出以下 JSON（只输出 JSON）：
{
  "overallSummary": "主编一句话总评（直指是否可投番茄及最核心优缺点）",
  "readiness": "ready_to_submit|revise_recommended|major_rework",
  "topVerdicts": {
    "openingHook": { "verdict": "想追|一般|不想追", "summary": "开头想不想追的直觉判断与依据" },
    "middlePacing": { "verdict": "读得下去|略拖|想跳读", "summary": "中段读不读得下去的判断（结合抽查与每10章处境变化）" },
    "endingPayoff": { "verdict": "值得|偏平|烂尾", "summary": "结尾值不值得、伏笔代价是否落地" }
  },
  "blindReaderFirst3Chapters": {
    "whyKeepReading": [
      { "chapterNumber": 2, "quote": "让你想继续追的那一句或情节", "reason": "为什么抓人" }
    ],
    "whereToSkip": [
      { "chapterNumber": 1, "quote": "让你开始想跳读的那一段或句子", "reason": "为什么想跳读（啰嗦/重复/缺冲突）" }
    ]
  },
  "sixDimensions": {
    "sellingPointAndOpening": { "score": 4, "chapterNumber": 2, "evidence": "原文或具体情节点证据", "comment": "诊断评语", "fixAdvice": "可执行改法" },
    "worldSettingAndCausality": { "score": 4, "chapterNumber": 1, "evidence": "...", "comment": "...", "fixAdvice": "..." },
    "characterAndChoices": { "score": 4, "chapterNumber": 98, "evidence": "...", "comment": "...", "fixAdvice": "..." },
    "plotAndPacing": { "score": 3.5, "chapterNumber": 22, "evidence": "...", "comment": "...", "fixAdvice": "..." },
    "foreshadowingAndEnding": { "score": 4, "chapterNumber": 95, "evidence": "...", "comment": "...", "fixAdvice": "..." },
    "styleAndReadability": { "score": 3.5, "chapterNumber": 3, "evidence": "...", "comment": "...", "fixAdvice": "..." }
  },
  "aiFlavorIssues": [
    {
      "chapterNumber": 1,
      "category": "over_explaining|formulaic_reaction|shared_voice|static_situation|repetitive_hooks|inconsistent_details",
      "quote": "原文原句",
      "impact": "为什么影响阅读",
      "priority": "P0|P1|P2",
      "suggestion": "修改/删减建议"
    }
  ],
  "rewritePlan": [
    "按优先级排序的具体改稿动作清单"
  ]
}""")

_SURGICAL_SYSTEM = Template("""[TASK:SURGICAL_DE_AI]
你是专业小说精修编辑。改稿时请始终先问自己一句话：
「删掉这一段，读者会少知道什么，人物又会少做什么？如果只少了一遍解释、一次熟悉的反应，或者一条绕路的线索，那就是最值得先动刀的地方。」

精修铁律：
1. 严格保留本章全部核心剧情事实、人物关系、门牌号、金额、物品位置与时间线，绝不擅自编造矛盾设定。
2. 逐项清理底下指出的 AI 味条目：
   - 对白和动作已经表达清楚的地方，直接删掉旁白的解释句；
   - 替换机械重复的反应词（如反复出现的瞳孔一缩、指节发白、动作一顿、喉结滚动），改用贴合当下处境的真实动作或干脆留白；
   - 拉开不同角色的说话方式和长短节奏，不要所有人都用同一副冷静腔解释动机。
3. 保持${pov}视角与${style_guide}节奏，控制成稿字数在原文字数的 85%~105% 之间（除非明确需删掉大段啰嗦水词）。
4. 只输出精修后的完整章节正文（不要加任何“修改说明”前缀，不要用 Markdown **加粗** 包裹正文句子）。""")

_SURGICAL_PROMPT = Template("""【第${chapter_number}章《${title}》 待精修正文】
${chapter_text}

【本章已定位的 AI 味问题与改稿意见】
${issues}

请直接输出精修后的完整章节正文：""")


_ENDING_HOOK_PATTERNS = (
    (r"对讲机.{0,12}(?:响|炸|震)|手机.{0,12}(?:震|响|亮)", "通讯/手机突发消息打断"),
    (r"系统.{0,18}(?:弹出|提示|警告|红字)|面板.{0,18}(?:跳出|闪)", "系统面板突然弹出警告"),
    (r"还有一件事|等一下|别忘了", "临走补一句“还有一件事”"),
    (r"敲门声|拍门声|脚步声.{0,15}(?:逼近|停在|响起)", "门外突然响起脚步/拍门声"),
)
_MODES = ("full", "flash_only", "pro_only")
_PRIORITY_ORDER = {"P0": 0, "P1": 1, "P2": 2}
_BATCH_FIELDS = ("batchRange", "timelineSummary", "situationDelta", "endingHookSummary", "extractedSettings")
_THINK_PREFIX = re.compile(r"^<think>[\s\S]*?</think>\s*", re.I)
_FENCE = re.compile(r"^`{3}(?:json)?\s*([\s\S]*?)\s*`{3}$", re.I)
_running_evaluations = set()
_registry_lock = threading.Lock()


class EvaluationError(RuntimeError):
    def __init__(self, message, code, status=409):
        super().__init__(message)
        self.code = code
        self.status = status


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _dict(value):
    return value if isinstance(value, dict) else {}


def _list(value):
    return value if isinstance(value, list) else []


def _has_text(value):
    return isinstance(value, str) and bool(value.strip())


def _number(value, default=0):
    try:
        value = float(value)
        return (int(value) if value.is_integer() else value) if math.isfinite(value) else default
    except (TypeError, ValueError, OverflowError):
        return default


def _chapter_number(chapter):
    return _number(chapter.get("chapterNumber"))


def _excerpt(value, units):
    return str(value or "").encode("utf-16-le", "surrogatepass")[:2 * units].decode("utf-16-le", errors="ignore")


def collect_book_signals(chapters=()):
    ordered = sorted(chapters, key=_chapter_number)
    reaction = {}
    for pattern in _REACTION_CLICHE_PATTERNS:
        hits = [(_chapter_number(chapter), len(re.findall(pattern["phrase"], str(chapter.get("content") or ""))))
                for chapter in ordered]
        counts = [(number, count) for number, count in hits if count]
        if counts:
            reaction[pattern["label"]] = {"totalCount": sum(count for _, count in counts),
                                           "chapterCount": len(counts),
                                           "sampleChapters": [number for number, _ in counts[:15]]}
    ending = {}
    for chapter in ordered:
        lines = [line for line in str(chapter.get("content") or "").strip().split("\n") if line.strip()]
        tail = " ".join(lines[-3:])
        for pattern, label in _ENDING_HOOK_PATTERNS:
            if re.search(pattern, tail):
                group = ending.setdefault(label, {"count": 0, "chapters": []})
                group["count"] += 1
                group["chapters"].append(_chapter_number(chapter))
    return {"totalChapters": len(ordered), "reactionStats": reaction, "endingHookStats": ending}


def _js_json(value):
    """Match JSON.stringify for the persisted inputs used by legacy checkpoint hashes."""
    if isinstance(value, dict):
        def index(key):
            if re.fullmatch(r"0|[1-9][0-9]{0,9}", key) and int(key) < 2**32 - 1:
                return int(key)
            return 2**32
        return "{" + ",".join(_json(key) + ":" + _js_json(value[key]) for key in sorted(value, key=index)) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(map(_js_json, value)) + "]"
    if type(value) in (int, float):
        try:
            numeric = float(value)
        except OverflowError:
            return "null"
        if not math.isfinite(numeric):
            return "null"
        if numeric == 0:
            return "0"
        if type(value) is int and abs(value) <= 2**53 - 1:
            return str(value)
        shortest = repr(numeric)
        if 1e-6 <= abs(numeric) < 1e21:
            fixed = format(Decimal(shortest), "f")
            return fixed.rstrip("0").rstrip(".") if "." in fixed else fixed
        return re.sub(r"e([+-])0+", r"e\1", shortest)
    return _json(value)


def evaluation_source(job, chapters):
    fields = [job.get(key) for key in ("title", "genre", "pov", "bookPlan", "canonicalFacts")]
    manuscript = [[chapter.get(key) for key in ("chapterNumber", "title", "content", "summary", "aiIssues")] for chapter in chapters]
    return hashlib.sha256(_js_json([*fields, manuscript]).encode("utf-8")).hexdigest()


def _chapter_range(chapters):
    return f"{chapters[0]['chapterNumber']}-{chapters[-1]['chapterNumber']}"


def _failed_batch(batch):
    return bool(re.match(r"^JSON\s*解析失败", str(_dict(batch).get("timelineSummary") or "")))


def _issue_list(value):
    return isinstance(value, list) and all(isinstance(issue, dict) for issue in value)


def _valid_batch(batch, expected_range):
    if not isinstance(batch, dict) or not _has_text(batch.get("timelineSummary")) or _failed_batch(batch):
        return False
    if "batchRange" in batch:
        span = re.sub(r"\s", "", str(batch["batchRange"])).translate(str.maketrans("–—~～", "----"))
        if span != expected_range:
            return False
    return (
        _issue_list(batch.get("aiFlavorIssues"))
        and ("situationDelta" not in batch or isinstance(batch["situationDelta"], str))
        and ("endingHookSummary" not in batch or _issue_list(batch["endingHookSummary"]))
        and ("extractedSettings" not in batch or isinstance(batch["extractedSettings"], list)
             and all(isinstance(item, str) for item in batch["extractedSettings"]))
    )


def _valid_pro_review(review):
    if not isinstance(review, dict) or not _has_text(review.get("overallSummary")) or not _issue_list(review.get("aiFlavorIssues")):
        return False
    if "rewritePlan" in review and (not isinstance(review["rewritePlan"], list) or not all(map(_has_text, review["rewritePlan"]))):
        return False
    dims = _dict(review.get("sixDimensions"))
    scores = [_dict(dims.get(entry["id"])).get("score") for entry in SIX_DIMENSION_DEFS]
    return all(type(score) in (int, float) and 0 <= score <= 5 for score in scores) and all(
        field not in review or isinstance(review[field], dict) for field in ("topVerdicts", "blindReaderFirst3Chapters")
    )


def checkpoint_status(checkpoint):
    total = _number(checkpoint.get("totalBatches"))
    completed = min(total, sum(
        bool(_dict(batch).get("batchRange")) and not _failed_batch(batch) for batch in _list(checkpoint.get("batchResults"))
    ))
    return {
        **{key: checkpoint[key] for key in ("mode", "updatedAt", "selection") if key in checkpoint},
        "stage": "flash" if completed < total else checkpoint.get("stage"),
        "totalBatches": total, "completedBatches": completed,
    }


def _chapter_texts(chapters):
    return "\n\n".join(
        f"=== 第{chapter['chapterNumber']}章《{chapter.get('title') or ''}》 ({chapter.get('wordCount') or 0}字) ===\n"
        + str(chapter.get("content") or "").strip() for chapter in chapters
    )


def _build_batch_review(job, batch, hint):
    prompt = _BATCH_PROMPT.substitute(
        book_title=job.get("title", "undefined"), genre=job.get("genre", "undefined"), pov=job.get("pov", "undefined"),
        worldview=_json(_dict(job.get("bookPlan")).get("worldview") or {}, indent=2), signal_hint=_json(hint, indent=2),
        start_chapter=batch[0]["chapterNumber"], end_chapter=batch[-1]["chapterNumber"], chapter_texts=_chapter_texts(batch),
    )
    return {"system": _BATCH_SYSTEM, "prompt": prompt}


def _sample_chapters(ordered):
    total = len(ordered)
    numbers = set(range(1, min(10, total) + 1))
    if total > 26:
        numbers.update(range(22, 27))
    elif total > 12:
        mid = total // 2
        numbers.update(range(max(11, mid - 2), min(total - 3, mid + 2) + 1))
    if total >= 35:
        turning = math.floor(total * 0.65)
        numbers.update((turning, turning + 1))
    numbers.update(range(max(1, total - 2), total + 1))
    return [chapter for chapter in ordered if _chapter_number(chapter) in numbers]


def _build_pro_review(job, ordered, batches, signals):
    compact = [{**{key: batch[key] for key in _BATCH_FIELDS if key in batch},
                "topIssues": _list(batch.get("aiFlavorIssues"))[:8]} for batch in batches]
    prompt = _PRO_PROMPT.substitute(
        book_title=job.get("title", "undefined"), genre=job.get("genre", "undefined"), pov=job.get("pov", "undefined"),
        total_chapters=len(ordered), total_words=job.get("totalWords", 0), book_plan=_json(job.get("bookPlan") or {}, indent=2),
        signals=_json(signals, indent=2), batches=_json(compact, indent=2), sample_text=_chapter_texts(_sample_chapters(ordered)),
    )
    return {"system": _PRO_SYSTEM, "prompt": prompt}


def _merge_issues(batches, pro_issues):
    issues, seen = [], set()
    for raw in [*pro_issues, *(item for batch in batches for item in _list(batch.get("aiFlavorIssues")))]:
        if not isinstance(raw, dict) or not raw.get("chapterNumber"):
            continue
        number = _chapter_number(raw)
        quote = str(raw.get("quote") or "").strip()
        category = raw.get("category")
        key = number, str(category or ""), _excerpt(quote, 24)
        if key in seen:
            continue
        seen.add(key)
        priority = raw.get("priority")
        issues.append({
            "id": f"issue_{number}_{len(issues) + 1}", "chapterNumber": number,
            "category": category or "over_explaining", "quote": quote, "impact": str(raw.get("impact") or "").strip(),
            "priority": priority if priority in ("P0", "P1", "P2") else "P1",
            "suggestion": str(raw.get("suggestion") or "").strip(), "resolved": False,
        })
    return sorted(issues, key=lambda issue: (_PRIORITY_ORDER[issue["priority"]], issue["chapterNumber"]))


def _mock_report(job, chapters, signals, mode):
    first, middle, last = [chapter["chapterNumber"] for chapter in (chapters[0], chapters[len(chapters) // 2], chapters[-1])]
    original = next((line for line in str(chapters[0].get("content") or "").split("\n") if line.strip() and not line.startswith("#")), "")
    dimensions = (
        (4, first, "开篇即建立重生/末世或核心强冲突卖点。", "核心钩子成立。", "减少纯解释性清单，尽早切入对手反制。"),
        (4, middle, "关键物品与规则链条在中段保持一致。", "基础因果自洽。", "关注数值与空间/时间跨章核对。"),
        (4, last, "关键节点主角做出高代价抉择。", "人物立场鲜明。", "让配角有独立于主角算计之外的自私目标。"),
        (3.5, middle, "局部章节存在线索传递密度偏高。", "整体在线但中段可更干脆。", "合并单纯只换下一条线索的过渡章。"),
        (4.5, last, "终局回收前期埋下的关键铺垫。", "结尾闭环有力。", "补足决定前的一两步实体准备工作。"),
        (3.5, first, "局部出现高频身体反应词或解释句。", "口语节奏不错，但需去AI味去重。", "清理“写完又解释”和套公式微表情。"),
    )
    return {
        "evaluatedAt": _now(), "mode": mode, "models": {"flash": "mock-flash-eval", "pro": "mock-pro-review"},
        "bookTitle": job.get("title"), "totalChapters": len(chapters), "totalWords": job.get("totalWords", 0),
        "deterministicSignals": signals, "readiness": "revise_recommended",
        "overallSummary": "Mock 评估完成：主线与闭环完整，已定位若干 AI 味重复反应与局部解释句供精修。",
        "topVerdicts": {
            "openingHook": {"verdict": "想追", "summary": f"第{first}章开篇即见核心危机，前三章已建立明确优势。"},
            "middlePacing": {"verdict": "略拖", "summary": f"第{middle}章前后局部调查偏密集，建议压缩纯线索传话。"},
            "endingPayoff": {"verdict": "值得", "summary": f"第{last}章呼应前文核心铺垫，主角选择具备代价感。"},
        },
        "blindReaderFirst3Chapters": {
            "whyKeepReading": [{"chapterNumber": first, "quote": _excerpt(original, 45) or "开篇危机引入",
                                "reason": "开局直接抛出生存危机与核心金手指反差，代入感强。"}],
            "whereToSkip": [{"chapterNumber": min(2, last), "quote": "部分铺陈性过渡段落信息密度偏低",
                             "reason": "读者已经懂主角要做什么时，重复盘点会拉慢节奏。"}],
        },
        "sixDimensions": {
            entry["id"]: dict(zip(("score", "chapterNumber", "evidence", "comment", "fixAdvice"), values))
            for entry, values in zip(SIX_DIMENSION_DEFS, dimensions)
        },
        "batchSummaries": [{"batchRange": f"{first}-{last}", "timelineSummary": "mock 时间线顺承完整",
                            "situationDelta": "主角处境随推进逐步升级", "extractedSettings": ["Mock 核心规则已校验"]}],
        "aiFlavorIssues": [{
            "id": f"issue_{first}_1", "chapterNumber": first, "category": "formulaic_reaction",
            "quote": _excerpt(original, 40) or "示例引文", "impact": "连续重复同类身体微反应会削弱危机张力",
            "priority": "P1", "suggestion": "删去公式化反应套话，改用动作或直接留白。", "resolved": False,
        }],
        "rewritePlan": ["优先处理 P0/P1 AI 味条目，清理解释性旁白与重复反应词。"],
    }


async def _checked_review(request, check, label, on_retry, limit_advice, *, store, client):
    retry_instruction = (
        "\n上一次回复未完整通过 JSON 校验。重新审读同一段，只输出一个完整 JSON 对象，"
        "按原有字段输出，字符串中的引号和换行须正确转义。不要解释、不要代码围栏。" + limit_advice
    )
    budget, apply_floor = request["max_tokens"], True
    for attempt in range(2):
        reason = "返回的 JSON 无效或缺少必要字段"
        system = request["system"] + "\n最终回复必须是一个完整 JSON 对象。不要输出 Markdown。"
        try:
            reply = await call_llm(
                **{**request, "system": system + (retry_instruction if attempt else ""), "max_tokens": budget},
                retry_empty_response=False, apply_output_floor=apply_floor, store=store, client=client,
            )
            if output_was_truncated(reply.get("finishReason")):
                reason = "输出被模型截断"
            elif not _has_text(reply.get("text")):
                reason = "模型返回空内容"
            else:
                text = _THINK_PREFIX.sub("", reply["text"].strip())
                fenced = _FENCE.fullmatch(text)
                try:
                    parsed = _strict_json(fenced[1] if fenced else text)
                except (ValueError, RecursionError):
                    parsed = None
                if check(parsed):
                    return parsed
        except LLMError as error:
            if output_was_truncated(error.finish_reason):
                reason = "输出被模型截断"
                if error.code == "OUTPUT_TRUNCATED" and error.request_max_tokens is not None:
                    rejected = error.rejected_output_tokens if error.rejected_output_tokens is not None else math.inf
                    budget = next_output_budget(error.request_max_tokens, rejected) or error.request_max_tokens
                    apply_floor = False
            elif error.code == "EMPTY_RESPONSE":
                reason = "模型返回空内容"
            else:
                raise
        if attempt == 0:
            await on_retry(f"{label}校验未通过（{reason}），正在自动重跑当前段…")
        else:
            raise EvaluationError(f"{label}自动重跑后仍未通过校验（{reason}）。已保存通过校验的批次，可以换模型后继续。",
                                  "EVALUATION_INVALID_RESULT", 502)


def _job_runner_key(store, job_id):
    return str(store.path.resolve()), job_id


def is_book_evaluation_running(job_id, *, store=None):
    with _registry_lock:
        if store is not None:
            return _job_runner_key(store, job_id) in _running_evaluations
        return any(active_id == job_id for _, active_id in _running_evaluations)


def _finished(key):
    with _registry_lock:
        _running_evaluations.discard(key)


async def _db_write(method, *args):
    pending = asyncio.create_task(asyncio.to_thread(method, *args))
    try:
        return await asyncio.shield(pending)
    except asyncio.CancelledError:
        await pending
        raise


def _load_job_and_chapters(store, job_id):
    with store._connection() as connection:
        connection.execute("BEGIN")
        job = store._get_job(connection, job_id)
        if job is None:
            raise EvaluationError("找不到作品。", "JOB_NOT_FOUND", 404)
        return job, store._get_chapters(connection, job_id)


def _repair_batch_size(report, ordered):
    batches = _list(report.get("batchSummaries"))
    if not all(isinstance(batch, dict) for batch in batches):
        return 0
    first_range = str(_dict(batches[0]).get("batchRange") or "") if batches else ""
    first_end = _number(first_range.partition("-")[2], None)
    size = report.get("batchSize")
    if size is None:
        size = next((index for index, chapter in enumerate(ordered, 1) if _chapter_number(chapter) == first_end), 0)
    if type(size) is not int or not 1 <= size <= 15:
        return 0
    ranges = [_chapter_range(ordered[index:index + size]) for index in range(0, len(ordered), size)]
    return max(3, size) if [batch.get("batchRange") for batch in batches] == ranges else 0


def _reuse_saved_batches(report):
    revived = []
    for batch in _list(report.get("batchSummaries")):
        if _failed_batch(batch):
            continue
        first, _, last = batch["batchRange"].partition("-")
        issues = [issue for issue in _list(report.get("aiFlavorIssues"))
                  if _number(first) <= _chapter_number(issue) <= _number(last)]
        revived.append({**batch, "aiFlavorIssues": issues})
    return revived


def _setting(*values):
    return next((value for value in values if value is not None), None)


def _make_checkpoint(job, ordered, options):
    report = _dict(job.get("evaluationReport"))
    source_hash = evaluation_source(job, ordered)
    repairing = options["repair_failed_batches"]
    repair_size = 0
    if repairing and report.get("mode") in ("full", "flash_only") and report.get("sourceHash") == source_hash and any(
        _failed_batch(batch) for batch in _list(report.get("batchSummaries"))
    ):
        repair_size = _repair_batch_size(report, ordered)
    if repairing and (not repair_size or job.get("evaluationCheckpoint")):
        raise EvaluationError("当前报告不能补跑失败批次。请继续现有断点，或基于当前正文重新评估。",
                              "EVALUATION_REPAIR_UNAVAILABLE")

    previous = {} if repairing or options["restart_evaluation"] else _dict(job.get("evaluationCheckpoint"))
    mode = report["mode"] if repairing else options["mode"] or previous.get("mode") or "full"
    if mode not in _MODES:
        raise EvaluationError("不支持该评估模式。", "INVALID_EVALUATION_MODE", 400)
    same_run = previous.get("version") == 1 and previous.get("sourceHash") == source_hash and previous.get("mode") == mode
    requested = _number(options["batch_size"]) or (_number(previous.get("batchSize")) if same_run else 10) or 10
    batch_size = repair_size or max(3, min(15, math.floor(requested)))
    if repairing:
        force_rescan = True
    elif same_run and options["force_rescan"] is None:
        force_rescan = previous.get("forceRescan", False)
    else:
        force_rescan = options["force_rescan"] is True
    reused = any(isinstance(chapter.get("aiIssues"), list) for chapter in ordered) and not force_rescan
    ranges = [] if mode == "pro_only" else [_chapter_range(ordered[i:i + batch_size]) for i in range(0, len(ordered), batch_size)]
    total = len(ranges)
    resumable = (same_run and previous.get("batchSize") == batch_size and previous.get("forceRescan") == force_rescan
                 and previous.get("totalBatches") == total and isinstance(previous.get("batchResults"), list))
    if options["resume_evaluation"] and not resumable:
        raise EvaluationError("正文或评估设置已变化，请选择「从头评估」。", "EVALUATION_CHECKPOINT_MISMATCH")
    if repairing:
        batches = _reuse_saved_batches(report)
    elif resumable:
        batches = previous["batchResults"]
    elif mode == "pro_only" and report.get("sourceHash") in (None, "", source_hash):
        batches = _list(report.get("batchSummaries"))
    else:
        batches = []
    by_range = {batch["batchRange"]: batch for batch in batches
                if isinstance(batch, dict) and batch.get("batchRange") and not _failed_batch(batch)}
    batches = list(by_range.values()) if mode == "pro_only" else [
        by_range[span] for span in ranges if span in by_range and _valid_batch(by_range[span], span)
    ]
    pro_review = previous.get("proReview") if resumable and len(batches) >= total else None
    pro_review = pro_review if _valid_pro_review(pro_review) else None
    stage = "flash" if len(batches) < total else "finalizing" if mode == "flash_only" or pro_review else "pro"

    base_provider = options["provider"] or _dict(job.get("llmConfig")).get("provider") or "vertex"
    last = _dict(previous.get("selection")) if resumable else {}
    flash = options["flash_provider"] or last.get("flashProvider") or base_provider
    pro = options["pro_provider"] or last.get("proProvider") or base_provider
    flash_name = options["flash_model"] or last.get("flashModel") or ("mock-novel-v1" if flash == "mock" else "gemini-3.8-flash")
    pro_name = options["pro_model"] or last.get("proModel") or ("mock-novel-v1" if pro == "mock" else "gemini-3.1-pro-preview")
    all_mock = (flash == "mock" or mode == "pro_only") and (pro == "mock" or mode == "flash_only")
    if (resumable or repairing) and (
        stage == "flash" and not reused and flash == "mock"
        or stage != "finalizing" and mode != "flash_only" and pro == "mock"
    ):
        raise EvaluationError("请选择真实模型续跑，或从头运行模拟评估。", "EVALUATION_MODEL_MISMATCH")
    selection = {"flashProvider": flash, "flashModel": flash_name, "proProvider": pro, "proModel": pro_name}
    for kind, chosen in (("flash", flash), ("pro", pro)):
        field = f"useSaved{kind.title()}Connection"
        saved = _setting(options[f"use_saved_{kind}_connection"], options["use_saved_connection"],
                         last.get(field) if last.get(f"{kind}Provider") == chosen else None)
        if saved is not None:
            selection[field] = saved
    models = str(_dict(report.get("models"))["flash"]).split("、") if repairing and batches and isinstance(
        _dict(report.get("models")).get("flash"), str,
    ) else _list(previous.get("flashModels")) if resumable else []
    checkpoint = {
        "version": 1, "sourceHash": source_hash, "mode": mode, "batchSize": batch_size, "totalBatches": total,
        "forceRescan": force_rescan, "stage": stage, "batchResults": batches, "flashModels": models,
        "proReview": pro_review, "proUsed": previous.get("proUsed") if pro_review else None,
        "selection": selection, "updatedAt": _now(),
    }
    return checkpoint, reused, all_mock and not resumable and not repairing, resumable


async def recover_interrupted_evaluations(store):
    for job in await asyncio.to_thread(store.list_jobs):
        if job.get("evaluationStatus") != "running" or is_book_evaluation_running(job["id"], store=store):
            continue
        def recovered(latest):
            if latest.get("evaluationStatus") != "running":
                return {}
            return {
                "evaluationStatus": "error",
                "evaluationProgress": ("服务重启中断了评估，已保存的批次可以继续使用。" if latest.get("evaluationCheckpoint")
                                       else "服务重启中断了评估，请重新开始。"),
            }
        await _db_write(store.update_job, job["id"], recovered)


async def start_book_evaluation(store, job_id, *, mode=None, batch_size=None, force_rescan=None,
                                restart_evaluation=False, resume_evaluation=False, repair_failed_batches=False,
                                provider=None, flash_provider=None, flash_model=None, pro_provider=None, pro_model=None,
                                use_saved_connection=None, use_saved_flash_connection=None, use_saved_pro_connection=None,
                                base_url=None, api_key=None, timeout_ms=None, max_retries=None, on_progress=None, client=None,
                                flash_max_tokens=DEFAULT_MAX_TOKENS):
    key = _job_runner_key(store, job_id)
    with _registry_lock:
        if key in _running_evaluations:
            raise EvaluationError("此作品正在评估。", "EVALUATION_RUNNING")
        _running_evaluations.add(key)
    try:
        job, ordered = await asyncio.to_thread(_load_job_and_chapters, store, job_id)
        if not ordered:
            raise EvaluationError("该任务暂无可评估的章节内容", "NO_CHAPTERS", 400)
        options = {
            "mode": mode, "batch_size": batch_size, "force_rescan": force_rescan,
            "restart_evaluation": restart_evaluation, "resume_evaluation": resume_evaluation,
            "repair_failed_batches": repair_failed_batches, "provider": provider, "flash_provider": flash_provider,
            "flash_model": flash_model, "pro_provider": pro_provider, "pro_model": pro_model,
            "use_saved_connection": use_saved_connection, "use_saved_flash_connection": use_saved_flash_connection,
            "use_saved_pro_connection": use_saved_pro_connection,
        }
        checkpoint, reused, uses_mock, resumable = _make_checkpoint(job, ordered, options)
        started = await _db_write(store.update_job, job_id, {
            "evaluationStatus": "running", "evaluationCheckpoint": None if uses_mock else checkpoint,
            "evaluationProgress": ("正在补跑初审失败批次，保留有效批次…" if repair_failed_batches
                                   else "正在从已保存的断点继续评估…" if resumable else "正在开始评估…"),
        })

        async def update(fields):
            return await _db_write(store.update_job, job_id, fields)

        async def log(entry):
            await _db_write(store.append_job_log, job_id, entry)

        async def complete():
            try:
                return await _complete_evaluation(
                    job, ordered, checkpoint, reused=reused, uses_mock=uses_mock, update=update, log=log,
                    on_progress=on_progress, store=store, client=client, base_url=base_url, api_key=api_key,
                    timeout_ms=timeout_ms, max_retries=max_retries, flash_max_tokens=flash_max_tokens,
                )
            except asyncio.CancelledError:
                await update({"evaluationStatus": "error", "evaluationProgress": "评估已中断，可以从已保存的断点继续。"})
                raise
            except Exception as error:
                await update({"evaluationStatus": "error", "evaluationProgress": f"评估失败：{error}"})
                await log({"level": "error", "stage": "evaluation", "message": f"模型评估失败：{error}"})
                raise
        completion = asyncio.create_task(complete(), name=f"evaluate:{job_id}")
        completion.add_done_callback(lambda _: _finished(key))
        return {"job": started, "completion": completion}
    except BaseException:
        _finished(key)
        raise


async def evaluate_book(store, job_id, **options):
    started = await start_book_evaluation(store, job_id, **options)
    return await started["completion"]


async def evaluate_candidate(store, job_id, *, chapters, flash, pro, checkpoint=None, progress=None, client=None):
    job = await asyncio.to_thread(store.get_job_record, job_id)
    if job is None:
        raise EvaluationError("找不到作品。", "JOB_NOT_FOUND", 404)
    if not isinstance(chapters, list) or not chapters or not all(
        isinstance(chapter, dict) and chapter.get("chapterNumber") == index
        and _has_text(chapter.get("title")) and _has_text(chapter.get("content"))
        for index, chapter in enumerate(chapters, 1)
    ):
        raise EvaluationError("候选稿章节不完整。", "EVALUATION_CANDIDATE", 400)
    source = evaluation_source(job, chapters)
    if checkpoint is not None and _dict(checkpoint).get("sourceHash") != source:
        raise EvaluationError("作品设置已变动，请从头评估这版稿件。", "EVALUATION_CHECKPOINT_MISMATCH")
    flash_options, pro_options = _llm_options(flash), _llm_options(pro)
    size = 10
    ranges = [_chapter_range(chapters[index:index + size]) for index in range(0, len(chapters), size)]
    total = len(ranges)
    saved = _dict(checkpoint)
    batches = {batch.get("batchRange"): batch for batch in _list(saved.get("batchResults")) if isinstance(batch, dict)}
    valid = [batches[span] for span in ranges if _valid_batch(batches.get(span), span)]
    pro_review = saved.get("proReview")
    if len(valid) != total or not _valid_pro_review(pro_review):
        pro_review = None
    entry = {
        **saved, "version": 1, "sourceHash": source, "mode": "full", "batchSize": size, "totalBatches": total,
        "stage": "flash" if len(valid) < total else "finalizing" if pro_review else "pro",
        "batchResults": valid, "flashModels": _list(saved.get("flashModels")), "proReview": pro_review,
        "selection": {
            "flashProvider": flash_options["provider"], "flashModel": flash_options["model"],
            "proProvider": pro_options["provider"], "proModel": pro_options["model"],
            "useSavedFlashConnection": flash_options.get("use_saved_connection"),
            "useSavedProConnection": pro_options.get("use_saved_connection"),
        },
    }

    async def update(fields):
        await _notify(progress, fields)

    result = await _complete_evaluation(
        {**job, "totalWords": sum(count_chinese_words(chapter["content"]) for chapter in chapters)}, chapters, entry,
        update=update, log=None, reused=False, uses_mock=flash_options["provider"] == pro_options["provider"] == "mock",
        store=store, client=client,
    )
    return result["evaluationReport"]


def _signal_hint(signals, start, end):
    result = {}
    for kind, source, field in (("reactionStats", "reactionStats", "sampleChapters"),
                                ("endingHooks", "endingHookStats", "chapters")):
        result[kind] = {
            label: in_range for label, value in signals[source].items()
            if (in_range := [number for number in value[field] if start <= number <= end])
        }
    return result


def _chapter_review_batch(batch):
    first, last = batch[0]["chapterNumber"], batch[-1]["chapterNumber"]
    issues = [{**issue, "chapterNumber": _number(issue.get("chapterNumber") or chapter["chapterNumber"])}
              for chapter in batch for issue in _list(chapter.get("aiIssues")) if isinstance(issue, dict)]
    return {
        "batchRange": _chapter_range(batch), "timelineSummary": f"已复用第 {first}~{last} 章审校台账与摘要",
        "situationDelta": _excerpt("；".join(f"第{item['chapterNumber']}章: {item.get('summary') or ''}" for item in batch), 220),
        "endingHookSummary": [{"chapterNumber": item["chapterNumber"],
                               "hookType": _excerpt(str(item.get("content") or "").strip().split("\n")[-1], 45)} for item in batch],
        "extractedSettings": [], "aiFlavorIssues": issues,
    }


async def _complete_evaluation(job, ordered, checkpoint, *, update, log, reused, uses_mock, store, client,
                               on_progress=None, base_url=None, api_key=None, timeout_ms=None, max_retries=None,
                               flash_max_tokens=DEFAULT_MAX_TOKENS):
    mode, batch_size = checkpoint["mode"], checkpoint["batchSize"]
    selection = checkpoint["selection"]
    signals = await asyncio.to_thread(collect_book_signals, ordered)
    await _notify(log, {"level": "info", "stage": "evaluation",
                        "message": f"启动模型评估：{mode}，初审={selection['flashProvider']}/{selection['flashModel']}，"
                                   f"精读={selection['proProvider']}/{selection['proModel']}"})
    if uses_mock:
        report = _mock_report(job, ordered, signals, mode)
        updated = await update({"evaluationStatus": "completed", "evaluationProgress": "Mock 评估完成", "evaluationReport": report})
        return {"job": updated, "evaluationReport": report}

    configuration = _llm_options(job.get("llmConfig"))
    default_provider = configuration.get("provider")
    timeout = max(60_000, _number(timeout_ms) or 330_000)
    retries = _setting(max_retries, configuration.get("max_retries"), 4)
    stored_batches = {item["batchRange"] for item in checkpoint["batchResults"]}

    def request(kind, prompt):
        provider, model = selection[f"{kind}Provider"], selection[f"{kind}Model"]
        same_provider = provider == default_provider
        settings = {
            "provider": provider, "model": model,
            "use_saved_connection": _setting(selection.get(f"useSaved{kind.title()}Connection"),
                                             configuration.get("use_saved_connection"), False),
            "base_url": (base_url or configuration.get("base_url") or "") if same_provider else "",
            "api_key": (api_key or configuration.get("api_key") or "") if same_provider else "",
            "temperature": 0.2 if kind == "flash" else 0.25,
            "max_tokens": flash_max_tokens if kind == "flash" else DEFAULT_MAX_TOKENS,
            "timeout_ms": timeout if kind == "flash" else max(timeout, 360_000), "max_retries": retries,
        }
        return {**settings, **prompt}

    async def save_progress(fields, message):
        nonlocal checkpoint
        next_checkpoint = {**checkpoint, **fields, "updatedAt": _now()}
        await update({"evaluationCheckpoint": next_checkpoint, "evaluationProgress": message})
        checkpoint = next_checkpoint

    async def notify_progress(message):
        await _notify(on_progress, message)
        await update({"evaluationProgress": message})

    async def save_batch(batch, label):
        batches = [*checkpoint["batchResults"], batch]
        stage = "flash" if len(batches) < checkpoint["totalBatches"] else "finalizing" if mode == "flash_only" else "pro"
        await save_progress(
            {"batchResults": batches, "stage": stage, "flashModels": list(dict.fromkeys([*checkpoint["flashModels"], label]))},
            f"已保存初审第 {len(batches)}/{checkpoint['totalBatches']} 批",
        )

    if mode != "pro_only":
        for index, offset in enumerate(range(0, len(ordered), batch_size), 1):
            batch = ordered[offset:offset + batch_size]
            span = _chapter_range(batch)
            if span in stored_batches:
                continue
            if reused:
                await save_batch(_chapter_review_batch(batch), "章节审校记录")
                continue
            start, end = [_chapter_number(chapter) for chapter in (batch[0], batch[-1])]
            total = checkpoint["totalBatches"]
            await notify_progress(f"Stage 1 初审进度 ({index}/{total})：正在用 {selection['flashModel']} 扫描第 {start}~{end} 章...")
            prompt = _build_batch_review(job, batch, _signal_hint(signals, start, end))
            result = await _checked_review(
                request("flash", prompt), lambda value: _valid_batch(value, span),
                f"初审第 {index}/{total} 批（{start}–{end} 章）", notify_progress,
                "简写时间线和处境变化，问题优先 P0/P1，每处引文最多 60 字。", store=store, client=client,
            )
            await save_batch({**result, "batchRange": span}, f"{selection['flashProvider']}/{selection['flashModel']}")
            if offset + batch_size < len(ordered):
                await asyncio.sleep(0.350)
        if reused:
            await _notify(on_progress, f"已复用全书 {len(ordered)} 章审校记录，跳过重复初审扫描，直接进入精读汇总...")

    batches = sorted(checkpoint["batchResults"], key=lambda batch: _number(batch["batchRange"].split("-")[0]))
    pro_review = checkpoint.get("proReview")
    if mode != "flash_only" and not pro_review:
        await notify_progress(f"Stage 2 精读复审：正在用 {selection['proModel']} 通盘审读前10章+关键转折+结尾及全部初审台账...")
        pro_review = await _checked_review(
            request("pro", _build_pro_review(job, ordered, batches, signals)), _valid_pro_review,
            "精读", notify_progress, "保留六维评分，每条说明精简为一句，优先给出关键 P0/P1 问题。", store=store, client=client,
        )
        await save_progress(
            {"proReview": pro_review, "proUsed": f"{selection['proProvider']}/{selection['proModel']}", "stage": "finalizing"},
            "已保存精读结果，正在汇总报告…",
        )
    pro_review = _dict(pro_review)
    issues = _merge_issues(batches, _list(pro_review.get("aiFlavorIssues")))
    report = {
        "evaluatedAt": _now(), "sourceHash": checkpoint["sourceHash"], "batchSize": batch_size, "mode": mode,
        "models": {"flash": None if mode == "pro_only" else "、".join(checkpoint["flashModels"]) or None,
                   "pro": None if mode == "flash_only" else checkpoint.get("proUsed")},
        "bookTitle": job.get("title"), "totalChapters": len(ordered), "totalWords": job.get("totalWords", 0),
        "deterministicSignals": signals, "overallSummary": pro_review.get("overallSummary") or
        f"已完成全书 {len(ordered)} 章分批 AI 味初审（共检出 {len(issues)} 条具体建议）。",
        "readiness": pro_review.get("readiness") or "revise_recommended",
        "topVerdicts": pro_review.get("topVerdicts"), "blindReaderFirst3Chapters": pro_review.get("blindReaderFirst3Chapters"),
        "sixDimensions": pro_review.get("sixDimensions"),
        "batchSummaries": [{
            "batchRange": batch["batchRange"], "timelineSummary": batch.get("timelineSummary") or "",
            "situationDelta": batch.get("situationDelta") or "", "endingHookSummary": batch.get("endingHookSummary") or [],
            "extractedSettings": batch.get("extractedSettings") or [],
        } for batch in batches],
        "aiFlavorIssues": issues, "rewritePlan": pro_review.get("rewritePlan") or [],
    }
    updated = await update({
        "evaluationStatus": "completed", "evaluationProgress": f"评估完成：共检出 {len(issues)} 条 AI 味/设定改进项",
        "evaluationReport": report, "evaluationCheckpoint": None,
    })
    p0 = sum(issue["priority"] == "P0" for issue in issues)
    p1 = sum(issue["priority"] == "P1" for issue in issues)
    await _notify(log, {"level": "info", "stage": "evaluation",
                        "message": f"✅ 模型评估完成：共定位 {len(issues)} 条具体问题（P0: {p0}，P1: {p1}）"})
    return {"job": updated, "evaluationReport": report}


def _surgical_prompt(chapter, issues, pov, style_guide):
    listing = "\n".join(
        f"{index}. [{issue.get('priority') or 'P1'}][{issue.get('category', 'undefined')}] 原句：「{issue.get('quote') or ''}」\n"
        f"   问题：{issue.get('impact') or ''}\n   改法：{issue.get('suggestion') or ''}"
        for index, issue in enumerate(issues, 1)
    ) or "请按六大 AI 味病灶通篇排查：删除写完又解释、替换重复身体反应套话、拉开角色说话腔调差异、避免套路化结尾。"
    return {
        "system": _SURGICAL_SYSTEM.substitute(pov=pov or "第三人称限知", style_guide=style_guide or "番茄爽文"),
        "prompt": _SURGICAL_PROMPT.substitute(
            chapter_number=chapter["chapterNumber"], title=chapter.get("title") or "",
            chapter_text=str(chapter.get("content") or "").strip(), issues=listing,
        ),
    }


async def surgical_de_ai_chapter(chapter, *, issues=(), pov=None, style_guide=None, llm_config=None, store=None, client=None):
    config = _llm_options(llm_config)
    original = str(chapter.get("content") or "")
    if config.get("provider", "mock") == "mock":
        rewritten = re.sub(r"不仅如此，|总而言之，|空气仿佛凝固了一瞬，", "", original).strip()
    else:
        request = {**config, **_surgical_prompt(chapter, issues, pov, style_guide),
                   "max_tokens": DEFAULT_MAX_TOKENS, "temperature": 0.45}
        reply = await call_llm(**request, store=store, client=client)
        if output_was_truncated(reply.get("finishReason")):
            raise EvaluationError("精修输出被截断，已保留原稿。", "OUTPUT_TRUNCATED", 502)
        rewritten = re.sub(r"\*\*(.*?)\*\*", r"\1", str(reply.get("text") or "")).strip()
    before_words = count_chinese_words(original)
    after_words = count_chinese_words(rewritten)
    if not rewritten or after_words < max(200, before_words * 0.4):
        raise EvaluationError(
            f"第 {chapter['chapterNumber']} 章去 AI 味输出异常偏短（{after_words} 字），已中止覆盖以保护原稿",
            "DE_AI_TOO_SHORT", 502,
        )
    return {"content": rewritten, "beforeWords": before_words, "afterWords": after_words}


def _issue_identity(issue):
    return tuple(issue.get(field) for field in ("id", "chapterNumber", "category", "quote"))


def _mark_resolved(latest, original_report, addressed, total_words=None):
    active = _dict(latest.get("evaluationReport"))
    if not active or any(active.get(key) != original_report.get(key) for key in ("evaluatedAt", "sourceHash", "mode")):
        return {}
    issues = [
        {**item, "resolved": True} if isinstance(item, dict) and _issue_identity(item) in addressed else item
        for item in _list(active.get("aiFlavorIssues"))
    ]
    return {"evaluationReport": {
        **active, "aiFlavorIssues": issues, "lastDeAiAt": _now(),
        "totalWords": latest.get("totalWords", 0) if total_words is None else total_words,
    }}


async def de_ai_rewrite_chapters(store, job_id, *, chapter_numbers=None, priorities=None, categories=None,
                                 provider=None, model=None, use_saved_connection=None, timeout_ms=None,
                                 max_retries=None, client=None):
    job, chapters = await asyncio.to_thread(_load_job_and_chapters, store, job_id)
    by_number = {_chapter_number(chapter): chapter for chapter in chapters}
    report = _dict(job.get("evaluationReport"))
    issues = [issue for issue in _list(report.get("aiFlavorIssues")) if isinstance(issue, dict)]
    selected_priorities = set(priorities or ("P0", "P1"))
    selected_categories = set(categories or [])
    targets = list(dict.fromkeys(_number(value, None) for value in chapter_numbers or []))
    targets = [target for target in targets if target is not None]
    if not targets:
        targets = sorted({_chapter_number(issue) for issue in issues
                          if not issue.get("resolved") and issue.get("priority") in selected_priorities
                          and (not selected_categories or issue.get("category") in selected_categories)})
    if not targets:
        raise EvaluationError("没有符合条件的待精修章节（请先运行评估或手动指定章号）", "NO_DE_AI_CHAPTERS", 400)
    configuration = _llm_options(job.get("llmConfig"))
    provider = provider or configuration.get("provider") or "vertex"
    same_provider = provider == configuration.get("provider")
    config = {
        "provider": provider, "model": model or ("mock-novel-v1" if provider == "mock" else "gemini-3.8-flash"),
        "timeout_ms": max(60_000, _number(timeout_ms) or 300_000),
        "max_retries": _setting(max_retries, configuration.get("max_retries"), 4),
        "use_saved_connection": _setting(use_saved_connection, configuration.get("use_saved_connection"), False),
        "base_url": configuration.get("base_url") or "" if same_provider else "",
        "api_key": configuration.get("api_key") or "" if same_provider else "",
    }
    updated = []
    for chapter_number in targets:
        chapter = by_number.get(chapter_number)
        if chapter is None:
            continue
        chapter_issues = [issue for issue in issues if _chapter_number(issue) == chapter_number and not issue.get("resolved")
                          and (not selected_categories or issue.get("category") in selected_categories)]
        change = await surgical_de_ai_chapter(chapter, issues=chapter_issues, pov=job.get("pov"),
                                              style_guide=job.get("styleGuide"), llm_config=config, store=store, client=client)
        current = await asyncio.to_thread(store.get_chapter, job_id, chapter_number)
        if current != chapter:
            raise EvaluationError(f"第 {chapter_number} 章在精修期间已更新，已保留当前正文。", "DE_AI_SOURCE_CHANGED")
        saved = await _db_write(store.save_chapter, job_id, chapter_number, {
            **chapter, "content": change["content"], "wordCount": change["afterWords"],
            "status": "needs_review" if chapter.get("status") == "needs_review" else "repaired",
            "repairCount": (chapter.get("repairCount") or 0) + 1,
        })
        updated.append({"chapterNumber": chapter_number, "title": saved["title"], "beforeWords": change["beforeWords"],
                        "afterWords": change["afterWords"], "addressedIssues": len(chapter_issues)})
        if report:
            addressed = [_issue_identity(issue) for issue in chapter_issues]
            await _db_write(store.update_job, job_id, lambda latest: _mark_resolved(latest, report, addressed))
        if provider != "mock":
            await asyncio.sleep(0.300)
    all_chapters = await asyncio.to_thread(store.get_chapters, job_id)
    total = sum(chapter.get("wordCount") or count_chinese_words(chapter.get("content") or "") for chapter in all_chapters)
    result = await _db_write(
        store.update_job, job_id,
        lambda latest: {"totalWords": total, **(_mark_resolved(latest, report, (), total_words=total) if updated else {})},
    )
    descriptions = "、".join(f"第{chapter['chapterNumber']}章" for chapter in updated)
    await _db_write(store.append_job_log, job_id, {
        "level": "info", "stage": "de_ai", "message": f"✨ 完成定点去 AI 味精修：已处理 {descriptions}，当前全书 {total} 字",
    })
    return {"job": result, "updatedChapters": updated, "evaluationReport": result.get("evaluationReport")}
