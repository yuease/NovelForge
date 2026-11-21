"""Local chapter checks and evidence-backed review decisions."""

import re
import unicodedata
from itertools import combinations

from .storage import count_chinese_words
from .text_evidence import locate_quote as _locate_quote, normalize_quote as _normalize_quote


_SPACE = re.compile(r"[\u0009-\u000d\u0020\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+")
_PSYCHOLOGICAL_LEAD = re.compile(r"(?:不禁(?:想到|想起|觉得)|心(?:里|中)(?:明白|知道|觉得|暗想|暗道)|(?:他|她|我)意识到|脑海中闪过)")
_QUOTED_DIALOGUE = re.compile(r'“[^”]*”|「[^」]*」|『[^』]*』|"[^"]*"')
_STYLE_DIALOGUE = re.compile(_QUOTED_DIALOGUE.pattern + r"|‘[^’]*’|＂[^＂]*＂")
_FILLER = re.compile(r"瞬间|骤然|死寂|仿佛|一丝(?!不苟|不挂|一毫)|一抹(?!黑|布)")
_MOUTH_ARC = re.compile(r"(?:嘴角|唇角)[^。！？.!?；;\r\n]{0,10}?(?:勾起|泛起|扯出|咧开|扬起|浮现)")
_EXPLAINING = re.compile(
    r"(?:这番话|这句话)看似[^。！？!?；;\r\n]{2,40}?(?:实则|实际上)"
    r"|(?:^|(?<=[。！？!?；;\n]))[ \t]*说白了[，,]?就是"
)
_RHETORICAL_RECAP = re.compile(
    r"(?:这甚至不是|这不仅是)[^。！？!?；;\r\n]{2,36}?(?:更是|而是)"
    r"|打的正是[^。！？!?；;\r\n]{2,30}?算盘"
)
_PAREN_NOTE = re.compile(r"（[^（）\r\n]+）|\([^()\r\n]+\)")
_ACCOUNT_TERMS = re.compile(r"余额|进账|到账|入账|转账|扣除|支出|累计|合计|结算|款项|欠款|本金|明细")
_AMOUNT = re.compile(
    r"(?:[0-9]+(?:,[0-9]{3})*(?:\.[0-9]+)?|[零〇一二两三四五六七八九十百千万亿点]+)\s*"
    r"(?:(?P<currency>(?:(?:万亿|万|亿)\s*)?(?:元|块钱|人民币))|(?:万亿|万|亿)(?![个名人米公里秒次天点分岁本件步]))"
)
_LATE_TURN = re.compile(
    r"(?:(?:然而|可|但)[，,]?[ \t]*)?(?:就在|还没等|话音刚落)"
    r"[^。！？!?\r\n]{0,24}?(?:瞬间|刹那|之际|平息|收敛|话音未落)"
)
_RESOLUTION = re.compile(r"尘埃终于落定|一切归于平静|事情就此了结")
_SYSTEM_CLOSE = re.compile(r"(【[^【】\r\n]{1,120}】)[。！？!?…\s]*\Z")
_SYSTEM_MARKER = re.compile(
    r"^【[ \t]*(?:(?:系统(?:提示|警报|警告|公告|通知|弹窗)?|警报|警告|任务更新|倒计时)"
    r"(?=[:：\s】！!]|提示|触发|响起)|倒计时(?=\d))"
)
_MOTIF_NOISE = r"然而|就在|还没等|话音刚落|他们|我们|这个|那个|忽然|突然|只见|于是|仿佛|终于|[的了着在向从与被把和又也]"
_CJK_PHRASE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]{4,}")
_SOUND = re.compile(r"咚[、，,\s]?咚|砰[、，,\s]?砰|哐当|咔嚓|嘎吱|叮咚|滴[、，,\s]?滴")
_ENDING_WINDOW = 380
_MOTIF_WINDOW = 150
_PLACEHOLDERS = (
    re.compile(r"【待补充(?:[^】\n]*】)?"),
    re.compile(r"【此处[^】]*略[^】]*】"),
    re.compile(r"\[TODO[^\]]*\]", re.I | re.ASCII),
    re.compile(r"\bTBD\b", re.ASCII),
    re.compile(r"\bplaceholder\b", re.I | re.ASCII),
    re.compile(r"（待完善[^）]*）"),
    re.compile(r"（此处省略[^）]*）"),
)
_NON_FATAL_STYLE_RULES = {
    "dash_density", "psychological_lead_density", "repeated_subject", "single_paragraph",
    "cliche_filler_density", "mouth_arc_cliche", "explanatory_narration", "repetitive_ending_hook",
}
_NON_FATAL_AI_CATEGORIES = {"over_explaining", "formulaic_reaction", "shared_voice", "static_situation", "repetitive_hooks"}


def _trim(text):
    return re.sub(rf"^{_SPACE.pattern}|{_SPACE.pattern}$", "", text)


def _utf16_length(text):
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


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


def _narration(text, start=0):
    """Yield unquoted prose and its offset in the unmodified chapter."""
    cursor = start
    for quote in _STYLE_DIALOGUE.finditer(text):
        if quote.end() <= start:
            continue
        if quote.start() > cursor:
            yield cursor, text[cursor:quote.start()]
        cursor = quote.end()
    if cursor < len(text):
        yield cursor, text[cursor:]


def _around_match(prose, match):
    left = re.split(r"[。！？!?\n]", prose[max(0, match.start() - 24):match.start()])[-1]
    right = re.split(r"[。！？!?\n]", prose[match.end():match.end() + 34], maxsplit=1)[0]
    return left + match[0] + right


def _account_recap(prose):
    for note in _PAREN_NOTE.finditer(prose):
        if count_chinese_words(note[0][1:-1]) < 45:
            continue
        body = unicodedata.normalize("NFKC", note[0])
        markers = set(_ACCOUNT_TERMS.findall(body))
        amounts = list(_AMOUNT.finditer(body))
        currency = sum(match["currency"] is not None for match in amounts)
        if currency >= 3 or len(markers) >= 2 and len(amounts) >= 2:
            return note[0]
    return None


def _late_turn(text, tail):
    for offset, prose in reversed(tail):
        for match in reversed(list(_LATE_TURN.finditer(prose))):
            rest = text[offset + match.end():].strip()
            if len(rest) <= 100 and len(re.findall(r"[。！？!?]+", rest)) <= 2 and not _RESOLUTION.search(rest):
                return _around_match(prose, match)
    return None


def _system_close(text, tail):
    if tail:
        offset, prose = tail[-1]
        if offset + len(prose) == len(text) and (match := _SYSTEM_CLOSE.search(prose)):
            if _SYSTEM_MARKER.match(match[1]):
                return match[1]
    return None


def _motif_parts(tail, start, noise):
    anchors, sounds = [], []
    for offset, prose in tail:
        skip = max(0, start - offset)
        prose = prose[skip:]
        offset += skip
        for match in _SOUND.finditer(prose):
            sounds.append((re.sub(r"[、，,\s]", "", match[0]), offset + match.start(), offset + match.end()))
        masked = noise.sub(lambda match: " " * len(match[0]), prose)
        masked = _SOUND.sub(lambda match: " " * len(match[0]), masked)
        for run in _CJK_PHRASE.finditer(masked):
            for index in range(run.start(), run.end() - 3):
                key = masked[index:index + 4]
                if len(set(key)) > 1:
                    anchors.append((key, offset + index, offset + index + 4))
    return anchors, sounds


def _motif_quote(chapter, tail, history, names):
    names = [re.escape(name) for name in names if len(name) >= 2]
    noise = re.compile("|".join([*names, _MOTIF_NOISE]))
    anchors, sounds = _motif_parts(tail, max(0, len(chapter) - _MOTIF_WINDOW), noise)
    prior = []
    for text, prose in history:
        words, noises = _motif_parts(prose, max(0, len(text) - _MOTIF_WINDOW), noise)
        prior.append(({key for key, _, _ in words}, {key for key, _, _ in noises}))
    for (first, first_sounds), (second, second_sounds) in combinations(prior, 2):
        common = first & second
        spans = []
        for key, start, end in anchors:
            if key not in common:
                continue
            if spans and start <= spans[-1][1]:
                spans[-1] = spans[-1][0], end
            else:
                spans.append((start, end))
        if not spans:
            continue
        start, end = spans[0]
        if other := next((part for part in spans[1:] if chapter[part[0]:part[1]] != chapter[start:end]), None):
            return chapter[start:other[1]]
        if sound := next((part for part in sounds if part[0] in first_sounds and part[0] in second_sounds), None):
            return chapter[min(start, sound[1]):max(end, sound[2])]
    return None


def _check_new_style(text, word_count, recent_chapters, names, add):
    prose_segments = [prose for _, prose in _narration(text)]
    fillers = [(prose, match) for prose in prose_segments for match in _FILLER.finditer(prose)]
    instant = sum(match[0] == "瞬间" for _, match in fillers)
    if instant >= 6 or len(fillers) >= 12 and word_count > 0 and len(fillers) * 1000 >= word_count * 4.5:
        prose, match = next((hit for hit in fillers if hit[1][0] == "瞬间"), fillers[0])
        add("cliche_filler_density", _around_match(prose, match),
            f"叙述中填充词出现 {len(fillers)} 次，其中「瞬间」{instant} 次。",
            "保留需要强调的时点，其余用具体动作或直接转场表达。")

    history = [entry if isinstance(entry, str) else "" for entry in (recent_chapters or ())[-3:]]
    history = [entry.strip() for entry in history]
    mouths = [(prose, match) for prose in prose_segments for match in _MOUTH_ARC.finditer(prose)]
    if mouths:
        count = len(mouths)
        combined = count + sum(len(_MOUTH_ARC.findall(prose)) for old in history for _, prose in _narration(old))
        if count >= 2 or sum(bool(entry) for entry in history) >= 2 and combined >= 3:
            add("mouth_arc_cliche", _around_match(*mouths[0]),
                f"本章出现 {count} 处嘴角套式，连同近章共 {combined} 处。",
                "保留必要表情，其余改为与人物立场相符的动作、对话或沉默。")

    strong = next(((prose, match) for prose in prose_segments if (match := _EXPLAINING.search(prose))), None)
    recaps = [(prose, match) for prose in prose_segments for match in _RHETORICAL_RECAP.finditer(prose)]
    if strong or len(recaps) >= 2:
        add("explanatory_narration", _around_match(*(strong or recaps[0])),
            "旁白出现解释性复述或抽象总结。", "删去重复翻译，保留读者尚未获知的信息并继续推进情节。")
    elif note := next((note for prose in prose_segments if (note := _account_recap(prose))), None):
        add("explanatory_narration", note, "长括号内集中复述多笔账务。",
            "保留关键数值、余额或因果，把必要细目交给自然动作或证据。")

    if not any(history):
        return
    text = text.rstrip()
    tail = list(_narration(text, max(0, len(text) - _ENDING_WINDOW)))
    prior = [(old, list(_narration(old, max(0, len(old) - _ENDING_WINDOW)))) for old in history]
    previous_turns = [_late_turn(old, ending) for old, ending in prior]
    if (quote := _late_turn(text, tail)) and (previous_turns[-1] or sum(bool(item) for item in previous_turns) >= 2):
        reason = "相邻或近期章尾复用了同构突转句式。"
    elif (quote := _system_close(text, tail)) and len(prior) >= 2 and all(
        _system_close(old, ending) for old, ending in prior[-2:]
    ):
        reason = "连续三章以系统弹窗或警报收尾。"
    elif quote := _motif_quote(text, tail, prior, names):
        reason = "本章与近期至少两章复用同一组章尾意象。"
    else:
        return
    add("repetitive_ending_hook", quote, reason, "改用本章独有的行动后果、人物决定或新信息收尾。")


def find_chapter_style_issues(chapter_text, canonical_facts, word_count, *, recent_chapters=None):
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
    _check_new_style(chapter_text, word_count, recent_chapters, names, add)
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
