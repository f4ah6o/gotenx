"""Live benchmark capture and deterministic non-inferiority reporting."""

from __future__ import annotations

import math
import random
from pathlib import Path

from .llm import Transport, extract_json_object
from .orchestrator import run_staged
from .usage import budget_status
from .validation import (
    read_json, require_int, require_list, require_number, require_object, require_string,
    validate_benchmark_checkpoint,
)


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
    manifest_path = Path(path) / "manifest.json"
    suite = require_object(read_json(manifest_path), path=manifest_path)
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
        "risk/testing quality, and concision. Score EACH response independently. "
        "Return ONLY JSON with: preference ('A', 'B', or 'tie'); reason; scores, "
        "an object whose A and B values each contain numeric 1-5 keys correctness, "
        "coverage, actionability, risk_testing, and concision; and critical_failures, "
        "an object whose A and B values are lists of short failure codes. A critical "
        "failure means a materially wrong central conclusion, a missed explicit blocker "
        "that makes the answer unsafe or unusable, violation of a hard task constraint, "
        "or a destructive/security-risky recommendation without a required guard. "
        "Do not flag minor omissions or style issues as critical.\n\n"
        f"TASK:\n{case['task']}\n\nA:\n{a}\n\nB:\n{b}"
    )


def _rubric_scores(value: object, label: str) -> dict[str, float]:
    if not isinstance(value, dict) or not GRADE_DIMENSIONS.issubset(value):
        raise ValueError(f"grader omitted required rubric scores for {label}")
    scores: dict[str, float] = {}
    for key in GRADE_DIMENSIONS:
        raw = value[key]
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not 1 <= raw <= 5:
            raise ValueError(f"grader {label} rubric scores must be numeric values from 1 to 5")
        scores[key] = float(raw)
    return scores


def _critical_failures(value: object, label: str) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"grader critical_failures.{label} must be a list")
    if len(value) > 32:
        raise ValueError(f"grader critical_failures.{label} exceeds 32 items")
    failures = []
    for raw in value:
        if not isinstance(raw, str) or not raw.strip() or len(raw) > 256:
            raise ValueError(f"grader critical_failures.{label} items must be non-empty strings up to 256 chars")
        failures.append(raw.strip())
    return failures


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
        score_sets = verdict.get("scores")
        if not isinstance(score_sets, dict):
            raise ValueError("grader scores must contain A and B score objects")
        scores_a = _rubric_scores(score_sets.get("A"), "A")
        scores_b = _rubric_scores(score_sets.get("B"), "B")
        failure_sets = verdict.get("critical_failures")
        if not isinstance(failure_sets, dict):
            raise ValueError("grader critical_failures must contain A and B lists")
        failures_a = _critical_failures(failure_sets.get("A"), "A")
        failures_b = _critical_failures(failure_sets.get("B"), "B")
        candidate_label = "A" if candidate_first else "B"
        score = 0.5 if preference == "tie" else (1.0 if preference == candidate_label else 0.0)
        candidate_scores = scores_a if candidate_label == "A" else scores_b
        baseline_scores = scores_b if candidate_label == "A" else scores_a
        candidate_failures = failures_a if candidate_label == "A" else failures_b
        baseline_failures = failures_b if candidate_label == "A" else failures_a
        grader_cost += cost
        accrued += cost
        grades.append({
            "grader": grader_spec["id"], "baseline": baseline["model"],
            "candidate_first": candidate_first, "score": score,
            "candidate_scores": candidate_scores, "baseline_scores": baseline_scores,
            "candidate_critical_failures": candidate_failures,
            "baseline_critical_failures": baseline_failures,
            "critical_failure": bool(candidate_failures), "verdict": verdict,
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


def _case_cost(case: dict, index: int = 0) -> dict[str, float]:
    field = f"$.cases[{index}]"
    explicit = case.get("cost")
    if explicit is not None:
        explicit = require_object(explicit, f"{field}.cost")
        candidate = require_number(explicit.get("candidate_usd", 0.0), f"{field}.cost.candidate_usd", minimum=0.0)
        baseline = require_number(explicit.get("baseline_usd", 0.0), f"{field}.cost.baseline_usd", minimum=0.0)
        grader = require_number(explicit.get("grader_usd", 0.0), f"{field}.cost.grader_usd", minimum=0.0)
        total = require_number(explicit.get("total_usd", candidate + baseline + grader), f"{field}.cost.total_usd", minimum=0.0)
        if abs(total - (candidate + baseline + grader)) > 1e-9:
            raise ValueError(f"{field}.cost.total_usd must equal the component sum")
        return {"candidate": candidate, "baseline": baseline, "grader": grader, "total": total}
    candidate_obj = require_object(case.get("candidate"), f"{field}.candidate")
    candidate_usage = require_object(candidate_obj.get("usage"), f"{field}.candidate.usage")
    candidate = require_number(candidate_usage.get("cost_usd"), f"{field}.candidate.usage.cost_usd", minimum=0.0)
    baselines = require_list(case.get("baselines", []), f"{field}.baselines")
    baseline = 0.0
    for bindex, raw in enumerate(baselines):
        item = require_object(raw, f"{field}.baselines[{bindex}]")
        usage = require_object(item.get("usage"), f"{field}.baselines[{bindex}].usage")
        baseline += require_number(usage.get("cost_usd"), f"{field}.baselines[{bindex}].usage.cost_usd", minimum=0.0)
    grades = require_list(case.get("grades", []), f"{field}.grades")
    grader = 0.0
    for gindex, raw in enumerate(grades):
        item = require_object(raw, f"{field}.grades[{gindex}]")
        usage = item.get("usage")
        if usage is not None:
            usage = require_object(usage, f"{field}.grades[{gindex}].usage")
            grader += require_number(usage.get("cost_usd", 0.0), f"{field}.grades[{gindex}].usage.cost_usd", minimum=0.0)
    return {"candidate": candidate, "baseline": baseline, "grader": grader, "total": candidate + baseline + grader}


def _capability_floor(cases: list[dict], cfg: dict) -> dict:
    floor_cfg = cfg.get("capability_floor")
    if floor_cfg is None:
        return {"enabled": False, "measured": False, "passed": True}
    floor_cfg = require_object(floor_cfg, "$.config.capability_floor")
    dimension_min_raw = require_object(
        floor_cfg.get("dimension_mean_min", {}),
        "$.config.capability_floor.dimension_mean_min",
    )
    dimension_min = {
        key: require_number(
            dimension_min_raw.get(key, 1.0),
            f"$.config.capability_floor.dimension_mean_min.{key}",
            minimum=1.0, maximum=5.0,
        )
        for key in GRADE_DIMENSIONS
    }
    case_mean_min = require_number(
        floor_cfg.get("case_mean_min", 2.5),
        "$.config.capability_floor.case_mean_min", minimum=1.0, maximum=5.0,
    )
    case_correctness_min = require_number(
        floor_cfg.get("case_correctness_min", 2.5),
        "$.config.capability_floor.case_correctness_min", minimum=1.0, maximum=5.0,
    )
    min_case_pass_rate = require_number(
        floor_cfg.get("min_case_pass_rate", 0.90),
        "$.config.capability_floor.min_case_pass_rate", minimum=0.0, maximum=1.0,
    )
    max_critical_failure_rate = require_number(
        floor_cfg.get("max_critical_failure_rate", 0.05),
        "$.config.capability_floor.max_critical_failure_rate", minimum=0.0, maximum=1.0,
    )
    votes_required = require_int(
        floor_cfg.get("critical_failure_votes_required", 2),
        "$.config.capability_floor.critical_failure_votes_required", minimum=1, maximum=16,
    )

    missing: list[dict] = []
    case_results: list[dict] = []
    for cindex, case in enumerate(cases):
        grades = require_list(case.get("grades", []), f"$.cases[{cindex}].grades")
        score_sets = []
        critical_votes = 0
        for gindex, raw in enumerate(grades):
            grade = require_object(raw, f"$.cases[{cindex}].grades[{gindex}]")
            raw_scores = grade.get("candidate_scores")
            if raw_scores is None or "critical_failure" not in grade:
                missing.append({"case": case.get("id"), "grader_index": gindex})
                continue
            score_sets.append(_rubric_scores(raw_scores, f"candidate case {case.get('id')!r}"))
            if bool(grade.get("critical_failure")):
                critical_votes += 1
        if len(score_sets) != len(grades):
            continue
        dimensions = {
            key: sum(scores[key] for scores in score_sets) / len(score_sets)
            for key in GRADE_DIMENSIONS
        }
        case_mean = sum(dimensions.values()) / len(dimensions)
        critical = critical_votes >= min(votes_required, len(grades))
        passed = (
            case_mean + 1e-9 >= case_mean_min
            and dimensions["correctness"] + 1e-9 >= case_correctness_min
            and not critical
        )
        case_results.append({
            "id": case.get("id"), "passed": passed, "mean": case_mean,
            "dimensions": dimensions, "critical_failure": critical,
            "critical_failure_votes": critical_votes,
        })

    measured = not missing and len(case_results) == len(cases) and bool(cases)
    if not measured:
        return {
            "enabled": True, "measured": False, "passed": False,
            "reason": "absolute_scores_missing",
            "missing": missing,
            "cases": len(case_results), "expected_cases": len(cases),
        }

    dimension_means = {
        key: sum(case["dimensions"][key] for case in case_results) / len(case_results)
        for key in GRADE_DIMENSIONS
    }
    dimension_pass = {
        key: dimension_means[key] + 1e-9 >= dimension_min[key]
        for key in GRADE_DIMENSIONS
    }
    case_pass_rate = sum(1 for case in case_results if case["passed"]) / len(case_results)
    critical_failure_rate = sum(1 for case in case_results if case["critical_failure"]) / len(case_results)
    passed = (
        all(dimension_pass.values())
        and case_pass_rate + 1e-9 >= min_case_pass_rate
        and critical_failure_rate <= max_critical_failure_rate + 1e-9
    )
    failed_cases = [
        {"id": case["id"], "mean": case["mean"],
         "correctness": case["dimensions"]["correctness"],
         "critical_failure": case["critical_failure"]}
        for case in case_results if not case["passed"]
    ]
    return {
        "enabled": True, "measured": True, "passed": passed,
        "dimension_means": dimension_means,
        "dimension_thresholds": dimension_min,
        "dimension_passed": dimension_pass,
        "case_mean_min": case_mean_min,
        "case_correctness_min": case_correctness_min,
        "case_pass_rate": case_pass_rate,
        "min_case_pass_rate": min_case_pass_rate,
        "critical_failure_rate": critical_failure_rate,
        "max_critical_failure_rate": max_critical_failure_rate,
        "critical_failure_votes_required": votes_required,
        "failed_cases": failed_cases,
    }


def report(results: dict, cfg: dict) -> dict:
    results = validate_benchmark_checkpoint(results, require_cases=True)
    cfg = require_object(cfg, "$.config")
    cases = results["cases"]
    scores = []
    for cindex, case in enumerate(cases):
        grades = require_list(case.get("grades", []), f"$.cases[{cindex}].grades")
        if not grades:
            raise ValueError("every benchmark case requires at least one grade")
        values = []
        for gindex, raw in enumerate(grades):
            grade = require_object(raw, f"$.cases[{cindex}].grades[{gindex}]")
            values.append(require_number(grade.get("score"), f"$.cases[{cindex}].grades[{gindex}].score", minimum=0.0, maximum=1.0))
        scores.append(sum(values) / len(values))
    seed = require_int(cfg.get("seed", 20260618), "$.config.seed", minimum=0)
    rng = random.Random(seed)
    samples = require_int(cfg.get("bootstrap_samples", 10000), "$.config.bootstrap_samples", minimum=1, maximum=1_000_000)
    means = []
    for _ in range(samples):
        draw = [scores[rng.randrange(len(scores))] for _ in scores]
        means.append(sum(draw) / len(draw))
    means.sort()
    confidence = require_number(cfg.get("confidence", 0.95), "$.config.confidence", minimum=0.0, maximum=1.0)
    lower = means[max(0, int((1.0 - confidence) * samples) - 1)]
    margin = require_number(cfg.get("noninferiority_margin", 0.10), "$.config.noninferiority_margin", minimum=0.0, maximum=1.0)
    threshold = 0.5 - margin

    costs = [_case_cost(case, index) for index, case in enumerate(cases)]
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
    max_ratio = require_number(cfg.get("max_baseline_ratio", 0.65), "$.config.max_baseline_ratio", minimum=0.0)
    expected_cases = require_int(cfg.get("cases", len(cases)), "$.config.cases", minimum=1)
    complete = len(cases) == expected_cases
    quality_passed = lower >= threshold
    cost_passed = ratio <= max_ratio
    capability_floor = _capability_floor(cases, cfg)
    return {
        "passed": complete and quality_passed and capability_floor["passed"] and cost_passed,
        "quality": {
            "mean": sum(scores) / len(scores), "lower_confidence_bound": lower,
            "threshold": threshold, "passed": quality_passed,
        },
        "capability_floor": capability_floor,
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
