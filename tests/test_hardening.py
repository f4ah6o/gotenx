import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from gotenx import benchmark, cli, store
from gotenx.llm import (
    ModelResult,
    doctor_adapters,
    extract_json_array,
    extract_json_object,
    merge_adapters,
    parse_codex_events,
)
from gotenx.orchestrator import run_staged


class TestBoundedJsonExtraction(unittest.TestCase):
    def test_first_complete_value_does_not_greedily_merge(self):
        self.assertEqual(extract_json_array('prefix [1, {"x": "]"}] trailing [2]'), [1, {"x": "]"}])
        self.assertEqual(extract_json_object('log {"a": "}"} next {"b": 2}'), {"a": "}"})

    def test_wrong_type_value_is_skipped_as_a_unit(self):
        text = 'schema {"example": ["not-output"]} actual [{"kind": "insight"}]'
        self.assertEqual(extract_json_array(text), [{"kind": "insight"}])

    def test_oversized_payload_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "exceeds"):
            extract_json_array(" " * 20 + "[]", max_chars=10)


class TestCodexAdapter(unittest.TestCase):
    def test_codex_jsonl_extracts_message_and_usage(self):
        raw = "\n".join([
            json.dumps({"type": "thread.started", "thread_id": "t"}),
            json.dumps({
                "type": "item.completed",
                "item": {"type": "agent_message", "text": '[{"kind":"insight","content":"x"}]'},
            }),
            json.dumps({
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 12,
                    "cached_input_tokens": 3,
                    "output_tokens": 4,
                    "reasoning_output_tokens": 2,
                },
            }),
        ])
        result = parse_codex_events(raw)
        self.assertIn('"insight"', result.text)
        self.assertEqual(result.usage["tokens"]["input"], 12)
        self.assertEqual(result.usage["tokens"]["cache_read"], 3)
        self.assertEqual(result.usage["tokens"]["reasoning"], 2)

    def test_default_codex_adapter_is_read_only_and_structured(self):
        codex = merge_adapters()["codex"]
        self.assertIn("--json", codex["argv"])
        self.assertIn("read-only", codex["argv"])
        self.assertIn("--ephemeral", codex["argv"])
        self.assertIn("{cwd}", codex["argv"])

    def test_doctor_reports_resolved_binary_without_model_call(self):
        proc = SimpleNamespace(returncode=0, stdout="codex-cli 1.0\n", stderr="")
        with mock.patch("gotenx.llm.shutil.which", return_value="/usr/bin/codex"), \
             mock.patch("gotenx.llm.subprocess.run", return_value=proc) as run:
            result = doctor_adapters(merge_adapters(), ["codex"], cwd=Path("/tmp"))
        self.assertTrue(result["ok"])
        self.assertEqual(run.call_args.args[0], ["codex", "--version"])


class SequenceTransport:
    mode = "real"

    def __init__(self, values):
        self.values = iter(values)

    def invoke_model(self, stage, prompt):
        value = next(self.values)
        if isinstance(value, Exception):
            raise value
        return value


def one_stage_policy():
    return SimpleNamespace(
        stages=[{
            "id": "scout", "source": "scout", "role": "scout", "model": "m",
            "max_items": 3, "max_content_chars": 100,
        }],
        orchestration={"readonly_agent": "gotenx-readonly"},
        cost_budget={
            "reserve_per_run_usd": 0.0,
            "windows_usd": {"5h": 100.0, "7d": 100.0, "30d": 100.0},
        },
    )


class TestRetryAccounting(unittest.TestCase):
    def result(self, text, cost):
        return ModelResult(text=text, usage={"cost_usd": cost, "tokens": {"input": 1}}, raw_events=[])

    def test_retry_success_counts_each_call_once(self):
        transport = SequenceTransport([
            self.result("not json", 0.02),
            self.result('[{"kind":"insight","content":"ok"}]', 0.03),
        ])
        with tempfile.TemporaryDirectory() as tmp:
            result = run_staged("task", one_stage_policy(), transport, root=Path(tmp))
        self.assertEqual(result["status"], "ok")
        self.assertAlmostEqual(result["usage"]["cost_usd"], 0.05)
        self.assertAlmostEqual(result["stages"][0]["usage"]["cost_usd"], 0.05)
        self.assertEqual([a["status"] for a in result["stages"][0]["attempts"]], ["invalid_output", "accepted"])

    def test_retry_failure_still_counts_both_metered_calls(self):
        transport = SequenceTransport([
            self.result("bad", 0.02),
            self.result("still bad", 0.03),
        ])
        with tempfile.TemporaryDirectory() as tmp:
            result = run_staged("task", one_stage_policy(), transport, root=Path(tmp))
        self.assertEqual(result["status"], "blocked")
        self.assertAlmostEqual(result["usage"]["cost_usd"], 0.05)
        self.assertEqual(len(result["failure"]["attempts"]), 2)

    def test_transport_exception_has_zero_cost(self):
        transport = SequenceTransport([
            RuntimeError("offline"),
            self.result('[{"kind":"insight","content":"ok"}]', 0.03),
        ])
        with tempfile.TemporaryDirectory() as tmp:
            result = run_staged("task", one_stage_policy(), transport, root=Path(tmp))
        self.assertEqual(result["status"], "ok")
        self.assertAlmostEqual(result["usage"]["cost_usd"], 0.03)

    def test_unmetered_real_output_fails_without_retry(self):
        transport = SequenceTransport([
            ModelResult('[{"kind":"insight","content":"x"}]', {}, []),
            self.result('[{"kind":"insight","content":"should not run"}]', 0.5),
        ])
        with tempfile.TemporaryDirectory() as tmp:
            result = run_staged("task", one_stage_policy(), transport, root=Path(tmp))
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["usage"]["cost_usd"], 0.0)
        self.assertEqual(len(result["failure"]["attempts"]), 1)
        self.assertEqual(result["failure"]["attempts"][0]["status"], "invalid_usage")


class TestInputAndBenchmarkAccounting(unittest.TestCase):
    def test_empty_task_fails_before_state_or_transport(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.dict("os.environ", {"GOTENX_PROJECT_DIR": tmp}, clear=False), \
             contextlib.redirect_stdout(io.StringIO()), \
             contextlib.redirect_stderr(io.StringIO()):
            rc = cli.main(["run"])
            self.assertEqual(rc, 64)
            self.assertFalse(store.gotenx_dir(Path(tmp)).exists())

    def test_run_case_reports_full_cost_breakdown(self):
        policy = SimpleNamespace(
            benchmark_cfg={
                "baseline_models": [
                    {"id": "base-a", "variant": "high"},
                    {"id": "base-b", "variant": "high"},
                ]
            },
            cost_budget={
                "reserve_per_run_usd": 0.0,
                "windows_usd": {"5h": 100.0, "7d": 100.0, "30d": 100.0},
            },
        )
        grade = json.dumps({
            "preference": "tie", "reason": "equal",
            "scores": {key: 4 for key in benchmark.GRADE_DIMENSIONS},
        })
        transport = SequenceTransport([
            ModelResult("baseline a", {"cost_usd": 0.6}, []),
            ModelResult("baseline b", {"cost_usd": 0.7}, []),
            ModelResult(grade, {"cost_usd": 0.1}, []),
            ModelResult(grade, {"cost_usd": 0.2}, []),
        ])
        candidate = {
            "status": "ok", "failure": None,
            "judge": {"items": [{"content": "candidate"}]},
            "usage": {"cost_usd": 0.4},
        }
        case = {"id": "c1", "task": "review", "context": "", "kind": "review", "origin": "synthetic", "language": "python"}
        recorded = []
        with tempfile.TemporaryDirectory() as tmp, mock.patch("gotenx.benchmark.run_staged", return_value=candidate):
            result = benchmark.run_case(
                case, policy, transport, seed=1, root=Path(tmp),
                record_cost=lambda role, cost: recorded.append((role, cost)),
            )
        self.assertEqual(recorded, [
            ("candidate", 0.4), ("baseline:0", 0.6), ("baseline:1", 0.7),
            ("grader:0", 0.1), ("grader:1", 0.2),
        ])
        self.assertAlmostEqual(result["cost"]["candidate_usd"], 0.4)
        self.assertAlmostEqual(result["cost"]["baseline_usd"], 1.3)
        self.assertAlmostEqual(result["cost"]["grader_usd"], 0.3)
        self.assertAlmostEqual(result["cost"]["total_usd"], 2.0)


if __name__ == "__main__":
    unittest.main()
