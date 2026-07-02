import json
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from gotenx import cli, store
from gotenx.llm import InvokeResult, Transport
from gotenx.panel import run_panel
from gotenx.judge import run_judge


class TestTransportUsage(unittest.TestCase):
    def _run(self, stdout, adapters=None, source="claude"):
        proc = SimpleNamespace(returncode=0, stdout=stdout, stderr="")
        adapters = adapters or {
            source: {
                "argv": ["tool", "{prompt}"],
                "result_path": "result",
                "usage_path": "usage",
            }
        }
        with patch("gotenx.llm.subprocess.run", return_value=proc):
            return Transport(adapters=adapters).invoke_result(source, "prompt", "slot")

    def test_envelope_result_and_usage(self):
        res = self._run(json.dumps({
            "result": "[{\"kind\":\"insight\",\"content\":\"x\"}]",
            "usage": {"input_tokens": 10, "output_tokens": 2},
        }))
        self.assertEqual(res.raw, '[{"kind":"insight","content":"x"}]')
        self.assertEqual(res.usage, {"input_tokens": 10, "output_tokens": 2})

    def test_invalid_result_keeps_valid_usage(self):
        stdout = json.dumps({
            "result": {"not": "text"},
            "usage": {"input_tokens": 10},
        })
        res = self._run(stdout)
        self.assertEqual(res.raw, stdout)
        self.assertEqual(res.usage, {"input_tokens": 10})

    def test_invalid_usage_becomes_none(self):
        res = self._run(json.dumps({"result": "[]", "usage": ["bad"]}))
        self.assertEqual(res.raw, "[]")
        self.assertIsNone(res.usage)

    def test_non_json_or_non_dict_envelope(self):
        self.assertEqual(self._run("plain text").raw, "plain text")
        self.assertIsNone(self._run("plain text").usage)
        res = self._run(json.dumps(["not", "dict"]))
        self.assertEqual(res.raw, json.dumps(["not", "dict"]))
        self.assertIsNone(res.usage)

    def test_custom_adapter_paths(self):
        res = self._run(
            json.dumps({"message": "[]", "token_usage": {"total": 3}}),
            adapters={
                "custom": {
                    "argv": ["custom", "{prompt}"],
                    "result_path": "message",
                    "usage_path": "token_usage",
                }
            },
            source="custom",
        )
        self.assertEqual(res.raw, "[]")
        self.assertEqual(res.usage, {"total": 3})

    def test_plain_adapter_has_no_usage(self):
        proc = SimpleNamespace(returncode=0, stdout="hello", stderr="")
        adapters = {"codex": {"argv": ["codex", "{prompt}"], "result_path": None}}
        with patch("gotenx.llm.subprocess.run", return_value=proc):
            res = Transport(adapters=adapters).invoke_result("codex", "prompt", "slot")
        self.assertEqual(res.raw, "hello")
        self.assertIsNone(res.usage)

    def test_replay_usage_is_none(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "panel"
            path.mkdir()
            (path / "claude.raw.txt").write_text("[]")
            res = Transport(mode="replay", replay_dir=Path(d)).invoke_result(
                "claude", "prompt", "panel/claude"
            )
        self.assertEqual(res.raw, "[]")
        self.assertIsNone(res.usage)


class FakeTransport:
    def __init__(self, results=None, errors=None):
        self.results = results or {}
        self.errors = errors or {}

    def invoke_result(self, source, prompt, slot):
        if source in self.errors:
            raise self.errors[source]
        return self.results[source]


class TestPanelJudgeUsage(unittest.TestCase):
    def test_panel_records_usage_before_parse(self):
        transport = FakeTransport(results={
            "claude": InvokeResult(raw="not json", usage={"input_tokens": 1}),
            "codex": InvokeResult(
                raw='[{"kind":"insight","content":"ok"}]',
                usage=None,
            ),
        })
        panel = run_panel(["claude", "codex"], "task", transport)
        self.assertEqual(panel["usage"], {"claude": {"input_tokens": 1}, "codex": None})
        self.assertEqual(panel["panels"], ["codex"])
        self.assertEqual(len(panel["insights"]), 1)
        self.assertTrue(panel["warnings"])

    def test_panel_invocation_failure_omits_usage(self):
        transport = FakeTransport(
            results={"codex": InvokeResult(raw="[]", usage=None)},
            errors={"claude": RuntimeError("boom")},
        )
        panel = run_panel(["claude", "codex"], "task", transport)
        self.assertEqual(panel["usage"], {"codex": None})

    def test_judge_records_usage_before_parse(self):
        panel = {"insights": []}
        transport = FakeTransport(results={
            "claude": InvokeResult(raw="not json", usage={"output_tokens": 4})
        })
        judge = run_judge(panel, "claude", transport)
        self.assertEqual(judge["items"], [])
        self.assertEqual(judge["usage"], {"output_tokens": 4})
        self.assertTrue(judge["warnings"])


class TestUsagePersistence(unittest.TestCase):
    def test_save_run_writes_usage_when_provided(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            rdir = store.save_run(
                "run-1",
                {"insights": []},
                {"items": []},
                {},
                {},
                root,
                usage={"panel": {"claude": None}, "judge": None},
            )
            self.assertTrue((rdir / "usage.json").exists())
            self.assertEqual(
                store.read_json(rdir / "usage.json"),
                {"panel": {"claude": None}, "judge": None},
            )

    def test_save_run_omits_usage_when_none(self):
        with tempfile.TemporaryDirectory() as d:
            rdir = store.save_run("run-1", {}, {}, {}, {}, Path(d), usage=None)
            self.assertFalse((rdir / "usage.json").exists())

    def test_cmd_run_real_persists_usage_outside_artifacts(self):
        policy = {
            "schema_version": "gotenx.config.policy.v3",
            "panel": {"sources": ["claude"]},
            "judge": {"source": "claude"},
            "orchestration": {"stages": []},
        }
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            with patch.dict("os.environ", {"CLAUDE_PROJECT_DIR": str(root)}), \
                patch("gotenx.cli._load_applied", return_value=policy), \
                patch("gotenx.cli.run_panel", return_value={
                    "insights": [],
                    "panels": [],
                    "warnings": [],
                    "usage": {"claude": {"input_tokens": 1}},
                }), \
                patch("gotenx.cli.run_judge", return_value={
                    "items": [],
                    "warnings": [],
                    "usage": {"output_tokens": 2},
                }), \
                patch("gotenx.cli._emit"):
                cli.cmd_run(Namespace(replay=None, task="t", human_override=False))
            run_id = store.list_runs(root)[0]
            rdir = store.runs_dir(root) / run_id
            self.assertEqual(
                store.read_json(rdir / "usage.json"),
                {
                    "panel": {"claude": {"input_tokens": 1}},
                    "judge": {"output_tokens": 2},
                },
            )
            self.assertNotIn("usage", store.read_json(rdir / "panel.json"))
            self.assertNotIn("usage", store.read_json(rdir / "judge.json"))

    def test_cmd_run_replay_does_not_persist_usage(self):
        policy = {
            "schema_version": "gotenx.config.policy.v3",
            "panel": {"sources": ["claude"]},
            "judge": {"source": "claude"},
            "orchestration": {"stages": []},
        }
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            with patch.dict("os.environ", {"CLAUDE_PROJECT_DIR": str(root)}), \
                patch("gotenx.cli._load_applied", return_value=policy), \
                patch("gotenx.cli.run_panel", return_value={
                    "insights": [],
                    "panels": [],
                    "warnings": [],
                    "usage": {"claude": None},
                }), \
                patch("gotenx.cli.run_judge", return_value={
                    "items": [],
                    "warnings": [],
                    "usage": None,
                }), \
                patch("gotenx.cli._emit"):
                cli.cmd_run(Namespace(replay=str(root), task="t", human_override=False))
            run_id = store.list_runs(root)[0]
            self.assertFalse((store.runs_dir(root) / run_id / "usage.json").exists())


if __name__ == "__main__":
    unittest.main()
