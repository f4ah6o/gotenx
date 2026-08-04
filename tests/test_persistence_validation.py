import contextlib
import io
import json
import multiprocessing
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from gotenx import cli, policy as policy_mod, store, usage
from gotenx.validation import DataValidationError

ROOT = Path(__file__).resolve().parent.parent
POLICY = json.loads((ROOT / "config" / "policy.json").read_text())


def _allocate_run_id(root: str, queue) -> None:
    queue.put(store.new_run_id(Path(root)))



def _save_unique_run(root: str, index: int, queue) -> None:
    path = Path(root)
    run_id = store.new_run_id(path)
    panel = {"worker": index}
    metadata = store.make_metadata(run_id, "replay", "ok", [], [])
    store.save_run(run_id, panel, {}, {}, metadata, path)
    queue.put((run_id, index))


def _attempt_foreign_save(root: str, run_id: str, queue) -> None:
    metadata = store.make_metadata(run_id, "replay", "ok", [], [])
    try:
        store.save_run(run_id, {"foreign": True}, {}, {}, metadata, Path(root))
    except Exception as exc:
        queue.put(type(exc).__name__)
    else:
        queue.put("published")


def _record_usage(root: str, index: int) -> None:
    usage.record(f"worker-{index}", 0.01, root=Path(root),
                 now=datetime(2026, 8, 4, 13, 0, tzinfo=timezone.utc))


class TestConcurrentPersistence(unittest.TestCase):
    def test_run_id_reservations_are_unique_across_processes(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = multiprocessing.get_context("fork")
            queue = ctx.Queue()
            processes = [ctx.Process(target=_allocate_run_id, args=(tmp, queue)) for _ in range(12)]
            for process in processes:
                process.start()
            ids = [queue.get(timeout=10) for _ in processes]
            for process in processes:
                process.join(10)
                self.assertEqual(process.exitcode, 0)
            self.assertEqual(len(ids), len(set(ids)))

    def test_concurrent_run_publication_never_mixes_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = multiprocessing.get_context("fork")
            queue = ctx.Queue()
            processes = [ctx.Process(target=_save_unique_run, args=(tmp, index, queue)) for index in range(8)]
            for process in processes:
                process.start()
            published = [queue.get(timeout=10) for _ in processes]
            for process in processes:
                process.join(10)
                self.assertEqual(process.exitcode, 0)
            for run_id, index in published:
                panel = store.read_json(store.runs_dir(Path(tmp)) / run_id / "panel.json")
                self.assertEqual(panel, {"worker": index})

    def test_foreign_process_cannot_publish_another_process_reservation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = store.new_run_id(root)
            ctx = multiprocessing.get_context("fork")
            queue = ctx.Queue()
            process = ctx.Process(target=_attempt_foreign_save, args=(tmp, run_id, queue))
            process.start()
            self.assertEqual(queue.get(timeout=10), "DataValidationError")
            process.join(10)
            self.assertEqual(process.exitcode, 0)
            self.assertTrue((store.reservations_dir(root) / run_id).exists())
            metadata = store.make_metadata(run_id, "replay", "ok", [], [])
            store.save_run(run_id, {"owner": True}, {}, {}, metadata, root)
            self.assertEqual(store.read_json(store.runs_dir(root) / run_id / "panel.json"), {"owner": True})

    def test_concurrent_usage_updates_preserve_every_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = multiprocessing.get_context("fork")
            processes = [ctx.Process(target=_record_usage, args=(tmp, index)) for index in range(16)]
            for process in processes:
                process.start()
            for process in processes:
                process.join(10)
                self.assertEqual(process.exitcode, 0)
            entries = usage.read_ledger(Path(tmp))
            self.assertEqual(len(entries), 16)
            self.assertEqual({entry["run_id"] for entry in entries}, {f"worker-{i}" for i in range(16)})

    def test_normal_process_exit_releases_unpublished_reservation(self):
        with tempfile.TemporaryDirectory() as tmp:
            code = (
                "from pathlib import Path; "
                "from gotenx import store; "
                "print(store.new_run_id(Path(r'" + tmp + "')))"
            )
            proc = subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                                  capture_output=True, text=True, check=True)
            run_id = proc.stdout.strip()
            self.assertFalse((store.reservations_dir(Path(tmp)) / run_id).exists())

    def test_stale_reservations_and_temp_runs_are_recovered(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store.ensure_layout(root)
            stale_reservation = store.reservations_dir(root) / "stale-run"
            stale_reservation.mkdir()
            (stale_reservation / "owner-pid").write_text("999999")
            stale_temp = store.runs_dir(root) / ".tmp-stale-run-x"
            stale_temp.mkdir()
            old = 1_600_000_000
            os.utime(stale_reservation, (old, old))
            os.utime(stale_temp, (old, old))
            store.new_run_id(root)
            self.assertFalse(stale_reservation.exists())
            self.assertFalse(stale_temp.exists())

    def test_failed_atomic_replace_preserves_previous_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "policy.json"
            store.write_json_atomic(path, {"version": 1})
            with mock.patch("gotenx.store.os.replace", side_effect=OSError("interrupted")):
                with self.assertRaises(OSError):
                    store.write_json_atomic(path, {"version": 2})
            self.assertEqual(store.read_json(path), {"version": 1})
            self.assertFalse(any(child.name.startswith(".policy.json.") for child in path.parent.iterdir()))

    def test_failed_run_publication_leaves_no_visible_partial_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = store.new_run_id(root)
            metadata = store.make_metadata(run_id, "replay", "ok", [], [])
            original = store.write_json_atomic
            calls = 0

            def fail_second(path, data):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("simulated interruption")
                return original(path, data)

            with mock.patch("gotenx.store.write_json_atomic", side_effect=fail_second):
                with self.assertRaises(OSError):
                    store.save_run(run_id, {}, {}, {}, metadata, root)
            self.assertNotIn(run_id, store.list_runs(root))
            self.assertFalse((store.reservations_dir(root) / run_id).exists())
            self.assertFalse(any(path.name.startswith(".tmp-") for path in store.runs_dir(root).iterdir()))


class TestStrictValidation(unittest.TestCase):
    def _initialized_root(self, root: Path) -> None:
        store.ensure_layout(root)
        policy = json.loads(json.dumps(POLICY))
        policy["_epoch"] = 0
        policy.pop("_baseline_pending", None)
        store.write_json(store.policy_path(root), policy)
        store.write_json(store.baseline_path(root), {})
        store.write_json(store.adapters_path(root), json.loads((ROOT / "config" / "adapters.json").read_text()))

    def _run_cli(self, root: Path, argv: list[str]):
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {"GOTENX_PROJECT_DIR": str(root)}, clear=False), \
             contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            rc = cli.main(argv)
        return rc, json.loads(stdout.getvalue()), stderr.getvalue()

    def test_corrupt_policy_returns_machine_readable_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._initialized_root(root)
            store.policy_path(root).write_text('{"schema_version":')
            rc, result, stderr = self._run_cli(root, ["status"])
            self.assertEqual(rc, 2)
            self.assertEqual(result["error"]["code"], "invalid_json")
            self.assertIn("policy.json", result["error"]["path"])
            self.assertNotIn("Traceback", stderr)

    def test_policy_rejects_nested_wrong_types_and_boolean_numbers(self):
        broken = json.loads(json.dumps(POLICY))
        broken["orchestration"]["stages"][0] = "not-an-object"
        with self.assertRaises(DataValidationError):
            policy_mod.from_dict(broken)
        broken = json.loads(json.dumps(POLICY))
        broken["cost_budget"]["windows_usd"]["5h"] = True
        with self.assertRaises(DataValidationError):
            policy_mod.from_dict(broken)

    def test_ledger_rejects_invalid_timestamp_negative_and_nan(self):
        invalid = [
            {"run_id": "x", "timestamp": "not-a-time", "cost_usd": 1.0},
            {"run_id": "x", "timestamp": "2026-08-04T00:00:00+00:00", "cost_usd": -1.0},
        ]
        for entry in invalid:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                store.ensure_layout(root)
                store.write_json(store.usage_ledger_path(root), {"entries": [entry]})
                with self.assertRaises(DataValidationError):
                    usage.read_ledger(root)
        with tempfile.TemporaryDirectory() as tmp:
            path = store.usage_ledger_path(Path(tmp))
            path.parent.mkdir(parents=True)
            path.write_text('{"entries":[{"run_id":"x","timestamp":"2026-08-04T00:00:00+00:00","cost_usd":NaN}]}')
            with self.assertRaises(DataValidationError):
                usage.read_ledger(Path(tmp))

    def test_invalid_baseline_proposal_and_benchmark_are_reported_without_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._initialized_root(root)
            store.write_json(store.baseline_path(root), {"metric": True})
            rc, result, stderr = self._run_cli(root, ["status"])
            self.assertEqual(rc, 2)
            self.assertEqual(result["error"]["field"], "$.metric")
            self.assertNotIn("Traceback", stderr)

            store.write_json(store.baseline_path(root), {})
            proposal = root / "proposal.json"
            proposal.write_text('{"id":"p","changes":[],"provenance":[],"evidence":{"support_runs":true}}')
            rc, result, stderr = self._run_cli(root, ["propose", str(proposal)])
            self.assertEqual(rc, 2)
            self.assertEqual(result["error"]["field"], "$.changes")
            self.assertNotIn("Traceback", stderr)

            results = root / "results.json"
            results.write_text('{"cases":"not-a-list"}')
            rc, result, stderr = self._run_cli(root, ["benchmark", "report", "--results", str(results)])
            self.assertEqual(rc, 2)
            self.assertEqual(result["error"]["field"], "$.cases")
            self.assertNotIn("Traceback", stderr)

    def test_invalid_replay_case_fails_eval_cleanly(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._initialized_root(root)
            golden = root / "golden" / "case-bad"
            golden.mkdir(parents=True)
            store.write_json(golden / "case.json", {
                "id": "bad", "mode": "replay", "task": [], "expected": {}
            })
            rc, result, stderr = self._run_cli(root, ["eval", "--golden", str(root / "golden")])
            self.assertEqual(rc, 2)
            self.assertEqual(result["error"]["field"], "$.task")
            self.assertNotIn("Traceback", stderr)


if __name__ == "__main__":
    unittest.main()
