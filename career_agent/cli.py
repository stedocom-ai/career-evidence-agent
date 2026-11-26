"""Command-line entry point. Import data locally; explicitly opt into live APIs."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from .agent import Agent, JsonlTrace
from .client import ChatModel, ModelError
from .demo import ScriptedModel
from .report import render_markdown
from .storage import Store
from .mail_cli import handle_mail_command, register_mail_commands


def _json(data) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def _output_dir(value: str | None) -> Path:
    path = Path(value) if value else Path(".local/runs") / (
        datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid4().hex[:6]
    )
    if path.exists() and any(path.iterdir()):
        raise ValueError("输出目录已包含文件；请换一个目录，保留原运行记录。")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _analyze(store, model, job_id, output: Path, max_turns=12, max_tool_calls=24) -> int:
    agent = Agent(store, model, max_turns=max_turns, max_tool_calls=max_tool_calls)
    trace = JsonlTrace(output / "trace.jsonl")
    try:
        result = agent.run(job_id, trace)
    finally:
        trace.close()
    (output / "run.json").write_text(_json(result.metadata()) + "\n", encoding="utf-8")
    if result.status != "completed":
        print(f"运行停止：{result.error}\n记录：{output.resolve()}", file=sys.stderr)
        return 1
    markdown = render_markdown(result.report, store)
    if result.mode == "scripted_demo":
        markdown = "> **脚本演示：未调用真实大模型，候选人及岗位均为虚构。**\n\n" + markdown
    (output / "report.md").write_text(markdown, encoding="utf-8")
    (output / "report.json").write_text(_json(result.report) + "\n", encoding="utf-8")
    label = "离线脚本演示完成（未调用 LLM）" if result.mode == "scripted_demo" else "真实模型运行完成（请核对报告语义）"
    print(f"{label}\n模型调用：{result.model_calls}；工具调用：{result.tool_calls}\n报告与记录：{output.resolve()}")
    return 0


def build_parser():
    parser = argparse.ArgumentParser(description="Career Evidence Agent：带引用的岗位材料分析")
    parser.add_argument("--db", default=".local/career.db", help="本地 SQLite 数据库")
    sub = parser.add_subparsers(dest="command", required=True)
    ingest = sub.add_parser("ingest", help="导入 UTF-8 文本/Markdown，或可选 PDF")
    ingest.add_argument("path")
    ingest.add_argument("--kind", required=True, choices=["resume", "project", "job"])
    ingest.add_argument("--title")
    sub.add_parser("documents", help="列出已导入的资料 ID")
    analyze = sub.add_parser("analyze", help="使用环境变量中配置的真实模型")
    analyze.add_argument("--job", required=True, help="kind=job 的 document_id")
    analyze.add_argument("--output", help="空目录；默认自动生成唯一运行目录")
    analyze.add_argument("--max-turns", type=int, default=12)
    analyze.add_argument("--max-tool-calls", type=int, default=24)
    demo = sub.add_parser("demo", help="无需密钥的固定脚本演示；不验证模型能力")
    demo.add_argument("--output", default="demo-output")
    register_mail_commands(sub)
    return parser


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    try:
        if args.command.startswith("mail-"):
            return handle_mail_command(args)
        if args.command == "demo":
            examples = Path(__file__).resolve().parent.parent / "examples"
            if not (examples / "job.md").is_file():
                raise ValueError("未找到演示资料，请在完整源码目录运行，或使用 pip install -e . 安装。")
            output = _output_dir(args.output)
            with Store(output / "demo.db") as store:
                store.add_document(examples / "resume.md", "resume")
                store.add_document(examples / "project.md", "project")
                job = store.add_document(examples / "job.md", "job")
                return _analyze(store, ScriptedModel(job["document_id"]), job["document_id"], output)
        if args.command == "analyze":
            model = ChatModel.from_env()
            # Check budgets and job before creating an output directory or issuing HTTP.
            Agent(None, model, max_turns=args.max_turns, max_tool_calls=args.max_tool_calls)
            with Store(args.db) as store:
                job = store.get_document(args.job)
                if not job or job["kind"] != "job":
                    raise ValueError("岗位 ID 不存在或文档类型不是 job；先用 documents 查看 ID。")
                output = _output_dir(args.output)
                print("将把岗位与检索片段发送到已配置的模型服务商。", flush=True)
                return _analyze(store, model, args.job, output, args.max_turns, args.max_tool_calls)
        with Store(args.db) as store:
            if args.command == "ingest":
                doc = store.add_document(args.path, args.kind, args.title)
                print(_json({key: doc[key] for key in ("document_id", "title", "kind", "chunk_count")}))
            else:
                print(_json(store.list_documents()))
        return 0
    except (ValueError, OSError, sqlite3.Error, RuntimeError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
