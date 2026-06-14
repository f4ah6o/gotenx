---
description: Run one Gotenx cycle — panel analysts -> Judge synthesis -> deterministic metrics.
allowed-tools: Bash(${CLAUDE_PLUGIN_ROOT}/bin/gotenx:*)
argument-hint: "<task description>  |  --replay <fixtures-dir>"
---

Execute one top-level Gotenx run for the task: **$ARGUMENTS**

Run:

```
"${CLAUDE_PLUGIN_ROOT}/bin/gotenx" run --task "$ARGUMENTS"
```

The panel sources (claude / codex / opencode) are invoked headless; gotenx
assigns the namespaced insight ids (`<source>:<kind>:<seq>`) — the models never
supply their own. The Judge synthesizes a plan citing those ids via
`source_ids`, and metrics are computed from the resulting provenance graph.

If the panel CLIs are not available, run deterministically against recorded
fixtures instead by passing `--replay <dir>` (a directory with `panel/<src>.raw.txt`
and `judge/judge.raw.txt`).

From the JSON output, summarize: the `run_id`, the metric set (highlight
`panel_insight_survival_rate`), which `panels` contributed, and any `warnings`
(e.g. dangling source_ids — a provenance red flag).
