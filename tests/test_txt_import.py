import unittest

from novel_forge.pipeline import _export
from novel_forge.storage import count_chinese_words
from novel_forge.txt_import import parse_txt_manuscript


class TxtImportTests(unittest.TestCase):
    def test_detects_mixed_full_line_headings_and_renumbers_continuously(self):
        raw = ("\ufeff书名：旧港见闻\r\n作者：佚名\r第一卷 风起\r\n楔子 夜航\r\n潮水漫过码头。\r\n"
               "### 第一章：到港\r\n两位乘客先行下船。\r"
               "【第 12 章】第 12 章 - 回看\r\n他翻出了旧信。\r"
               "=== 第100回《风雪》 (2100字) ===\r雪落了一夜。\r"
               "第1节\r回船找人。\r卷一 转场\r"
               "Chapter 99: The Bell\r钟声回荡。\r"
               "终章：风停\r天色转晴。\r番外一 重逢\r后来的事也有人记得。")
        result = parse_txt_manuscript(raw)
        self.assertEqual(result["inferredTitle"], "旧港见闻")
        self.assertEqual(result["detectedBy"], "headings")
        self.assertEqual(result["warnings"], [])
        expected = ["楔子 夜航", "第2章 到港", "第3章 回看", "第4章 风雪", "第5章", "第6章 The Bell",
                    "终章 风停", "番外一 重逢"]
        self.assertEqual([chapter["title"] for chapter in result["chapters"]], expected)
        self.assertEqual([chapter["chapterNumber"] for chapter in result["chapters"]], list(range(1, 9)))
        self.assertEqual(result["totalChapters"], 8)
        self.assertEqual(result["totalWords"], sum(count_chinese_words(row["content"]) for row in result["chapters"]))
        self.assertNotIn("卷一", "".join(chapter["content"] for chapter in result["chapters"]))

    def test_opening_toc_and_empty_headings_warn_once_and_keep_the_actual_chapters(self):
        raw = ("《旧港档案》\n作者：余人\n目录：\n第1章 候船···1\n第2章 交接···39\n\n"
               "第1章 候船\n\n他看见摆渡船回来。\n第2章 交接\n\n他把船票递给了老人。\n"
               "第3章 无正文\n---")
        result = parse_txt_manuscript(raw, fallback_title="指定书名")
        self.assertEqual(result["inferredTitle"], "旧港档案")
        self.assertEqual([row["title"] for row in result["chapters"]], ["第1章 候船", "第2章 交接"])
        self.assertEqual(result["chapters"][0]["content"], "他看见摆渡船回来。")
        self.assertEqual(len(result["warnings"]), 1)
        self.assertIn("目录", result["warnings"][0])

    def test_preamble_preserves_prologue_at_100_effective_words_ignoring_metadata(self):
        for size in (99, 100):
            with self.subTest(size=size):
                raw = f"未标记的书名\n作者：林人\n\n{'风' * size}。\n\n第 4 章 - 开门\n他推开门。"
                result = parse_txt_manuscript(raw)
                self.assertEqual(result["inferredTitle"], "未标记的书名")
                if size == 100:
                    self.assertEqual([row["title"] for row in result["chapters"]], ["序章", "第2章 开门"])
                    self.assertEqual(result["chapters"][0]["wordCount"], 100)
                else:
                    self.assertEqual([row["title"] for row in result["chapters"]], ["第1章 开门"])

    def test_only_standalone_candidates_up_to_80_characters_are_headings(self):
        long_line = "第8章 " + "潮声很远。" * 20
        content = f"　　他提到第9章，随后离开。\n{long_line}\n\n---\n\n　　又起风了。"
        result = parse_txt_manuscript("第1章 潮汐\n" + content + "\n第2章 归去\n　　灯熄了。")
        self.assertEqual(result["totalChapters"], 2)
        self.assertEqual(result["chapters"][0]["content"], content)
        self.assertEqual(result["chapters"][1]["content"], "　　灯熄了。")

    def test_single_and_chunk_override_headings_keep_indents_and_authored_scene_breaks(self):
        content = "第3章 潮汐\n　　风声急。\n\n---\n\n第4章 归去\n　　灯熄了。"
        raw = "《港口旧信》\n\n" + content
        single = parse_txt_manuscript(raw, split_mode="single")
        self.assertEqual(single["inferredTitle"], "港口旧信")
        self.assertEqual(single["detectedBy"], "single")
        self.assertEqual(single["chapters"][0]["content"], content)
        chunked = parse_txt_manuscript(raw, split_mode="chunk", chunk_words=8)
        self.assertEqual(chunked["detectedBy"], "chunk")
        self.assertGreater(chunked["totalChapters"], 1)
        self.assertEqual(chunked["totalWords"], count_chinese_words(content))
        joined = "\n".join(row["content"] for row in chunked["chapters"])
        self.assertIn("第3章 潮汐", joined)
        self.assertIn("　　风声急。", joined)
        self.assertIn("---", joined)

    def test_no_headings_auto_threshold_chunk_cap_and_unbroken_fallback(self):
        single = parse_txt_manuscript("潮" * 10_000)
        self.assertEqual((single["detectedBy"], single["totalChapters"], single["totalWords"]), ("single", 1, 10_000))
        chunked = parse_txt_manuscript(("潮" * 5000) + ("雾" * 5001))
        self.assertEqual((chunked["detectedBy"], chunked["totalChapters"], chunked["totalWords"]), ("chunk", 5, 10_001))
        self.assertEqual([row["wordCount"] for row in chunked["chapters"]], [2500] * 4 + [1])
        self.assertEqual("".join(row["content"] for row in chunked["chapters"]), "潮" * 5000 + "雾" * 5001)
        self.assertEqual(len(chunked["warnings"]), 1)
        paragraphs = "　　船来了。\n\n　　人下船。\n\n　　夜色更深。"
        split = parse_txt_manuscript(paragraphs, split_mode="chunk", chunk_words=5)
        self.assertEqual([row["content"] for row in split["chapters"]],
                         ["　　船来了。", "　　人下船。", "　　夜色更深。"])

    def test_pipeline_txt_export_round_trip_discards_export_only_metadata(self):
        original = [
            {"chapterNumber": 1, "title": "第1章 初潮", "content": "第一段。\n\n---\n\n第二段。"},
            {"chapterNumber": 2, "title": "第2章 回响", "content": "海风吹过石阶。"},
        ]
        job = {"title": "雾港", "genre": "悬疑", "pov": "第一人称", "totalChapters": 2, "totalWords": 24,
               "bookPlan": {"logline": "这行是简介，不是正文。"}}
        exported = _export(job, original, "txt")["content"]
        result = parse_txt_manuscript(exported)
        self.assertEqual(result["inferredTitle"], "雾港")
        self.assertEqual([{key: row[key] for key in original[0]} for row in result["chapters"]], original)
        single = parse_txt_manuscript(exported, split_mode="single")["chapters"][0]["content"]
        self.assertNotIn("-" * 30, single)
        self.assertIn("\n---\n", single)
        self.assertNotIn("这行是简介", single)

    def test_invalid_or_contentless_input_has_chinese_value_errors(self):
        for sample in ("", "\ufeff\t\r\n", "🍂——___。", "第1章 空目录\n第2章 空目录", "书名：《只有标题》\n===\n"):
            with self.subTest(sample=sample), self.assertRaisesRegex(ValueError, "有效的小说正文"):
                parse_txt_manuscript(sample)
        with self.assertRaisesRegex(ValueError, "有效的小说正文"):
            parse_txt_manuscript(None)
        with self.assertRaisesRegex(ValueError, "分章模式"):
            parse_txt_manuscript("正文本体", split_mode="unsupported")
        for value in (0, -1, 1.5, True):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "正整数"):
                parse_txt_manuscript("正文本体", chunk_words=value)

    def test_plain_first_line_can_supply_title_without_discarding_unstructured_prose(self):
        raw = "港口天亮\n\n　　刚才下过雨。"
        result = parse_txt_manuscript(raw)
        self.assertEqual(result["inferredTitle"], "港口天亮")
        self.assertEqual(result["chapters"][0]["content"], raw)
        self.assertEqual(parse_txt_manuscript("海风起来了。", fallback_title="文件名")["inferredTitle"], "文件名")
