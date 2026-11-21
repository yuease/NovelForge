"""Local chapter checks and evidence-backed review decisions."""

import re
import unicodedata

from .storage import count_chinese_words


_SPACE = re.compile(r"[\u0009-\u000d\u0020\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+")
_PSYCHOLOGICAL_LEAD = re.compile(r"(?:不禁(?:想到|想起|觉得)|心(?:里|中)(?:明白|知道|觉得|暗想|暗道)|(?:他|她|我)意识到|脑海中闪过)")
_QUOTED_DIALOGUE = re.compile(r'“[^”]*”|「[^」]*」|『[^』]*』|"[^"]*"')
_COMMON_PUNCTUATION = str.maketrans({"。": ".", "、": ",", "“": '"', "”": '"', "‘": "'", "’": "'"})
_ELLIPSIS = re.compile(r"…+|\.{3,}|．{3,}")
_PLACEHOLDERS = (
    re.compile(r"【待补充(?:[^】\n]*】)?"),
    re.compile(r"【此处[^】]*略[^】]*】"),
    re.compile(r"\[TODO[^\]]*\]", re.I | re.ASCII),
    re.compile(r"\bTBD\b", re.ASCII),
    re.compile(r"\bplaceholder\b", re.I | re.ASCII),
    re.compile(r"（待完善[^）]*）"),
    re.compile(r"（此处省略[^）]*）"),
)
_NON_FATAL_STYLE_RULES = {"dash_density", "psychological_lead_density", "repeated_subject", "single_paragraph"}
_NON_FATAL_AI_CATEGORIES = {"over_explaining", "formulaic_reaction", "shared_voice", "static_situation", "repetitive_hooks"}


def _trim(text):
    return re.sub(rf"^{_SPACE.pattern}|{_SPACE.pattern}$", "", text)


def _utf16_length(text):
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def _meaningful_count(text):
    return sum(unicodedata.category(char)[0] in "LN" for char in text)


def _normalize_quote(value):
    return _SPACE.sub("", unicodedata.normalize("NFKC", value)).translate(_COMMON_PUNCTUATION)


def _locate_quote(quote, source):
    quote = _trim(quote)
    wrappers = {"“": "”", "「": "」", "『": "』", '"': '"'}
    if len(quote) >= 2 and quote[0] in wrappers and quote[-1] == wrappers[quote[0]]:
        quote = quote[1:-1]
    exact = _normalize_quote(quote)
    if _meaningful_count(exact) < 4:
        return None
    if exact in source:
        return exact

    parts = [_normalize_quote(part).strip(",.;:!?") for part in _ELLIPSIS.split(quote)]
    parts = [part for part in parts if part]
    if not _ELLIPSIS.search(quote) or not parts or any(_meaningful_count(part) < 2 for part in parts):
        return None
    start = source.find(parts[0])
    while start >= 0:
        cursor = start + len(parts[0])
        for part in parts[1:]:
            next_at = source.find(part, cursor)
            if next_at < 0 or next_at - cursor > 320 or _utf16_length(source[cursor:next_at]) > 320:
                break
            cursor = next_at + len(part)
        else:
            return source[start:cursor]
        start = source.find(parts[0], start + 1)
    return None


def verified_ai_blockers(ai_issues, chapter_text):
    source = _normalize_quote(chapter_text)
    seen = set()
    blockers = []
    for issue in ai_issues or ():
        if not isinstance(issue, dict) or issue.get("priority") not in ("P0", "P1"):
            continue
        quote = issue.get("quote")
        passage = _locate_quote(quote, source) if isinstance(quote, str) else None
        if not passage or passage in seen:
            continue
        seen.add(passage)
        blockers.append({
            "type": "ai_flavor", "severity": "error", "priority": issue["priority"],
            **({"category": issue["category"]} if "category" in issue else {}),
            "message": issue.get("impact") or issue.get("suggestion") or "审校发现需在本章修复的 AI 味问题。",
            "quote": quote, **({"suggestion": issue["suggestion"]} if "suggestion" in issue else {}),
        })
    return blockers


def _issue_quote(text):
    return text.encode("utf-16-le", "surrogatepass")[:320].decode("utf-16-le", errors="ignore")


def find_chapter_style_issues(chapter_text, canonical_facts, word_count):
    paragraphs = [part for part in re.split(r"\n+", _trim(chapter_text)) if _trim(part)]
    sequence = []
    for prose in _QUOTED_DIALOGUE.split(chapter_text):
        sequence.extend(text for sentence in re.split(r"[。！？!?\n]+", prose) if (text := _trim(sentence)))
        sequence.append("")
    sentences = [sentence for sentence in sequence if sentence]
    issues = []

    def add(rule, quote, message, suggestion):
        issues.append({"type": "ai_flavor", "severity": "error", "rule": rule,
                       "quote": _issue_quote(quote), "message": message, "suggestion": suggestion})

    def check_density(pattern, minimum, rule, message, suggestion):
        hits = [(sentence, len(matches)) for sentence in sentences if (matches := pattern.findall(sentence))]
        if sum(count for _, count in hits) >= minimum and len(hits) * 4 >= len(sentences):
            add(rule, hits[0][0], message, suggestion)

    check_density(re.compile(r"—+|－{2,}"), 8, "dash_density", "叙述中破折号过密。", "按语义改用句号、逗号或分句。")
    check_density(_PSYCHOLOGICAL_LEAD, 6, "psychological_lead_density", "反复用套语引出心理描写。",
                  "保留必要心理信息，其余用行动、对白或处境变化表达。")
    names = sorted(filter(None, (canonical_facts or {}).get("characters") or {}), key=_utf16_length, reverse=True)
    previous = ""
    streak = 0
    for sentence in sequence:
        subject = next((name for name in names if sentence.startswith(name)), "")
        if not subject and (pronoun := re.match(r"^[我他她](?!们)", sentence)):
            subject = pronoun[0]
        streak = (streak + 1 if subject == previous else 1) if subject else 0
        previous = subject
        if streak == 5:
            add("repeated_subject", sentence, "连续叙述句以同一主语起笔。", "按语义调整句首和句式，保留人物行动。")
            break
    if len(paragraphs) == 1 and word_count >= 800 and any(
        unicodedata.category(char).startswith("L") for sentence in sentences for char in sentence
    ):
        add("single_paragraph", paragraphs[0], "长篇正文挤在同一个自然段。", "按说话人、动作转换或场景节奏分段。")
    return issues


def _chapter_value(value, missing=None):
    if value is None:
        return missing
    try:
        number = float(value or 0)
        return int(number) if number.is_integer() else number
    except (TypeError, ValueError, OverflowError):
        return missing


def run_deterministic_checks(*, chapter_number, chapter_text="", canonical_facts=None,
                             pov="第三人称限知", min_words=300, max_words=10000):
    text = str(chapter_text or "")
    facts = canonical_facts or {}
    word_count = count_chinese_words(text)
    issues = []

    def add(kind, message, severity="error", **fields):
        issues.append({"type": kind, "severity": severity, "message": message, **fields})

    if not _trim(text):
        add("empty_chapter", f"第{chapter_number}章正文为空")
        return {"issues": issues, "wordCount": word_count}
    for pattern in _PLACEHOLDERS:
        if match := pattern.search(text):
            add("placeholder", f'第{chapter_number}章包含未完成的占位符文本: "{match[0]}"')
            break
    if min_words > 0 and word_count < min_words:
        add("word_count", f"第{chapter_number}章字数过少（当前 {word_count} 字，低于下限 {min_words} 字）")
    elif max_words > 0 and word_count > max_words:
        add("word_count", f"第{chapter_number}章字数超出上限（当前 {word_count} 字，上限 {max_words} 字）", "warning")

    for injury in facts.get("injuries") or []:
        if not isinstance(injury, dict) or injury.get("active") is False:
            continue
        source = _chapter_value(injury.get("sourceChapter"), 0 if "sourceChapter" in injury else None)
        desc = str(injury.get("description") or "")
        if source is None or not source < chapter_number:
            continue
        limb = re.search(r"左臂|右臂|左手|右手|左腿|右腿|双腿|胸口", desc)
        if limb and re.search(rf"完好无损的{limb[0]}|毫发无伤的{limb[0]}", text):
            name = injury.get("character", "undefined")
            add("injury_contradiction", f"第{chapter_number}章伤势矛盾：{name}在第{injury.get('sourceChapter')}章记录“{desc}”尚未痊愈，本章却出现“完好无损的{limb[0]}”",
                sourceChapter=source, character=name)

    for entry in facts.get("secrets") or []:
        if not isinstance(entry, dict) or not entry.get("secret"):
            continue
        source = _chapter_value(entry.get("sourceChapter"), 0 if "sourceChapter" in entry else None)
        if source is not None and source >= chapter_number:
            continue
        secret = entry["secret"]
        clue = secret.encode("utf-16-le", "surrogatepass")[:12].decode("utf-16-le", errors="ignore")
        for name in entry.get("excludedCharacters") or []:
            if not name:
                continue
            pattern = rf"{re.escape(name)}[^。！？\n]{{0,20}}(?:早已知道|早知|道破了|揭穿了)[^。！？\n]{{0,25}}{re.escape(clue)}"
            if re.search(pattern, text):
                mark = entry.get("sourceChapter", "undefined")
                add("secret_leak", f"第{chapter_number}章秘密知情边界越界：{name}在第{mark}章被标记为不知情（{secret}），本章却无铺垫知晓该秘密",
                    sourceChapter=source, character=name)

    if "第一人称" in pov:
        for name in facts.get("characters") or {}:
            if name and name != "我" and re.search(rf"{re.escape(name)}(?:暗自心想|心中暗道|内心独白)", text):
                add("pov_violation", f"第{chapter_number}章视角越界：第一人称视角下直接描写了「{name}」的不可见内心活动",
                    "warning")
    return {"issues": issues, "wordCount": word_count}


def can_save_unverified_draft(chapter_text, verification, **check_options):
    if not isinstance(verification, dict) or verification.get("passed") is not False:
        return False
    issues = verification.get("issues")
    ai_issues = verification.get("aiIssues", [])
    if not isinstance(issues, list) or not all(isinstance(issue, dict) for issue in issues):
        return False
    if not isinstance(ai_issues, list) or not all(isinstance(issue, dict) for issue in ai_issues):
        return False
    local = run_deterministic_checks(chapter_text=chapter_text, **check_options)
    if any(issue["severity"] == "error" for issue in local["issues"]):
        return False
    if verified_ai_blockers([issue for issue in ai_issues if issue.get("priority") == "P0"], chapter_text):
        return False
    blocking = [issue for issue in issues if issue.get("severity") != "warning"]
    return bool(blocking) and all(
        issue.get("type") == "ai_flavor" and (
            issue.get("rule") in _NON_FATAL_STYLE_RULES
            or issue.get("priority") == "P1" and issue.get("category") in _NON_FATAL_AI_CATEGORIES
        )
        for issue in blocking
    )
