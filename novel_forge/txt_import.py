"""Parse user-supplied novel text without saving or modifying existing jobs."""

import re

from .storage import _ASCII_WORD, _CJK, count_chinese_words


_NUMBER = r"[0-9０-９零〇一二三四五六七八九十百千万萬亿億两兩壹贰貳叁參肆伍陆陸柒捌玖拾佰仟]+"
_HEADING_DIVIDERS = r"\s:：\-—·、.．《〈"
_NUMBERED = re.compile(rf"^(?:第\s*{_NUMBER}\s*[章回节節]|Chapter[ \t]+[0-9０-９]+)(?=$|[{_HEADING_DIVIDERS}])", re.I)
_NAMED = re.compile(rf"^(?:序章|楔子|引子|尾声|尾聲|终章|終章|番外(?:篇)?)(?:[ \t]*{_NUMBER})?(?=$|[{_HEADING_DIVIDERS}])")
_VOLUME = re.compile(rf"^(?:第\s*{_NUMBER}\s*卷|卷\s*{_NUMBER})(?=$|[{_HEADING_DIVIDERS}])")
_MARKDOWN = re.compile(r"^#{1,6}[ \t]*")
_BANNER = re.compile(r"^={3,}\s*(.*?)\s*={3,}$")
_WORD_COUNT = re.compile(r"[（(]\s*[0-9０-９,，]+\s*字\s*[)）]\s*$")
_SEPARATOR = re.compile(r"^(?:={3,}|-{3,}|[―—]{3,})$")
_TOC = re.compile(r"^(?:目\s*[录錄]|内容目录|章[节節]目录)[：:]?$")
_BOOK_NAME = re.compile(r"^(?:书名|書名|作品名)[：:]\s*(.+)$")
_BOOK_MARKDOWN = re.compile(r"^#[ \t]+(.+)$")
_METADATA = re.compile(r"^(?:作者|题材|題材|视角|視角|总字数|總字數|章节数|章節數|简介|簡介|标签|標籤|来源|來源)[：:]")
_EXPORT_META = re.compile(r"^题材：.* \| 视角：.* \| 总字数：.*$")
_PLAIN_TITLE_PUNCTUATION = re.compile(r"[。！？!?，,；;：“”\n]")
_SENTENCES = re.compile(r"(?<=[。！？!?；;])|(?<=\.)(?=\s)")
_WORD_SPAN = re.compile(f"{_CJK.pattern}|{_ASCII_WORD.pattern}")
_TITLE_PADDING = " \t:：-—·、.．"
_AUTO_CHUNK_THRESHOLD = 10_000


def _bracketed_title(value):
    value = value.strip()
    if value.startswith("《") and value.endswith("》"):
        return value[1:-1].strip()
    return value


def _chapter_heading(line):
    line = line.strip()
    if len(line) > 80:
        return None
    line = _MARKDOWN.sub("", line).strip()
    if banner := _BANNER.fullmatch(line):
        line = _WORD_COUNT.sub("", banner[1]).strip()
    if line.startswith("【") and "】" in line:
        prefix, rest = line[1:].split("】", 1)
        line = prefix.strip() + (" " + rest.strip() if rest.strip() else "")
    prefix = _NUMBERED.match(line)
    label = ""
    if prefix is None:
        prefix = _NAMED.match(line)
        if prefix is None:
            return None
        label = prefix[0].strip()
    subtitle = line[prefix.end():].strip(_TITLE_PADDING)
    while duplicate := _NUMBERED.match(subtitle):
        subtitle = subtitle[duplicate.end():].strip(_TITLE_PADDING)
    return label, _bracketed_title(subtitle)


def _volume_heading(line):
    line = line.strip()
    return len(line) <= 80 and bool(_VOLUME.match(_MARKDOWN.sub("", line)))


def _body_words(text):
    return count_chinese_words(text) if any(char.isalnum() for char in text) else 0


def _opening_metadata(lines, *, has_headings):
    title = ""
    has_toc = False
    for index, raw in enumerate(lines):
        line = raw.strip()
        if not line or _SEPARATOR.fullmatch(line):
            continue
        if _TOC.fullmatch(line):
            has_toc = True
            continue
        explicit = _BOOK_NAME.fullmatch(line) or _BOOK_MARKDOWN.fullmatch(line)
        if explicit or line.startswith("《") and line.endswith("》") and len(line) <= 80:
            title = title or _bracketed_title(explicit[1] if explicit else line)
            continue
        if _METADATA.match(line) or _volume_heading(line):
            continue
        if not title and len(line) <= 40 and not _PLAIN_TITLE_PUNCTUATION.search(line) and _body_words(line):
            title = line
            # A bare first line may be prose in an unstructured file. Keep it unless a
            # chapter heading gives us a structural boundary for the title.
            if has_headings:
                continue
        return title, lines[index:], has_toc
    return title, [], has_toc


def _chapter_content(lines, *, exported=False):
    kept = [line for line in lines if not _volume_heading(line) and not (exported and line.strip() == "-" * 30)]
    start, end = 0, len(kept)
    while start < end and (not kept[start].strip() or _SEPARATOR.fullmatch(kept[start].strip())):
        start += 1
    while end > start and (not kept[end - 1].strip() or _SEPARATOR.fullmatch(kept[end - 1].strip())):
        end -= 1
    return "\n".join(kept[start:end])


def _heading_chapters(lines, headings, opening, warnings, has_toc):
    chapters = []
    if _body_words(opening) >= 100:
        chapters.append(("序章", opening))
    skipped_empty = False
    for position, (start, (label, subtitle)) in enumerate(headings):
        end = headings[position + 1][0] if position + 1 < len(headings) else len(lines)
        content = _chapter_content(lines[start + 1:end])
        if not _body_words(content):
            skipped_empty = True
            continue
        title = label or f"第{len(chapters) + 1}章"
        chapters.append((f"{title} {subtitle}" if subtitle else title, content))
    if skipped_empty or has_toc:
        warnings.append("已跳过目录或没有正文的章标题。")
    return chapters


def _line_pieces(line, limit):
    if count_chinese_words(line) <= limit:
        yield line
        return
    for sentence in _SENTENCES.split(line):
        if count_chinese_words(sentence) <= limit:
            yield sentence
            continue
        # Last resort for an overlong sentence with no safe punctuation boundary.
        offset = 0
        for index, token in enumerate(_WORD_SPAN.finditer(sentence)):
            if index and index % limit == 0:
                yield sentence[offset:token.start()]
                offset = token.start()
        yield sentence[offset:]


def _chunks(content, limit):
    buffer = []
    words = 0
    for line in content.splitlines(keepends=True):
        for piece in _line_pieces(line, limit):
            piece_words = count_chinese_words(piece)
            if words and words + piece_words > limit:
                yield "".join(buffer).strip("\n")
                buffer.clear()
                words = 0
            buffer.append(piece)
            words += piece_words
    if buffer:
        yield "".join(buffer).strip("\n")


def parse_txt_manuscript(text: str, *, fallback_title: str = "", split_mode: str = "auto", chunk_words: int = 2500) -> dict:
    if not isinstance(text, str):
        raise ValueError("请提供有效的小说正文文本。")
    if split_mode not in ("auto", "single", "chunk"):
        raise ValueError("分章模式必须是 auto、single 或 chunk。")
    if type(chunk_words) is not int or chunk_words < 1:
        raise ValueError("每章目标字数必须是正整数。")
    if not isinstance(fallback_title, str):
        raise ValueError("作品标题必须是文本。")

    lines = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    headings = [(index, heading) for index, line in enumerate(lines) if (heading := _chapter_heading(line)) is not None]
    first_heading = headings[0][0] if headings else len(lines)
    title, opening, has_toc = _opening_metadata(lines[:first_heading], has_headings=bool(headings))
    raw_lines = [*opening, *lines[first_heading:]]
    exported = any(_EXPORT_META.fullmatch(line.strip()) for line in lines[:first_heading])
    warnings = []
    if split_mode == "auto" and headings:
        detected_by = "headings"
        units = _heading_chapters(lines, headings, _chapter_content(opening), warnings, has_toc)
    else:
        content = _chapter_content(raw_lines, exported=exported)
        valid_body = any(_body_words(line) for line in raw_lines if not _chapter_heading(line) and not _volume_heading(line))
        if not valid_body:
            raise ValueError("没有找到有效的小说正文。")
        if split_mode == "chunk" or split_mode == "auto" and _body_words(content) > _AUTO_CHUNK_THRESHOLD:
            detected_by = "chunk"
            if split_mode == "auto":
                warnings.append(f"未检测到章标题，已按每章约 {chunk_words} 字拆分正文。")
            units = [("", body) for body in _chunks(content, chunk_words)]
        else:
            detected_by = "single"
            units = [("第1章", content)]

    chapters = []
    for chapter_title, content in units:
        if words := _body_words(content):
            number = len(chapters) + 1
            chapters.append({"chapterNumber": number, "title": chapter_title or f"第{number}章", "content": content, "wordCount": words})
    if not chapters:
        raise ValueError("没有找到有效的小说正文。")
    return {
        "inferredTitle": title or fallback_title.strip() or "未命名作品",
        "chapters": chapters,
        "totalChapters": len(chapters),
        "totalWords": sum(chapter["wordCount"] for chapter in chapters),
        "warnings": warnings,
        "detectedBy": detected_by,
    }
