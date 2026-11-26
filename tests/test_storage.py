import tempfile
import unittest
from pathlib import Path

from career_agent.storage import CHUNK_SIZE, MAX_JOB_CHARS, Store


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "nested" / "evidence.sqlite3"
        self.store = Store(self.db)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_dedup_stable_ids_and_reopen(self):
        first = self.store.add_text("Python 搜索算法作业", "resume", "简历")
        again = self.store.add_text("Python 搜索算法作业", "resume", "重命名简历")
        self.assertEqual(first["document_id"], again["document_id"])
        self.assertEqual(len(self.store.list_documents()), 1)
        before = self.store.search("python")[0]
        self.store.close()
        self.store = Store(self.db)
        self.assertEqual(self.store.search("Python")[0]["chunk_id"], before["chunk_id"])
        self.assertEqual(self.store.get_document(first["document_id"])["title"], "简历")

    def test_english_complete_words_and_technical_punctuation(self):
        self.store.add_text("Used JavaScript and C++ for a game.", "project", "游戏")
        self.store.add_text("Java exercises", "resume", "课程")
        self.assertEqual(len(self.store.search("Java")), 1)
        self.assertIn("Java exercises", self.store.search("Java")[0]["text"])
        self.assertEqual(len(self.store.search("C++")), 1)
        self.assertEqual(self.store.search("C#"), [])

    def test_chinese_bigrams_and_job_is_not_candidate_evidence(self):
        self.store.add_text("完成交互原型设计并进行功能验收", "project", "角色项目")
        self.store.add_text("需要交互原型设计及 Docker 部署能力", "job", "岗位")
        result = self.store.search("原型设计")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["kind"], "project")
        self.assertEqual(self.store.search("Docker"), [])

    def test_empty_and_unrelated_search(self):
        self.assertEqual(self.store.search("Python"), [])
        self.store.add_text("课程使用 Python", "resume", "简历")
        self.assertEqual(self.store.search("   "), [])
        self.assertEqual(self.store.search('" OR * ( )'), [])
        self.assertEqual(self.store.search("Kubernetes"), [])

    def test_boundaries_ids_page_and_exact_quotes(self):
        page_one = "Python 实验。" * 180
        page_two = "第二页项目材料：检索系统和用户反馈。"
        doc = self.store.add_text(page_one + "\f" + page_two, "project", "长材料")
        chunks = self.store.search("Python", limit=20)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk["text"]), CHUNK_SIZE)
            self.assertIn(chunk["text"], doc["text"])
            self.assertEqual(chunk["page"], 1)
            self.assertEqual(chunk, self.store.get_chunk(chunk["chunk_id"]) | {"score": chunk["score"]})
        second = self.store.search("第二页")[0]
        self.assertEqual(second["page"], 2)
        read = self.store.get_chunks([second["chunk_id"], "missing", second["chunk_id"]])
        self.assertEqual(len(read), 1)
        self.assertIsNone(self.store.get_document("missing"))
        self.assertIsNone(self.store.get_chunk("missing"))

    def test_utf8_bom_file_and_bad_inputs(self):
        file_path = Path(self.temp.name) / "中文.md"
        file_path.write_text("课程：Python 分类实验。", encoding="utf-8-sig")
        result = self.store.add_document(file_path, "resume")
        self.assertEqual(result["title"], file_path.name)
        self.assertTrue(self.store.search("Python"))
        bad_values = [
            lambda: self.store.add_text(" ", "resume", "空白"),
            lambda: self.store.add_text("text", "other", "标题"),
            lambda: self.store.add_text("text", "resume", ""),
            lambda: self.store.add_text("a" * (MAX_JOB_CHARS + 1), "job", "太长"),
            lambda: self.store.search("Python", limit=True),
            lambda: self.store.search("Python", limit=0),
            lambda: self.store.search("a" * 501),
            lambda: self.store.get_chunks("not-a-list"),
            lambda: self.store.add_document(Path(self.temp.name) / "absent.txt", "resume"),
        ]
        for operation in bad_values:
            with self.assertRaises(ValueError):
                operation()


if __name__ == "__main__":
    unittest.main()
