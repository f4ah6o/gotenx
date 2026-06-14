---
description: Apply a validated, eval-passing Gotenx proposal and ratchet the baseline.
allowed-tools: Bash(${CLAUDE_PLUGIN_ROOT}/bin/gotenx:*)
argument-hint: "<proposal.json> [--golden <dir>]"
---

Apply a proposal: **$ARGUMENTS**

Run:

```
"${CLAUDE_PLUGIN_ROOT}/bin/gotenx" apply $ARGUMENTS
```

Apply re-validates and re-Evals the proposal, then — only if it passes —
commits the changes to `.gotenx/policy.json`, advances the config epoch
(invalidating any other proposal evaluated against the old epoch), and ratchets
the baseline to `measured - tolerance` (monotonic; floors never drop).

A stale proposal (evaluated against an older epoch) is refused. Report whether
it was `applied`, the new `epoch`, and the updated `baseline`.
