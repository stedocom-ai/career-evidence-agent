"""Local documents and transparent lexical retrieval.

The FTS index contains pre-tokenized words and Chinese bigrams, not embeddings.
Only the CLI imports files; the model receives document and chunk identifiers.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Iterable

MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_TEXT_CHARS = 100_000
MAX_JOB_CHARS = 20_000
CHUNK_SIZE = 850
CHUNK_OVERLAP = 100
KINDS = frozenset({"resume", "project", "job"})
_WORDS = re.compile(r"[A-Za-z0-9_]+(?:\+\+|#)?|[\u3400-\u4dbf\u4e00-\u9fff]+")


def _normalize(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def _tokens(text: str) -> list[str]:
    """Encode complete English words and Chinese bigrams as FTS-safe atoms.

    Hex encoding preserves punctuation in skills such as C++ and C# instead of
    letting SQLite split them into the same single-letter token.
    """
    result: list[str] = []
    for match in _WORDS.finditer(text):
        word = match.group(0).lower()
        if "\u3400" <= word[0] <= "\u9fff":
            pieces = [word] if len(word) == 1 else [word[i:i + 2] for i in range(len(word) - 1)]
        else:
            pieces = [word]
        result.extend("t" + piece.encode("utf-8").hex() for piece in pieces)
    return result


def _split_chunks(text: str) -> Iterable[str]:
    start = 0
    while start < len(text):
        end = min(start + CHUNK_SIZE, len(text))
        if end < len(text):
            # Prefer a sentence/line boundary, while retaining a hard size cap.
            floor = start + CHUNK_SIZE // 2
            for boundary in ("\n", "。", ". ", " "):
                pos = text.rfind(boundary, floor, end)
                if pos >= floor:
                    end = pos + len(boundary)
                    break
        chunk = text[start:end].strip()
        if chunk:
            yield chunk
        if end == len(text):
            break
        start = max(start + 1, end - CHUNK_OVERLAP)


def _load_pages(path: Path) -> list[tuple[int, str]]:
    if not path.is_file():
        raise ValueError(f"资料文件不存在：{path}")
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("资料文件超过 5 MiB，请先裁剪到与岗位相关的内容。")
    suffix = path.suffix.lower()
    if suffix in {".txt", ".md", ".markdown"}:
        try:
            text = path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError("文本文件必须使用 UTF-8 编码。") from exc
        return [(i, page) for i, page in enumerate(text.split("\f"), 1)]
    if suffix != ".pdf":
        raise ValueError("仅支持 .txt、.md 和带文字层的 .pdf 文件。")
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise ValueError('读取 PDF 需要可选依赖：python -m pip install ".[pdf]"') from exc
    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted and not reader.decrypt(""):
            raise ValueError("PDF 已加密，请先导出可读取的文本或未加密副本。")
        pages = [(i, page.extract_text() or "") for i, page in enumerate(reader.pages, 1)]
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("无法解析 PDF，请检查文件，或导出为 UTF-8 文本后导入。") from exc
    if not any(text.strip() for _, text in pages):
        raise ValueError("PDF 没有可提取的文字层；扫描件需要先做 OCR，本项目不包含 OCR。")
    return pages


class Store:
    """SQLite evidence store. Re-importing the same kind/content is idempotent."""

    def __init__(self, db_path: str | Path):
        db_name = str(db_path)
        if db_name != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(db_name)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        try:
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS documents (
                    document_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN ('resume', 'project', 'job')),
                    text TEXT NOT NULL,
                    chunk_count INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS chunks (
                    chunk_id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL REFERENCES documents(document_id),
                    page INTEGER NOT NULL,
                    text TEXT NOT NULL
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5(tokens, tokenize='ascii');
                """
            )
        except sqlite3.OperationalError as exc:
            self._db.close()
            raise RuntimeError("SQLite 缺少 FTS5；请使用启用 FTS5 的 Python 发行版。") from exc

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._db.close()

    def add_text(self, text: str, kind: str, title: str) -> dict:
        if not isinstance(text, str):
            raise ValueError("资料正文必须是字符串。")
        pages = [(i, page) for i, page in enumerate(text.split("\f"), 1)]
        return self._add_pages(pages, kind, title)

    def add_document(self, path: str | Path, kind: str, title: str | None = None) -> dict:
        file_path = Path(path)
        return self._add_pages(_load_pages(file_path), kind, title if title is not None else file_path.name)

    def _add_pages(self, pages: list[tuple[int, str]], kind: str, title: str) -> dict:
        if kind not in KINDS:
            raise ValueError("kind 必须是 resume、project 或 job。")
        if not isinstance(title, str) or not title.strip() or len(title) > 200:
            raise ValueError("资料标题必须是 1 到 200 字符的非空字符串。")
        normalized = [(page, _normalize(text)) for page, text in pages]
        text = "\n\n".join(body for _, body in normalized)
        if not text.strip():
            raise ValueError("资料正文为空，未导入。")
        max_chars = MAX_JOB_CHARS if kind == "job" else MAX_TEXT_CHARS
        if len(text) > max_chars:
            raise ValueError(f"正文超过 {max_chars} 字符，请先裁剪。")
        # The kind and page boundaries are part of identity; title/path are not.
        identity = json.dumps([kind, normalized], ensure_ascii=False, separators=(",", ":"))
        doc_id = "doc_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
        existing = self.get_document(doc_id)
        if existing is not None:
            return existing
        prepared: list[tuple[str, int, str]] = []
        for page, body in normalized:
            for index, chunk in enumerate(_split_chunks(body), 1):
                prepared.append((f"{doc_id}:p{page}:c{index}", page, chunk))
        with self._db:
            self._db.execute(
                "INSERT INTO documents(document_id,title,kind,text,chunk_count) VALUES (?,?,?,?,?)",
                (doc_id, title.strip(), kind, text, len(prepared)),
            )
            for chunk_id, page, chunk in prepared:
                cursor = self._db.execute(
                    "INSERT INTO chunks(chunk_id,document_id,page,text) VALUES (?,?,?,?)",
                    (chunk_id, doc_id, page, chunk),
                )
                if kind != "job":
                    self._db.execute(
                        "INSERT INTO chunk_fts(rowid,tokens) VALUES (?,?)",
                        (cursor.lastrowid, " ".join(_tokens(title + "\n" + chunk))),
                    )
        result = self.get_document(doc_id)
        assert result is not None
        return result

    def list_documents(self) -> list[dict]:
        rows = self._db.execute(
            "SELECT document_id,title,kind,chunk_count FROM documents ORDER BY rowid"
        ).fetchall()
        return [dict(row) for row in rows]

    def get_document(self, document_id: str) -> dict | None:
        row = self._db.execute(
            "SELECT document_id,title,kind,chunk_count,text FROM documents WHERE document_id=?",
            (document_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def get_chunk(self, chunk_id: str) -> dict | None:
        row = self._db.execute(
            """SELECT c.chunk_id,c.document_id,d.title,d.kind,c.page,c.text
               FROM chunks c JOIN documents d ON d.document_id=c.document_id
               WHERE c.chunk_id=?""",
            (chunk_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def get_chunks(self, chunk_ids: Iterable[str]) -> list[dict]:
        if isinstance(chunk_ids, (str, bytes)):
            raise ValueError("chunk_ids 必须是资料块 ID 列表。")
        result = []
        for chunk_id in dict.fromkeys(chunk_ids):
            if not isinstance(chunk_id, str):
                raise ValueError("资料块 ID 必须是字符串。")
            chunk = self.get_chunk(chunk_id)
            if chunk is not None:
                result.append(chunk)
        return result

    def search(self, query: str, limit: int = 5) -> list[dict]:
        if not isinstance(query, str) or len(query) > 500:
            raise ValueError("query 必须是不超过 500 字符的字符串。")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
            raise ValueError("limit 必须是 1 到 20 之间的整数。")
        terms = list(dict.fromkeys(_tokens(query)))
        if not terms:
            return []
        # User text never reaches MATCH syntax: only encoded, quoted atoms do.
        expression = " OR ".join('"' + term + '"' for term in terms)
        rows = self._db.execute(
            """SELECT c.chunk_id,c.document_id,d.title,d.kind,c.page,c.text,
                      -bm25(chunk_fts) AS score
               FROM chunk_fts JOIN chunks c ON c.rowid=chunk_fts.rowid
               JOIN documents d ON d.document_id=c.document_id
               WHERE chunk_fts MATCH ? AND d.kind IN ('resume','project')
               ORDER BY bm25(chunk_fts),c.chunk_id LIMIT ?""",
            (expression, limit),
        ).fetchall()
        return [dict(row) for row in rows]
