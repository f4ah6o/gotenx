"""gotenx CLI — the deterministic core driven by the plugin slash commands.

Subcommands: init | doctor | run | eval | propose | apply | status

Every subcommand prints a JSON result to stdout (machine-readable for the
slash-command layer) and a one-line human summary to stderr.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import baseline as baseline_mod
from . import config as config_mod
from . import metrics
from . import policy as policy_mod
from . import proposal as proposal_mod
from . import store
from .evalrun import eval_suite
from .judge import run_judge
from .llm import Transport, doctor_adapters, load_adapters, merge_adapters
from .orchestrator import run_staged
from .panel import run_panel
from .provenance import build_graph
from . import usage as usage_mod
from . import benchmark as benchmark_mod
from .validation import (
    DataValidationError, validate_baseline, validate_benchmark_checkpoint,
    validate_proposal,
)


def plugin_root() -> Path:
    return Path(
        os.environ.get("GOTENX_PLUGIN_ROOT")
        or os.environ.get("CLAUDE_PLUGIN_ROOT")
        or os.environ.get("CODEX_PLUGIN_ROOT")
        or Path(__file__).resolve().parent.parent
    )


def template_policy_path() -> Path:
    return plugin_root() / "config" / "policy.json"


def template_adapters_path() -> Path:
    return plugin_root() / "config" / "adapters.json"


def _load_adapters() -> dict:
    path = store.adapters_path()
    return load_adapters(path) if path.exists() else merge_adapters()


def _required_adapter_sources(policy) -> list[str]:
    if policy.uses_staged_orchestration:
        return ["opencode"]
    return list(dict.fromkeys([*policy.panel_sources, policy.judge_source]))


def _load_applied() -> dict:
    p = store.policy_path()
    if not p.exists():
        raise DataValidationError("run `gotenx init` first", path=p, code="missing_state")
    applied = store.read_json(p)
    if applied.get("schema_version") == policy_mod.LEGACY_SCHEMA_VERSION:
        with store.file_lock("policy-migration"):
            applied = store.read_json(p)
            if applied.get("schema_version") == policy_mod.LEGACY_SCHEMA_VERSION:
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                backup = store.migrations_dir() / f"v2-to-v3-{stamp}"
                store.write_json(backup / "policy.json", applied)
                old_baseline = _load_baseline()
                store.write_json(backup / "baseline.json", old_baseline)
                template = json.loads(template_policy_path().read_text())
                applied = policy_mod.migrate_v2_dict(applied, template)
                applied["_baseline_pending"] = True
                store.write_json_atomic(p, applied)
                store.write_json_atomic(store.baseline_path(), {})
    policy_mod.from_dict(applied, path=p)
    return applied


def _load_baseline() -> dict:
    p = store.baseline_path()
    value = store.read_json(p) if p.exists() else {}
    return validate_baseline(value, path=p)


def _emit(result: dict, summary: str) -> None:
    print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
    print(summary, file=sys.stderr)


# -- subcommands ----------------------------------------------------------

def _cmd_init_locked(args: argparse.Namespace) -> int:
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
    adapters_created = False
    if not store.adapters_path().exists() or args.force:
        adapters = json.loads(template_adapters_path().read_text())
        store.write_json(store.adapters_path(), adapters)
        adapters_created = True
    problems = policy_mod.validate(dst)
    _emit(
        {
            "initialized": True, "policy": str(dst), "created": created,
            "adapters": str(store.adapters_path()), "adapters_created": adapters_created,
            "problems": problems,
        },
        f"gotenx initialized at {store.gotenx_dir()} (policy {'written' if created else 'kept'})",
    )
    return 1 if problems else 0


def cmd_init(args: argparse.Namespace) -> int:
    with store.file_lock("policy-state"):
        return _cmd_init_locked(args)


def _resolve_task(args: argparse.Namespace) -> str:
    """Positional TASK words or --task; the explicit flag wins (issue #3)."""
    if args.task is not None:
        task = args.task
    else:
        task = " ".join(getattr(args, "task_words", []) or [])
    if not task.strip() and getattr(args, "replay", None):
        case_path = Path(args.replay) / "case.json"
        if case_path.exists():
            case = store.read_json(case_path)
            replay_task = case.get("task")
            if isinstance(replay_task, str):
                task = replay_task
    return task


def _validate_task(task: object) -> str:
    if not isinstance(task, str) or not task.strip():
        raise ValueError("task must not be empty")
    if len(task) > 100_000:
        raise ValueError("task exceeds 100000 characters")
    return task.strip()


def cmd_doctor(args: argparse.Namespace) -> int:
    policy = policy_mod.from_dict(_load_applied())
    adapters = _load_adapters()
    result = doctor_adapters(adapters, _required_adapter_sources(policy), cwd=store.project_root())
    _emit(result, f"doctor {'PASSED' if result['ok'] else 'FAILED'}")
    return 0 if result["ok"] else 2


def cmd_run(args: argparse.Namespace) -> int:
    try:
        task = _validate_task(_resolve_task(args))
    except ValueError as exc:
        _emit(
            {"error": {"code": "invalid_task", "message": str(exc)}},
            f"run rejected: {exc}",
        )
        return 64
    policy = policy_mod.from_dict(_load_applied())
    if args.replay:
        transport = Transport(mode="replay", replay_dir=Path(args.replay))
        mode = "replay"
    else:
        adapters = _load_adapters()
        preflight = doctor_adapters(adapters, _required_adapter_sources(policy), cwd=store.project_root())
        if not preflight["ok"]:
            _emit(
                {"error": {"code": "adapter_preflight_failed"}, "doctor": preflight},
                "run rejected: adapter preflight failed; run `gotenx doctor`",
            )
            return 2
        transport = Transport(mode="real", cwd=store.project_root(), adapters=adapters)
        mode = "run"

    run_id = store.new_run_id()
    staged = None
    legacy_usage = None
    if policy.uses_staged_orchestration:
        staged = run_staged(task, policy, transport)
        panel = staged["panel"]
        judge = staged["judge"]
        configured_sources = [s["source"] for s in policy.stages if s["role"] != "judge"]
    else:
        panel = run_panel(policy.panel_sources, task, transport)
        judge = run_judge(panel, policy.judge_source, transport)
        configured_sources = policy.panel_sources
        panel_usage = panel.pop("usage", None)
        judge_usage = judge.pop("usage", None)
        if mode == "run":
            legacy_usage = {"panel": panel_usage or {}, "judge": judge_usage}
    graph = build_graph(panel, judge)
    run_metrics = metrics.compute_all(graph, configured_sources)

    warnings = list(panel.get("warnings", [])) + list(judge.get("warnings", [])) + list(graph.warnings)
    if staged:
        status = staged["status"]
        legacy_failure = None
    else:
        # P8 fail closed (issue #2): a real (non-replay) run with nothing
        # usable must not report ok. Replay stays lenient for golden fixtures.
        legacy_failure = None
        if mode == "run":
            if not panel.get("insights"):
                legacy_failure = {"reason": "panel_empty"}
            elif not judge.get("items"):
                legacy_failure = {"reason": "judge_empty"}
            elif not graph.referenced_panel_ids:
                legacy_failure = {"reason": "no_grounded_judge_items"}
        status = "failed" if legacy_failure else "ok"
    metadata = store.make_metadata(
        run_id=run_id,
        mode=mode,
        status=status,
        panels=panel.get("panels", []),
        warnings=warnings,
        human_override=args.human_override,
    )
    if staged:
        metadata["models"] = [stage["model"] for stage in staged["stages"]]
        metadata["cost_usd"] = staged["usage"]["cost_usd"]
        if staged.get("failure"):
            metadata["failure"] = staged["failure"]
    elif legacy_failure:
        metadata["failure"] = legacy_failure
    store.save_run(
        run_id, panel, judge, run_metrics, metadata,
        stages=staged["stages"] if staged else None,
        usage=staged["usage"] if staged else legacy_usage,
    )
    if staged and mode == "run" and staged["usage"]["cost_usd"]:
        usage_mod.record(run_id, staged["usage"]["cost_usd"])
    _emit(
        {"run_id": run_id, "mode": mode, "metrics": run_metrics, "metadata": metadata,
         "usage": staged["usage"] if staged else legacy_usage},
        f"run {run_id}: survival={run_metrics['panel_insight_survival_rate']:.2f} "
        f"panels={panel.get('panels')} warnings={len(warnings)}",
    )
    return 0 if status == "ok" else 2


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
    if applied.get("_baseline_pending"):
        _emit({"proposal": None, "status": "blocked", "reason": "v3_baseline_pending"},
              "propose blocked: run and pass the v3 benchmark first")
        return 2
    policy = policy_mod.from_dict(applied)
    baseline = _load_baseline()
    proposal_path = Path(args.proposal)
    proposal = validate_proposal(store.read_json(proposal_path), path=proposal_path)
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


def _cmd_apply_locked(args: argparse.Namespace) -> int:
    applied = _load_applied()
    if applied.get("_baseline_pending"):
        _emit({"applied": False, "reason": "v3_baseline_pending"},
              "apply blocked: run and pass the v3 benchmark first")
        return 2
    policy = policy_mod.from_dict(applied)
    baseline = _load_baseline()
    proposal_path = Path(args.proposal)
    proposal = validate_proposal(store.read_json(proposal_path), path=proposal_path)
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


def cmd_apply(args: argparse.Namespace) -> int:
    with store.file_lock("policy-state"):
        return _cmd_apply_locked(args)


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
        "baseline_pending": bool(applied.get("_baseline_pending")),
        "usage": usage_mod.budget_status(policy),
        "models": [stage.get("model") for stage in policy.stages],
        "adapters": str(store.adapters_path()),
    }
    _emit(
        result,
        f"status: epoch {result['epoch']}, {len(runs)} runs, "
        f"{len(policy.protected_metrics)} protected metrics, "
        f"{'OK' if not problems else str(len(problems)) + ' problems'}",
    )
    return 1 if problems else 0


def _benchmark_cfg(policy) -> dict:
    cfg = dict(policy.benchmark_cfg)
    cfg["max_baseline_ratio"] = float(policy.cost_budget.get("max_baseline_ratio", 0.65))
    return cfg


def cmd_benchmark_run(args: argparse.Namespace) -> int:
    applied = _load_applied()
    policy = policy_mod.from_dict(applied)
    suite_dir = Path(args.suite)
    suite = benchmark_mod.load_suite(suite_dir, int(policy.benchmark_cfg.get("cases", 60)))
    out_path = Path(args.output or (store.gotenx_dir() / "benchmarks" / "v1-results.json"))
    result = store.read_json(out_path) if out_path.exists() and args.resume else {"suite": suite.get("id", "v1"), "cases": []}
    result = validate_benchmark_checkpoint(result, path=out_path)
    done = {case["id"] for case in result["cases"]}
    transport = Transport(mode="real", cwd=store.project_root())
    for case in suite["cases"]:
        if case["id"] in done:
            continue
        try:
            captured = benchmark_mod.run_case(
                case, policy, transport, seed=int(policy.benchmark_cfg.get("seed", 20260618)),
                root=store.project_root(),
                record_cost=lambda role, cost, case_id=case["id"]: usage_mod.record(
                    f"benchmark:{case_id}:{role}", cost
                ),
            )
        except benchmark_mod.BenchmarkBudgetExhausted as exc:
            blocked = {
                "passed": False, "status": "blocked", "reason": "budget_exhausted",
                "phase": exc.phase, "budget": exc.status, "completed_cases": len(result["cases"]),
                "checkpoint": str(out_path),
            }
            store.write_json_atomic(out_path, result)
            _emit(blocked, f"benchmark blocked before {exc.phase}; resume after budget recovers")
            return 2
        result["cases"].append(captured)
        store.write_json_atomic(out_path, result)
    summary = benchmark_mod.report(result, _benchmark_cfg(policy))
    result["report"] = summary
    store.write_json_atomic(out_path, result)
    if summary["passed"]:
        with store.file_lock("policy-state"):
            current = _load_applied()
            current.pop("_baseline_pending", None)
            current["_benchmark_baseline"] = summary
            store.write_json_atomic(store.policy_path(), current)
    floor = summary["capability_floor"]
    floor_text = (
        f"floor={floor['case_pass_rate']:.3f}"
        if floor.get("measured") else "floor=unmeasured"
    )
    _emit(summary, f"benchmark {'PASSED' if summary['passed'] else 'FAILED'}: quality={summary['quality']['lower_confidence_bound']:.3f} {floor_text} cost_ratio={summary['cost']['ratio']:.3f}")
    return 0 if summary["passed"] else 2


def cmd_benchmark_report(args: argparse.Namespace) -> int:
    policy = policy_mod.from_dict(_load_applied())
    results_path = Path(args.results)
    result = validate_benchmark_checkpoint(store.read_json(results_path), path=results_path, require_cases=True)
    summary = benchmark_mod.report(result, _benchmark_cfg(policy))
    floor = summary["capability_floor"]
    floor_text = (
        f"floor={floor['case_pass_rate']:.3f}"
        if floor.get("measured") else "floor=unmeasured"
    )
    _emit(summary, f"benchmark {'PASSED' if summary['passed'] else 'FAILED'}: quality={summary['quality']['lower_confidence_bound']:.3f} {floor_text} cost_ratio={summary['cost']['ratio']:.3f}")
    return 0 if summary["passed"] else 2


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="gotenx", description="Gotenx cost-aware deliberation pipeline")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("init", help="initialize .gotenx/ and copy canonical policy")
    sp.add_argument("--force", action="store_true", help="overwrite existing applied policy")
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("doctor", help="validate configured agent CLI adapters without model calls")
    sp.set_defaults(func=cmd_doctor)

    sp = sub.add_parser("run", help="run one four-stage deliberation cycle")
    sp.add_argument("task_words", nargs="*", metavar="TASK",
                     help="task prompt (positional; joined with spaces)")
    sp.add_argument("--task", help="task prompt for the panel (overrides positional TASK)")
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

    sp = sub.add_parser("benchmark", help="capture or report the v1 quality/cost benchmark")
    bench_sub = sp.add_subparsers(dest="benchmark_command", required=True)
    bp = bench_sub.add_parser("run", help="run all live candidate, baseline, and grading calls")
    bp.add_argument("--suite", required=True, help="directory containing manifest.json")
    bp.add_argument("--output", help="checkpoint/result JSON path")
    bp.add_argument("--resume", action="store_true", help="resume an existing checkpoint")
    bp.set_defaults(func=cmd_benchmark_run)
    bp = bench_sub.add_parser("report", help="recompute deterministic acceptance from captured results")
    bp.add_argument("--results", required=True, help="captured result JSON")
    bp.set_defaults(func=cmd_benchmark_report)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except DataValidationError as exc:
        _emit({"ok": False, "error": exc.as_dict()}, f"{args.command} failed: {exc}")
        return 2
    except (OSError, ValueError, TypeError, KeyError) as exc:
        error = DataValidationError(str(exc), code="invalid_data")
        _emit({"ok": False, "error": error.as_dict()}, f"{args.command} failed: {exc}")
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
