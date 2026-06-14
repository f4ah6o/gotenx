"""P13 Candidate config composition.

Eval always runs against the COMPLETE effective config, defined as:

    effective = current applied policy  +  proposal under test

Apply is sequential. Each apply advances an ``epoch``; a proposal records the
epoch it was evaluated against, and once a newer apply lands every prior
``eval_passed`` proposal is invalidated (it must be re-evaluated against the new
applied config). This prevents stacking changes that were each green in
isolation but unsafe together.
"""

from __future__ import annotations

import copy

from . import policy as policy_mod
from .policy import Policy


def current_epoch(applied: dict) -> int:
    return int(applied.get("_epoch", 0))


def compose(applied: dict, proposal_changes: dict) -> Policy:
    """Build the effective Policy from applied config + proposal changes.

    Changes are shallow key overrides on the policy dict. The result is
    re-validated through ``policy.from_dict`` so an illegal composition (e.g. a
    bad schema or protecting observed_diversity_index) fails loudly here.
    """
    merged = copy.deepcopy(applied)
    for key, value in proposal_changes.items():
        merged[key] = value
    return policy_mod.from_dict(merged)


def apply_changes(applied: dict, proposal_changes: dict) -> dict:
    """Apply a proposal's changes to the live config, advancing the epoch (P13)."""
    merged = copy.deepcopy(applied)
    for key, value in proposal_changes.items():
        merged[key] = value
    merged["_epoch"] = current_epoch(applied) + 1
    return merged


def is_stale(proposal: dict, applied: dict) -> bool:
    """True if a previously-evaluated proposal was eval'd against an older epoch."""
    evaluated_epoch = proposal.get("evaluated_epoch")
    if evaluated_epoch is None:
        return False
    return int(evaluated_epoch) != current_epoch(applied)
