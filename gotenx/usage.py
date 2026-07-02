"""Local accounting for Gotenx-owned OpenCode Go usage."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from . import store


WINDOWS = {"5h": timedelta(hours=5), "7d": timedelta(days=7), "30d": timedelta(days=30)}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def read_ledger(root=None) -> list[dict]:
    path = store.usage_ledger_path(root)
    if not path.exists():
        return []
    data = store.read_json(path)
    return list(data.get("entries", []))


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def totals(entries: list[dict], now: datetime | None = None) -> dict[str, float]:
    now = now or _now()
    out = {}
    for name, delta in WINDOWS.items():
        cutoff = now - delta
        out[name] = sum(
            float(entry.get("cost_usd", 0.0))
            for entry in entries
            if _parse_time(entry["timestamp"]) >= cutoff
        )
    return out


def budget_status(policy, *, accrued: float = 0.0, root=None, now=None) -> dict:
    entries = read_ledger(root)
    current = totals(entries, now)
    limits = policy.cost_budget.get("windows_usd", {})
    reserve = float(policy.cost_budget.get("reserve_per_run_usd", 0.10))
    projected = {name: current.get(name, 0.0) + accrued + reserve for name in WINDOWS}
    blocked = [name for name, value in projected.items() if value > float(limits.get(name, float("inf")))]
    return {"allowed": not blocked, "totals_usd": current, "projected_usd": projected, "blocked_windows": blocked}


def record(run_id: str, cost_usd: float, *, root=None, now=None) -> None:
    now = now or _now()
    entries = read_ledger(root)
    cutoff = now - WINDOWS["30d"]
    entries = [entry for entry in entries if _parse_time(entry["timestamp"]) >= cutoff]
    entries.append({"run_id": run_id, "timestamp": now.isoformat(), "cost_usd": float(cost_usd)})
    store.write_json_atomic(store.usage_ledger_path(root), {"entries": entries})
