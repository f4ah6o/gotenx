"""Validated, cross-process-safe local usage accounting."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from . import store
from .validation import require_number, require_string, validate_usage_ledger

WINDOWS = {"5h": timedelta(hours=5), "7d": timedelta(days=7), "30d": timedelta(days=30)}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def read_ledger(root=None) -> list[dict]:
    path = store.usage_ledger_path(root)
    if not path.exists():
        return []
    return validate_usage_ledger(store.read_json(path), path=path)


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def totals(entries: list[dict], now: datetime | None = None) -> dict[str, float]:
    validated = validate_usage_ledger({"entries": entries})
    now = now or _now()
    out = {}
    for name, delta in WINDOWS.items():
        cutoff = now - delta
        out[name] = sum(
            entry["cost_usd"] for entry in validated
            if _parse_time(entry["timestamp"]) >= cutoff
        )
    return out


def budget_status(policy, *, accrued: float = 0.0, root=None, now=None) -> dict:
    accrued = require_number(accrued, "$.accrued", minimum=0.0)
    entries = read_ledger(root)
    current = totals(entries, now)
    limits = policy.cost_budget.get("windows_usd", {})
    reserve = require_number(policy.cost_budget.get("reserve_per_run_usd", 0.10),
                             "$.cost_budget.reserve_per_run_usd", minimum=0.0)
    projected = {name: current.get(name, 0.0) + accrued + reserve for name in WINDOWS}
    blocked = []
    for name, value in projected.items():
        limit = require_number(limits.get(name, float("inf")),
                               f"$.cost_budget.windows_usd.{name}", minimum=0.0)
        if value > limit:
            blocked.append(name)
    return {"allowed": not blocked, "totals_usd": current,
            "projected_usd": projected, "blocked_windows": blocked}


def record(run_id: str, cost_usd: float, *, root=None, now=None) -> None:
    now = now or _now()
    run_id = require_string(run_id, "$.run_id", max_chars=512)
    cost = require_number(cost_usd, "$.cost_usd", minimum=0.0)
    with store.file_lock("usage-ledger", root):
        entries = read_ledger(root)
        cutoff = now - WINDOWS["30d"]
        entries = [entry for entry in entries if _parse_time(entry["timestamp"]) >= cutoff]
        entries.append({"run_id": run_id, "timestamp": now.isoformat(), "cost_usd": cost})
        validate_usage_ledger({"entries": entries}, path=store.usage_ledger_path(root))
        store.write_json_atomic(store.usage_ledger_path(root), {"entries": entries})
