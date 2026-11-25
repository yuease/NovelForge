"""Lightweight read-only manuscript search and chapter reading tools for evaluators and verifiers."""

import re


SNIPPETS_PER_PAGE = 12
MAX_BATCH_QUERIES = 40
MAX_READ_BATCH = 6
DEFAULT_READ_CHARS = 5200


def _bounded_int(value, default, minimum, maximum):
    return min(max(value, minimum), maximum) if type(value) is int else default


def _clean_text(value, limit):
    return value[:limit] if isinstance(value, str) else ""


def search_chapters(book, *, query="", queries=None, use_regex=False, page=1, snippet_limit=SNIPPETS_PER_PAGE):
    """Search one keyword or a batch of keywords across chapter titles and bodies."""
    page = _bounded_int(page, 1, 1, 10_000)
    if isinstance(queries, list) and queries:
        terms = [_clean_text(item, 80).strip() for item in queries]
        terms = [term for term in dict.fromkeys(terms) if term][:MAX_BATCH_QUERIES]
        if not terms:
            return {"说明": "请提供非空关键词列表。"}, set()
        return _batch_search(book, terms, page, use_regex=bool(use_regex), snippet_limit=snippet_limit)
    single = _clean_text(query, 160).strip()
    if not single:
        return {"说明": "请提供 query 或 queries 关键词。"}, set()
    if use_regex:
        return _batch_search(book, [single], page, use_regex=True, snippet_limit=snippet_limit)
    return _single_search(book, single, page, snippet_limit=snippet_limit)


def _single_search(book, query, page, *, snippet_limit):
    counts = []
    snippets = []
    touched = set()
    start = (page - 1) * snippet_limit
    seen = 0
    for chapter, item in sorted(book.items()):
        for field in ("title", "content"):
            value = str(item.get(field) or "")
            count = value.count(query)
            if not count:
                continue
            counts.append((chapter, count))
            if seen + count > start and len(snippets) < snippet_limit:
                pos = 0
                for hit in range(min(count, start + snippet_limit - seen)):
                    idx = value.index(query, pos)
                    pos = idx + len(query)
                    if seen + hit >= start:
                        touched.add(chapter)
                        snippets.append({
                            "章": chapter,
                            "位置": "标题" if field == "title" else "正文",
                            "引文": value[max(idx - 65, 0):pos + 65],
                        })
            seen += count
    hit_chapters = sorted({ch for ch, _ in counts})
    return {
        "共出现": seen,
        "命中章数": len(hit_chapters),
        "命中章": hit_chapters,
        "页码": page,
        "命中片段": snippets,
    }, touched


def _batch_search(book, terms, page, *, use_regex, snippet_limit):
    compiled = []
    for term in terms:
        try:
            pattern = re.compile(term) if use_regex else re.compile(re.escape(term))
        except re.error:
            return {"说明": f"无效的正则表达式：{term}"}, set()
        compiled.append((term, pattern))
    per_term = {}
    matched_chapters = set()
    snippets = []
    touched = set()
    start = (page - 1) * snippet_limit
    seen = 0
    for chapter, item in sorted(book.items()):
        for field in ("title", "content"):
            value = str(item.get(field) or "")
            for term, pattern in compiled:
                hits = list(pattern.finditer(value))
                if not hits:
                    continue
                matched_chapters.add(chapter)
                entry = per_term.setdefault(term, {"次数": 0, "章": []})
                entry["次数"] += len(hits)
                if chapter not in entry["章"]:
                    entry["章"].append(chapter)
                for match in hits:
                    if seen >= start and len(snippets) < snippet_limit:
                        touched.add(chapter)
                        snippets.append({
                            "章": chapter,
                            "关键词": term,
                            "命中": match.group(0)[:40],
                            "位置": "标题" if field == "title" else "正文",
                            "引文": value[max(match.start() - 55, 0):match.end() + 55],
                        })
                    seen += 1
    return {
        "总命中次数": seen,
        "命中章数": len(matched_chapters),
        "命中章": sorted(matched_chapters),
        "按词统计": {k: v for k, v in sorted(per_term.items(), key=lambda pair: -pair[1]["次数"])},
        "页码": page,
        "命中片段": snippets,
    }, touched


def read_chapter_excerpt(book, chapter, *, offset=0, max_chars=DEFAULT_READ_CHARS):
    """Read a chapter title and slice of content."""
    if type(chapter) is not int or chapter not in book:
        return {"说明": f"第 {chapter} 章不存在。"}, set()
    item = book[chapter]
    content = str(item.get("content") or "")
    start = _bounded_int(offset, 0, 0, len(content))
    limit = _bounded_int(max_chars, DEFAULT_READ_CHARS, 400, 8000)
    return {
        "章": chapter,
        "标题": str(item.get("title") or ""),
        "正文": content[start:start + limit],
        "下一段起点": start + limit if len(content) > start + limit else None,
    }, {chapter}


def read_chapters_batch(book, chapters, *, query="", max_chars=1800):
    """Read up to MAX_READ_BATCH chapters, optionally centered around query."""
    if not isinstance(chapters, list) or not chapters:
        return {"说明": "请提供章号数组 chapters。"}, set()
    wanted = [ch for ch in dict.fromkeys(chapters) if type(ch) is int and ch in book][:MAX_READ_BATCH]
    if not wanted:
        return {"说明": "未找到有效章号。"}, set()
    limit = _bounded_int(max_chars, 1800, 600, 3200)
    needle = _clean_text(query, 120).strip()
    sections = []
    for ch in wanted:
        item = book[ch]
        body = str(item.get("content") or "")
        if needle and needle in body:
            pos = body.index(needle)
            begin = max(0, pos - limit // 2)
            excerpt = body[begin:begin + limit]
        else:
            excerpt = body[:limit]
        sections.append({
            "章": ch,
            "标题": str(item.get("title") or ""),
            "总字数": len(body),
            "摘录": excerpt,
        })
    return {"章节": sections}, set(wanted)


def readonly_tool_specs(versions=("current",)):
    """Build standard JSON Schema definitions for read-only manuscript verification tools."""
    version_list = list(versions)
    multi = len(version_list) > 1
    version_prop = (
        {"version": {"type": "string", "enum": version_list,
                     "description": f"要查的稿件版本，默认 {version_list[0]}。"}}
        if multi else {}
    )
    return [
        {
            "name": "search_manuscript",
            "description": "在全书标题与正文中检索关键词（支持单个 query 或 queries 批量扫描最多 40 词），用于核实前文伏笔、人物年龄/金额/器物是否已在别章交代或存在跨章冲突。",
            "parameters": {
                "type": "object",
                "properties": {
                    **version_prop,
                    "query": {"type": "string", "description": "单个关键词。"},
                    "queries": {"type": "array", "items": {"type": "string"}, "description": "批量检索关键词（最多 40 个）。"},
                    "regex": {"type": "boolean", "description": "是否启用正则匹配，默认 false。"},
                    "page": {"type": "integer", "description": "命中片段页码，从 1 开始。"},
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "read_chapter",
            "description": "读取全书中任意指定章节的原文（单次最多 5200 字，可用 offset 续读），用于核实跨章细节。",
            "parameters": {
                "type": "object",
                "properties": {
                    **version_prop,
                    "chapter": {"type": "integer", "description": "章号（从 1 开始）。"},
                    "offset": {"type": "integer", "description": "起始字符偏移，默认 0。"},
                },
                "required": ["chapter"],
                "additionalProperties": False,
            },
        },
        {
            "name": "read_chapters_batch",
            "description": "一次批量读取最多 6 章摘录（传入 query 时围绕命中词居中摘录），适合快速核对多个关联章。",
            "parameters": {
                "type": "object",
                "properties": {
                    **version_prop,
                    "chapters": {"type": "array", "items": {"type": "integer"}, "description": "章号数组，最多 6 章。"},
                    "query": {"type": "string", "description": "可选关键词，命中时居中截取上下文。"},
                    "maxChars": {"type": "integer", "description": "每章返回字数上限（600~3200）。"},
                },
                "required": ["chapters"],
                "additionalProperties": False,
            },
        },
    ]


def run_readonly_tool(books, default_version, name, args):
    """Execute a read-only manuscript tool and return (payload, touched_chapters)."""
    args = args if isinstance(args, dict) else {}
    version = args.get("version") if isinstance(args.get("version"), str) and args.get("version") in books else default_version
    book = books.get(version)
    if not isinstance(book, dict):
        return {"说明": f"版本 {version} 不可用。"}, set()
    if name == "search_manuscript":
        data, touched = search_chapters(
            book,
            query=args.get("query") or "",
            queries=args.get("queries"),
            use_regex=args.get("regex") is True,
            page=args.get("page") or 1,
        )
        return {"version": version, **data}, touched
    if name == "read_chapter":
        data, touched = read_chapter_excerpt(book, args.get("chapter"), offset=args.get("offset") or 0)
        return {"version": version, **data}, touched
    if name == "read_chapters_batch":
        data, touched = read_chapters_batch(
            book, args.get("chapters"), query=args.get("query") or "", max_chars=args.get("maxChars") or 1800,
        )
        return {"version": version, **data}, touched
    return {"说明": f"未知只读工具：{name}"}, set()
