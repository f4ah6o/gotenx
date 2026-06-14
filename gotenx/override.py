"""P6/P20 Human override filtering.

P6  A run flagged ``human_override`` is excluded from the primary framework
    metrics (it represents a human stepping outside the loop, so counting it
    would pollute the deterministic signal).
P20 Finer grain: ``overridden_items`` lists specific insight/judge ids to drop;
    metrics are then computed on the remaining graph. Whole-run exclusion (P6)
    is the fallback when item-level granularity is unavailable.
"""

from __future__ import annotations

import copy


def filter_override_runs(run_metadatas: list[dict]) -> list[dict]:
    """P6: drop whole runs marked ``human_override`` from a metric window."""
    return [m for m in run_metadatas if not m.get("human_override", False)]


def apply_item_overrides(panel: dict, judge: dict, overridden_items: list[str]) -> tuple[dict, dict]:
    """P20: remove overridden insight/judge ids before metric computation.

    Returns filtered (panel, judge). Removing a panel insight also strips it
    from any judge item's source_ids so the provenance graph stays consistent.
    """
    drop = set(overridden_items or [])
    if not drop:
        return panel, judge

    new_panel = copy.deepcopy(panel)
    new_panel["insights"] = [i for i in new_panel.get("insights", []) if i["id"] not in drop]

    new_judge = copy.deepcopy(judge)
    kept_items = []
    for item in new_judge.get("items", []):
        if item["id"] in drop:
            continue
        item["source_ids"] = [s for s in item.get("source_ids", []) if s not in drop]
        kept_items.append(item)
    new_judge["items"] = kept_items
    return new_panel, new_judge


def should_exclude_whole_run(metadata: dict) -> bool:
    """P6 fallback: True when a run must be excluded wholesale."""
    return bool(metadata.get("human_override", False)) and not metadata.get(
        "overridden_items"
    )
