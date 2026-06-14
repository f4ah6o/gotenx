import unittest

from gotenx import baseline as B
from gotenx import config as C
from gotenx import proposal as PR
from gotenx.policy import load_policy
from pathlib import Path

POLICY_PATH = Path(__file__).resolve().parent.parent / "config" / "policy.json"


def base_proposal(**changes):
    return {
        "id": "p",
        "provenance": ["metric:panel_insight_survival_rate", "run:r1"],
        "evidence": {"support_runs": 4},
        "changes": changes or {"panel": {"sources": ["claude", "codex"]}},
    }


class TestProposalValidation(unittest.TestCase):
    def setUp(self):
        self.policy = load_policy(POLICY_PATH)

    def test_accepted(self):
        self.assertEqual(PR.validate(base_proposal(), self.policy).status, PR.ACCEPTED)

    def test_protected_key_rejected(self):  # P17
        r = PR.validate(base_proposal(protected_policy_keys=[]), self.policy)
        self.assertEqual(r.status, PR.REJECTED)
        self.assertEqual(r.reason, "protected_key")

    def test_promote_future_candidate_rejected(self):  # P22
        r = PR.validate(
            base_proposal(protected_metrics={"semantic_minority_survival_rate": {}}),
            self.policy,
        )
        self.assertEqual(r.status, PR.REJECTED)
        self.assertEqual(r.reason, "no_proposal_metric_promotion")

    def test_missing_provenance_insufficient(self):  # P7
        p = base_proposal()
        p["provenance"] = []
        r = PR.validate(p, self.policy)
        self.assertEqual(r.status, PR.INSUFFICIENT)

    def test_insufficient_support(self):  # P17
        p = base_proposal()
        p["evidence"] = {"support_runs": 1}
        self.assertEqual(PR.validate(p, self.policy).status, PR.INSUFFICIENT)


class TestBaseline(unittest.TestCase):
    def setUp(self):
        self.policy = load_policy(POLICY_PATH)

    def test_ratchet_floor_is_measured_minus_tolerance(self):  # P15
        nb = B.ratchet({}, {"panel_insight_survival_rate": 0.95}, self.policy)
        self.assertAlmostEqual(nb["panel_insight_survival_rate"], 0.93)

    def test_ratchet_monotonic(self):  # P4/P15
        nb = B.ratchet({"panel_insight_survival_rate": 0.93},
                       {"panel_insight_survival_rate": 0.90}, self.policy)
        self.assertAlmostEqual(nb["panel_insight_survival_rate"], 0.93)

    def test_regression_detection(self):
        regs = B.regression({"panel_insight_survival_rate": 0.9},
                            {"panel_insight_survival_rate": 0.8})
        self.assertEqual(len(regs), 1)


class TestConfigEpoch(unittest.TestCase):
    def test_apply_advances_epoch_and_invalidates(self):  # P13
        applied = {"schema_version": "gotenx.config.policy.v2", "_epoch": 0}
        new = C.apply_changes(applied, {"judge": {"source": "codex"}})
        self.assertEqual(C.current_epoch(new), 1)
        self.assertTrue(C.is_stale({"evaluated_epoch": 0}, new))
        self.assertFalse(C.is_stale({"evaluated_epoch": 1}, new))


if __name__ == "__main__":
    unittest.main()
