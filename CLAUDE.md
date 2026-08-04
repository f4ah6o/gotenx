# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Gotenx v1.5 is a Codex / Claude Code **plugin and CLI** implementing a deterministic
Panel → Judge → Eval → Proposal pipeline. Codex uses `.codex-plugin/plugin.json`
and `skills/gotenx/SKILL.md`; Claude Code uses `.claude-plugin/plugin.json`,
`commands/*.md`, `agents/`, and `hooks/`. Both surfaces wrap the same stdlib-only
Python core in `gotenx/`.

Read `README.md` first — it documents the design principle, slash commands,
layout, metrics, and guardrails in detail and is not repeated here.

## Commands

```bash
# run the test suite (only dependency: Python 3 stdlib)
python3 -m unittest discover -s tests

# run a single test file / case
python3 -m unittest tests.test_metrics
python3 -m unittest tests.test_metrics.MetricsTest.test_panel_insight_survival_rate

# the CLI itself (bin/gotenx is the deterministic core; slash commands wrap it)
bin/gotenx init
bin/gotenx doctor
bin/gotenx run --replay golden/case-001
bin/gotenx eval
bin/gotenx propose examples/proposal-accepted.json
bin/gotenx apply   examples/proposal-accepted.json
bin/gotenx status
```

There is no build/lint step — this is a stdlib-only Python package plus
Markdown plugin assets.

## Architecture

### Core invariant: metrics are a pure function of the provenance graph

Everything in `gotenx/metrics.py` operates on a `ProvenanceGraph`
(`gotenx/provenance.py`), built purely from IDs — never from text matching.
IDs (`<source>:<kind>:<seq>`, e.g. `claude:insight:001`) are *assigned by
gotenx* (`gotenx/ids.py`), never by the panel/judge models, so the graph
cannot be forged. The only place LLM judgement enters is
`provenance_faithful`, confined to the Eval layer and sampled — keep it that
way when modifying the pipeline.

### Pipeline data flow (`bin/gotenx run`)

1. `gotenx/panel.py` — invokes each configured panel source (claude / codex /
   opencode) via `gotenx/llm.py`'s `Transport`, assigns namespaced ids to each
   returned insight.
2. `gotenx/judge.py` — invokes the judge source with all panel insights
   (ids included), gets back plan items citing `source_ids`. Malformed cited
   ids are dropped with a warning, not silently kept.
3. `gotenx/provenance.py::build_graph` — links panel insight ids to judge
   `source_ids` into a `ProvenanceGraph` (referenced / dangling ids).
4. `gotenx/metrics.py::compute_all` — pure functions over the graph:
   `panel_insight_survival_rate` (protected), `single_attribution_acted_on`,
   `insight_adoption`, `observed_diversity_index`,
   `configured_diversity_index` (protected).
5. `gotenx/store.py` — persists `panel.json` / `judge.json` / `metrics.json` /
   `metadata.json` under `.gotenx/runs/<run_id>/` in the *consuming project*
   (`$GOTENX_PROJECT_DIR`, host-specific project variables, or cwd) — not in this plugin repo.

`Transport` (`gotenx/llm.py`) has two modes: `real` (shells out to configurable
`claude`/`codex`/`opencode` adapters; Codex is JSONL, read-only, and ephemeral) and `replay` (reads
`panel/<src>.raw.txt` / `judge/judge.raw.txt` fixtures). Eval and the test
suite always use `replay` so they are deterministic and require no LLM.

### Policy / Eval / Proposal lifecycle

- `gotenx/policy.py` — `policy.json` (schema `gotenx.config.policy.v3`)
  splits `protected_metrics`, `protected_policy_keys`, and
  `structural_invariants`. `from_dict` enforces that
  `observed_diversity_index` is never protected (only
  `configured_diversity_index` may be, P16). `validate()` checks
  `future_candidate_metrics` are never gating/protected and that the
  `no_proposal_metric_promotion` invariant is present (P22) — this backs the
  `gotenx-validate-policy` PostToolUse hook.
- `gotenx/evalrun.py` — `eval_suite` runs every `golden/case-*/` directory:
  repeats each case N times (protected `panel_insight_survival_rate.runs`,
  default 5) for stability, checks spread against tolerance, gates aggregated
  metrics against `case.json`'s `expected` thresholds AND the ratcheted
  baseline, and runs the sampled `provenance_faithful` check from
  `faithful.json` if present.
- `gotenx/proposal.py` — `validate()` runs *before* Eval: requires
  provenance (`metric:` + `run:` entries), rejects proposals touching
  protected keys, weakening structural invariants, or promoting a
  `future_candidate_metric` into `protected_metrics` (P22), and requires
  `evidence.support_runs >= 3`.
- `gotenx/config.py` — `compose()` builds the *effective* config (applied +
  proposal changes) for Eval; `apply_changes()` advances `_epoch`;
  `is_stale()` invalidates a proposal evaluated against an older epoch (P13).
- `gotenx/baseline.py` / `cmd_apply` — on a passing apply, ratchets each
  protected metric's baseline to `min(measured) - tolerance`, monotonically
  (never decreases existing floors).
- `gotenx/override.py` — P6/P20 human-override filtering, excluding whole
  runs or specific insight/judge ids from metric windows.

### Golden cases (`golden/case-*/`)

Each case directory has `case.json` (`id`, `mode`, `task`, `expected`
thresholds), `panel/<src>.raw.txt` and `judge/judge.raw.txt` replay fixtures,
and an optional `faithful.json` with pre-recorded faithfulness verdicts for
the sampled `provenance_faithful` assertion. `mode` must be one of
`run`/`replay`/`eval` (P2) — Eval only ever executes top-level runs.

### Runtime state vs. plugin repo

This repo is the plugin source (templates, code, golden fixtures). Runtime
state for a consuming project lives in `<project>/.gotenx/` (`policy.json`,
`adapters.json`, `baseline.json`, `usage-ledger.json`, `runs/`, `proposals/`) — see `gotenx/store.py`. `bin/gotenx`
resolves `GOTENX_PLUGIN_ROOT`, `CLAUDE_PLUGIN_ROOT`, or `CODEX_PLUGIN_ROOT` to find
its templates and `golden/` suite independently of the project root.
