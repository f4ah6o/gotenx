"""Cost-aware four-stage OpenCode deliberation pipeline."""

from __future__ import annotations

import json
import math
from collections import defaultdict

from . import ids
from .llm import Transport, extract_json_array
from .usage import budget_status


ROLE_INSTRUCTIONS = {
    "scout": "Inspect the task and repository read-only. Identify distinct requirements, constraints, and blind spots.",
    "author": "Create a concrete plan or review response. Act on the supplied scout items and cite their ids.",
    "critic": "Attack the proposed response. Identify correctness gaps, missing tests, risks, and unjustified assumptions; cite supporting ids.",
    "judge": "Synthesize the strongest final plan or review. Resolve the critique and cite every prior item genuinely used.",
}


def _prompt(stage: dict, task: str, prior: list[dict]) -> str:
    schema = (
        '[{"kind":"short_token","content":"one atomic point","source_ids":["prior:id:001"]}]'
        if stage["role"] != "scout"
        else '[{"kind":"insight","content":"one atomic point"}]'
    )
    return (
        f"You are the {stage['role']} stage in a read-only deliberation pipeline.\n"
        f"{ROLE_INSTRUCTIONS[stage['role']]}\n"
        f"Return ONLY a JSON array matching {schema}. Do not edit files. "
        f"Return at most {stage.get('max_items', 12)} items and keep each content field under "
        f"{stage.get('max_content_chars', 800)} characters.\n\n"
        f"TASK:\n{task}\n\nPRIOR ITEMS:\n{json.dumps(prior, ensure_ascii=False)}"
    )


def _normalize(stage: dict, raw_items: list, valid_prior_ids: set[str]) -> list[dict]:
    seqs: dict[str, int] = defaultdict(int)
    out = []
    for raw in raw_items[: int(stage.get("max_items", 12))]:
        if not isinstance(raw, dict):
            raise ValueError(f"stage {stage['id']!r} returned a non-object item")
        kind = str(raw.get("kind", "plan")).strip().lower()
        kind = "".join(c if (c.isalnum() or c == "_") else "_" for c in kind)
        if not kind or not kind[0].isalpha():
            kind = "plan"
        seqs[kind] += 1
        content = str(raw.get("content", "")).strip()[: int(stage.get("max_content_chars", 800))]
        item = {
            "id": ids.make_id(stage["source"], kind, seqs[kind]),
            "source": stage["source"],
            "kind": kind,
            "content": content,
        }
        if stage["role"] != "scout":
            source_ids = raw.get("source_ids", [])
            if not isinstance(source_ids, list):
                raise ValueError(f"stage {stage['id']!r} returned non-list source_ids")
            item["source_ids"] = [
                str(sid) for sid in source_ids if str(sid) in valid_prior_ids
            ]
        out.append(item)
    return out


def _validate_items(stage: dict, items: list[dict]) -> None:
    if not items:
        raise ValueError(f"stage {stage['id']!r} returned no items")
    if any(not item["content"] for item in items):
        raise ValueError(f"stage {stage['id']!r} returned empty content")
    if stage["role"] != "scout" and any(not item.get("source_ids") for item in items):
        raise ValueError(f"stage {stage['id']!r} returned an ungrounded item")


def _invocation_cost(usage: dict, *, real: bool) -> float:
    if real and not usage:
        raise ValueError("OpenCode output did not contain usage metering")
    raw = usage.get("cost_usd", 0.0) if isinstance(usage, dict) else 0.0
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ValueError("OpenCode usage cost_usd must be numeric")
    cost = float(raw)
    if not math.isfinite(cost) or cost < 0:
        raise ValueError("OpenCode usage cost_usd must be finite and non-negative")
    return cost


def _aggregate_usage(attempts: list[dict], total_cost: float) -> dict:
    token_totals: dict[str, int] = defaultdict(int)
    for attempt in attempts:
        usage = attempt.get("usage")
        tokens = usage.get("tokens", {}) if isinstance(usage, dict) else {}
        if isinstance(tokens, dict):
            for key, value in tokens.items():
                if isinstance(value, int) and not isinstance(value, bool):
                    token_totals[key] += value
    result: dict = {"cost_usd": total_cost, "attempts": len(attempts)}
    if token_totals:
        result["tokens"] = dict(token_totals)
    return result


def run_staged(task: str, policy, transport: Transport, *, root=None, agent_override: str | None = None) -> dict:
    stages_out: list[dict] = []
    prior: list[dict] = []
    warnings: list[str] = []
    total_cost = 0.0
    failed = None

    for configured_stage in policy.stages:
        stage = dict(configured_stage)
        stage["agent"] = agent_override or policy.orchestration.get("readonly_agent", "gotenx-readonly")
        budget = budget_status(policy, accrued=total_cost, root=root)
        if transport.mode == "real" and not budget["allowed"]:
            failed = {"stage": stage["id"], "reason": "budget_exhausted", "budget": budget}
            break

        attempts: list[dict] = []
        stage_cost = 0.0
        items: list[dict] | None = None
        first_error: Exception | None = None

        for attempt_no in (1, 2):
            if attempt_no == 2:
                retry_budget = budget_status(policy, accrued=total_cost, root=root)
                if transport.mode == "real" and not retry_budget["allowed"]:
                    failed = {
                        "stage": stage["id"], "reason": "budget_exhausted_before_retry",
                        "budget": retry_budget, "attempts": attempts,
                    }
                    break
            prompt = _prompt(stage, task, prior)
            if attempt_no == 2:
                prompt += "\nYour prior response was invalid. Return only the required JSON array."
            try:
                result = transport.invoke_model(stage, prompt)
            except Exception as exc:
                attempts.append({
                    "attempt": attempt_no, "status": "transport_error", "usage": None,
                    "cost_usd": 0.0, "error": str(exc),
                })
                if attempt_no == 1:
                    first_error = exc
                    continue
                failed = {
                    "stage": stage["id"], "reason": "stage_failed", "detail": str(exc),
                    "attempts": attempts,
                }
                break

            try:
                cost = _invocation_cost(result.usage, real=transport.mode == "real")
            except Exception as exc:
                attempts.append({
                    "attempt": attempt_no, "status": "invalid_usage", "usage": result.usage,
                    "cost_usd": 0.0, "error": str(exc),
                })
                if transport.mode == "real":
                    failed = {
                        "stage": stage["id"], "reason": "stage_failed", "detail": str(exc),
                        "attempts": attempts,
                    }
                    break
                if attempt_no == 1:
                    first_error = exc
                    continue
                failed = {
                    "stage": stage["id"], "reason": "stage_failed", "detail": str(exc),
                    "attempts": attempts,
                }
                break

            stage_cost += cost
            total_cost += cost
            audit = {
                "attempt": attempt_no, "status": "returned", "usage": result.usage,
                "cost_usd": cost,
            }
            attempts.append(audit)
            try:
                parsed = extract_json_array(result.text)
                items = _normalize(stage, parsed, {item["id"] for item in prior})
                _validate_items(stage, items)
            except Exception as exc:
                audit["status"] = "invalid_output"
                audit["error"] = str(exc)
                items = None
                if attempt_no == 1:
                    first_error = exc
                    continue
                failed = {
                    "stage": stage["id"], "reason": "stage_failed", "detail": str(exc),
                    "attempts": attempts,
                }
                break

            audit["status"] = "accepted"
            if attempt_no == 2:
                warnings.append(f"stage {stage['id']!r} required one retry: {first_error}")
            break

        if failed:
            break
        if items is None:
            failed = {
                "stage": stage["id"], "reason": "stage_failed",
                "detail": "stage completed without accepted output", "attempts": attempts,
            }
            break

        stage_usage = _aggregate_usage(attempts, stage_cost)
        stages_out.append({
            "id": stage["id"], "role": stage["role"], "model": stage["model"],
            "variant": stage.get("variant"), "items": items, "usage": stage_usage,
            "attempts": attempts,
        })
        prior.extend(items)

    non_judge = [item for stage in stages_out if stage["role"] != "judge" for item in stage["items"]]
    judge_stage = next((stage for stage in stages_out if stage["role"] == "judge"), None)
    judge_items = []
    if judge_stage:
        for i, item in enumerate(judge_stage["items"], start=1):
            judge_items.append({
                "id": ids.make_id("judge", "plan", i),
                "source_ids": item.get("source_ids", []),
                "content": item["content"],
            })
    return {
        "status": "blocked" if failed else "ok",
        "stages": stages_out,
        "panel": {
            "insights": non_judge,
            "panels": [s["id"] for s in stages_out if s["role"] != "judge"],
            "warnings": warnings,
        },
        "judge": {"items": judge_items, "warnings": []},
        "usage": {"cost_usd": total_cost, "stages": len(stages_out)},
        "failure": failed,
        "warnings": warnings,
    }
