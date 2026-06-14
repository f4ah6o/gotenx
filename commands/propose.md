---
description: Validate a Gotenx config proposal, then Eval the candidate effective config.
allowed-tools: Bash(${CLAUDE_PLUGIN_ROOT}/bin/gotenx:*)
argument-hint: "<proposal.json> [--golden <dir>]"
---

Validate and trial a proposal: **$ARGUMENTS**

Run:

```
"${CLAUDE_PLUGIN_ROOT}/bin/gotenx" propose $ARGUMENTS
```

The proposal first passes the validation stage (runs **before** Eval):

- missing provenance -> `insufficient_evidence`
- touches a protected policy key -> `rejected`
- weakens a structural invariant -> `rejected`
- tries to promote a `future_candidate_metric` -> `rejected` (no_proposal_metric_promotion)
- too few support runs -> `insufficient_evidence`

Only an `accepted` proposal is Eval'd, against the **complete effective config**
(current applied + proposal). Report the `validation.status`/`reason`, and if it
was evaluated, whether `eval_passed` and the failing-case details.
