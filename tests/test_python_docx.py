import json
from hashlib import sha256
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree
from zipfile import ZipFile

from novel_forge import docx_export


FIXTURE = Path(__file__).with_name("fixtures") / "test_python_docx.json"
NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}


class FanqieDocxTests(unittest.TestCase):
    def test_normalizes_titles_and_paragraphs(self):
        self.assertEqual(docx_export.format_fanqie_chapter_title("## 第八章：第8章 — 新港", 8), "第8章 新港")
        self.assertEqual(docx_export.format_fanqie_chapter_title("", 0), "第1章")
        self.assertEqual(
            docx_export.split_fanqie_paragraphs(" Ａ\r\n\r\n---\r\n ## 内题\n\n“对白。”\n"),
            ["Ａ", "内题", "“对白。”"],
        )

    def test_export_has_clean_opening_sorted_heading_styles_and_escaped_text(self):
        chapters = [
            {"chapterNumber": 2, "title": "后续", "content": "后来，船到了。"},
            {"chapterNumber": 1, "title": "第1章：启程", "content": "\n## 第1章 启程\n第1章 启程\n\n甲&乙 <丙> \"丁\" '戊'\x03\n\n“先上船。”"},
        ]
        result = docx_export.export_fanqie_docx("故事", chapters)
        self.assertEqual(result["filename"], "故事-番茄投稿.docx")
        self.assertEqual(result["contentType"], docx_export.DOCX_CONTENT_TYPE)
        self.assertEqual(result["sizeBytes"], len(result["content"]))
        self.assertEqual(docx_export.FANQIE_SIZE_LIMIT, 5 * 1024 * 1024)
        self.assertEqual(docx_export.export_fanqie_docx("故事", chapters)["content"], result["content"])
        with ZipFile(BytesIO(result["content"])) as archive:
            self.assertIsNone(archive.testzip())
            self.assertEqual(len(archive.namelist()), 5)
            xml = archive.read("word/document.xml")
            self.assertIn(b"&amp;", xml)
            self.assertIn(b"&lt;", xml)
            self.assertIn(b"&gt;", xml)
            self.assertIn(b"&quot;", xml)
            self.assertIn(b"&apos;", xml)
            root = ElementTree.fromstring(xml)
        paragraphs = root.findall("./w:body/w:p", NS)
        self.assertEqual([p.find("w:r/w:t", NS).text for p in paragraphs],
                         ["第1章 启程", "甲&乙 <丙> \"丁\" '戊'", "“先上船。”", "第2章 后续", "后来，船到了。"])
        self.assertEqual([i for i, p in enumerate(paragraphs) if p.find("w:pPr/w:pStyle", NS) is not None], [0, 3])
        with patch.object(docx_export, "FANQIE_SIZE_LIMIT", result["sizeBytes"]):
            self.assertFalse(docx_export.export_fanqie_docx("故事", chapters)["exceedsFanqieLimit"])
        with patch.object(docx_export, "FANQIE_SIZE_LIMIT", result["sizeBytes"] - 1):
            self.assertTrue(docx_export.export_fanqie_docx("故事", chapters)["exceedsFanqieLimit"])

    def test_all_xml_parts_match_the_compatible_document(self):
        entries = [{"heading": "第1章 港口", "paragraphs": ["海面🚢 & <灯> \"亮\" '暗'", "下一段。"]},
                   {"heading": "第2章 航线", "paragraphs": ["“走吧。”"]}]
        expected = json.loads(FIXTURE.read_text(encoding="utf-8"))["test_all_xml_parts_match_the_compatible_document"]
        with ZipFile(BytesIO(docx_export.build_fanqie_docx(entries))) as package:
            self.assertEqual(package.namelist(), list(expected))
            for name, digest in expected.items():
                with self.subTest(part=name):
                    self.assertEqual(sha256(package.read(name)).hexdigest(), digest)
