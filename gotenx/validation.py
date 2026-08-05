"""Strict stdlib-only validation for user-controlled and persisted data."""

from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

MAX_JSON_BYTES = 16 * 1024 * 1024
MAX_COLLECTION_ITEMS = 100_000


class DataValidationError(ValueError):
    """Stable field-level error suitable for CLI JSON responses."""

    def __init__(self, message: str, *, field: str = "$", path: str | Path | None = None,
                 code: str = "invalid_data"):
        self.message = message
        self.field = field
        self.path = str(path) if path is not None else None
        self.code = code
        location = f"{self.path}:" if self.path else ""
        super().__init__(f"{location}{self.field}: {self.message}")

    def as_dict(self) -> dict:
        result = {"code": self.code, "field": self.field, "message": self.message}
        if self.path is not None:
            result["path"] = self.path
        result["hint"] = (
            "Run `gotenx init` to create the missing state."
            if self.code == "missing_state" else
            "Repair or restore the referenced JSON file; Gotenx left the last published state unchanged."
        )
        return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number {value!r} is not allowed")


def read_json(path: str | Path, *, max_bytes: int = MAX_JSON_BYTES) -> Any:
    path = Path(path)
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise DataValidationError(str(exc), path=path, code="state_io_error") from exc
    if size > max_bytes:
        raise DataValidationError(
            f"JSON file exceeds {max_bytes} bytes", path=path, code="data_too_large"
        )
    try:
        text = path.read_text()
    except OSError as exc:
        raise DataValidationError(str(exc), path=path, code="state_io_error") from exc
    try:
        return json.loads(text, parse_constant=_reject_constant)
    except (json.JSONDecodeError, ValueError) as exc:
        detail = str(exc)
        if isinstance(exc, json.JSONDecodeError):
            detail = f"invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"
        raise DataValidationError(detail, path=path, code="invalid_json") from exc


def require_object(value: Any, field: str = "$", *, path=None) -> dict:
    if not isinstance(value, dict):
        raise DataValidationError("must be an object", field=field, path=path)
    return value


def require_list(value: Any, field: str, *, path=None, max_items: int = MAX_COLLECTION_ITEMS) -> list:
    if not isinstance(value, list):
        raise DataValidationError("must be a list", field=field, path=path)
    if len(value) > max_items:
        raise DataValidationError(f"must contain at most {max_items} items", field=field, path=path)
    return value


def require_string(value: Any, field: str, *, path=None, nonempty: bool = True,
                   max_chars: int = 100_000) -> str:
    if not isinstance(value, str):
        raise DataValidationError("must be a string", field=field, path=path)
    if nonempty and not value.strip():
        raise DataValidationError("must not be empty", field=field, path=path)
    if len(value) > max_chars:
        raise DataValidationError(f"must contain at most {max_chars} characters", field=field, path=path)
    return value


def require_bool(value: Any, field: str, *, path=None) -> bool:
    if not isinstance(value, bool):
        raise DataValidationError("must be a boolean", field=field, path=path)
    return value


def require_int(value: Any, field: str, *, path=None, minimum: int | None = None,
                maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DataValidationError("must be an integer", field=field, path=path)
    if minimum is not None and value < minimum:
        raise DataValidationError(f"must be >= {minimum}", field=field, path=path)
    if maximum is not None and value > maximum:
        raise DataValidationError(f"must be <= {maximum}", field=field, path=path)
    return value


def require_number(value: Any, field: str, *, path=None, minimum: float | None = None,
                   maximum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DataValidationError("must be a number", field=field, path=path)
    result = float(value)
    if not math.isfinite(result):
        raise DataValidationError("must be finite", field=field, path=path)
    if minimum is not None and result < minimum:
        raise DataValidationError(f"must be >= {minimum}", field=field, path=path)
    if maximum is not None and result > maximum:
        raise DataValidationError(f"must be <= {maximum}", field=field, path=path)
    return result


def require_string_list(value: Any, field: str, *, path=None, nonempty: bool = False,
                        unique: bool = False) -> list[str]:
    items = require_list(value, field, path=path)
    result = [require_string(item, f"{field}[{index}]", path=path) for index, item in enumerate(items)]
    if nonempty and not result:
        raise DataValidationError("must not be empty", field=field, path=path)
    if unique and len(result) != len(set(result)):
        raise DataValidationError("must contain unique values", field=field, path=path)
    return result


def parse_timestamp(value: Any, field: str, *, path=None) -> datetime:
    text = require_string(value, field, path=path, max_chars=128)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DataValidationError("must be a valid ISO-8601 timestamp", field=field, path=path) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DataValidationError("must include a timezone offset", field=field, path=path)
    return parsed


def validate_baseline(value: Any, *, path=None) -> dict:
    data = require_object(value, path=path)
    for key, raw in data.items():
        require_string(key, "$.<metric>", path=path, max_chars=256)
        require_number(raw, f"$.{key}", path=path, minimum=0.0, maximum=1.0)
    return data


def validate_usage_ledger(value: Any, *, path=None) -> list[dict]:
    data = require_object(value, path=path)
    entries = require_list(data.get("entries", []), "$.entries", path=path)
    normalized = []
    for index, raw in enumerate(entries):
        field = f"$.entries[{index}]"
        entry = require_object(raw, field, path=path)
        run_id = require_string(entry.get("run_id"), f"{field}.run_id", path=path, max_chars=512)
        timestamp = require_string(entry.get("timestamp"), f"{field}.timestamp", path=path, max_chars=128)
        parse_timestamp(timestamp, f"{field}.timestamp", path=path)
        cost = require_number(entry.get("cost_usd"), f"{field}.cost_usd", path=path, minimum=0.0)
        normalized_entry = dict(entry)
        normalized_entry.update({"run_id": run_id, "timestamp": timestamp, "cost_usd": cost})
        normalized.append(normalized_entry)
    return normalized


def validate_proposal(value: Any, *, path=None) -> dict:
    data = require_object(value, path=path)
    if "id" in data:
        require_string(data["id"], "$.id", path=path, max_chars=256)
    require_object(data.get("changes"), "$.changes", path=path)
    provenance = require_string_list(data.get("provenance", []), "$.provenance", path=path)
    if len(provenance) > 10_000:
        raise DataValidationError("must contain at most 10000 items", field="$.provenance", path=path)
    evidence = require_object(data.get("evidence", {}), "$.evidence", path=path)
    if "support_runs" in evidence:
        require_int(evidence["support_runs"], "$.evidence.support_runs", path=path, minimum=0)
    if "evaluated_epoch" in data:
        require_int(data["evaluated_epoch"], "$.evaluated_epoch", path=path, minimum=0)
    return data


def validate_benchmark_checkpoint(value: Any, *, path=None, require_cases: bool = False) -> dict:
    data = require_object(value, path=path)
    cases = require_list(data.get("cases", []), "$.cases", path=path)
    if require_cases and not cases:
        raise DataValidationError("must not be empty", field="$.cases", path=path)
    ids = []
    for index, raw in enumerate(cases):
        field = f"$.cases[{index}]"
        case = require_object(raw, field, path=path)
        case_id = require_string(case.get("id"), f"{field}.id", path=path, max_chars=256)
        ids.append(case_id)
        candidate = require_object(case.get("candidate"), f"{field}.candidate", path=path)
        candidate_usage = require_object(candidate.get("usage"), f"{field}.candidate.usage", path=path)
        require_number(candidate_usage.get("cost_usd"), f"{field}.candidate.usage.cost_usd", path=path, minimum=0.0)
        baselines = require_list(case.get("baselines"), f"{field}.baselines", path=path)
        if not baselines:
            raise DataValidationError("must not be empty", field=f"{field}.baselines", path=path)
        for bindex, baseline in enumerate(baselines):
            item = require_object(baseline, f"{field}.baselines[{bindex}]", path=path)
            item_usage = require_object(item.get("usage"), f"{field}.baselines[{bindex}].usage", path=path)
            require_number(item_usage.get("cost_usd"), f"{field}.baselines[{bindex}].usage.cost_usd", path=path, minimum=0.0)
        grades = require_list(case.get("grades"), f"{field}.grades", path=path)
        if not grades:
            raise DataValidationError("must not be empty", field=f"{field}.grades", path=path)
        for gindex, grade in enumerate(grades):
            grade_field = f"{field}.grades[{gindex}]"
            item = require_object(grade, grade_field, path=path)
            require_number(item.get("score"), f"{grade_field}.score", path=path, minimum=0.0, maximum=1.0)
            for score_name in ("candidate_scores", "baseline_scores"):
                if item.get(score_name) is None:
                    continue
                score_obj = require_object(item[score_name], f"{grade_field}.{score_name}", path=path)
                for dimension in ("correctness", "coverage", "actionability", "risk_testing", "concision"):
                    require_number(score_obj.get(dimension), f"{grade_field}.{score_name}.{dimension}",
                                   path=path, minimum=1.0, maximum=5.0)
            if "critical_failure" in item:
                require_bool(item["critical_failure"], f"{grade_field}.critical_failure", path=path)
            for failure_name in ("candidate_critical_failures", "baseline_critical_failures"):
                if item.get(failure_name) is not None:
                    failures = require_string_list(item[failure_name], f"{grade_field}.{failure_name}",
                                                   path=path)
                    if len(failures) > 32:
                        raise DataValidationError("must contain at most 32 items",
                                                  field=f"{grade_field}.{failure_name}", path=path)
            if item.get("usage") is not None:
                item_usage = require_object(item["usage"], f"{grade_field}.usage", path=path)
                require_number(item_usage.get("cost_usd", 0.0), f"{grade_field}.usage.cost_usd", path=path, minimum=0.0)
        if case.get("cost") is not None:
            cost = require_object(case["cost"], f"{field}.cost", path=path)
            for name in ("candidate_usd", "baseline_usd", "grader_usd", "total_usd"):
                if name in cost:
                    require_number(cost[name], f"{field}.cost.{name}", path=path, minimum=0.0)
    if len(ids) != len(set(ids)):
        raise DataValidationError("case ids must be unique", field="$.cases", path=path)
    return data


def validate_run_artifacts(run_id: Any, panel: Any, judge: Any, metrics: Any,
                           metadata: Any, stages: Any = None, usage: Any = None) -> None:
    require_string(run_id, "$.run_id", max_chars=256)
    require_object(panel, "$.panel")
    require_object(judge, "$.judge")
    metric_obj = require_object(metrics, "$.metrics")
    for key, value in metric_obj.items():
        require_string(key, "$.metrics.<name>", max_chars=256)
        require_number(value, f"$.metrics.{key}", minimum=0.0)
    meta = require_object(metadata, "$.metadata")
    if "run_id" in meta and meta.get("run_id") != run_id:
        raise DataValidationError("must match reserved run_id", field="$.metadata.run_id")
    if stages is not None:
        require_list(stages, "$.stages")
    if usage is not None:
        require_object(usage, "$.usage")
