# Gotenx Specification v1.2 Freeze Patch 3 (Final Pre-Freeze Delta)

## Minority Metric Separation & Freeze Header

Status: Last delta before Frozen Baseline.
Numbering continues: this patch is P22. After applying, the spec is frozen.

-----

# P22. Minority Metric Separation

## Decision (resolving the Patch 2 Open Decision)

The Patch 2 Open Decision is NOT resolved by definition; it is RESOLVED BY
SEPARATION. The deterministic floor and the semantic notion are recorded as
two distinct metrics with different statuses, so the P10 trade-off cannot be
silently forgotten after freeze.

```text
panel_insight_survival_rate
  status:   PROTECTED
  computed: deterministic ID graph traversal (now)
  measures: whether the Judge dropped a panel's uniquely-ID'd insight
  is NOT:   a measure of whether the insight was semantically minority

semantic_minority_survival_rate
  status:   FUTURE CANDIDATE (non-protected, non-gating)
  computed: not yet defined; requires Eval-layer LLM judgment OR
            panel-declared clustering keys (Patch 2 options a / b)
  measures: survival of content conceived by only one analyst
  MUST NOT: be used as a gate or protected metric until a deterministic
            or Eval-confined definition is ratified
```

## policy.json amendment (canonical)

The protected set contains the survival FLOOR only. The semantic metric is
explicitly listed as a non-protected candidate so future readers see the gap.

```json
{
  "protected_metrics": {
    "panel_insight_survival_rate": {
      "method": "repeated_runs",
      "runs": 5,
      "tolerance": 0.02
    },
    "configured_diversity_index": {
      "method": "static_config",
      "tolerance": 0
    }
  },

  "future_candidate_metrics": [
    {
      "name": "semantic_minority_survival_rate",
      "status": "unratified",
      "gating": false,
      "note": "Requires Eval-layer or clustering definition. Until ratified, never protected, never gating."
    }
  ]
}
```

## Guard against silent promotion

```text
A future_candidate_metric may be moved into protected_metrics only by an
explicit spec revision (v1.3+), never by a proposal. "metric promotion"
is added to structural_invariants:

structural_invariants += "no_proposal_metric_promotion"
```

This closes the path where the optimizer could promote a weakly-defined
metric into a gate.

-----

# P23. Determinism Ceiling Note (Observability, not a blocker)

## Statement

The residual non-determinism in `provenance_faithful` (Patch 2 P11) is
recorded as a CEILING, not a TODO.

```text
provenance_faithful is sampled (K items, threshold 0.90).
It bounds citation gaming; it cannot eliminate it.

This is not a deferred refinement. It is a structural ceiling:
while an LLM Judge authors source_ids, no purely deterministic guarantee
of citation faithfulness exists without an additional structural-enforcement
layer (e.g. Judge mechanically emitting source_ids from a tool, not prose).

Raising this ceiling is a v1.3+ architecture question, out of scope for the
frozen baseline.
```

Recommended record in the spec’s limitations section:

```text
Determinism: 9.8 / 10
  0.2 gap = provenance faithfulness is sampled, by structural necessity,
            not by under-specification.
```

-----

# P24. Freeze Header

Apply to the top of the consolidated specification.

```text
# Gotenx Specification v1.2
Status: Frozen Baseline
Frozen: <iso-date>
Supersedes: v1.1, Freeze Patches 1–3 (folded in)

Known Open Items (carried, not blocking):
  - semantic_minority_survival_rate: definition deferred to v1.3+
    (see P22). Survival is currently approximated by the deterministic
    floor panel_insight_survival_rate.
  - provenance faithfulness is sampled, not exhaustive (see P23);
    structural-enforcement of citations deferred to v1.3+.

Freeze means: this version is closed and immutable.
It does NOT mean every question is answered.
The open items above are acknowledged limitations of the frozen baseline,
not defects to be patched into v1.2.
```

-----

# Freeze Disposition (matches reviewer’s assessment)

```text
P1–P9    REQUIRED                          folded in
P10      ACCEPTED WITH DOCUMENTED LIMITATION  -> formalized as P22 separation
P11–P21  REQUIRED                          folded in
P22–P24  FINAL DELTA                       this patch

Result: Frozen Baseline reached.
Only the semantic-minority DEFINITION remains open, and it is now carried
explicitly rather than silently. The deterministic framework is closed.
```
