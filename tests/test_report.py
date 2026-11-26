"""Provenance, schema, and Markdown safety checks for generated reports."""

from copy import deepcopy
import unittest

from career_agent.report import render_markdown, validate_report


class FakeStore:
    def __init__(self):
        self.documents = {
            "job-1": {"document_id": "job-1", "kind": "job", "text": "熟悉 Python 编程。了解 Docker 部署。", "title": "Agent 实习"},
            "project-1": {"document_id": "project-1", "kind": "project", "text": "课程使用 Python 编程完成搜索算法。", "title": "课程项目"},
        }
        self.chunks = {
            "chunk-1": {"chunk_id": "chunk-1", "document_id": "project-1", "kind": "project", "text": "课程使用 Python 编程完成搜索算法。", "page": 2},
            "job-chunk": {"chunk_id": "job-chunk", "document_id": "job-1", "kind": "job", "text": "熟悉 Python 编程。了解 Docker 部署。", "page": None},
        }

    def get_document(self, document_id):
        return self.documents.get(document_id)

    def get_chunk(self, chunk_id):
        return self.chunks.get(chunk_id)


def example_report():
    return {
        "job_id": "job-1",
        "summary": "课程材料体现 Python 使用；Docker 尚无材料。",
        "requirements": [
            {"requirement": "熟悉 Python 编程", "status": "partial", "reasoning": "材料只展示课程使用，不能推断生产经验。", "evidence": [{"chunk_id": "chunk-1", "quote": "Python 编程"}]},
            {"requirement": "了解 Docker 部署", "status": "not_evidenced", "reasoning": "已提供的材料未体现此项。", "evidence": []},
        ],
        "next_actions": [{"title": "完成部署示例", "deliverable": "提交可运行的 Dockerfile 与运行说明。", "related_requirements": ["了解 Docker 部署"]}],
    }


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.store = FakeStore()
        self.report = example_report()
        self.seen = {"chunk-1"}

    def validate(self, report=None, seen=None):
        return validate_report(self.report if report is None else report, self.store, "job-1", self.seen if seen is None else seen)

    def test_valid_report_is_copied_without_mutating_input(self):
        self.report["requirements"][0]["evidence"][0]["quote"] = "  Python 编程\n"
        original = deepcopy(self.report)
        result = self.validate()
        self.assertEqual(self.report, original)
        self.assertEqual(result["requirements"][0]["evidence"][0]["quote"], "Python 编程")
        result["next_actions"][0]["title"] = "changed"
        self.assertNotEqual(result["next_actions"], self.report["next_actions"])

    def test_supported_and_partial_both_require_evidence(self):
        for status in ("supported", "partial"):
            with self.subTest(status=status):
                report = example_report()
                report["requirements"][0]["status"] = status
                report["requirements"][0]["evidence"] = []
                with self.assertRaisesRegex(ValueError, "requires evidence"):
                    self.validate(report)

    def test_not_evidenced_cannot_have_evidence(self):
        self.report["requirements"][0]["status"] = "not_evidenced"
        with self.assertRaisesRegex(ValueError, "must have no evidence"):
            self.validate()

    def test_sources_must_have_been_seen_in_this_run(self):
        with self.assertRaisesRegex(ValueError, "not returned during this run"):
            self.validate(seen=set())

    def test_missing_chunk_is_rejected_even_when_id_was_seen(self):
        del self.store.chunks["chunk-1"]
        with self.assertRaisesRegex(ValueError, "does not exist"):
            self.validate()

    def test_job_is_never_candidate_evidence(self):
        self.report["requirements"][0]["evidence"] = [{"chunk_id": "job-chunk", "quote": "Python 编程"}]
        with self.assertRaisesRegex(ValueError, "job cannot be candidate evidence"):
            self.validate(seen={"job-chunk"})

    def test_quote_must_match_exactly_without_fuzzy_whitespace_or_case(self):
        for quote in ("python 编程", "Python  编程", "Python\n编程", "完成生产部署"):
            with self.subTest(quote=quote):
                report = example_report()
                report["requirements"][0]["evidence"][0]["quote"] = quote
                with self.assertRaisesRegex(ValueError, "exact substring"):
                    self.validate(report)

    def test_short_or_whitespace_only_quotes_are_rejected(self):
        self.store.chunks["chunk-1"]["text"] = "abc a b c"
        for quote in ("abc", "a b c", "\n  "):
            with self.subTest(quote=quote):
                report = example_report()
                report["requirements"][0]["evidence"][0]["quote"] = quote
                with self.assertRaises(ValueError):
                    self.validate(report)

    def test_requirement_must_be_from_job_not_model_paraphrase(self):
        self.report["requirements"][0]["requirement"] = "精通 Python 生产服务"
        with self.assertRaisesRegex(ValueError, "exact substring of the job"):
            self.validate()

    def test_duplicate_requirement_is_rejected(self):
        self.report["requirements"].append(deepcopy(self.report["requirements"][0]))
        with self.assertRaisesRegex(ValueError, "duplicate requirement"):
            self.validate()

    def test_action_must_link_to_an_actual_report_requirement(self):
        self.report["next_actions"][0]["related_requirements"] = ["学习 Kubernetes"]
        with self.assertRaisesRegex(ValueError, "must name a report requirement"):
            self.validate()

    def test_unknown_fields_are_rejected_at_every_object_level(self):
        for level in ("top", "requirement", "evidence", "action"):
            with self.subTest(level=level):
                report = example_report()
                target = {"top": report, "requirement": report["requirements"][0], "evidence": report["requirements"][0]["evidence"][0], "action": report["next_actions"][0]}[level]
                target["invented"] = True
                with self.assertRaisesRegex(ValueError, "unknown fields"):
                    self.validate(report)

    def test_required_fields_are_not_optional(self):
        del self.report["requirements"][0]["reasoning"]
        with self.assertRaisesRegex(ValueError, "missing fields: reasoning"):
            self.validate()

    def test_wrong_json_types_are_rejected(self):
        changes = (
            ("summary", 42), ("requirements", {}), ("requirements", None), ("next_actions", "learn"),
        )
        for key, value in changes:
            with self.subTest(key=key, value=value):
                report = example_report()
                report[key] = value
                with self.assertRaises(ValueError):
                    self.validate(report)
        self.report["requirements"][0]["evidence"][0]["quote"] = ["Python 编程"]
        with self.assertRaisesRegex(ValueError, "expected a string"):
            self.validate()

    def test_empty_and_excessive_lists_are_rejected(self):
        for field, items in (("requirements", []), ("requirements", [example_report()["requirements"][0]] * 21), ("next_actions", []), ("next_actions", example_report()["next_actions"] * 9)):
            with self.subTest(field=field, length=len(items)):
                report = example_report()
                report[field] = items
                with self.assertRaises(ValueError):
                    self.validate(report)
        self.report["requirements"][0]["evidence"] *= 6
        with self.assertRaisesRegex(ValueError, "0..5 items"):
            self.validate()

    def test_empty_and_excessive_related_requirements_are_rejected(self):
        for related in ([], ["了解 Docker 部署"] * 6):
            with self.subTest(related=related):
                report = example_report()
                report["next_actions"][0]["related_requirements"] = related
                with self.assertRaises(ValueError):
                    self.validate(report)

    def test_text_length_limits_are_enforced(self):
        for level, key, maximum in (("top", "summary", 1500), ("requirement", "reasoning", 1500), ("requirement", "requirement", 1200), ("action", "title", 200), ("action", "deliverable", 1000)):
            with self.subTest(key=key):
                report = example_report()
                target = {"top": report, "requirement": report["requirements"][0], "action": report["next_actions"][0]}[level]
                target[key] = "x" * (maximum + 1)
                with self.assertRaises(ValueError):
                    self.validate(report)

    def test_unknown_status_is_rejected(self):
        self.report["requirements"][0]["status"] = "expert"
        with self.assertRaisesRegex(ValueError, "expected supported"):
            self.validate()

    def test_selected_job_must_exist_be_job_and_match_report(self):
        with self.assertRaisesRegex(ValueError, "does not match"):
            report = example_report()
            report["job_id"] = "another-job"
            self.validate(report)
        for replacement in (None, {"kind": "project", "text": "Python"}, {"kind": "job", "text": ""}):
            with self.subTest(replacement=replacement):
                self.store.documents["job-1"] = replacement
                with self.assertRaises(ValueError):
                    self.validate()

    def test_render_shows_source_page_status_and_honest_scope(self):
        markdown = render_markdown(self.validate(), self.store)
        for expected in ("部分材料支持", "材料未体现", "chunk\\-1", "课程项目", "**页码：** 2", "> Python 编程", "可验收交付物", "不证明引用必然支持对应结论", "不等同于没有该能力"):
            self.assertIn(expected, markdown)
        self.assertNotIn("%", markdown)

    def test_render_escapes_model_and_source_markup(self):
        payload = "![外链](https://example.invalid/image) <script>alert(1)</script>\n# injected"
        self.report["summary"] = payload
        self.report["next_actions"][0]["title"] = payload
        self.store.documents["project-1"]["title"] = payload
        self.store.chunks["chunk-1"]["text"] = payload
        self.report["requirements"][0]["evidence"][0]["quote"] = payload
        markdown = render_markdown(self.validate(), self.store)
        self.assertNotIn("<script>", markdown)
        self.assertNotIn("![外链]", markdown)
        self.assertNotIn("\n# injected", markdown)
        self.assertIn("\\!\\[外链\\]", markdown)
        self.assertIn("&lt;script&gt;", markdown)
        self.assertIn("> \\# injected", markdown)

    def test_validation_does_not_claim_semantic_entailment(self):
        self.report["requirements"][0]["reasoning"] = "此句可能夸大引用；结构校验无法判断语义。"
        self.assertEqual(self.validate()["requirements"][0]["reasoning"], self.report["requirements"][0]["reasoning"])


if __name__ == "__main__":
    unittest.main()
