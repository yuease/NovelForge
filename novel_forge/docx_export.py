"""Build Fanqie-compatible Word documents without a Word dependency."""

import math
import re
from io import BytesIO
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


DOCX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
FANQIE_SIZE_LIMIT = 5 * 1024 * 1024
_INVALID_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")
_MARKDOWN_HEADING = re.compile(r"^#{1,6}\s*")
_TITLE_PREFIX = re.compile(r"^第\s*[0-9一二三四五六七八九十百千万零两]+\s*章(?:[\s：:\-—·、.]+|$)")
_WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _chapter_number(value, fallback=1):
    try:
        return max(1, math.floor(float(value) or fallback))
    except (ValueError, TypeError, OverflowError):
        return fallback


def format_fanqie_chapter_title(raw_title="", chapter_number=1):
    number = _chapter_number(chapter_number)
    subtitle = _MARKDOWN_HEADING.sub("", str(raw_title or "")).strip()
    while _TITLE_PREFIX.match(subtitle):
        subtitle = _TITLE_PREFIX.sub("", subtitle).strip()
    return f"第{number}章" + (f" {subtitle}" if subtitle else "")


def split_fanqie_paragraphs(body_text=""):
    paragraphs = []
    for line in str(body_text or "").replace("\r\n", "\n").split("\n"):
        line = line.strip()
        if not line or re.fullmatch(r"[-=_*~—]{3,}", line):
            continue
        line = re.sub(r"^#{1,6}\s+", "", line).strip()
        if line:
            paragraphs.append(line)
    return paragraphs


def _strip_leading_chapter_heading(content, title, chapter_number):
    lines = content.replace("\r\n", "\n").split("\n")
    first = next((index for index, line in enumerate(lines) if line.strip()), len(lines))
    if first == len(lines):
        return ""
    line = lines[first].strip()
    clean_first = _MARKDOWN_HEADING.sub("", line).strip()
    clean_title = _MARKDOWN_HEADING.sub("", title).strip()
    numbered_heading = re.match(rf"^(?:#{{1,6}}\s*)?第\s*{chapter_number}\s*章(?:\s|$|[：:\-—])", line)
    markdown_heading = re.match(r"^#{1,6}\s*第\s*[0-9一二三四五六七八九十百千万]+\s*章(?:\s|$|[：:\-—])", line)
    if (clean_title and clean_first == clean_title) or markdown_heading or (
        numbered_heading and len(clean_first.encode("utf-16-le", "surrogatepass")) <= 80
    ):
        return "\n".join(lines[first + 1:]).strip()
    return content.strip()


def _escape_xml(value):
    value = _INVALID_XML.sub("", str(value))
    # JSON from legacy files may contain isolated surrogates.
    value = re.sub(r"[\ud800-\udfff]", "\ufffd", value)
    return escape(value, {'"': "&quot;", "'": "&apos;"})


CONTENT_TYPES_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
</Types>"""

ROOT_RELS_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""

DOCUMENT_RELS_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>"""

STYLES_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:docDefaults>
    <w:rPrDefault>
      <w:rPr>
        <w:rFonts w:ascii="Calibri" w:eastAsia="宋体" w:hAnsi="Calibri" w:cs="Times New Roman"/>
        <w:sz w:val="21"/>
        <w:szCs w:val="21"/>
        <w:lang w:val="en-US" w:eastAsia="zh-CN"/>
      </w:rPr>
    </w:rPrDefault>
    <w:pPrDefault>
      <w:pPr>
        <w:spacing w:after="120" w:line="360" w:lineRule="auto"/>
      </w:pPr>
    </w:pPrDefault>
  </w:docDefaults>
  <w:style w:type="paragraph" w:default="1" w:styleId="Normal">
    <w:name w:val="Normal"/>
    <w:qFormat/>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Heading1">
    <w:name w:val="heading 1"/>
    <w:basedOn w:val="Normal"/>
    <w:next w:val="Normal"/>
    <w:uiPriority w:val="9"/>
    <w:qFormat/>
    <w:pPr>
      <w:keepNext/>
      <w:keepLines/>
      <w:spacing w:before="240" w:after="120"/>
      <w:outlineLvl w:val="0"/>
    </w:pPr>
    <w:rPr>
      <w:b/>
      <w:bCs/>
      <w:sz w:val="32"/>
      <w:szCs w:val="32"/>
    </w:rPr>
  </w:style>
</w:styles>"""


def build_fanqie_docx(chapter_entries=()) -> bytes:
    body = []
    for item in chapter_entries:
        heading = _escape_xml(item.get("heading") or "")
        if heading:
            body.append('<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r>'
                        f'<w:t xml:space="preserve">{heading}</w:t></w:r></w:p>')
        for paragraph in item.get("paragraphs") or ():
            text = _escape_xml(paragraph)
            if text:
                body.append(f'<w:p><w:r><w:t xml:space="preserve">{text}</w:t></w:r></w:p>')
    paragraphs = "\n    ".join(body)
    document = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="{_WORD_NS}">
  <w:body>
    {paragraphs}
    <w:sectPr>
      <w:pgSz w:w="11906" w:h="16838"/>
      <w:pgMar w:top="1440" w:right="1800" w:bottom="1440" w:left="1800" w:header="851" w:footer="992" w:gutter="0"/>
    </w:sectPr>
  </w:body>
</w:document>"""
    members = (
        ("[Content_Types].xml", CONTENT_TYPES_XML),
        ("_rels/.rels", ROOT_RELS_XML),
        ("word/_rels/document.xml.rels", DOCUMENT_RELS_XML),
        ("word/styles.xml", STYLES_XML),
        ("word/document.xml", document),
    )
    with BytesIO() as output:
        with ZipFile(output, "w") as archive:
            for filename, xml in members:
                info = ZipInfo(filename, (2026, 1, 1, 0, 0, 0))
                info.create_system = 0
                archive.writestr(info, xml.encode("utf-8"), compress_type=ZIP_DEFLATED, compresslevel=6)
        return output.getvalue()


def export_fanqie_docx(title, chapters):
    """Prepare the download payload from stored chapters in any input order."""
    def stored_number(chapter):
        try:
            return float(chapter.get("chapterNumber") or 0)
        except (ValueError, TypeError, OverflowError):
            return 0

    entries = []
    for i, chapter in enumerate(sorted(chapters, key=stored_number), 1):
        number = _chapter_number(chapter.get("chapterNumber"), i)
        raw_title = str(chapter.get("title") or "")
        heading = format_fanqie_chapter_title(raw_title, number)
        body = _strip_leading_chapter_heading(str(chapter.get("content") or ""), raw_title or heading, number)
        body = _strip_leading_chapter_heading(body, heading, number)
        entries.append({"heading": heading, "paragraphs": split_fanqie_paragraphs(body)})
    data = build_fanqie_docx(entries)
    return {
        "filename": f"{title or '未命名小说'}-番茄投稿.docx", "contentType": DOCX_CONTENT_TYPE,
        "content": data, "sizeBytes": len(data), "exceedsFanqieLimit": len(data) > FANQIE_SIZE_LIMIT,
    }
