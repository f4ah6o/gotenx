"""P7/P17/P22 Proposal provenance + validation stage.

Validation runs BEFORE any Eval (P17). A proposal is a dict:

    {
      "id": "prop-001",
      "provenance": ["metric:panel_insight_survival_rate", "run:<run_id>"],
      "changes": { "<policy-key>": <value>, ... },
      "evidence": { "support_runs": 4 }
    }

Outcomes:
  accepted               -> eligible for Eval
  rejected               -> protected key / structural invariant / metric
                            promotion (P17/P22); never reaches Eval
  insufficient_evidence  -> not enough provenance/support to justify the change
"""

from __future__ import annotations

from dataclasses import dataclass

from .policy import INVARIANT_NO_PROMOTION, Policy
from .validation import validate_proposal

ACCEPTED = "accepted"
REJECTED = "rejected"
INSUFFICIENT = "insufficient_evidence"

MIN_SUPPORT_RUNS = 3


@dataclass(frozen=True)
class ValidationResult:
    status: str
    reason: str | None = None
    detail: dict | None = None

    @property
    def ok(self) -> bool:
        return self.status == ACCEPTED


def _has_provenance(proposal: dict) -> bool:
    prov = proposal.get("provenance", [])
    has_metric = any(str(p).startswith("metric:") for p in prov)
    has_run = any(str(p).startswith("run:") for p in prov)
    return bool(prov) and has_metric and has_run


def _promotes_future_candidate(changes: dict, policy: Policy) -> str | None:
    """Return a future-candidate metric name the proposal tries to promote (P22)."""
    if "protected_metrics" not in changes:
        return None
    new_protected = changes["protected_metrics"]
    if not isinstance(new_protected, dict):
        return None
    for name in policy.future_candidate_names():
        if name in new_protected:
            return name
    return None


def validate(proposal: dict, policy: Policy) -> ValidationResult:
    """Run the P17/P22 validation stage against the currently applied policy."""
    proposal = validate_proposal(proposal)
    changes = proposal.get("changes", {})
    if not changes:
        return ValidationResult(REJECTED, "empty_changes")

    # P7: provenance is mandatory.
    if not _has_provenance(proposal):
        return ValidationResult(
            INSUFFICIENT,
            "missing_provenance",
            {"need": "at least one metric: and one run: provenance entry"},
        )

    # P22: a proposal may never promote a future-candidate metric into the
    # protected set. Checked before the generic protected-key rule so the
    # specific invariant is the reported reason.
    promoted = _promotes_future_candidate(changes, policy)
    if promoted is not None:
        return ValidationResult(
            REJECTED,
            INVARIANT_NO_PROMOTION,
            {"metric": promoted},
        )

    # P17: protected policy keys are off-limits.
    protected_hit = [k for k in changes if policy.is_protected_key(k)]
    if protected_hit:
        return ValidationResult(REJECTED, "protected_key", {"keys": protected_hit})

    # P17: structural invariants may not be weakened. A change that removes any
    # currently-declared invariant violates the invariant set.
    if "structural_invariants" in changes:
        proposed = set(changes["structural_invariants"])
        removed = set(policy.structural_invariants) - proposed
        if removed:
            return ValidationResult(
                REJECTED, "invariant", {"removed": sorted(removed)}
            )

    # P17: insufficient evidence.
    support = int(proposal.get("evidence", {}).get("support_runs", 0))
    if support < MIN_SUPPORT_RUNS:
        return ValidationResult(
            INSUFFICIENT,
            "insufficient_support_runs",
            {"support_runs": support, "required": MIN_SUPPORT_RUNS},
        )

    return ValidationResult(ACCEPTED)
