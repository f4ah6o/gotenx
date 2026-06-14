"""P10/P12/P16 Deterministic metrics.

Every metric here is a pure function of the provenance graph (P1). No text
matching, no LLM judgement. Identical graphs always yield identical numbers,
which is what makes protected-metric stability (P3) meaningful.

Metrics
-------
panel_insight_survival_rate (P10, PROTECTED)
    |referenced panel ids| / |all panel ids|
    The deterministic survival FLOOR. It is NOT a semantic-minority measure
    (see P22 / future_candidate_metrics.semantic_minority_survival_rate).

single_attribution_acted_on (P10)
    fraction of judge items whose source_ids has length exactly 1 — i.e. a
    plan item acted on a single analyst's insight.

insight_adoption
    fraction of judge items that cite >= 1 valid panel insight (grounded items).

observed_diversity_index / configured_diversity_index (P16)
    observed  = distinct sources among referenced insights / distinct sources
                among all panel insights (what actually happened).
    configured = distinct configured panel sources / configured panel sources
                seen in the run — a static-config measure; only THIS one is
                protected.
"""

from __future__ import annotations

from .provenance import ProvenanceGraph, distinct_sources


def _safe_div(num: float, den: float) -> float:
    return num / den if den else 0.0


def panel_insight_survival_rate(g: ProvenanceGraph) -> float:
    return _safe_div(len(g.referenced_panel_ids), len(g.panel_id_set))


def single_attribution_acted_on(g: ProvenanceGraph) -> float:
    items = g.judge_items
    if not items:
        return 0.0
    single = sum(1 for it in items if len(it.get("source_ids", [])) == 1)
    return _safe_div(single, len(items))


def insight_adoption(g: ProvenanceGraph) -> float:
    items = g.judge_items
    if not items:
        return 0.0
    grounded = 0
    panel = g.panel_id_set
    for it in items:
        if any(sid in panel for sid in it.get("source_ids", [])):
            grounded += 1
    return _safe_div(grounded, len(items))


def observed_diversity_index(g: ProvenanceGraph) -> float:
    all_sources = distinct_sources(g.panel_ids)
    if not all_sources:
        return 0.0
    referenced_sources = distinct_sources(g.referenced_panel_ids)
    return _safe_div(len(referenced_sources), len(all_sources))


def configured_diversity_index(g: ProvenanceGraph, configured_sources: list[str]) -> float:
    """Static-config diversity (P16, PROTECTED).

    Fraction of configured panel sources that actually appear in the run. This
    is deterministic given the run + policy; it does not depend on Judge
    behaviour, which is why it is the protected diversity metric.
    """
    if not configured_sources:
        return 0.0
    present = distinct_sources(g.panel_ids)
    covered = sum(1 for s in configured_sources if s in present)
    return _safe_div(covered, len(configured_sources))


def compute_all(g: ProvenanceGraph, configured_sources: list[str]) -> dict[str, float]:
    """Compute the full deterministic metric set for one run."""
    return {
        "panel_insight_survival_rate": panel_insight_survival_rate(g),
        "single_attribution_acted_on": single_attribution_acted_on(g),
        "insight_adoption": insight_adoption(g),
        "observed_diversity_index": observed_diversity_index(g),
        "configured_diversity_index": configured_diversity_index(g, configured_sources),
    }


def aggregate_window(run_metrics: list[dict[str, float]], window: dict | None = None) -> dict:
    """P12 Metrics window restoration.

    Average each metric across a window of runs. ``window`` carries the
    provenance of the aggregation (``from``/``to``/``runs``) and is echoed back
    so callers can see exactly which runs fed the number.
    """
    n = len(run_metrics)
    keys: set[str] = set()
    for m in run_metrics:
        keys.update(m.keys())
    averaged = {
        k: _safe_div(sum(m.get(k, 0.0) for m in run_metrics), n) for k in sorted(keys)
    }
    out_window = dict(window or {})
    out_window.setdefault("from", None)
    out_window.setdefault("to", None)
    out_window["runs"] = n
    return {"window": out_window, "metrics": averaged}
