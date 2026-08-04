"""Live benchmark capture and deterministic non-inferiority reporting."""

from __future__ import annotations

import math
import random
from pathlib import Path

from .llm import Transport, extract_json_object
from .orchestrator import run_staged
from .usage import budget_status


LANGUAGES = {"python", "typescript", "go", "rust", "swift", "moonbit"}
KINDS = {"plan", "review"}
ORIGINS = {"public_pr", "synthetic"}
GRADE_DIMENSIONS = {"correctness", "coverage", "actionability", "risk_testing", "concision"}
MAX_TASK_CHARS = 100_000


class BenchmarkBudgetExhausted(RuntimeError):
    def __init__(self, phase: str, status: dict):
        super().__init__(f"benchmark budget exhausted before {phase}")
        self.phase = phase
        self.status = status


def _nonempty_text(value: object, field: str, case_id: object, *, required: bool) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"case {case_id}: {field} must be a string")
    if required and not value.strip():
        raise ValueError(f"case {case_id}: {field} must not be empty")
    if len(value) > MAX_TASK_CHARS:
        raise ValueError(f"case {case_id}: {field} exceeds {MAX_TASK_CHARS} characters")
    return value


def load_suite(path: str | Path, expected_cases: int = 60) -> dict:
    import json

    suite = json.loads((Path(path) / "manifest.json").read_text())
    if not isinstance(suite, dict):
        raise ValueError("benchmark manifest root must be an object")
    cases = suite.get("cases", [])
    if not isinstance(cases, list):
        raise ValueError("benchmark manifest cases must be a list")
    if len(cases) != expected_cases:
        raise ValueError(f"benchmark suite requires {expected_cases} cases, got {len(cases)}")
    if any(not isinstance(case, dict) for case in cases):
        raise ValueError("benchmark cases must be objects")
    ids = [case.get("id") for case in cases]
    if any(not isinstance(case_id, str) or not case_id.strip() for case_id in ids):
        raise ValueError("benchmark case ids must be non-empty strings")
    if len(ids) != len(set(ids)):
        raise ValueError("benchmark case ids must be unique")
    for case in cases:
        case_id = case.get("id")
        _nonempty_text(case.get("task"), "task", case_id, required=True)
        _nonempty_text(case.get("context", ""), "context", case_id, required=False)
        if case.get("language") not in LANGUAGES:
            raise ValueError(f"case {case_id}: unsupported language")
        if case.get("kind") not in KINDS or case.get("origin") not in ORIGINS:
            raise ValueError(f"case {case_id}: invalid kind/origin")
        if case.get("origin") == "public_pr":
            source = case.get("source", {})
            if not isinstance(source, dict) or not all(source.get(k) for k in ("url", "commit", "license")):
                raise ValueError(f"case {case_id}: public PR source is incomplete")
    for kind in KINDS:
        if sum(case["kind"] == kind for case in cases) != expected_cases // 2:
            raise ValueError("benchmark suite requires equal plan/review cases")
    for origin in ORIGINS:
        if sum(case["origin"] == origin for case in cases) != expected_cases // 2:
            raise ValueError("benchmark suite requires equal public/synthetic cases")
    expected_per_language = expected_cases // len(LANGUAGES)
    for language in LANGUAGES:
        if sum(case["language"] == language for case in cases) != expected_per_language:
            raise ValueError(f"benchmark suite requires {expected_per_language} {language} cases")
    return suite


def _baseline_prompt(case: dict) -> str:
    return (
        "Produce the best possible concrete coding plan or code review for the task below. "
        "Be correct, complete, actionable, explicit about risks and tests, and concise. "
        "Work read-only.\n\n"
        f"TASK:\n{case['task']}\n\nCONTEXT:\n{case.get('context', '')}"
    )


def _grade_prompt(case: dict, candidate: str, baseline: str, candidate_first: bool) -> str:
    a, b = (candidate, baseline) if candidate_first else (baseline, candidate)
    return (
        "Blindly compare responses A and B for correctness, coverage, actionability, "
        "risk/testing quality, and concision. Return ONLY JSON with preference "
        "('A', 'B', or 'tie'), reason, and scores containing numeric 1-5 keys "
        "correctness, coverage, actionability, risk_testing, and concision.\n\n"
        f"TASK:\n{case['task']}\n\nA:\n{a}\n\nB:\n{b}"
    )


def _json_object(text: str) -> dict:
    return extract_json_object(text)


def _final_text(staged: dict) -> str:
    return "\n".join(item["content"] for item in staged["judge"].get("items", []))


def _metered_cost(usage: object, label: str) -> float:
    if not isinstance(usage, dict) or "cost_usd" not in usage:
        raise ValueError(f"{label} was unmetered")
    raw = usage["cost_usd"]
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ValueError(f"{label} cost_usd must be numeric")
    cost = float(raw)
    if not math.isfinite(cost) or cost < 0:
        raise ValueError(f"{label} cost_usd must be finite and non-negative")
    return cost


def _guard_budget(policy, root, accrued: float, phase: str) -> None:
    status = budget_status(policy, accrued=accrued, root=root)
    if not status["allowed"]:
        raise BenchmarkBudgetExhausted(phase, status)


def run_case(
    case: dict, policy, transport: Transport, *, seed: int, root=None,
    record_cost=None,
) -> dict:
    _guard_budget(policy, root, 0.0, f"candidate:{case['id']}")
    candidate = run_staged(
        f"{case['task']}\n\n{case.get('context', '')}", policy, transport,
        root=root, agent_override="gotenx-benchmark",
    )
    candidate_cost = _metered_cost(candidate.get("usage"), "candidate")
    if candidate_cost and record_cost is not None:
        record_cost("candidate", candidate_cost)
    if candidate["status"] != "ok":
        failure = candidate.get("failure") or {}
        if failure.get("reason", "").startswith("budget_exhausted"):
            raise BenchmarkBudgetExhausted("candidate", failure.get("budget", {}))
        raise RuntimeError(f"candidate failed: {failure}")
    candidate_text = _final_text(candidate)
    accrued = candidate_cost

    baselines = policy.benchmark_cfg["baseline_models"]
    baseline_results = []
    baseline_cost = 0.0
    for index, spec in enumerate(baselines):
        _guard_budget(policy, root, 0.0 if record_cost is not None else accrued, f"baseline:{spec['id']}")
        stage = {
            "id": f"baseline_{index}", "model": spec["id"],
            "variant": spec.get("variant"), "agent": "gotenx-benchmark",
        }
        result = transport.invoke_model(stage, _baseline_prompt(case))
        cost = _metered_cost(result.usage, f"baseline {spec['id']}")
        if cost and record_cost is not None:
            record_cost(f"baseline:{index}", cost)
        if not result.text.strip():
            raise ValueError(f"baseline {spec['id']} returned no text")
        baseline_cost += cost
        accrued += cost
        baseline_results.append({
            "model": spec["id"], "text": result.text, "usage": result.usage,
            "cost_usd": cost,
        })

    grades = []
    grader_cost = 0.0
    rng = random.Random(f"{seed}:{case['id']}")
    for index, baseline in enumerate(baseline_results):
        grader_spec = baselines[1 - index]
        _guard_budget(policy, root, 0.0 if record_cost is not None else accrued, f"grader:{grader_spec['id']}")
        candidate_first = bool(rng.getrandbits(1))
        stage = {
            "id": f"grader_{index}", "model": grader_spec["id"],
            "variant": grader_spec.get("variant"), "agent": "gotenx-benchmark",
        }
        result = transport.invoke_model(
            stage, _grade_prompt(case, candidate_text, baseline["text"], candidate_first)
        )
        cost = _metered_cost(result.usage, f"grader {grader_spec['id']}")
        if cost and record_cost is not None:
            record_cost(f"grader:{index}", cost)
        verdict = _json_object(result.text)
        preference = str(verdict.get("preference", "")).strip()
        if preference.lower() == "tie":
            preference = "tie"
        else:
            preference = preference.upper()
        if preference not in {"A", "B", "tie"}:
            raise ValueError(f"grader returned invalid preference {preference!r}")
        scores = verdict.get("scores")
        if not isinstance(scores, dict) or not GRADE_DIMENSIONS.issubset(scores):
            raise ValueError("grader omitted required rubric scores")
        if any(
            isinstance(scores[key], bool) or not isinstance(scores[key], (int, float)) or not 1 <= scores[key] <= 5
            for key in GRADE_DIMENSIONS
        ):
            raise ValueError("grader rubric scores must be numeric values from 1 to 5")
        candidate_label = "A" if candidate_first else "B"
        score = 0.5 if preference == "tie" else (1.0 if preference == candidate_label else 0.0)
        grader_cost += cost
        accrued += cost
        grades.append({
            "grader": grader_spec["id"], "baseline": baseline["model"],
            "candidate_first": candidate_first, "score": score, "verdict": verdict,
            "usage": result.usage, "cost_usd": cost,
        })
    total_cost = candidate_cost + baseline_cost + grader_cost
    return {
        "id": case["id"], "kind": case["kind"], "origin": case["origin"],
        "language": case["language"],
        "candidate": {"text": candidate_text, "usage": candidate["usage"]},
        "baselines": baseline_results, "grades": grades,
        "cost": {
            "candidate_usd": candidate_cost,
            "baseline_usd": baseline_cost,
            "grader_usd": grader_cost,
            "total_usd": total_cost,
        },
    }


def _case_cost(case: dict) -> dict[str, float]:
    explicit = case.get("cost")
    if isinstance(explicit, dict):
        candidate = float(explicit.get("candidate_usd", 0.0))
        baseline = float(explicit.get("baseline_usd", 0.0))
        grader = float(explicit.get("grader_usd", 0.0))
        total = float(explicit.get("total_usd", candidate + baseline + grader))
        return {"candidate": candidate, "baseline": baseline, "grader": grader, "total": total}
    candidate = float(case["candidate"]["usage"].get("cost_usd", 0.0))
    baseline = sum(float(b["usage"].get("cost_usd", 0.0)) for b in case.get("baselines", []))
    grader = sum(float(g.get("usage", {}).get("cost_usd", 0.0)) for g in case.get("grades", []))
    return {"candidate": candidate, "baseline": baseline, "grader": grader, "total": candidate + baseline + grader}


def report(results: dict, cfg: dict) -> dict:
    cases = results.get("cases", [])
    if not isinstance(cases, list) or not cases:
        raise ValueError("benchmark has no completed cases")
    if any(not isinstance(case, dict) or not case.get("grades") for case in cases):
        raise ValueError("every benchmark case requires at least one grade")
    scores = [sum(g["score"] for g in case["grades"]) / len(case["grades"]) for case in cases]
    rng = random.Random(int(cfg.get("seed", 20260618)))
    samples = int(cfg.get("bootstrap_samples", 10000))
    means = []
    for _ in range(samples):
        draw = [scores[rng.randrange(len(scores))] for _ in scores]
        means.append(sum(draw) / len(draw))
    means.sort()
    confidence = float(cfg.get("confidence", 0.95))
    lower = means[max(0, int((1.0 - confidence) * samples) - 1)]
    threshold = 0.5 - float(cfg.get("noninferiority_margin", 0.10))

    costs = [_case_cost(case) for case in cases]
    candidate_cost = sum(cost["candidate"] for cost in costs)
    baseline_total = sum(cost["baseline"] for cost in costs)
    grader_cost = sum(cost["grader"] for cost in costs)
    total_cost = sum(cost["total"] for cost in costs)
    baseline_mean_cost = sum(
        cost["baseline"] / len(case["baselines"])
        for case, cost in zip(cases, costs)
        if case.get("baselines")
    )
    ratio = candidate_cost / baseline_mean_cost if baseline_mean_cost else float("inf")
    max_ratio = float(cfg.get("max_baseline_ratio", 0.65))
    expected_cases = int(cfg.get("cases", len(cases)))
    complete = len(cases) == expected_cases
    quality_passed = lower >= threshold
    cost_passed = ratio <= max_ratio
    return {
        "passed": complete and quality_passed and cost_passed,
        "quality": {
            "mean": sum(scores) / len(scores), "lower_confidence_bound": lower,
            "threshold": threshold, "passed": quality_passed,
        },
        "cost": {
            "candidate_usd": candidate_cost,
            "baseline_usd": baseline_total,
            "baseline_mean_usd": baseline_mean_cost,
            "grader_usd": grader_cost,
            "total_usd": total_cost,
            "ratio": ratio, "max_ratio": max_ratio, "passed": cost_passed,
        },
        "cases": len(cases), "expected_cases": expected_cases, "complete": complete,
    }
