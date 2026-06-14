"""gotenx CLI — the deterministic core driven by the plugin slash commands.

Subcommands: init | run | eval | propose | apply | status

Every subcommand prints a JSON result to stdout (machine-readable for the
slash-command layer) and a one-line human summary to stderr.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import baseline as baseline_mod
from . import config as config_mod
from . import metrics
from . import policy as policy_mod
from . import proposal as proposal_mod
from . import store
from .evalrun import eval_suite
from .judge import run_judge
from .llm import Transport
from .panel import run_panel
from .provenance import build_graph


def plugin_root() -> Path:
    return Path(os.environ.get("CLAUDE_PLUGIN_ROOT", Path(__file__).resolve().parent.parent))


def template_policy_path() -> Path:
    return plugin_root() / "config" / "policy.json"


def _load_applied() -> dict:
    p = store.policy_path()
    if not p.exists():
        raise SystemExit("no applied policy; run `gotenx init` first")
    return store.read_json(p)


def _load_baseline() -> dict:
    p = store.baseline_path()
    return store.read_json(p) if p.exists() else {}


def _emit(result: dict, summary: str) -> None:
    print(json.dumps(result, indent=2, sort_keys=True))
    print(summary, file=sys.stderr)


# -- subcommands ----------------------------------------------------------

def cmd_init(args: argparse.Namespace) -> int:
    store.ensure_layout()
    dst = store.policy_path()
    created = False
    if not dst.exists() or args.force:
        template = json.loads(template_policy_path().read_text())
        template.setdefault("_epoch", 0)
        store.write_json(dst, template)
        created = True
    if not store.baseline_path().exists():
        store.write_json(store.baseline_path(), {})
    problems = policy_mod.validate(dst)
    _emit(
        {"initialized": True, "policy": str(dst), "created": created, "problems": problems},
        f"gotenx initialized at {store.gotenx_dir()} (policy {'written' if created else 'kept'})",
    )
    return 1 if problems else 0


def cmd_run(args: argparse.Namespace) -> int:
    policy = policy_mod.from_dict(_load_applied())
    if args.replay:
        transport = Transport(mode="replay", replay_dir=Path(args.replay))
        mode = "replay"
    else:
        transport = Transport(mode="real")
        mode = "run"

    panel = run_panel(policy.panel_sources, args.task or "", transport)
    judge = run_judge(panel, policy.judge_source, transport)
    graph = build_graph(panel, judge)
    run_metrics = metrics.compute_all(graph, policy.panel_sources)

    warnings = list(panel.get("warnings", [])) + list(judge.get("warnings", [])) + list(graph.warnings)
    run_id = store.new_run_id()
    metadata = store.make_metadata(
        run_id=run_id,
        mode=mode,
        status="ok",
        panels=panel.get("panels", []),
        warnings=warnings,
        human_override=args.human_override,
    )
    store.save_run(run_id, panel, judge, run_metrics, metadata)
    _emit(
        {"run_id": run_id, "mode": mode, "metrics": run_metrics, "metadata": metadata},
        f"run {run_id}: survival={run_metrics['panel_insight_survival_rate']:.2f} "
        f"panels={panel.get('panels')} warnings={len(warnings)}",
    )
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    policy = policy_mod.from_dict(_load_applied())
    baseline = _load_baseline()
    golden = Path(args.golden or (plugin_root() / "golden"))
    result = eval_suite(golden, policy, baseline)
    n_fail = sum(1 for c in result["cases"] if not c["passed"])
    _emit(
        result,
        f"eval {'PASSED' if result['passed'] else 'FAILED'}: "
        f"{len(result['cases'])} cases, {n_fail} failing, cost={result['cost']['runs_x_cases_x_panels']}",
    )
    return 0 if result["passed"] else 2


def _eval_candidate(proposal: dict, applied: dict, baseline: dict, golden: Path) -> dict:
    """Compose effective config (P13) and Eval the candidate."""
    effective = config_mod.compose(applied, proposal.get("changes", {}))
    return eval_suite(golden, effective, baseline)


def cmd_propose(args: argparse.Namespace) -> int:
    applied = _load_applied()
    policy = policy_mod.from_dict(applied)
    baseline = _load_baseline()
    proposal = store.read_json(Path(args.proposal))
    golden = Path(args.golden or (plugin_root() / "golden"))

    verdict = proposal_mod.validate(proposal, policy)
    result = {"proposal": proposal.get("id"), "validation": verdict.__dict__}
    if not verdict.ok:
        _emit(result, f"propose {proposal.get('id')}: {verdict.status} ({verdict.reason})")
        return 0

    eval_result = _eval_candidate(proposal, applied, baseline, golden)
    result["eval"] = eval_result
    result["eval_passed"] = eval_result["passed"]
    result["evaluated_epoch"] = config_mod.current_epoch(applied)
    _emit(
        result,
        f"propose {proposal.get('id')}: validated, eval "
        f"{'PASSED' if eval_result['passed'] else 'FAILED'}",
    )
    return 0 if eval_result["passed"] else 2


def _measured_floor(eval_result: dict, policy: policy_mod.Policy) -> dict:
    """Conservative measured metrics for ratchet = min across passing cases."""
    measured: dict[str, float] = {}
    for metric in policy.protected_metrics:
        vals = [c["metrics"][metric] for c in eval_result["cases"] if "metrics" in c]
        if vals:
            measured[metric] = min(vals)
    return measured


def cmd_apply(args: argparse.Namespace) -> int:
    applied = _load_applied()
    policy = policy_mod.from_dict(applied)
    baseline = _load_baseline()
    proposal = store.read_json(Path(args.proposal))
    golden = Path(args.golden or (plugin_root() / "golden"))

    verdict = proposal_mod.validate(proposal, policy)
    if not verdict.ok:
        _emit(
            {"applied": False, "validation": verdict.__dict__},
            f"apply {proposal.get('id')}: blocked ({verdict.reason})",
        )
        return 2

    # P13 staleness guard: a proposal eval'd against an older epoch is invalid.
    if config_mod.is_stale(proposal, applied):
        _emit(
            {"applied": False, "reason": "stale_proposal"},
            f"apply {proposal.get('id')}: stale (re-eval against current epoch)",
        )
        return 2

    eval_result = _eval_candidate(proposal, applied, baseline, golden)
    if not eval_result["passed"]:
        _emit(
            {"applied": False, "eval": eval_result},
            f"apply {proposal.get('id')}: eval FAILED, not applied",
        )
        return 2

    # P13 sequential apply; P4/P15 baseline ratchet from measured metrics.
    new_applied = config_mod.apply_changes(applied, proposal.get("changes", {}))
    measured = _measured_floor(eval_result, policy)
    new_baseline = baseline_mod.ratchet(baseline, measured, policy)
    store.write_json(store.policy_path(), new_applied)
    store.write_json(store.baseline_path(), new_baseline)
    _emit(
        {
            "applied": True,
            "epoch": config_mod.current_epoch(new_applied),
            "baseline": new_baseline,
        },
        f"apply {proposal.get('id')}: APPLIED (epoch {config_mod.current_epoch(new_applied)}), "
        f"baseline ratcheted",
    )
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    applied = _load_applied()
    policy = policy_mod.from_dict(applied)
    baseline = _load_baseline()
    runs = store.list_runs()
    problems = policy_mod.validate(store.policy_path())
    result = {
        "epoch": config_mod.current_epoch(applied),
        "schema_version": policy.schema_version,
        "panel_sources": policy.panel_sources,
        "judge_source": policy.judge_source,
        "protected_metrics": list(policy.protected_metrics),
        "structural_invariants": policy.structural_invariants,
        "future_candidate_metrics": [m["name"] for m in policy.future_candidate_metrics],
        "baseline": baseline,
        "recent_runs": runs[-5:],
        "policy_problems": problems,
    }
    _emit(
        result,
        f"status: epoch {result['epoch']}, {len(runs)} runs, "
        f"{len(policy.protected_metrics)} protected metrics, "
        f"{'OK' if not problems else str(len(problems)) + ' problems'}",
    )
    return 1 if problems else 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="gotenx", description="Gotenx deterministic pipeline")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("init", help="initialize .gotenx/ and copy canonical policy")
    sp.add_argument("--force", action="store_true", help="overwrite existing applied policy")
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("run", help="run one panel->judge->metrics cycle")
    sp.add_argument("--task", help="task prompt for the panel")
    sp.add_argument("--replay", help="replay fixtures dir (deterministic, no LLM)")
    sp.add_argument("--human-override", action="store_true", help="mark run as human override (P6)")
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("eval", help="run the golden eval suite")
    sp.add_argument("--golden", help="golden cases dir (default: bundled golden/)")
    sp.set_defaults(func=cmd_eval)

    sp = sub.add_parser("propose", help="validate + eval a candidate proposal")
    sp.add_argument("proposal", help="proposal json file")
    sp.add_argument("--golden", help="golden cases dir")
    sp.set_defaults(func=cmd_propose)

    sp = sub.add_parser("apply", help="apply a validated, eval-passing proposal")
    sp.add_argument("proposal", help="proposal json file")
    sp.add_argument("--golden", help="golden cases dir")
    sp.set_defaults(func=cmd_apply)

    sp = sub.add_parser("status", help="show applied policy, baseline, recent runs")
    sp.set_defaults(func=cmd_status)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
