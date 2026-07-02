"""CLI parsing and run-status fail-closed tests (issues #2/#3)."""

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gotenx import cli
from gotenx import store
from gotenx.cli import build_parser, _resolve_task

ROOT = Path(__file__).resolve().parent.parent
POLICY_PATH = ROOT / "config" / "policy.json"


class TestRunArgParsing(unittest.TestCase):  # issue #3
    def setUp(self):
        self.parser = build_parser()

    def test_positional_task_words_joined(self):
        args = self.parser.parse_args(["run", "review", "this", "repo"])
        self.assertEqual(_resolve_task(args), "review this repo")

    def test_task_flag_still_works(self):
        args = self.parser.parse_args(["run", "--task", "review this repo"])
        self.assertEqual(_resolve_task(args), "review this repo")

    def test_task_flag_wins_over_positional(self):
        args = self.parser.parse_args(["run", "a", "b", "--task", "c"])
        self.assertEqual(_resolve_task(args), "c")

    def test_replay_without_task(self):
        args = self.parser.parse_args(["run", "--replay", "golden/case-001"])
        self.assertEqual(args.replay, "golden/case-001")
        self.assertEqual(_resolve_task(args), "")


class TestLegacyRunFailClosed(unittest.TestCase):  # issue #2
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        self.env_patch = mock.patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": str(self.root)})
        self.env_patch.start()
        store.ensure_layout()

        template = json.loads(POLICY_PATH.read_text())
        template["orchestration"] = {"stages": []}
        template.setdefault("_epoch", 0)
        store.write_json(store.policy_path(), template)
        store.write_json(store.baseline_path(), {})

    def tearDown(self):
        self.env_patch.stop()
        self.tmpdir.cleanup()

    def _run(self, panel, judge):
        with mock.patch("gotenx.cli.run_panel", return_value=panel), \
             mock.patch("gotenx.cli.run_judge", return_value=judge), \
             contextlib.redirect_stdout(io.StringIO()):
            return cli.main(["run", "--task", "x"])

    def test_empty_panel_fails_closed(self):
        rc = self._run(
            panel={"insights": [], "panels": [], "warnings": ["all sources failed"]},
            judge={"items": [], "warnings": []},
        )
        self.assertEqual(rc, 2)
        run_id = store.list_runs()[-1]
        meta = store.read_json(store.runs_dir() / run_id / "metadata.json")
        self.assertEqual(meta["status"], "failed")
        self.assertEqual(meta["failure"]["reason"], "panel_empty")
        self.assertIn("all sources failed", meta["warnings"])

    def test_empty_judge_fails_closed(self):
        rc = self._run(
            panel={"insights": [{"id": "claude:insight:001", "source": "claude",
                                  "kind": "insight", "content": "x"}],
                   "panels": ["claude"], "warnings": []},
            judge={"items": [], "warnings": ["judge unavailable"]},
        )
        self.assertEqual(rc, 2)
        run_id = store.list_runs()[-1]
        meta = store.read_json(store.runs_dir() / run_id / "metadata.json")
        self.assertEqual(meta["status"], "failed")
        self.assertEqual(meta["failure"]["reason"], "judge_empty")

    def test_ungrounded_judge_items_fail_closed(self):
        rc = self._run(
            panel={"insights": [{"id": "claude:insight:001", "source": "claude",
                                  "kind": "insight", "content": "x"}],
                   "panels": ["claude"], "warnings": []},
            judge={"items": [{"id": "judge:plan:001",
                               "source_ids": ["claude:insight:999"],
                               "content": "y"}], "warnings": []},
        )
        self.assertEqual(rc, 2)
        run_id = store.list_runs()[-1]
        meta = store.read_json(store.runs_dir() / run_id / "metadata.json")
        self.assertEqual(meta["status"], "failed")
        self.assertEqual(meta["failure"]["reason"], "no_grounded_judge_items")

    def test_healthy_run_reports_ok(self):
        rc = self._run(
            panel={"insights": [{"id": "claude:insight:001", "source": "claude",
                                  "kind": "insight", "content": "x"}],
                   "panels": ["claude"], "warnings": []},
            judge={"items": [{"id": "judge:plan:001",
                               "source_ids": ["claude:insight:001"],
                               "content": "y"}], "warnings": []},
        )
        self.assertEqual(rc, 0)
        run_id = store.list_runs()[-1]
        meta = store.read_json(store.runs_dir() / run_id / "metadata.json")
        self.assertEqual(meta["status"], "ok")
        self.assertNotIn("failure", meta)


if __name__ == "__main__":
    unittest.main()
