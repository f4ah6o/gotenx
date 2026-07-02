"""Cost-aware four-stage OpenCode deliberation pipeline."""

from __future__ import annotations

import json
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
            item["source_ids"] = [
                str(sid) for sid in raw.get("source_ids", [])
                if str(sid) in valid_prior_ids
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


def run_staged(task: str, policy, transport: Transport, *, root=None, agent_override: str | None = None) -> dict:
    stages_out: list[dict] = []
    prior: list[dict] = []
    warnings: list[str] = []
    total_cost = 0.0
    failed = None

    for configured_stage in policy.stages:
        stage = dict(configured_stage)
        if agent_override:
            stage["agent"] = agent_override
        else:
            stage["agent"] = policy.orchestration.get("readonly_agent", "gotenx-readonly")
        budget = budget_status(policy, accrued=total_cost, root=root)
        if transport.mode == "real" and not budget["allowed"]:
            failed = {"stage": stage["id"], "reason": "budget_exhausted", "budget": budget}
            break
        attempt_cost = 0.0
        retry_cost = 0.0
        try:
            result = transport.invoke_model(stage, _prompt(stage, task, prior))
            attempt_cost = float(result.usage.get("cost_usd", 0.0))
            parsed = extract_json_array(result.text)
            items = _normalize(stage, parsed, {item["id"] for item in prior})
            _validate_items(stage, items)
            if transport.mode == "real" and not result.usage:
                raise ValueError("OpenCode output did not contain usage metering")
        except Exception as exc:  # one corrective retry
            total_cost += attempt_cost
            retry_budget = budget_status(policy, accrued=total_cost, root=root)
            if transport.mode == "real" and not retry_budget["allowed"]:
                failed = {"stage": stage["id"], "reason": "budget_exhausted_before_retry", "budget": retry_budget}
                break
            try:
                result = transport.invoke_model(stage, _prompt(stage, task, prior) + "\nYour prior response was invalid. Return only the required JSON array.")
                retry_cost = float(result.usage.get("cost_usd", 0.0))
                parsed = extract_json_array(result.text)
                items = _normalize(stage, parsed, {item["id"] for item in prior})
                _validate_items(stage, items)
                if transport.mode == "real" and not result.usage:
                    raise ValueError("OpenCode output did not contain usage metering")
                warnings.append(f"stage {stage['id']!r} required one retry: {exc}")
            except Exception as retry_exc:
                total_cost += retry_cost
                failed = {"stage": stage["id"], "reason": "stage_failed", "detail": str(retry_exc)}
                break
        stage_cost = float(result.usage.get("cost_usd", 0.0))
        total_cost += stage_cost
        stages_out.append({
            "id": stage["id"], "role": stage["role"], "model": stage["model"],
            "variant": stage.get("variant"), "items": items, "usage": result.usage,
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
        "panel": {"insights": non_judge, "panels": [s["id"] for s in stages_out if s["role"] != "judge"], "warnings": warnings},
        "judge": {"items": judge_items, "warnings": []},
        "usage": {"cost_usd": total_cost, "stages": len(stages_out)},
        "failure": failed,
        "warnings": warnings,
    }
