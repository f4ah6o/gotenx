"""Canonical policy loading, migration, and validation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from . import LEGACY_SCHEMA_VERSION, SCHEMA_VERSION
from .validation import (
    DataValidationError,
    read_json,
    require_bool,
    require_int,
    require_list,
    require_number,
    require_object,
    require_string,
    require_string_list,
)

INVARIANT_NO_PROMOTION = "no_proposal_metric_promotion"
INVARIANT_JUDGE_PROVENANCE = "judge_provenance_policy"
V3_INVARIANTS = {"ordered_stage_execution", "no_unmetered_run"}
V3_MODELS = [
    "opencode-go/deepseek-v4-flash",
    "opencode-go/kimi-k2.7-code",
    "opencode-go/deepseek-v4-pro",
    "opencode-go/glm-5.2",
]


@dataclass(frozen=True)
class Policy:
    raw: dict

    @property
    def schema_version(self) -> str:
        return self.raw["schema_version"]

    @property
    def protected_metrics(self) -> dict:
        return self.raw.get("protected_metrics", {})

    @property
    def protected_policy_keys(self) -> list[str]:
        return list(self.raw.get("protected_policy_keys", []))

    @property
    def structural_invariants(self) -> list[str]:
        return list(self.raw.get("structural_invariants", []))

    @property
    def future_candidate_metrics(self) -> list[dict]:
        return list(self.raw.get("future_candidate_metrics", []))

    @property
    def panel_sources(self) -> list[str]:
        return list(self.raw.get("panel", {}).get("sources", []))

    @property
    def judge_source(self) -> str:
        return self.raw.get("judge", {}).get("source", "claude")

    @property
    def provenance_faithful_cfg(self) -> dict:
        return self.raw.get("eval", {}).get("provenance_faithful", {})

    @property
    def orchestration(self) -> dict:
        return dict(self.raw.get("orchestration", {}))

    @property
    def stages(self) -> list[dict]:
        return [dict(stage) for stage in self.orchestration.get("stages", [])]

    @property
    def uses_staged_orchestration(self) -> bool:
        return bool(self.stages)

    @property
    def cost_budget(self) -> dict:
        return dict(self.raw.get("cost_budget", {}))

    @property
    def benchmark_cfg(self) -> dict:
        return dict(self.raw.get("benchmark", {}))

    def future_candidate_names(self) -> set[str]:
        return {m["name"] for m in self.future_candidate_metrics}

    def is_protected_key(self, key: str) -> bool:
        return key in set(self.protected_policy_keys)


def load_policy(path: str | Path) -> Policy:
    return from_dict(read_json(path), path=path)


def _validate_metric_config(name: str, value: object, *, path=None) -> None:
    cfg = require_object(value, f"$.protected_metrics.{name}", path=path)
    if "method" in cfg:
        require_string(cfg["method"], f"$.protected_metrics.{name}.method", path=path, max_chars=128)
    if "runs" in cfg:
        require_int(cfg["runs"], f"$.protected_metrics.{name}.runs", path=path, minimum=1, maximum=10_000)
    if "tolerance" in cfg:
        require_number(cfg["tolerance"], f"$.protected_metrics.{name}.tolerance", path=path,
                       minimum=0.0, maximum=1.0)


def from_dict(data: dict, *, path=None) -> Policy:
    data = require_object(data, path=path)
    sv = require_string(data.get("schema_version"), "$.schema_version", path=path, max_chars=128)
    if sv != SCHEMA_VERSION:
        raise DataValidationError(
            f"unsupported value {sv!r}; expected {SCHEMA_VERSION!r}",
            field="$.schema_version", path=path,
        )

    protected = require_object(data.get("protected_metrics", {}), "$.protected_metrics", path=path)
    for name, cfg in protected.items():
        require_string(name, "$.protected_metrics.<name>", path=path, max_chars=256)
        _validate_metric_config(name, cfg, path=path)
    if "observed_diversity_index" in protected:
        raise DataValidationError(
            "must not be protected; protect configured_diversity_index instead",
            field="$.protected_metrics.observed_diversity_index", path=path,
        )

    require_string_list(data.get("protected_policy_keys", []), "$.protected_policy_keys",
                        path=path, unique=True)
    require_string_list(data.get("structural_invariants", []), "$.structural_invariants",
                        path=path, unique=True)

    candidates = require_list(data.get("future_candidate_metrics", []),
                              "$.future_candidate_metrics", path=path)
    for index, raw in enumerate(candidates):
        item = require_object(raw, f"$.future_candidate_metrics[{index}]", path=path)
        require_string(item.get("name"), f"$.future_candidate_metrics[{index}].name", path=path,
                       max_chars=256)
        if "gating" in item:
            require_bool(item["gating"], f"$.future_candidate_metrics[{index}].gating", path=path)

    panel = require_object(data.get("panel", {}), "$.panel", path=path)
    require_string_list(panel.get("sources", []), "$.panel.sources", path=path, unique=True)
    judge = require_object(data.get("judge", {}), "$.judge", path=path)
    if "source" in judge:
        require_string(judge["source"], "$.judge.source", path=path, max_chars=256)

    orchestration = require_object(data.get("orchestration", {}), "$.orchestration", path=path)
    stages = require_list(orchestration.get("stages", []), "$.orchestration.stages", path=path,
                          max_items=64)
    if stages:
        if orchestration.get("mode") != "sequential":
            raise DataValidationError("must be 'sequential'", field="$.orchestration.mode", path=path)
        require_string(orchestration.get("readonly_agent"), "$.orchestration.readonly_agent",
                       path=path, max_chars=256)
        required_roles = ["scout", "author", "critic", "judge"]
        normalized = []
        for index, raw in enumerate(stages):
            stage = require_object(raw, f"$.orchestration.stages[{index}]", path=path)
            for key in ("id", "source", "role", "model"):
                require_string(stage.get(key), f"$.orchestration.stages[{index}].{key}", path=path,
                               max_chars=256)
            if "variant" in stage:
                require_string(stage["variant"], f"$.orchestration.stages[{index}].variant",
                               path=path, max_chars=128)
            for key in ("max_items", "max_content_chars"):
                if key in stage:
                    require_int(stage[key], f"$.orchestration.stages[{index}].{key}", path=path,
                                minimum=1, maximum=100_000)
            normalized.append(stage)
        roles = [stage["role"] for stage in normalized]
        if roles != required_roles:
            raise DataValidationError(f"must be {required_roles!r} in order",
                                      field="$.orchestration.stages", path=path)
        ids = [stage["id"] for stage in normalized]
        if len(ids) != len(set(ids)):
            raise DataValidationError("stage ids must be unique", field="$.orchestration.stages", path=path)
        if [stage["model"] for stage in normalized] != V3_MODELS:
            raise DataValidationError("must use the fixed cost-profile model order",
                                      field="$.orchestration.stages", path=path)
        if orchestration["readonly_agent"] != "gotenx-readonly":
            raise DataValidationError("must be 'gotenx-readonly'",
                                      field="$.orchestration.readonly_agent", path=path)

        cost_budget = require_object(data.get("cost_budget"), "$.cost_budget", path=path)
        require_number(cost_budget.get("reserve_per_run_usd", 0.10),
                       "$.cost_budget.reserve_per_run_usd", path=path, minimum=0.0)
        windows = require_object(cost_budget.get("windows_usd"), "$.cost_budget.windows_usd", path=path)
        if set(windows) != {"5h", "7d", "30d"}:
            raise DataValidationError("must contain exactly 5h, 7d, and 30d",
                                      field="$.cost_budget.windows_usd", path=path)
        for name, value in windows.items():
            require_number(value, f"$.cost_budget.windows_usd.{name}", path=path, minimum=0.0)

        benchmark = require_object(data.get("benchmark"), "$.benchmark", path=path)
        baselines = require_list(benchmark.get("baseline_models"), "$.benchmark.baseline_models",
                                 path=path, max_items=16)
        expected = ["github-copilot/claude-opus-4.6", "github-copilot/gpt-5.5"]
        checked = []
        for index, raw in enumerate(baselines):
            item = require_object(raw, f"$.benchmark.baseline_models[{index}]", path=path)
            checked.append(require_string(item.get("id"),
                                          f"$.benchmark.baseline_models[{index}].id", path=path,
                                          max_chars=256))
            if item.get("variant") != "high":
                raise DataValidationError("must be 'high'",
                                          field=f"$.benchmark.baseline_models[{index}].variant", path=path)
        if checked != expected:
            raise DataValidationError(f"must be {expected!r}", field="$.benchmark.baseline_models", path=path)
        for key in ("cases", "bootstrap_samples", "seed"):
            if key in benchmark:
                require_int(benchmark[key], f"$.benchmark.{key}", path=path, minimum=1)
        for key in ("noninferiority_margin", "confidence"):
            if key in benchmark:
                require_number(benchmark[key], f"$.benchmark.{key}", path=path,
                               minimum=0.0, maximum=1.0)
        if "max_baseline_ratio" in cost_budget:
            require_number(cost_budget["max_baseline_ratio"],
                           "$.cost_budget.max_baseline_ratio", path=path, minimum=0.0)
        if "pricing_snapshot" in cost_budget:
            require_object(cost_budget["pricing_snapshot"], "$.cost_budget.pricing_snapshot", path=path)

    eval_cfg = require_object(data.get("eval", {}), "$.eval", path=path)
    faithful = require_object(eval_cfg.get("provenance_faithful", {}),
                              "$.eval.provenance_faithful", path=path)
    if "sample_k" in faithful:
        require_int(faithful["sample_k"], "$.eval.provenance_faithful.sample_k",
                    path=path, minimum=1, maximum=100_000)
    if "threshold" in faithful:
        require_number(faithful["threshold"], "$.eval.provenance_faithful.threshold",
                       path=path, minimum=0.0, maximum=1.0)

    if "_epoch" in data:
        require_int(data["_epoch"], "$._epoch", path=path, minimum=0)
    return Policy(raw=data)


def migrate_v2_dict(data: dict, template: dict) -> dict:
    data = require_object(data)
    template = require_object(template)
    if data.get("schema_version") != LEGACY_SCHEMA_VERSION:
        raise DataValidationError("migration source is not a v2 policy", field="$.schema_version")
    migrated = json.loads(json.dumps(template, allow_nan=False))
    for key in ("protected_metrics", "future_candidate_metrics", "eval"):
        if key in data:
            migrated[key] = data[key]
    epoch = data.get("_epoch", 0)
    require_int(epoch, "$._epoch", minimum=0)
    migrated["_epoch"] = epoch + 1
    migrated["_migrated_from"] = LEGACY_SCHEMA_VERSION
    from_dict(migrated)
    return migrated


def validate(path: str | Path) -> list[str]:
    problems: list[str] = []
    try:
        pol = load_policy(path)
    except (DataValidationError, OSError, ValueError) as exc:
        return [str(exc)]
    if INVARIANT_NO_PROMOTION not in pol.structural_invariants:
        problems.append(f"missing required invariant {INVARIANT_NO_PROMOTION!r} (P22)")
    missing_v3 = V3_INVARIANTS - set(pol.structural_invariants)
    if missing_v3:
        problems.append(f"missing required v3 invariants: {sorted(missing_v3)!r}")
    for fcm in pol.future_candidate_metrics:
        if fcm.get("gating", False):
            problems.append(f"future_candidate_metric {fcm.get('name')!r} must not be gating (P22)")
        if fcm.get("name") in pol.protected_metrics:
            problems.append(f"future_candidate_metric {fcm.get('name')!r} is also protected (P22 violation)")
    return problems
