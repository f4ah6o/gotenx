---
description: Show the applied Gotenx policy, baseline, protected metrics, and recent runs.
allowed-tools: Bash(${CLAUDE_PLUGIN_ROOT}/bin/gotenx:*)
---

Show the current Gotenx state.

Run:

```
"${CLAUDE_PLUGIN_ROOT}/bin/gotenx" status
```

Summarize the config `epoch`, panel/judge sources, protected metrics, structural
invariants, the carried `future_candidate_metrics` (unratified, non-gating), the
current `baseline` floors, and the most recent run ids. If `policy_problems` is
non-empty, surface them prominently.

Also report `baseline_pending`, configured model ids, and the 5-hour/weekly/
monthly usage totals and projected reserve. A pending baseline blocks proposal
and apply until the v1 benchmark passes.
