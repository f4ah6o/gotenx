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
