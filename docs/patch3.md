# Gotenx Specification v1.2 Freeze Patch 3
## P22-P24

Status: Final Pre-Freeze Delta

---

# P22 Minority Metric Separation

Separate deterministic survival floor from semantic minority.

Protected:

```text
panel_insight_survival_rate
```

Future candidate:

```text
semantic_minority_survival_rate
```

Properties:

```text
non-protected
non-gating
unratified
```

Add invariant:

```text
no_proposal_metric_promotion
```

Future candidate metrics cannot be promoted through proposals.

Only future spec revisions may promote them.

---

# P23 Determinism Ceiling Note

Record limitation:

```text
provenance_faithful
```

is sampled.

Not exhaustive.

This is a structural ceiling, not a missing feature.

Record:

```text
Determinism: 9.8 / 10
```

Gap:

```text
0.2 = sampled provenance faithfulness
```

---

# P24 Freeze Header

Apply to consolidated specification.

```text
# Gotenx Specification v1.2

Status: Frozen Baseline
Frozen: <iso-date>

Supersedes:
  v1.1
  Freeze Patch 1
  Freeze Patch 2
  Freeze Patch 3
```

Known Open Items:

```text
semantic_minority_survival_rate
  deferred to v1.3+

provenance faithfulness
  sampled, not exhaustive
```

Freeze means:

```text
closed and immutable baseline
```

Open items are acknowledged limitations, not defects.

---

# Freeze Disposition

```text
P1-P9
  required

P10
  accepted with documented limitation

P11-P21
  required

P22-P24
  final delta
```

Result:

```text
Frozen Baseline Reached
```
