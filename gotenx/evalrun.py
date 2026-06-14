"""Eval harness (P2/P3/P8/P11/P19/P21).

A golden case directory ``golden/case-XXX/`` holds:

    case.json        {id, mode, task, expected:{metric: min_threshold, ...}}
    panel/<src>.raw.txt    recorded panel responses (replay transport)
    judge/judge.raw.txt    recorded judge response
    faithful.json    optional: per-judge-item faithfulness verdicts for the
                     sampled provenance_faithful assertion (P11)

Semantics
---------
P2  Eval executes top-level runs (panel->judge->metrics), never nested runs.
    An undefined ``mode`` is blocked rather than guessed.
P3  Protected metrics are measured over N repeated runs; their spread must stay
    within the policy tolerance (stability), else the case fails "unstable".
P19 Cost is N x cases x panels; N defaults to the protected runs count.
P8  Result carries per-case failures.
P21 Each failure carries actual / expected / baseline.
P11 provenance_faithful is a SAMPLED Eval-layer assertion (LLM judgement lives
    here, not in the metric layer). Determinism ceiling: see P23.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import metrics
from .llm import Transport
from .panel import run_panel
from .judge import run_judge
from .policy import Policy
from .provenance import build_graph

ALLOWED_MODES = {"run", "replay", "eval"}


def _protected_runs(policy: Policy) -> int:
    cfg = policy.protected_metrics.get("panel_insight_survival_rate", {})
    return int(cfg.get("runs", 5))


def _one_run(case_dir: Path, task: str, policy: Policy) -> dict:
    """Execute a single top-level run (P2) in replay mode and return metrics."""
    transport = Transport(mode="replay", replay_dir=case_dir)
    panel = run_panel(policy.panel_sources, task, transport)
    judge = run_judge(panel, policy.judge_source, transport)
    graph = build_graph(panel, judge)
    return metrics.compute_all(graph, policy.panel_sources)


def _provenance_faithful(case_dir: Path, policy: Policy) -> tuple[float | None, int]:
    """Sampled faithfulness rate from recorded verdicts (P11).

    Returns (rate, sample_size). rate is None when no verdicts are recorded
    (assertion skipped, surfaced as a warning by the caller).
    """
    fpath = case_dir / "faithful.json"
    if not fpath.exists():
        return None, 0
    verdicts = json.loads(fpath.read_text()).get("verdicts", [])
    if not verdicts:
        return None, 0
    k = int(policy.provenance_faithful_cfg.get("sample_k", 5))
    # deterministic sample: first k in recorded order (records are pre-shuffled
    # by the recorder; sampling here stays reproducible for Eval).
    sample = verdicts[:k]
    faithful = sum(1 for v in sample if v.get("faithful") is True)
    return faithful / len(sample), len(sample)


def eval_case(case_dir: Path, policy: Policy, baseline: dict) -> dict:
    """Evaluate a single golden case, returning a P8/P21 case result."""
    case = json.loads((case_dir / "case.json").read_text())
    case_id = case.get("id", case_dir.name)
    mode = case.get("mode", "replay")
    failures: list[dict] = []
    warnings: list[str] = []

    if mode not in ALLOWED_MODES:  # P2
        return {
            "id": case_id,
            "passed": False,
            "status": "blocked",
            "failures": [{"reason": f"undefined mode {mode!r}"}],
            "runs": 0,
        }

    n = _protected_runs(policy)  # P3/P19
    runs = [_one_run(case_dir, case.get("task", ""), policy) for _ in range(n)]
    agg = metrics.aggregate_window(runs)["metrics"]

    # P3 stability: protected metric spread must stay within tolerance.
    for metric, cfg in policy.protected_metrics.items():
        if cfg.get("method") != "repeated_runs":
            continue
        values = [r.get(metric, 0.0) for r in runs]
        spread = max(values) - min(values)
        tol = float(cfg.get("tolerance", 0.0))
        if spread > tol:
            failures.append(
                {
                    "metric": metric,
                    "reason": "unstable",
                    "actual": spread,
                    "expected": tol,
                    "baseline": baseline.get(metric),
                }
            )

    # threshold + baseline gating (P15/P21)
    for metric, threshold in case.get("expected", {}).items():
        actual = agg.get(metric, 0.0)
        base = baseline.get(metric)
        if actual + 1e-9 < threshold:
            failures.append(
                {
                    "metric": metric,
                    "reason": "below_expected",
                    "actual": actual,
                    "expected": threshold,
                    "baseline": base,
                }
            )
        elif base is not None and actual + 1e-9 < base:
            failures.append(
                {
                    "metric": metric,
                    "reason": "below_baseline",
                    "actual": actual,
                    "expected": threshold,
                    "baseline": base,
                }
            )

    # P11 provenance_faithful (sampled Eval-layer assertion)
    rate, sample_size = _provenance_faithful(case_dir, policy)
    pf_threshold = float(policy.provenance_faithful_cfg.get("threshold", 0.90))
    if rate is None:
        warnings.append("provenance_faithful: no verdicts recorded; assertion skipped")
    elif rate + 1e-9 < pf_threshold:
        failures.append(
            {
                "metric": "provenance_faithful",
                "reason": "below_threshold",
                "actual": rate,
                "expected": pf_threshold,
                "baseline": None,
                "sample_size": sample_size,
            }
        )

    return {
        "id": case_id,
        "passed": not failures,
        "status": "ok",
        "failures": failures,
        "runs": n,
        "metrics": agg,
        "warnings": warnings,
    }


def eval_suite(golden_dir: Path, policy: Policy, baseline: dict) -> dict:
    """Evaluate every ``case-*`` under ``golden_dir`` (P8 detail, P19 cost)."""
    golden_dir = Path(golden_dir)
    case_dirs = sorted(
        d for d in golden_dir.iterdir() if d.is_dir() and (d / "case.json").exists()
    )
    cases = [eval_case(d, policy, baseline) for d in case_dirs]
    passed = all(c["passed"] for c in cases)
    cost = sum(c["runs"] * len(policy.panel_sources) for c in cases)
    return {
        "passed": passed,
        "cases": cases,
        "cost": {"runs_x_cases_x_panels": cost, "panels": len(policy.panel_sources)},
    }
