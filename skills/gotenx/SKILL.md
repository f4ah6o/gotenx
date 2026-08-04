---
name: gotenx
description: Run Gotenx multi-agent planning, review, evaluation, benchmark, status, and adapter diagnostics from Codex. Use when a task benefits from multiple independent AI perspectives, provenance-backed synthesis, deterministic evaluation, or cost-aware model orchestration.
---

# Gotenx

Use the bundled `bin/gotenx` CLI. Resolve the plugin root from this skill file: the root is two directories above `skills/gotenx/SKILL.md`.

## Safety and execution

- Keep analysis read-only unless the user separately requests implementation.
- Run `bin/gotenx doctor` before the first live run in a project.
- Run `bin/gotenx init` once per project before `run`, `eval`, `benchmark`, `propose`, or `apply`.
- Do not bypass a blocked budget, failed evaluation, stale proposal, or protected-policy rejection.
- Treat stdout as machine-readable JSON and stderr as the concise human summary.

## Common workflows

Initialize and verify dependencies:

```bash
<plugin-root>/bin/gotenx init
<plugin-root>/bin/gotenx doctor
<plugin-root>/bin/gotenx status
```

Create a multi-agent plan or review:

```bash
<plugin-root>/bin/gotenx run --task "<complete task>"
```

Use deterministic replay without paid model calls:

```bash
<plugin-root>/bin/gotenx run --replay <plugin-root>/golden/case-001
<plugin-root>/bin/gotenx eval
```

Run or report the quality/cost benchmark:

```bash
<plugin-root>/bin/gotenx benchmark run --suite <suite-dir> --resume
<plugin-root>/bin/gotenx benchmark report --results <results-json>
```

Validate and apply a configuration proposal only through the guarded workflow:

```bash
<plugin-root>/bin/gotenx propose <proposal.json>
<plugin-root>/bin/gotenx apply <proposal.json>
```

## Adapter configuration

Project-local adapter overrides live at `.gotenx/adapters.json`. Built-in adapters support:

- Claude Code JSON envelopes
- Codex `exec --json --sandbox read-only --ephemeral`
- OpenCode CLI

Use `{prompt}` in every adapter argv template and `{cwd}` when the CLI needs an explicit project root. Re-run `doctor` after changing adapter configuration.
