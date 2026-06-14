---
description: Run the Gotenx golden eval suite (repeated runs, per-case failures with actual/expected/baseline).
allowed-tools: Bash(${CLAUDE_PLUGIN_ROOT}/bin/gotenx:*)
argument-hint: "[--golden <dir>]"
---

Evaluate Gotenx against the golden cases.

Run:

```
"${CLAUDE_PLUGIN_ROOT}/bin/gotenx" eval $ARGUMENTS
```

Each golden case is executed N times (the protected-metric `runs` count) for
stability, then gated against both its expected thresholds and the ratcheted
baseline. The sampled `provenance_faithful` assertion (Eval-layer only) checks
citation faithfulness.

From the JSON output, report whether the suite `passed`, and for every failing
case list its `failures` with `metric`, `reason`, and the `actual / expected /
baseline` triple. Note the `cost` (runs x cases x panels).
