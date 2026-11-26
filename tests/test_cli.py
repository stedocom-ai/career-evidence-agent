import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class CliTests(unittest.TestCase):
    def call(self, *args, env=None):
        return subprocess.run([sys.executable, "-m", "career_agent", *map(str, args)],
                              capture_output=True, text=True, encoding="utf-8", env=env)

    def test_offline_demo_outputs_valid_report_and_trace(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "demo"
            result = self.call("demo", "--output", output)
            self.assertEqual(result.returncode, 0, result.stderr)
            run = json.loads((output / "run.json").read_text(encoding="utf-8"))
            self.assertEqual(run["status"], "completed")
            self.assertEqual(run["mode"], "scripted_demo")
            self.assertEqual(run["model_calls"], 5)
            self.assertEqual(run["tool_calls"], 5)
            report = json.loads((output / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(len(report["requirements"]), 3)
            self.assertEqual(report["requirements"][2]["status"], "not_evidenced")
            self.assertIn("脚本演示", (output / "report.md").read_text(encoding="utf-8"))
            events = [json.loads(line) for line in (output / "trace.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(events[-1]["event"], "run_finished")
            self.assertEqual(self.call("demo", "--output", output).returncode, 2)

    def test_live_mode_missing_config_never_falls_back_or_creates_report(self):
        env = {key: val for key, val in os.environ.items() if not key.startswith("AGENT_")}
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "report"
            result = self.call("analyze", "--job", "missing", "--output", output, env=env)
            self.assertEqual(result.returncode, 2)
            self.assertIn("AGENT_API_KEY", result.stderr)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
