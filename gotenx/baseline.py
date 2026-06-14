"""P4/P15 Baseline ratchet.

The baseline is the floor every future change must clear. Two rules:

  P15  floor = measured - tolerance  (not the measured value itself), which
       adds hysteresis so noise around the boundary does not thrash the gate.
  P4   a successful apply refreshes the baseline from the just-measured run.

The ratchet is monotonic: a floor only ever moves up. ``max(old, measured -
tolerance)`` means a lucky-low tolerance can never lower an already-earned
floor.
"""

from __future__ import annotations

from .policy import Policy


def ratchet(old_baseline: dict, measured: dict, policy: Policy) -> dict:
    """Return the new baseline after a successful apply (P4/P15).

    Only protected metrics carry a baseline floor; non-protected metrics are
    observational and are not gated.
    """
    new_baseline = dict(old_baseline)
    for metric, cfg in policy.protected_metrics.items():
        if metric not in measured:
            continue
        tol = float(cfg.get("tolerance", 0.0))
        floor = measured[metric] - tol
        prev = old_baseline.get(metric)
        new_baseline[metric] = floor if prev is None else max(prev, floor)
    return new_baseline


def regression(baseline: dict, measured: dict) -> list[dict]:
    """List protected metrics that fell below their baseline floor."""
    out = []
    for metric, floor in baseline.items():
        actual = measured.get(metric)
        if actual is not None and actual + 1e-9 < floor:
            out.append({"metric": metric, "actual": actual, "baseline": floor})
    return out
