---
description: Initialize the Gotenx workspace (.gotenx/) and install the canonical policy.json.
allowed-tools: Bash(${CLAUDE_PLUGIN_ROOT}/bin/gotenx:*)
argument-hint: "[--force]"
---

Initialize Gotenx in the current project.

Run:

```
"${CLAUDE_PLUGIN_ROOT}/bin/gotenx" init $ARGUMENTS
```

This creates `.gotenx/` (runs/, proposals/), copies the canonical
`gotenx.config.policy.v2` policy to `.gotenx/policy.json`, and seeds an empty
baseline. Report the resulting path and any `problems` from the JSON output. If
`problems` is non-empty, surface them — the policy is invalid and must be fixed
before running.
