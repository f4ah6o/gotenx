# AGENTS.md

## Product

Gotenx v1.5 is a stdlib-only Python CLI plus Codex and Claude Code plugin assets. It runs a provenance-backed Panel → Judge → Eval → Proposal workflow with budget and benchmark guardrails.

Read `README.md` before changing behavior. Preserve the frozen v1.2 provenance invariants and the v1.3 staged-pipeline guarantees.

## Commands

```bash
python3 -m unittest discover -s tests
python3 -m compileall -q gotenx tests scripts
bin/gotenx init
bin/gotenx doctor
bin/gotenx run --replay golden/case-001
bin/gotenx eval
bin/gotenx status
```

## Invariants

- Gotenx assigns all provenance IDs; never accept model-generated IDs as authoritative.
- Metrics remain pure functions of the provenance graph, not text matching.
- Real staged output must be metered; every invocation cost is counted exactly once.
- Keep agent execution read-only. Codex uses `exec --json --sandbox read-only --ephemeral`.
- Validate model JSON with the bounded structural decoder in `gotenx/llm.py`; do not reintroduce greedy regex extraction.
- Reject invalid input before allocating run IDs, writing artifacts, or invoking paid models.
- Runtime state belongs in the consuming project's `.gotenx/`, never in this source repository.

## Plugin surfaces

- Codex: `.codex-plugin/plugin.json`, `skills/gotenx/SKILL.md`
- Claude Code: `.claude-plugin/plugin.json`, `commands/*.md`, `agents/`, `hooks/`
- Shared deterministic core: `bin/gotenx`, `gotenx/`
- Project-local agent overrides: `.gotenx/adapters.json`, initialized from `config/adapters.json`
