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


if __name__ == "__main__":
    unittest.main()
