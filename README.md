# Gotenx — Claude Code Plugin

> # Gotenx Specification v1.2
> **Status: Frozen Baseline** · Supersedes v1.1 + Freeze Patches 1–3 (folded in)
>
> Freeze means: this version is closed and immutable. It does **not** mean every
> question is answered. The open items below are acknowledged limitations of the
> frozen baseline, not defects to be patched into v1.2.

A deterministic **Panel → Judge → Eval → Proposal** pipeline with
Goodhart-hardened metrics, packaged as a Claude Code plugin. A panel of analyst
CLIs (`claude` / `codex` / `opencode`) each emit insights; a Judge synthesizes a
plan citing them; metrics are computed **purely from the ID provenance graph**;
an Eval layer gates change proposals against protected metrics and structural
invariants before they can be applied.

## Design principle

Metrics are a pure function of the provenance graph (P1/P10) — never text
matching. The one place LLM judgement is needed (`provenance_faithful`) is
confined to the Eval layer and **sampled** (P11/P23), so the metric layer stays
fully deterministic. IDs (`<source>:<kind>:<seq>`, P9) are assigned by gotenx,
never by the models, so the graph cannot be forged.

## Install (local dev)

```bash
claude --plugin-dir /path/to/gotenx
```

## Slash commands

| Command | What it does |
|---|---|
| `/gotenx:init` | Create `.gotenx/`, install canonical `policy.json`, seed baseline |
| `/gotenx:run <task>` | One panel → judge → metrics cycle (add `--replay <dir>` for fixtures) |
| `/gotenx:eval` | Run the golden suite (repeated runs, per-case failures, actuals) |
| `/gotenx:propose <p.json>` | Validate a proposal, then Eval the candidate effective config |
| `/gotenx:apply <p.json>` | Apply a validated, eval-passing proposal; ratchet baseline |
| `/gotenx:status` | Show applied policy, epoch, baseline, recent runs |

## CLI (the deterministic core)

`bin/gotenx` is a stdlib-only Python CLI; the slash commands are thin wrappers.

```bash
bin/gotenx init
bin/gotenx run --replay golden/case-001
bin/gotenx eval
bin/gotenx propose examples/proposal-accepted.json
bin/gotenx apply   examples/proposal-accepted.json
bin/gotenx status
```

## Layout

```
.claude-plugin/plugin.json   manifest
commands/*.md                slash commands -> bin/gotenx
agents/gotenx-judge.md       Judge subagent (provenance-faithful synthesis)
bin/gotenx                   deterministic CLI entrypoint
bin/gotenx-validate-policy   PostToolUse hook: validate edited policy.json
gotenx/                      Python package (ids, provenance, metrics, eval, ...)
config/policy.json           canonical gotenx.config.policy.v2
golden/case-*/               known-good eval cases (replay fixtures)
examples/*.json              sample proposals (accepted / rejected)
tests/                       unittest suite
```

Runtime state lives in `.gotenx/` in the project (applied `policy.json`,
`baseline.json`, `runs/<run_id>/`, `proposals/`).

## Metrics (deterministic)

- `panel_insight_survival_rate` — referenced panel ids / all panel ids (**protected** floor, P10)
- `single_attribution_acted_on` — judge items with exactly one `source_id` (P10)
- `insight_adoption` — judge items grounded in ≥1 panel insight
- `observed_diversity_index` / `configured_diversity_index` — only the configured one is **protected** (P16)

## Guardrails

- **Protected policy keys / structural invariants** — proposals touching them are rejected before Eval (P5/P17).
- **`no_proposal_metric_promotion`** — the optimizer can never promote a future-candidate metric into the protected set; only a spec revision may (P22).
- **Baseline ratchet** — floor = `measured − tolerance`, monotonic (P4/P15).
- **Sequential apply** — each apply advances an epoch and invalidates proposals evaluated against the old one (P13).

## Tests

```bash
python3 -m unittest discover -s tests
```

## Known open items (carried, not blocking)

- **`semantic_minority_survival_rate`** — definition deferred to v1.3+. Survival
  is currently approximated by the deterministic floor
  `panel_insight_survival_rate`. It is carried as a non-protected, non-gating,
  unratified `future_candidate_metric` (P22).
- **Provenance faithfulness is sampled, not exhaustive** (P23). While an LLM
  Judge authors `source_ids`, no purely deterministic guarantee of citation
  faithfulness exists without an added structural-enforcement layer. Raising
  this ceiling is a v1.3+ architecture question, out of scope for the frozen
  baseline.

```
Determinism: 9.8 / 10
  0.2 gap = provenance faithfulness is sampled, by structural necessity,
            not by under-specification.
```
