import unittest
from pathlib import Path

from gotenx.evalrun import eval_suite, eval_case
from gotenx.policy import load_policy

ROOT = Path(__file__).resolve().parent.parent
POLICY_PATH = ROOT / "config" / "policy.json"
GOLDEN = ROOT / "golden"


class TestEval(unittest.TestCase):
    def setUp(self):
        self.policy = load_policy(POLICY_PATH)

    def test_golden_cases_all_pass(self):
        # golden/ is the known-good guardrail suite: every case must pass.
        for case in ("case-001", "case-002"):
            res = eval_case(GOLDEN / case, self.policy, baseline={})
            self.assertTrue(res["passed"], (case, res["failures"]))
            self.assertEqual(res["runs"], 5)  # P3/P19 repeated runs

    def test_regression_case_fails_with_actuals(self):  # P8/P21
        # Build a deliberate regression inline (Judge drops most insights).
        import json
        import shutil
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            case_dir = Path(d) / "case-regress"
            shutil.copytree(GOLDEN / "case-001", case_dir)
            (case_dir / "stages" / "glm.raw.txt").write_text(
                json.dumps([{"kind": "plan", "content": "Just cache it.",
                             "source_ids": ["flash:insight:001"]}])
            )
            res = eval_case(case_dir, self.policy,
                            baseline={"panel_insight_survival_rate": 0.8})
            self.assertFalse(res["passed"])
            f = next(x for x in res["failures"]
                     if x["metric"] == "panel_insight_survival_rate")
            self.assertIn("actual", f)
            self.assertIn("expected", f)
            self.assertIn("baseline", f)

    def test_suite_cost_is_runs_x_cases_x_panels(self):  # P19
        res = eval_suite(GOLDEN, self.policy, baseline={})
        self.assertEqual(res["cost"]["runs_x_cases_x_panels"], 5 * 2 * 3)

    def test_undefined_mode_blocked(self):  # P2
        import json
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            case_dir = Path(d) / "case-x"
            case_dir.mkdir()
            (case_dir / "case.json").write_text(json.dumps(
                {"id": "x", "mode": "frobnicate", "task": "t", "expected": {}}
            ))
            res = eval_case(case_dir, self.policy, baseline={})
            self.assertEqual(res["status"], "blocked")
            self.assertFalse(res["passed"])

    def test_determinism_replay(self):
        a = eval_case(GOLDEN / "case-001", self.policy, baseline={})["metrics"]
        b = eval_case(GOLDEN / "case-001", self.policy, baseline={})["metrics"]
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
