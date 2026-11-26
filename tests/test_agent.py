"""Orchestration tests with scripted protocol messages, not live-model evals."""

from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from career_agent.agent import Agent, JsonlTrace
from career_agent.client import ModelError
from career_agent.storage import Store


def call(name, arguments, identifier="call_test", *, raw=False):
    return {
        "id": identifier,
        "type": "function",
        "function": {
            "name": name,
            "arguments": arguments if raw else json.dumps(arguments, ensure_ascii=False),
        },
    }


def assistant(*calls, content=None):
    return {"role": "assistant", "content": content, "tool_calls": list(calls)}


class FakeModel:
    """Return only supplied protocol messages; it performs no LLM reasoning."""

    mode = "scripted-test"
    name = "fake-model-for-unit-tests"

    def __init__(self, script, *, repeat_last=False):
        self.script = list(script)
        self.repeat_last = repeat_last
        self.history = []
        self.usage = {}

    def complete(self, messages, tools):
        index = len(self.history)
        self.history.append({"messages": deepcopy(messages), "tools": deepcopy(tools)})
        if index >= len(self.script):
            if not self.repeat_last:
                raise AssertionError("Agent unexpectedly exceeded the scripted model responses")
            response = self.script[-1]
        else:
            response = self.script[index]
        if isinstance(response, BaseException):
            raise response
        self.usage["total_tokens"] = self.usage.get("total_tokens", 0) + 10
        return deepcopy(response)


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "evidence.sqlite3")
        self.resume = self.store.add_text(
            "完成 Python 搜索算法课程作业，对比 BFS、DFS 与 A*。\n使用 Git 记录代码。",
            "resume", "匿名简历",
        )
        self.job = self.store.add_text(
            "AI Agent 实习\n具备 Python 编程基础\n熟悉 Docker 部署\n",
            "job", "虚构测试岗位",
        )
        self.job_id = self.job["document_id"]
        self.chunk = self.store.search("Python")[0]
        self.report = {
            "job_id": self.job_id,
            "summary": "材料支持 Python 课程基础，Docker 部署经历尚未体现。",
            "requirements": [
                {
                    "requirement": "具备 Python 编程基础",
                    "status": "partial",
                    "reasoning": "有 Python 搜索算法课程作业，尚不能证明独立工程开发经验。",
                    "evidence": [{"chunk_id": self.chunk["chunk_id"], "quote": "Python 搜索算法课程作业"}],
                },
                {
                    "requirement": "熟悉 Docker 部署",
                    "status": "not_evidenced",
                    "reasoning": "当前材料未体现 Docker 部署记录。",
                    "evidence": [],
                },
            ],
            "next_actions": [{
                "title": "为课程项目补充可复现的 Docker 运行方式",
                "deliverable": "提交 Dockerfile、运行命令和一次从空环境启动成功的记录。",
                "related_requirements": ["熟悉 Docker 部署"],
            }],
        }
        self.run_count = 0

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def run_agent(self, model, *, job_id=None, **options):
        self.run_count += 1
        path = self.root / f"trace-{self.run_count}.jsonl"
        trace = JsonlTrace(path)
        try:
            result = Agent(self.store, model, **options).run(
                self.job_id if job_id is None else job_id, trace,
            )
        finally:
            trace.close()
        events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        return result, events

    def prelude(self):
        return [
            assistant(call("read_job", {"job_id": self.job_id}, "read")),
            assistant(call("search_evidence", {"query": "Python Docker", "limit": 5}, "search")),
        ]

    def test_protocol_loop_retrieves_real_store_evidence_and_finishes(self):
        model = FakeModel(self.prelude() + [assistant(call("finish_report", self.report, "finish"))])
        result, events = self.run_agent(model)
        self.assertEqual(result.status, "completed")
        self.assertEqual((result.model_calls, result.tool_calls), (3, 3))
        self.assertEqual(result.report, self.report)
        self.assertEqual(result.mode, "scripted-test")
        self.assertEqual(result.usage["total_tokens"], 30)
        self.assertEqual(events[0]["event"], "run_started")
        self.assertEqual(events[-1]["event"], "run_finished")
        self.assertEqual(events[-1]["status"], "completed")
        tool_events = [event for event in events if event["event"] == "tool_result"]
        self.assertEqual([event["tool"] for event in tool_events], ["read_job", "search_evidence", "finish_report"])
        self.assertTrue(all(event["observation"]["ok"] for event in tool_events))
        returned = json.loads(model.history[2]["messages"][-1]["content"])
        self.assertEqual(returned["result"]["chunks"][0]["chunk_id"], self.chunk["chunk_id"])
        self.assertEqual(model.history[2]["messages"][-1]["tool_call_id"], "search")
        self.assertEqual({tool["function"]["name"] for tool in model.history[0]["tools"]}, {
            "list_documents", "read_job", "search_evidence", "read_evidence", "finish_report",
        })

    def test_reused_model_reports_usage_for_each_run(self):
        script = self.prelude() + [assistant(call("finish_report", self.report, "finish"))]
        model = FakeModel(script + script)
        first, _ = self.run_agent(model)
        second, events = self.run_agent(model)
        self.assertEqual(first.status, "completed")
        self.assertEqual(second.status, "completed")
        self.assertEqual(model.usage["total_tokens"], 60)
        self.assertEqual(first.usage["total_tokens"], 30)
        self.assertEqual(second.usage["total_tokens"], 30)
        self.assertEqual(events[-1]["usage"]["total_tokens"], 30)

    def test_fabricated_quote_is_rejected_then_corrected_using_observation(self):
        fabricated = deepcopy(self.report)
        fabricated["requirements"][0]["evidence"][0]["quote"] = "独立部署了生产级 Python 服务"
        model = FakeModel(self.prelude() + [
            assistant(call("finish_report", fabricated, "bad_quote")),
            assistant(call("finish_report", self.report, "fixed_quote")),
        ])
        result, events = self.run_agent(model)
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.report, self.report)
        tools = [event for event in events if event["event"] == "tool_result"]
        self.assertFalse(tools[2]["observation"]["ok"])
        self.assertIn("quote", tools[2]["observation"]["error"])
        repair_observation = json.loads(model.history[3]["messages"][-1]["content"])
        self.assertFalse(repair_observation["ok"])
        self.assertEqual((result.model_calls, result.tool_calls), (4, 4))

    def test_unseen_evidence_id_cannot_be_used_even_if_it_exists(self):
        hidden = self.store.add_text("实现 HTTP 服务与文件上传接口。", "project", "另一个项目")
        hidden_chunk = self.store.search("HTTP")[0]
        self.assertEqual(hidden_chunk["document_id"], hidden["document_id"])
        fabricated = deepcopy(self.report)
        fabricated["requirements"][0]["evidence"] = [{
            "chunk_id": hidden_chunk["chunk_id"], "quote": "实现 HTTP 服务与文件上传接口",
        }]
        model = FakeModel(self.prelude() + [assistant(call("finish_report", fabricated, "unseen"))])
        result, events = self.run_agent(model, max_turns=3)
        self.assertEqual(result.status, "limit_reached")
        self.assertIsNone(result.report)
        failure = [event for event in events if event["event"] == "tool_result"][-1]
        self.assertFalse(failure["observation"]["ok"])
        self.assertIn("not returned during this run", failure["observation"]["error"])

    def test_unknown_tool_and_invalid_json_are_failed_observations_then_recover(self):
        read, search = self.prelude()
        model = FakeModel([
            read,
            assistant(call("execute_shell", {"command": "do not execute"}, "unknown")),
            assistant(call("search_evidence", '{"query":', "malformed", raw=True)),
            search,
            assistant(call("finish_report", self.report, "finish")),
        ])
        result, events = self.run_agent(model)
        self.assertEqual(result.status, "completed")
        tool_events = [event for event in events if event["event"] == "tool_result"]
        self.assertEqual([event["observation"]["ok"] for event in tool_events], [True, False, False, True, True])
        self.assertIn("未知工具", tool_events[1]["observation"]["error"])
        self.assertIsNone(tool_events[2]["arguments"])
        self.assertEqual((result.model_calls, result.tool_calls), (5, 5))

    def test_free_text_only_model_stops_at_turn_budget_without_report(self):
        model = FakeModel([assistant(content="我已完成所有分析。")], repeat_last=True)
        result, events = self.run_agent(model, max_turns=3)
        self.assertEqual(result.status, "limit_reached")
        self.assertIsNone(result.report)
        self.assertEqual((result.model_calls, result.tool_calls), (3, 0))
        self.assertEqual(events[-1]["status"], "limit_reached")
        self.assertIn("finish_report", model.history[1]["messages"][-1]["content"])

    def test_repeated_search_cannot_loop_forever(self):
        model = FakeModel([assistant(call("search_evidence", {"query": "Python"}))], repeat_last=True)
        result, events = self.run_agent(model, max_turns=3)
        self.assertEqual(result.status, "limit_reached")
        self.assertEqual((result.model_calls, result.tool_calls), (3, 3))
        self.assertIsNone(result.report)
        self.assertEqual(len([event for event in events if event["event"] == "tool_result"]), 3)

    def test_tool_budget_stops_partway_through_batch_without_executing_next_tool(self):
        model = FakeModel([assistant(
            call("read_job", {"job_id": self.job_id}, "read"),
            call("search_evidence", {"query": "Python"}, "search"),
        )])
        result, events = self.run_agent(model, max_tool_calls=1)
        self.assertEqual(result.status, "limit_reached")
        self.assertEqual((result.model_calls, result.tool_calls), (1, 1))
        self.assertEqual([event["tool"] for event in events if event["event"] == "tool_result"], ["read_job"])
        self.assertIsNone(result.report)

    def test_nonexistent_or_candidate_job_id_fails_before_model_call(self):
        for job_id in ("does-not-exist", self.resume["document_id"]):
            with self.subTest(job_id=job_id):
                model = FakeModel([])
                result, events = self.run_agent(model, job_id=job_id)
                self.assertEqual(result.status, "failed")
                self.assertEqual((result.model_calls, result.tool_calls), (0, 0))
                self.assertEqual(model.history, [])
                self.assertEqual(events[-1]["status"], "failed")

    def test_tool_cannot_read_a_different_job_and_can_recover(self):
        other_job = self.store.add_text("其他岗位：熟悉 Java 和 SQL。", "job", "其他岗位")
        model = FakeModel([
            assistant(call("read_job", {"job_id": other_job["document_id"]}, "wrong_job")),
            *self.prelude(),
            assistant(call("finish_report", self.report, "finish")),
        ])
        result, events = self.run_agent(model)
        self.assertEqual(result.status, "completed")
        first_tool = [event for event in events if event["event"] == "tool_result"][0]
        self.assertFalse(first_tool["observation"]["ok"])
        self.assertIn("本次指定的岗位", first_tool["observation"]["error"])

    def test_context_budget_can_stop_before_first_or_next_model_call(self):
        model = FakeModel([])
        result, _ = self.run_agent(model, max_context_chars=1)
        self.assertEqual(result.status, "limit_reached")
        self.assertEqual(result.model_calls, 0)
        verbose = FakeModel([assistant(
            call("read_job", {"job_id": self.job_id}), content="x" * 6000,
        )])
        result, _ = self.run_agent(verbose, max_context_chars=5000)
        self.assertEqual(result.status, "limit_reached")
        self.assertEqual((result.model_calls, result.tool_calls), (1, 1))
        self.assertIn("字符预算", result.error)

    def test_finish_requires_read_job_and_candidate_retrieval(self):
        model = FakeModel([
            assistant(call("finish_report", self.report, "premature")),
            self.prelude()[0],
            assistant(call("finish_report", self.report, "still_premature")),
            self.prelude()[1],
            assistant(call("finish_report", self.report, "complete")),
        ])
        result, events = self.run_agent(model)
        self.assertEqual(result.status, "completed")
        attempts = [event for event in events if event["event"] == "tool_result" and event["tool"] == "finish_report"]
        self.assertEqual([event["observation"]["ok"] for event in attempts], [False, False, True])
        self.assertIn("read_job", attempts[1]["observation"]["error"])

    def test_empty_results_support_unknown_status_instead_of_fabricated_evidence(self):
        unknown = deepcopy(self.report)
        for requirement in unknown["requirements"]:
            requirement.update(status="not_evidenced", reasoning="本次检索未取得对应材料。", evidence=[])
        model = FakeModel([
            self.prelude()[0],
            assistant(call("search_evidence", {"query": "unrelated_nonexistent_skill"}, "empty")),
            assistant(call("finish_report", unknown, "unknown_report")),
        ])
        result, events = self.run_agent(model)
        self.assertEqual(result.status, "completed")
        empty = [event for event in events if event["event"] == "tool_result"][1]
        self.assertEqual(empty["observation"]["result"]["chunks"], [])
        self.assertTrue(all(item["status"] == "not_evidenced" for item in result.report["requirements"]))

    def test_model_transport_error_and_malformed_protocol_fail_with_trace(self):
        invalid = assistant(
            call("read_job", {"job_id": self.job_id}, "duplicate"),
            call("search_evidence", {"query": "Python"}, "duplicate"),
        )
        for response in (ModelError("simulated transport failure"), invalid):
            with self.subTest(response=type(response).__name__):
                result, events = self.run_agent(FakeModel([response]))
                self.assertEqual(result.status, "failed")
                self.assertEqual(result.model_calls, 1)
                self.assertEqual(result.tool_calls, 0)
                self.assertIsNone(result.report)
                self.assertEqual(events[-1]["event"], "run_finished")


if __name__ == "__main__":
    unittest.main()
