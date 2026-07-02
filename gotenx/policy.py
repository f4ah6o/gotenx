"""P5/P14/P16/P22 Canonical policy.json handling.

policy.json v3 preserves the v2 protected-metric concerns and adds immutable
orchestration, cost, and benchmark policy.

  * ``protected_metrics``      - gating metrics + how they are measured
  * ``protected_policy_keys``  - config keys a proposal may never touch
  * ``structural_invariants``  - named invariants a proposal may never violate

``future_candidate_metrics`` (P22) are recorded as explicitly non-protected,
non-gating, unratified. The invariant ``no_proposal_metric_promotion`` blocks
the optimizer from ever promoting one through a proposal; only a spec revision
may (P22).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from . import LEGACY_SCHEMA_VERSION, SCHEMA_VERSION

INVARIANT_NO_PROMOTION = "no_proposal_metric_promotion"
INVARIANT_JUDGE_PROVENANCE = "judge_provenance_policy"
V3_INVARIANTS = {"ordered_stage_execution", "no_unmetered_run"}
V3_MODELS = [
    "opencode-go/deepseek-v4-flash",
    "opencode-go/kimi-k2.7-code",
    "opencode-go/deepseek-v4-pro",
    "opencode-go/glm-5.2",
]


@dataclass(frozen=True)
class Policy:
    raw: dict

    @property
    def schema_version(self) -> str:
        return self.raw["schema_version"]

    @property
    def protected_metrics(self) -> dict:
        return self.raw.get("protected_metrics", {})

    @property
    def protected_policy_keys(self) -> list[str]:
        return list(self.raw.get("protected_policy_keys", []))

    @property
    def structural_invariants(self) -> list[str]:
        return list(self.raw.get("structural_invariants", []))

    @property
    def future_candidate_metrics(self) -> list[dict]:
        return list(self.raw.get("future_candidate_metrics", []))

    @property
    def panel_sources(self) -> list[str]:
        return list(self.raw.get("panel", {}).get("sources", []))

    @property
    def judge_source(self) -> str:
        return self.raw.get("judge", {}).get("source", "claude")

    @property
    def provenance_faithful_cfg(self) -> dict:
        return self.raw.get("eval", {}).get("provenance_faithful", {})

    @property
    def orchestration(self) -> dict:
        return dict(self.raw.get("orchestration", {}))

    @property
    def stages(self) -> list[dict]:
        return [dict(stage) for stage in self.orchestration.get("stages", [])]

    @property
    def uses_staged_orchestration(self) -> bool:
        return bool(self.stages)

    @property
    def cost_budget(self) -> dict:
        return dict(self.raw.get("cost_budget", {}))

    @property
    def benchmark_cfg(self) -> dict:
        return dict(self.raw.get("benchmark", {}))

    def future_candidate_names(self) -> set[str]:
        return {m["name"] for m in self.future_candidate_metrics}

    def is_protected_key(self, key: str) -> bool:
        return key in set(self.protected_policy_keys)


def load_policy(path: str | Path) -> Policy:
    """Load and validate a policy.json. Raises ValueError on schema mismatch."""
    data = json.loads(Path(path).read_text())
    return from_dict(data)


def from_dict(data: dict) -> Policy:
    sv = data.get("schema_version")
    if sv != SCHEMA_VERSION:
        raise ValueError(
            f"unsupported policy schema_version {sv!r}; expected {SCHEMA_VERSION!r}"
        )
    # configured_diversity_index must be protected, observed must not (P16).
    protected = data.get("protected_metrics", {})
    if "observed_diversity_index" in protected:
        raise ValueError(
            "observed_diversity_index must not be protected (P16); protect "
            "configured_diversity_index instead"
        )
    stages = data.get("orchestration", {}).get("stages", [])
    if stages:
        if data.get("orchestration", {}).get("mode") != "sequential":
            raise ValueError("v3 orchestration.mode must be 'sequential'")
        required_roles = ["scout", "author", "critic", "judge"]
        roles = [stage.get("role") for stage in stages]
        if roles != required_roles:
            raise ValueError(f"v3 stages must have roles {required_roles!r} in order")
        ids = [stage.get("id") for stage in stages]
        if len(ids) != len(set(ids)):
            raise ValueError("v3 stage ids must be unique")
        for stage in stages:
            if not stage.get("model") or not stage.get("source"):
                raise ValueError("every v3 stage requires model and source")
        if [stage["model"] for stage in stages] != V3_MODELS:
            raise ValueError("v3 stages must use the fixed cost-profile model order")
        if data.get("orchestration", {}).get("readonly_agent") != "gotenx-readonly":
            raise ValueError("v3 orchestration must use the gotenx-readonly agent")
        windows = data.get("cost_budget", {}).get("windows_usd", {})
        if set(windows) != {"5h", "7d", "30d"} or any(float(v) <= 0 for v in windows.values()):
            raise ValueError("v3 cost_budget requires positive 5h, 7d, and 30d windows")
        baselines = data.get("benchmark", {}).get("baseline_models", [])
        expected_baselines = [
            "github-copilot/claude-opus-4.6", "github-copilot/gpt-5.5"
        ]
        if [item.get("id") for item in baselines] != expected_baselines or any(item.get("variant") != "high" for item in baselines):
            raise ValueError("v3 benchmark requires Opus 4.6 high and GPT-5.5 high baselines")
    return Policy(raw=data)


def migrate_v2_dict(data: dict, template: dict) -> dict:
    """Return a v3 policy based on the canonical template and v2 invariants."""
    if data.get("schema_version") != LEGACY_SCHEMA_VERSION:
        raise ValueError("migration source is not a v2 policy")
    migrated = json.loads(json.dumps(template))
    for key in (
        "protected_metrics",
        "future_candidate_metrics",
        "eval",
    ):
        if key in data:
            migrated[key] = data[key]
    migrated["_epoch"] = int(data.get("_epoch", 0)) + 1
    migrated["_migrated_from"] = LEGACY_SCHEMA_VERSION
    return migrated


def validate(path: str | Path) -> list[str]:
    """Validate a policy file, returning a list of human-readable problems.

    Empty list means the file is valid. Used by the editor hook and
    ``gotenx status``.
    """
    problems: list[str] = []
    try:
        pol = load_policy(path)
    except (ValueError, json.JSONDecodeError, OSError) as exc:
        return [str(exc)]
    if INVARIANT_NO_PROMOTION not in pol.structural_invariants:
        problems.append(
            f"missing required invariant {INVARIANT_NO_PROMOTION!r} (P22)"
        )
    missing_v3 = V3_INVARIANTS - set(pol.structural_invariants)
    if missing_v3:
        problems.append(f"missing required v3 invariants: {sorted(missing_v3)!r}")
    for fcm in pol.future_candidate_metrics:
        if fcm.get("gating", False):
            problems.append(
                f"future_candidate_metric {fcm.get('name')!r} must not be gating (P22)"
            )
        if fcm.get("name") in pol.protected_metrics:
            problems.append(
                f"future_candidate_metric {fcm.get('name')!r} is also protected (P22 violation)"
            )
    return problems
