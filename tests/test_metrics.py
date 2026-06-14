import unittest

from gotenx import metrics
from gotenx.provenance import build_graph

PANEL = {"insights": [
    {"id": "claude:insight:001", "source": "claude", "kind": "insight"},
    {"id": "claude:insight:002", "source": "claude", "kind": "insight"},
    {"id": "codex:blindspot:001", "source": "codex", "kind": "blindspot"},
    {"id": "opencode:insight:001", "source": "opencode", "kind": "insight"},
]}
JUDGE = {"items": [
    {"id": "judge:plan:001", "source_ids": ["claude:insight:001"]},
    {"id": "judge:plan:002", "source_ids": ["codex:blindspot:001", "opencode:insight:001"]},
    {"id": "judge:plan:003", "source_ids": ["ghost:x:999"]},
]}
SOURCES = ["claude", "codex", "opencode"]


class TestMetrics(unittest.TestCase):
    def setUp(self):
        self.g = build_graph(PANEL, JUDGE)

    def test_survival_rate(self):  # P10: 3 of 4 referenced
        self.assertAlmostEqual(metrics.panel_insight_survival_rate(self.g), 0.75)

    def test_single_attribution(self):  # P10: plan001 & plan003 are len==1 -> 2/3
        self.assertAlmostEqual(metrics.single_attribution_acted_on(self.g), 2 / 3)

    def test_insight_adoption(self):  # plan001 & plan002 grounded -> 2/3
        self.assertAlmostEqual(metrics.insight_adoption(self.g), 2 / 3)

    def test_diversity_split(self):  # P16
        self.assertAlmostEqual(metrics.observed_diversity_index(self.g), 1.0)
        self.assertAlmostEqual(metrics.configured_diversity_index(self.g, SOURCES), 1.0)
        # a source configured but absent lowers configured diversity
        self.assertAlmostEqual(
            metrics.configured_diversity_index(self.g, SOURCES + ["gemini"]), 0.75
        )

    def test_determinism(self):  # identical graph -> identical metrics
        a = metrics.compute_all(build_graph(PANEL, JUDGE), SOURCES)
        b = metrics.compute_all(build_graph(PANEL, JUDGE), SOURCES)
        self.assertEqual(a, b)

    def test_window_aggregation(self):  # P12
        runs = [
            {"panel_insight_survival_rate": 0.8},
            {"panel_insight_survival_rate": 1.0},
        ]
        agg = metrics.aggregate_window(runs, {"from": "t0", "to": "t1"})
        self.assertAlmostEqual(agg["metrics"]["panel_insight_survival_rate"], 0.9)
        self.assertEqual(agg["window"]["runs"], 2)

    def test_empty_panel_safe(self):
        g = build_graph({"insights": []}, {"items": []})
        m = metrics.compute_all(g, SOURCES)
        self.assertEqual(m["panel_insight_survival_rate"], 0.0)


if __name__ == "__main__":
    unittest.main()
