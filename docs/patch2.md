# Gotenx Specification v1.2 Freeze Patch 2
## P9-P21

Status: Required Before Freeze

---

# P9 Panel ID Namespacing

Format:

```text
<source>:<kind>:<seq>
```

Examples:

```text
claude:insight:001
codex:blindspot:003
judge:plan:004
```

---

# P10 Deterministic Minority Redefinition

Replace:

```text
minority_survival_rate
```

with:

```text
panel_insight_survival_rate
```

Definition:

```text
referenced panel insight IDs
/
all panel insight IDs
```

Replace:

```text
minority_acted_on
```

with:

```text
single_attribution_acted_on
```

where:

```text
len(source_ids)==1
```

---

# P11 Provenance Goodhart Hardening

Add invariant:

```text
judge_provenance_policy
```

Add eval assertion:

```text
provenance_faithful
```

Metrics remain deterministic.

Faithfulness is checked in Eval.

---

# P12 Metrics Window Restoration

Restore:

```json
{
  "window": {
    "from": "...",
    "to": "...",
    "runs": 0
  }
}
```

---

# P13 Candidate Config Composition

Eval always uses complete effective config.

```text
current applied
+
proposal under test
```

Apply is sequential.

Any apply invalidates prior eval_passed proposals.

---

# P14 Canonical policy.json

Introduce:

```json
{
  "schema_version": "gotenx.config.policy.v2"
}
```

Contains:

```text
protected_metrics
protected_policy_keys
structural_invariants
```

---

# P15 Baseline Floor

Baseline:

```text
measured - tolerance
```

not measured value itself.

Adds hysteresis.

---

# P16 Diversity Split

Separate:

```text
observed_diversity_index
configured_diversity_index
```

Protected:

```text
configured_diversity_index
```

only.

---

# P17 Proposal Validation Stage

Add validation before Eval.

Rules:

```text
protected key -> rejected
invariant -> rejected
insufficient evidence -> insufficient_evidence
```

---

# P18 metadata.json Restoration

Restore metadata schema.

Contains:

```text
run_id
mode
status
panels
warnings
```

---

# P19 Repeated Eval Granularity

Repeated evaluation:

```text
N runs per golden case
```

Cost:

```text
N × cases × panels
```

---

# P20 Item-Level Override

Add:

```json
{
  "overridden_items": []
}
```

Metrics exclude only overridden items.

Whole-run exclusion is fallback.

---

# P21 Eval Actuals

Eval failures must include:

```json
{
  "actual": 0.71,
  "expected": 0.80,
  "baseline": 0.83
}
```
