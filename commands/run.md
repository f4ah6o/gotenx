---
description: Run the cost-aware OpenCode four-stage deliberation pipeline.
allowed-tools: Bash(${CLAUDE_PLUGIN_ROOT}/bin/gotenx:*)
argument-hint: "<task description>  |  --replay <fixtures-dir>"
---

Execute one top-level Gotenx run for the task: **$ARGUMENTS**

Run:

```
"${CLAUDE_PLUGIN_ROOT}/bin/gotenx" run $ARGUMENTS
```

`$ARGUMENTS` is passed through unquoted so CLI flags survive: a plain task
(`/gotenx:run review this repo`) is joined into the task prompt, while
`/gotenx:run --replay golden/case-001` reaches the CLI's `--replay` flag
instead of being swallowed into the task text.

Gotenx invokes DeepSeek V4 Flash (scout), Kimi K2.7 Code (author), DeepSeek V4
Pro (critic), then GLM-5.2 (judge). OpenCode receives a runtime-enforced
read-only agent. Gotenx assigns namespaced ids and records per-stage model,
token usage, and cost.

If the panel CLIs are not available, run deterministically against recorded
fixtures instead by passing `--replay <dir>` (a directory with `panel/<src>.raw.txt`
and `judge/judge.raw.txt` for legacy v2, or `stages/<stage>.raw.txt` for v3).
For tasks with quoting or shell-special characters, prefer `/gotenx:run --task "..."`.

From the JSON output, summarize the `run_id`, status, metric set, model list,
total `usage.cost_usd`, contributing panels, warnings, and any blocked failure.
