"""P5/P14/P16/P22 Canonical policy.json handling.

policy.json is ``gotenx.config.policy.v2`` and splits three concerns (P5/P14):

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

from . import SCHEMA_VERSION

INVARIANT_NO_PROMOTION = "no_proposal_metric_promotion"
INVARIANT_JUDGE_PROVENANCE = "judge_provenance_policy"


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
    return Policy(raw=data)


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
