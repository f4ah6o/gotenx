# Gotenx v1.4 Operational Hardening

v1.4 extends the v1.2 frozen provenance baseline and the v1.3 staged-cost profile without weakening either specification.

## Cross-host plugin support

- Claude Code remains supported through `.claude-plugin/`, `commands/`, `agents/`, and `hooks/`.
- Codex is supported through `.codex-plugin/plugin.json` and `skills/gotenx/SKILL.md`.
- `bin/gotenx` remains the single deterministic implementation shared by both hosts.
- Project runtime roots may be supplied through `GOTENX_PROJECT_DIR`, host-specific project variables, or cwd.

## Agent adapter contract

Project-local overrides live in `.gotenx/adapters.json`, initialized from `config/adapters.json`.

Every invocation adapter must provide:

- a non-empty argv list containing `{prompt}`;
- an optional `{cwd}` placeholder;
- a supported output format (`plain`, `envelope`, or `codex_jsonl`);
- a positive timeout;
- an optional no-model-call diagnostic argv.

The built-in Codex adapter uses `codex exec --json --sandbox read-only --ephemeral -C {cwd}`. Gotenx extracts the final agent message and token usage from JSONL events.

`gotenx doctor` resolves every adapter required by the active policy and runs only its version/help probe. A real `gotenx run` repeats this preflight before allocating a run ID or invoking a model.

## Paid-input boundary

A run task must be a non-empty string after trimming and must not exceed 100,000 characters. Replay may recover its task from the fixture's `case.json`. Invalid tasks are rejected before transport construction, run-ID allocation, artifact creation, or usage-state mutation.

Benchmark manifests apply the same non-empty task and bounded string checks before live calls.

## Structured model-output extraction

Greedy regular-expression extraction is forbidden. Model arrays and grader objects use a shared bounded `JSONDecoder.raw_decode` scanner that:

- preserves nested values and brackets inside strings;
- returns the first complete value of the requested type;
- skips a complete wrong-type value as one unit rather than selecting a nested schema example;
- limits total input size and candidate scan count;
- fails deterministically when no acceptable value exists.

## Invocation accounting

Every staged transport return is recorded as an explicit attempt before parsing or grounding validation.

- A metered invocation contributes its cost exactly once.
- A transport exception before a result contributes zero.
- Retry budget checks include the first attempt's cost.
- Stage artifacts retain attempt status, usage, cost, and validation error.
- Stage and run totals are sums of the recorded attempts.

Benchmark accounting includes candidate stages, both baselines, both graders, and retries. Costs are written to the rolling ledger immediately by role, including costs incurred by a case that later fails. Budget is checked before every expensive invocation; exhaustion leaves the existing checkpoint resumable.

## Explicitly deferred

v1.4 does not claim to solve:

- cross-process locking and transactional publication for all runtime state;
- complete schema validation and repair semantics for every persisted JSON category.

Those remain separate changes because they alter the persistence model rather than the adapter and accounting boundaries introduced here.
