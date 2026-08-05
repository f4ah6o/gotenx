import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from gotenx import benchmark, policy as policy_mod, usage
from gotenx.llm import Transport, parse_opencode_events
from gotenx.metrics import compute_all
from gotenx.orchestrator import run_staged
from gotenx.provenance import build_graph


ROOT = Path(__file__).resolve().parent.parent


class TestOpenCodeEvents(unittest.TestCase):
    def test_extracts_text_tokens_and_cost(self):
        raw = "\n".join([
            json.dumps({"type": "text", "part": {"text": "[{\"kind\":\"insight\",\"content\":\"x\"}]"}}),
            json.dumps({"type": "step_finish", "part": {"cost": 0.012, "tokens": {
                "input": 100, "output": 20, "reasoning": 5,
                "cache": {"read": 50, "write": 0},
            }}}),
        ])
        result = parse_opencode_events(raw)
        self.assertIn('"insight"', result.text)
        self.assertEqual(result.usage["cost_usd"], 0.012)
        self.assertEqual(result.usage["tokens"]["cache_read"], 50)


class TestStagedOrchestration(unittest.TestCase):
    def test_four_stage_replay_builds_compatible_graph(self):
        policy = policy_mod.load_policy(ROOT / "config" / "policy.json")
        fixtures = {
            "flash": [{"kind": "insight", "content": "measure first"}],
            "kimi": [{"kind": "proposal", "content": "add metrics", "source_ids": ["flash:insight:001"]}],
            "deepseek_pro": [{"kind": "risk", "content": "avoid cardinality", "source_ids": ["kimi:proposal:001"]}],
            "glm": [{"kind": "plan", "content": "instrument with bounded labels", "source_ids": [
                "flash:insight:001", "kimi:proposal:001", "deepseek_pro:risk:001"
            ]}],
        }
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "stages"
            base.mkdir()
            for stage, payload in fixtures.items():
                (base / f"{stage}.raw.txt").write_text(json.dumps(payload))
            result = run_staged("improve latency", policy, Transport(mode="replay", replay_dir=Path(tmp)), root=Path(tmp))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(result["stages"]), 4)
        graph = build_graph(result["panel"], result["judge"])
        configured = [s["source"] for s in policy.stages if s["role"] != "judge"]
        metrics = compute_all(graph, configured)
        self.assertEqual(metrics["panel_insight_survival_rate"], 1.0)
        self.assertEqual(metrics["configured_diversity_index"], 1.0)


class TestMigrationAndBudget(unittest.TestCase):
    def test_v2_migration_advances_epoch_and_installs_stages(self):
        template = json.loads((ROOT / "config" / "policy.json").read_text())
        old = {"schema_version": "gotenx.config.policy.v2", "_epoch": 4,
               "protected_metrics": template["protected_metrics"]}
        migrated = policy_mod.migrate_v2_dict(old, template)
        self.assertEqual(migrated["schema_version"], "gotenx.config.policy.v3")
        self.assertEqual(migrated["_epoch"], 5)
        self.assertEqual(len(migrated["orchestration"]["stages"]), 4)

    def test_legacy_v3_policy_receives_default_capability_floor(self):
        template = json.loads((ROOT / "config" / "policy.json").read_text())
        template["benchmark"].pop("capability_floor")
        policy = policy_mod.from_dict(template)
        self.assertEqual(policy.benchmark_cfg["capability_floor"],
                         policy_mod.DEFAULT_CAPABILITY_FLOOR)

    def test_empty_capability_floor_is_rejected(self):
        template = json.loads((ROOT / "config" / "policy.json").read_text())
        template["benchmark"]["capability_floor"] = {}
        with self.assertRaises(ValueError):
            policy_mod.from_dict(template)

    def test_local_windows_block_reserved_run(self):
        policy = policy_mod.load_policy(ROOT / "config" / "policy.json")
        now = datetime(2026, 6, 18, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            usage.record("r1", 11.95, root=Path(tmp), now=now)
            status = usage.budget_status(policy, root=Path(tmp), now=now)
        self.assertFalse(status["allowed"])
        self.assertIn("5h", status["blocked_windows"])


class TestBenchmarkReport(unittest.TestCase):
    def test_quality_and_cost_acceptance(self):
        cases = []
        for i in range(60):
            cases.append({
                "id": str(i),
                "candidate": {"usage": {"cost_usd": 0.5}},
                "baselines": [{"usage": {"cost_usd": 1.0}}, {"usage": {"cost_usd": 1.0}}],
                "grades": [{"score": 0.5}, {"score": 0.5}],
            })
        result = benchmark.report({"cases": cases}, {
            "seed": 1, "bootstrap_samples": 1000, "confidence": 0.95,
            "noninferiority_margin": 0.10, "max_baseline_ratio": 0.65,
        })
        self.assertTrue(result["passed"])
        self.assertEqual(result["quality"]["lower_confidence_bound"], 0.5)
        self.assertEqual(result["cost"]["ratio"], 0.5)


class TestCapabilityFloor(unittest.TestCase):
    FLOOR = {
        "dimension_mean_min": {
            "correctness": 3.25, "coverage": 3.0, "actionability": 3.0,
            "risk_testing": 2.75, "concision": 2.5,
        },
        "case_mean_min": 2.75,
        "case_correctness_min": 2.5,
        "min_case_pass_rate": 0.9,
        "max_critical_failure_rate": 0.05,
        "critical_failure_votes_required": 2,
    }

    def _case(self, index, *, scores=None, critical=False):
        scores = scores or {key: 4.0 for key in benchmark.GRADE_DIMENSIONS}
        grades = [
            {"score": 0.5, "candidate_scores": dict(scores),
             "baseline_scores": dict(scores), "critical_failure": critical},
            {"score": 0.5, "candidate_scores": dict(scores),
             "baseline_scores": dict(scores), "critical_failure": critical},
        ]
        return {
            "id": f"c{index}",
            "candidate": {"usage": {"cost_usd": 0.5}},
            "baselines": [
                {"usage": {"cost_usd": 1.0}},
                {"usage": {"cost_usd": 1.0}},
            ],
            "grades": grades,
        }

    def _cfg(self, cases):
        return {
            "cases": cases, "seed": 1, "bootstrap_samples": 1000,
            "confidence": 0.95, "noninferiority_margin": 0.10,
            "max_baseline_ratio": 0.65, "capability_floor": self.FLOOR,
        }

    def test_absolute_floor_passes_independently_of_pairwise_gate(self):
        cases = [self._case(i) for i in range(10)]
        result = benchmark.report({"cases": cases}, self._cfg(10))
        self.assertTrue(result["passed"])
        self.assertTrue(result["capability_floor"]["passed"])
        self.assertEqual(result["capability_floor"]["case_pass_rate"], 1.0)

    def test_consensus_critical_failure_breaks_floor(self):
        cases = [self._case(i, critical=(i == 0)) for i in range(10)]
        result = benchmark.report({"cases": cases}, self._cfg(10))
        self.assertFalse(result["passed"])
        self.assertEqual(result["capability_floor"]["critical_failure_rate"], 0.1)
        self.assertFalse(result["capability_floor"]["passed"])

    def test_legacy_checkpoint_is_not_certified_as_measured(self):
        cases = [self._case(i) for i in range(10)]
        for case in cases:
            for grade in case["grades"]:
                grade.pop("candidate_scores")
                grade.pop("critical_failure")
        result = benchmark.report({"cases": cases}, self._cfg(10))
        self.assertFalse(result["passed"])
        self.assertFalse(result["capability_floor"]["measured"])
        self.assertEqual(result["capability_floor"]["reason"], "absolute_scores_missing")


if __name__ == "__main__":
    unittest.main()
