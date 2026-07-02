"""Live benchmark capture and deterministic non-inferiority reporting."""

from __future__ import annotations

import json
import random
import re
from pathlib import Path

from .llm import Transport
from .orchestrator import run_staged


LANGUAGES = {"python", "typescript", "go", "rust", "swift", "moonbit"}
KINDS = {"plan", "review"}
ORIGINS = {"public_pr", "synthetic"}
GRADE_DIMENSIONS = {"correctness", "coverage", "actionability", "risk_testing", "concision"}


def load_suite(path: str | Path, expected_cases: int = 60) -> dict:
    suite = json.loads((Path(path) / "manifest.json").read_text())
    cases = suite.get("cases", [])
    if len(cases) != expected_cases:
        raise ValueError(f"benchmark suite requires {expected_cases} cases, got {len(cases)}")
    ids = [case.get("id") for case in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("benchmark case ids must be unique")
    for case in cases:
        if case.get("language") not in LANGUAGES:
            raise ValueError(f"case {case.get('id')}: unsupported language")
        if case.get("kind") not in KINDS or case.get("origin") not in ORIGINS:
            raise ValueError(f"case {case.get('id')}: invalid kind/origin")
        if case.get("origin") == "public_pr":
            source = case.get("source", {})
            if not all(source.get(k) for k in ("url", "commit", "license")):
                raise ValueError(f"case {case.get('id')}: public PR source is incomplete")
    for kind in KINDS:
        if sum(case["kind"] == kind for case in cases) != expected_cases // 2:
            raise ValueError(f"benchmark suite requires equal plan/review cases")
    for origin in ORIGINS:
        if sum(case["origin"] == origin for case in cases) != expected_cases // 2:
            raise ValueError(f"benchmark suite requires equal public/synthetic cases")
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


_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def _json_object(text: str) -> dict:
    try:
        value = json.loads(text.strip())
    except json.JSONDecodeError:
        match = _OBJECT_RE.search(text)
        if not match:
            raise ValueError("grader returned no JSON object")
        value = json.loads(match.group(0))
    if not isinstance(value, dict):
        raise ValueError("grader output is not an object")
    return value


def _final_text(staged: dict) -> str:
    return "\n".join(item["content"] for item in staged["judge"].get("items", []))


def run_case(case: dict, policy, transport: Transport, *, seed: int, root=None) -> dict:
    candidate = run_staged(
        f"{case['task']}\n\n{case.get('context', '')}", policy, transport,
        root=root, agent_override="gotenx-benchmark",
    )
    if candidate["status"] != "ok":
        raise RuntimeError(f"candidate failed: {candidate.get('failure')}")
    candidate_text = _final_text(candidate)
    baselines = policy.benchmark_cfg["baseline_models"]
    baseline_results = []
    for index, spec in enumerate(baselines):
        stage = {"id": f"baseline_{index}", "model": spec["id"], "variant": spec.get("variant"), "agent": "gotenx-benchmark"}
        result = transport.invoke_model(stage, _baseline_prompt(case))
        if not result.usage:
            raise ValueError(f"baseline {spec['id']} was unmetered")
        if not result.text.strip():
            raise ValueError(f"baseline {spec['id']} returned no text")
        baseline_results.append({"model": spec["id"], "text": result.text, "usage": result.usage})

    grades = []
    rng = random.Random(f"{seed}:{case['id']}")
    for index, baseline in enumerate(baseline_results):
        grader_spec = baselines[1 - index]
        candidate_first = bool(rng.getrandbits(1))
        stage = {"id": f"grader_{index}", "model": grader_spec["id"], "variant": grader_spec.get("variant"), "agent": "gotenx-benchmark"}
        result = transport.invoke_model(
            stage, _grade_prompt(case, candidate_text, baseline["text"], candidate_first)
        )
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
        if any(not isinstance(scores[key], (int, float)) or not 1 <= scores[key] <= 5 for key in GRADE_DIMENSIONS):
            raise ValueError("grader rubric scores must be numeric values from 1 to 5")
        candidate_label = "A" if candidate_first else "B"
        score = 0.5 if preference == "tie" else (1.0 if preference == candidate_label else 0.0)
        grades.append({
            "grader": grader_spec["id"], "baseline": baseline["model"],
            "candidate_first": candidate_first, "score": score, "verdict": verdict,
            "usage": result.usage,
        })
    return {
        "id": case["id"], "kind": case["kind"], "origin": case["origin"],
        "language": case["language"], "candidate": {"text": candidate_text, "usage": candidate["usage"]},
        "baselines": baseline_results, "grades": grades,
    }


def report(results: dict, cfg: dict) -> dict:
    cases = results.get("cases", [])
    if not cases:
        raise ValueError("benchmark has no completed cases")
    if any(not case.get("grades") for case in cases):
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

    candidate_cost = sum(float(case["candidate"]["usage"].get("cost_usd", 0.0)) for case in cases)
    baseline_cost = sum(
        sum(float(b["usage"].get("cost_usd", 0.0)) for b in case["baselines"]) / len(case["baselines"])
        for case in cases
    )
    ratio = candidate_cost / baseline_cost if baseline_cost else float("inf")
    max_ratio = float(cfg.get("max_baseline_ratio", 0.65))
    expected_cases = int(cfg.get("cases", 60))
    complete = len(cases) == expected_cases
    quality_passed = lower >= threshold
    cost_passed = ratio <= max_ratio
    return {
        "passed": complete and quality_passed and cost_passed,
        "quality": {"mean": sum(scores) / len(scores), "lower_confidence_bound": lower,
                    "threshold": threshold, "passed": quality_passed},
        "cost": {"candidate_usd": candidate_cost, "baseline_mean_usd": baseline_cost,
                 "ratio": ratio, "max_ratio": max_ratio, "passed": cost_passed},
        "cases": len(cases), "expected_cases": expected_cases, "complete": complete,
    }
