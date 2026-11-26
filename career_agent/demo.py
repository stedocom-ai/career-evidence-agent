"""Deterministic protocol demonstration, explicitly NOT a language model."""

from __future__ import annotations

import json


REQUIREMENTS = [
    "能够使用 Python 完成数据处理与脚本开发。",
    "了解 LLM 工具调用（Tool Calling）的基本流程。",
    "具备 Docker 部署经验。",
]
PYTHON_QUOTE = "在课程练习中使用 Python 完成 CSV 数据整理与统计脚本。"


class ScriptedModel:
    mode = "scripted_demo"
    name = "ScriptedModel (no API)"

    def __init__(self, job_id: str):
        self.job_id = job_id
        self.step = 0
        self.usage = {}

    def _call(self, name: str, args: dict) -> dict:
        return {"role": "assistant", "content": None, "tool_calls": [{
            "id": f"demo_call_{self.step}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
        }]}

    @staticmethod
    def _python_chunk(messages: list[dict]) -> dict:
        for message in messages:
            if message.get("role") == "tool":
                data = json.loads(message["content"])
                for chunk in data.get("result", {}).get("chunks", []):
                    if PYTHON_QUOTE in chunk["text"]:
                        return chunk
        raise ValueError("演示样例的 Python 证据未找到；请使用仓库自带的 examples 文件。")

    def complete(self, messages: list[dict], tools: list[dict]) -> dict:
        self.step += 1
        if self.step == 1:
            return self._call("read_job", {"job_id": self.job_id})
        if self.step == 2:
            return self._call("search_evidence", {"query": "Python", "limit": 5})
        if self.step == 3:
            return self._call("search_evidence", {"query": "LLM Tool Calling Docker", "limit": 5})
        chunk = self._python_chunk(messages)
        if self.step == 4:
            return self._call("read_evidence", {"chunk_ids": [chunk["chunk_id"]]})
        if self.step == 5:
            report = {
                "job_id": self.job_id,
                "summary": "虚构样例有 Python 课程与脚本证据；材料尚不能支持已实现 LLM 工具调用或 Docker 部署的结论。此报告来自固定脚本，未调用真实大模型。",
                "requirements": [
                    {"requirement": REQUIREMENTS[0], "status": "supported",
                     "reasoning": "样例记录了使用 Python 处理 CSV 的课程练习，可作为基础脚本开发证据；实际熟练程度仍需面试或代码检验。",
                     "evidence": [{"chunk_id": chunk["chunk_id"], "quote": PYTHON_QUOTE}]},
                    {"requirement": REQUIREMENTS[1], "status": "not_evidenced",
                     "reasoning": "样例项目说明尚未接入 LLM 工具调用，目前缺少完成该流程的实现或验证记录。", "evidence": []},
                    {"requirement": REQUIREMENTS[2], "status": "not_evidenced",
                     "reasoning": "材料未提供已完成 Docker 部署的证据，需补充可复现的部署记录。", "evidence": []},
                ],
                "next_actions": [
                    {"title": "跑通真实模型的工具调用闭环",
                     "deliverable": "为日志查询声明工具，让真实模型选择查询参数并接收工具结果；提交代码、一次成功 trace 和一次无效参数被拒绝的测试记录。",
                     "related_requirements": [REQUIREMENTS[1]]},
                    {"title": "为已验证的命令行程序补充 Docker 部署",
                     "deliverable": "添加 Dockerfile 和启动说明，在干净容器中运行文本演示；保存构建命令、成功输出和失败排查记录。",
                     "related_requirements": [REQUIREMENTS[2]]},
                ],
            }
            return self._call("finish_report", report)
        raise ValueError("演示脚本已结束；固定样例未通过校验，请检查 trace。")
