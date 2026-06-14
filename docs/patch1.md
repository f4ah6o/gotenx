# Gotenx Specification v1.2 Freeze Patch 1
## P1-P8

Status: Required Before Freeze

---

# P1 Deterministic Provenance Graph

All panel insights receive stable IDs.

Example:

```json
{
  "id": "panel-insight-001"
}
```

Judge items reference source IDs.

```json
{
  "id": "judge-insight-001",
  "source_ids": [
    "panel-insight-001"
  ]
}
```

This enables deterministic computation of:

```text
minority_survival_rate
insight_adoption
minority_acted_on
```

without text matching.

---

# P2 Eval Recursion Semantics

Eval executes top-level runs.

```text
eval
 └─ top-level run
```

not nested runs.

Default:

```text
undefined mode combinations -> blocked
```

---

# P3 Protected Metric Stability

Protected metrics are evaluated using repeated runs.

Example:

```json
{
  "runs": 5,
  "tolerance": 0.02
}
```

---

# P4 Baseline Ratchet

Successful apply updates baseline.

```text
eval_passed
  ↓
apply
  ↓
baseline refresh
```

---

# P5 Protected Config Split

Separate:

```json
{
  "protected_policy_keys": [],
  "structural_invariants": []
}
```

---

# P6 Human Override Filtering

Override runs excluded from primary framework metrics.

```json
{
  "human_override": true
}
```

---

# P7 Proposal Provenance Restoration

Every proposal must carry provenance.

```json
{
  "provenance": [
    "metric:...",
    "run:..."
  ]
}
```

---

# P8 Eval Result Detail Restoration

Eval result must include per-case failures.

```json
{
  "cases": [
    {
      "id": "...",
      "passed": false,
      "failures": []
    }
  ]
}
```
