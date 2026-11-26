"""Validate and render reports grounded in retrieved source text.

Validation proves that a quoted passage exists in an eligible source returned
during this run. It does not prove that the passage entails the model's claim.
Call ``validate_report`` before persisting or rendering a model response.
"""

from __future__ import annotations

from copy import deepcopy
from html import escape as escape_html
from typing import Any, Protocol


class Store(Protocol):
    def get_document(self, document_id: str) -> dict[str, Any] | None: ...

    def get_chunk(self, chunk_id: str) -> dict[str, Any] | None: ...


STATUS_LABELS = {
    "supported": "有材料支持",
    "partial": "部分材料支持",
    "not_evidenced": "材料未体现",
}


def _object(value: Any, fields: set[str], path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected an object")
    missing = fields - value.keys()
    unknown = value.keys() - fields
    if missing:
        raise ValueError(f"{path}: missing fields: {', '.join(sorted(missing))}")
    if unknown:
        raise ValueError(f"{path}: unknown fields: {', '.join(sorted(map(str, unknown)))}")
    return value


def _string(value: Any, path: str, maximum: int, minimum: int = 1) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{path}: expected a string")
    if len(value.strip()) < minimum or len(value) > maximum:
        raise ValueError(f"{path}: expected {minimum}..{maximum} characters (nonblank)")
    return value


def _list(value: Any, path: str, minimum: int, maximum: int) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{path}: expected a list")
    if not minimum <= len(value) <= maximum:
        raise ValueError(f"{path}: expected {minimum}..{maximum} items")
    return value


def validate_report(
    report: dict, store: Store, job_id: str, seen_ids: set[str]
) -> dict:
    """Return a validated copy, or raise ``ValueError`` with a field path.

    Requirement strings must be exact substrings of the selected job document.
    Quotes may have surrounding whitespace removed; internal text and whitespace
    must match exactly. The result only verifies provenance and structure, not
    semantic entailment, skill proficiency, or the truth of an uploaded document.
    """
    _string(job_id, "job_id argument", 200)
    if not isinstance(seen_ids, set) or any(not isinstance(item, str) for item in seen_ids):
        raise ValueError("seen_ids: expected a set of chunk ID strings")
    job = store.get_document(job_id)
    if not isinstance(job, dict) or job.get("kind") != "job":
        raise ValueError("job_id: selected document does not exist or is not a job")
    if not isinstance(job.get("text"), str) or not job["text"].strip():
        raise ValueError("job_id: selected job has no text")

    _object(report, {"job_id", "summary", "requirements", "next_actions"}, "report")
    if _string(report["job_id"], "report.job_id", 200) != job_id:
        raise ValueError("report.job_id: does not match the selected job")
    _string(report["summary"], "report.summary", 1500)
    requirements = _list(report["requirements"], "report.requirements", 1, 20)
    requirement_texts: set[str] = set()
    duplicate_keys: set[str] = set()

    for index, value in enumerate(requirements):
        path = f"report.requirements[{index}]"
        requirement = _object(value, {"requirement", "status", "reasoning", "evidence"}, path)
        raw_text = _string(requirement["requirement"], f"{path}.requirement", 1200, 3)
        if raw_text not in job["text"]:
            raise ValueError(f"{path}.requirement: must be an exact substring of the job text")
        if raw_text.strip() in duplicate_keys:
            raise ValueError(f"{path}.requirement: duplicate requirement")
        requirement_texts.add(raw_text)
        duplicate_keys.add(raw_text.strip())
        status = _string(requirement["status"], f"{path}.status", 30)
        if status not in STATUS_LABELS:
            raise ValueError(f"{path}.status: expected supported, partial, or not_evidenced")
        _string(requirement["reasoning"], f"{path}.reasoning", 1500)
        evidence = _list(requirement["evidence"], f"{path}.evidence", 0, 5)
        if status == "not_evidenced" and evidence:
            raise ValueError(f"{path}.evidence: not_evidenced must have no evidence")
        if status != "not_evidenced" and not evidence:
            raise ValueError(f"{path}.evidence: supported/partial requires evidence")

        for evidence_index, item in enumerate(evidence):
            evidence_path = f"{path}.evidence[{evidence_index}]"
            entry = _object(item, {"chunk_id", "quote"}, evidence_path)
            chunk_id = _string(entry["chunk_id"], f"{evidence_path}.chunk_id", 200)
            if chunk_id not in seen_ids:
                raise ValueError(f"{evidence_path}.chunk_id: was not returned during this run")
            chunk = store.get_chunk(chunk_id)
            if not isinstance(chunk, dict):
                raise ValueError(f"{evidence_path}.chunk_id: source chunk does not exist")
            if chunk.get("kind") == "job":
                raise ValueError(f"{evidence_path}.chunk_id: a job cannot be candidate evidence")
            if not isinstance(chunk.get("text"), str):
                raise ValueError(f"{evidence_path}.chunk_id: source chunk has no text")
            quote = _string(entry["quote"], f"{evidence_path}.quote", 12000).strip()
            if len("".join(quote.split())) < 4:
                raise ValueError(f"{evidence_path}.quote: needs at least 4 non-whitespace characters")
            if quote not in chunk["text"]:
                raise ValueError(f"{evidence_path}.quote: must be an exact substring of the source chunk")

    actions = _list(report["next_actions"], "report.next_actions", 1, 8)
    for index, value in enumerate(actions):
        path = f"report.next_actions[{index}]"
        action = _object(value, {"title", "deliverable", "related_requirements"}, path)
        _string(action["title"], f"{path}.title", 200)
        _string(action["deliverable"], f"{path}.deliverable", 1000)
        related = _list(action["related_requirements"], f"{path}.related_requirements", 1, 5)
        for related_index, requirement in enumerate(related):
            text = _string(requirement, f"{path}.related_requirements[{related_index}]", 1200, 3)
            if text not in requirement_texts:
                raise ValueError(f"{path}.related_requirements[{related_index}]: must name a report requirement")

    result = deepcopy(report)
    for requirement in result["requirements"]:
        for evidence in requirement["evidence"]:
            evidence["quote"] = evidence["quote"].strip()
    return result


def _escape(value: Any) -> str:
    """Escape untrusted text as literal Markdown, including raw HTML."""
    escaped = escape_html(str(value), quote=False)
    special = set("\\`*_{}[]()#+-.!|~>")
    return "".join("\\" + char if char in special else char for char in escaped)


def _inline(value: Any) -> str:
    return _escape(" ".join(str(value).splitlines()))


def _block_quote(value: str) -> str:
    return "\n".join("> " + _escape(line) for line in value.splitlines())


def render_markdown(report: dict, store: Store) -> str:
    """Render a previously validated report without active links/images/HTML.

    Uploaded statements and the model's interpretation remain unverified. This
    renderer intentionally emits no matching percentage or proficiency score.
    """
    parts = [
        "# 岗位材料分析报告",
        "",
        f"岗位文档：{_inline(report['job_id'])}",
        "",
        "仅根据已导入材料分析。引用校验确认原文存在且一致，不证明引用必然支持对应结论，也不代表能力认证。",
        "",
        "## 概览",
        "",
        _inline(report["summary"]),
        "",
        "## 岗位要求与材料",
    ]
    for index, requirement in enumerate(report["requirements"], 1):
        parts.extend([
            "",
            f"### {index}. {_inline(requirement['requirement'])}",
            "",
            f"**状态：{STATUS_LABELS[requirement['status']]}**",
            "",
            _inline(requirement["reasoning"]),
        ])
        for evidence in requirement["evidence"]:
            chunk = store.get_chunk(evidence["chunk_id"])
            if not isinstance(chunk, dict):
                raise ValueError(f"evidence chunk no longer exists: {evidence['chunk_id']}")
            document_id = chunk.get("document_id")
            document = store.get_document(document_id) if isinstance(document_id, str) else None
            document = document if isinstance(document, dict) else {}
            title = chunk.get("title") or document.get("title") or document_id or "未命名文档"
            page = chunk.get("page")
            if page is None:
                page = "未提供"
            parts.extend([
                "",
                f"- **证据 ID：** {_inline(evidence['chunk_id'])} · **文档：** {_inline(title)} · **页码：** {_inline(page)}",
                "",
                _block_quote(evidence["quote"]),
            ])
        if not requirement["evidence"]:
            parts.extend(["", "材料中尚无可引用证据；这不等同于没有该能力。"])

    parts.extend(["", "## 本周行动与可验收交付物"])
    for index, action in enumerate(report["next_actions"], 1):
        parts.extend([
            "",
            f"### {index}. {_inline(action['title'])}",
            "",
            f"**交付物：** {_inline(action['deliverable'])}",
            "",
            "**对应要求：** " + "；".join(_inline(item) for item in action["related_requirements"]),
        ])
    return "\n".join(parts) + "\n"
