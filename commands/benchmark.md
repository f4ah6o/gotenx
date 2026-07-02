---
description: Capture or report the Gotenx v1 quality/cost benchmark.
allowed-tools: Bash(${CLAUDE_PLUGIN_ROOT}/bin/gotenx:*), Bash(python3:*)
argument-hint: "run|report [arguments]"
---

For a first live run, build the pinned 60-case suite and execute it:

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/build_benchmark_suite.py"
"${CLAUDE_PLUGIN_ROOT}/bin/gotenx" benchmark run \
  --suite ".gotenx/benchmarks/v1" --resume
```

This performs paid model calls. Resume from the checkpoint after interruption.
To recompute acceptance without model calls:

```
"${CLAUDE_PLUGIN_ROOT}/bin/gotenx" benchmark report --results <results.json>
```

Report the quality mean and 95% lower confidence bound, cost ratio, each gate,
and the overall result. Passing requires a lower bound of at least 0.40 and a
candidate cost ratio no greater than 0.65.
