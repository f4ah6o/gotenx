"""Agent CLI transports, preflight checks, metering, and JSON extraction.

The real transport supports Claude Code, Codex, OpenCode, and user-defined
adapters. Replay mode reads deterministic fixtures. IDs are always assigned by
Gotenx, never trusted from model output.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .validation import DataValidationError, read_json, require_object

MAX_MODEL_OUTPUT_CHARS = 1_000_000
MAX_JSON_SCAN_STARTS = 10_000


DEFAULT_ADAPTERS: dict[str, dict[str, Any]] = {
    "claude": {
        "argv": ["claude", "-p", "{prompt}", "--output-format", "json"],
        "format": "envelope",
        "result_path": "result",
        "usage_path": "usage",
        "doctor_argv": ["claude", "--version"],
        "timeout_seconds": 600,
    },
    "codex": {
        "argv": [
            "codex", "exec", "--json", "--sandbox", "read-only",
            "--ephemeral", "-C", "{cwd}", "{prompt}",
        ],
        "format": "codex_jsonl",
        "result_path": None,
        "doctor_argv": ["codex", "--version"],
        "timeout_seconds": 600,
    },
    "opencode": {
        "argv": ["opencode", "run", "{prompt}"],
        "format": "plain",
        "result_path": None,
        "doctor_argv": ["opencode", "--version"],
        "timeout_seconds": 600,
    },
}


@dataclass(frozen=True)
class InvokeResult:
    raw: str
    usage: dict | None = None


@dataclass(frozen=True)
class ModelResult:
    text: str
    usage: dict
    raw_events: list[dict]


def _copy_defaults() -> dict[str, dict[str, Any]]:
    return copy.deepcopy(DEFAULT_ADAPTERS)


def _validate_argv(value: object, *, field_name: str, require_prompt: bool) -> list[str]:
    if not isinstance(value, list) or not value or any(not isinstance(v, str) or not v for v in value):
        raise ValueError(f"{field_name} must be a non-empty list of strings")
    if require_prompt and not any("{prompt}" in item for item in value):
        raise ValueError(f"{field_name} must contain {{prompt}}")
    return list(value)


def merge_adapters(overrides: dict | None = None) -> dict[str, dict[str, Any]]:
    """Merge and validate user adapter overrides with safe built-in defaults."""
    merged = _copy_defaults()
    if overrides is None:
        return merged
    if not isinstance(overrides, dict):
        raise ValueError("adapters must be an object")
    for name, raw in overrides.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError("adapter names must be non-empty strings")
        if not isinstance(raw, dict):
            raise ValueError(f"adapter {name!r} must be an object")
        cfg = copy.deepcopy(merged.get(name, {}))
        cfg.update(copy.deepcopy(raw))
        cfg["argv"] = _validate_argv(cfg.get("argv"), field_name=f"adapter {name!r}.argv", require_prompt=True)
        if "doctor_argv" in cfg:
            cfg["doctor_argv"] = _validate_argv(
                cfg["doctor_argv"], field_name=f"adapter {name!r}.doctor_argv", require_prompt=False
            )
        fmt = cfg.get("format", "envelope" if cfg.get("result_path") else "plain")
        if fmt not in {"plain", "envelope", "codex_jsonl"}:
            raise ValueError(f"adapter {name!r}.format is unsupported: {fmt!r}")
        cfg["format"] = fmt
        for key in ("result_path", "usage_path"):
            if cfg.get(key) is not None and not isinstance(cfg[key], str):
                raise ValueError(f"adapter {name!r}.{key} must be a string or null")
        timeout = cfg.get("timeout_seconds", 600)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError(f"adapter {name!r}.timeout_seconds must be positive")
        cfg["timeout_seconds"] = float(timeout)
        merged[name] = cfg
    return merged


def load_adapters(path: str | Path) -> dict[str, dict[str, Any]]:
    """Load `.gotenx/adapters.json`, preserving defaults for omitted adapters."""
    data = require_object(read_json(path), path=path)
    schema = data.get("schema_version")
    if schema not in {None, "gotenx.adapters.v1"}:
        raise ValueError(f"unsupported adapters schema_version {schema!r}")
    try:
        return merge_adapters(data.get("adapters", {}))
    except ValueError as exc:
        raise DataValidationError(str(exc), field="$.adapters", path=path) from exc


def _render_argv(argv: list[str], *, prompt: str, cwd: Path | None) -> list[str]:
    root = str((cwd or Path.cwd()).resolve())
    return [item.replace("{prompt}", prompt).replace("{cwd}", root) for item in argv]


def doctor_adapters(
    adapters: dict[str, dict[str, Any]], sources: list[str], *, cwd: Path | None = None
) -> dict:
    """Perform a no-model-call operational preflight for required adapters."""
    results: list[dict] = []
    for source in dict.fromkeys(sources):
        cfg = adapters.get(source)
        if cfg is None:
            results.append({"source": source, "ok": False, "error": "adapter_not_configured"})
            continue
        try:
            argv = _validate_argv(cfg.get("argv"), field_name=f"adapter {source!r}.argv", require_prompt=True)
            rendered = _render_argv(argv, prompt="preflight", cwd=cwd)
        except ValueError as exc:
            results.append({"source": source, "ok": False, "error": "invalid_adapter", "detail": str(exc)})
            continue
        executable = rendered[0]
        resolved = shutil.which(executable)
        if resolved is None and not (Path(executable).is_file() and os.access(executable, os.X_OK)):
            results.append({
                "source": source, "ok": False, "error": "binary_not_found", "executable": executable,
            })
            continue
        doctor_argv = cfg.get("doctor_argv") or [executable, "--version"]
        probe = _render_argv(list(doctor_argv), prompt="preflight", cwd=cwd)
        try:
            proc = subprocess.run(
                probe, capture_output=True, text=True, timeout=min(float(cfg.get("timeout_seconds", 600)), 10.0),
                cwd=cwd,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            results.append({
                "source": source, "ok": False, "error": "probe_failed", "executable": resolved or executable,
                "detail": str(exc),
            })
            continue
        if proc.returncode != 0:
            results.append({
                "source": source, "ok": False, "error": "probe_nonzero", "executable": resolved or executable,
                "exit_code": proc.returncode, "detail": proc.stderr.strip()[:500],
            })
            continue
        version = (proc.stdout.strip() or proc.stderr.strip()).splitlines()
        results.append({
            "source": source, "ok": True, "executable": resolved or executable,
            "version": version[0][:200] if version else None,
        })
    return {"ok": all(item["ok"] for item in results), "adapters": results}


@dataclass
class Transport:
    mode: str = "real"  # "real" | "replay"
    replay_dir: Path | None = None
    adapters: dict = field(default_factory=_copy_defaults)
    timeout: int = 600
    cwd: Path | None = None

    def invoke(self, source: str, prompt: str, slot: str) -> str:
        return self.invoke_result(source, prompt, slot).raw

    def invoke_result(self, source: str, prompt: str, slot: str) -> InvokeResult:
        if self.mode == "replay":
            return InvokeResult(raw=self._replay(slot))
        return self._real(source, prompt)

    def invoke_model(self, stage: dict, prompt: str) -> ModelResult:
        """Invoke one configured OpenCode model and retain metering data."""
        if self.mode == "replay":
            raw = self._replay(f"stages/{stage['id']}")
            usage_path = Path(self.replay_dir or ".") / "stages" / f"{stage['id']}.usage.json"
            usage = require_object(read_json(usage_path), path=usage_path) if usage_path.exists() else {}
            return ModelResult(text=raw, usage=usage, raw_events=[])

        argv = [
            "opencode", "run", "--pure", "--agent",
            str(stage.get("agent", "gotenx-readonly")), "--model", str(stage["model"]),
            "--format", "json",
        ]
        variant = stage.get("variant")
        if variant:
            argv.extend(["--variant", str(variant)])
        argv.append(prompt)
        agent_config = {
            "description": "Read-only Gotenx planning and review analyst",
            "mode": "primary",
            "permission": {
                "edit": "deny", "bash": "deny", "webfetch": "deny",
                "task": "deny", "question": "deny",
            },
        }
        benchmark_agent_config = {
            "description": "Context-only Gotenx benchmark participant",
            "mode": "primary",
            "permission": {
                "read": "deny", "edit": "deny", "bash": "deny",
                "webfetch": "deny", "task": "deny", "question": "deny",
            },
        }
        env = dict(os.environ)
        try:
            inline = json.loads(env.get("OPENCODE_CONFIG_CONTENT", "{}"))
        except json.JSONDecodeError:
            inline = {}
        if not isinstance(inline, dict):
            inline = {}
        if not isinstance(inline.get("agent"), dict):
            inline["agent"] = {}
        agents = inline["agent"]
        agents["gotenx-readonly"] = agent_config
        agents["gotenx-benchmark"] = benchmark_agent_config
        env["OPENCODE_CONFIG_CONTENT"] = json.dumps(inline)
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=self.timeout,
            cwd=self.cwd, env=env,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"OpenCode stage {stage['id']!r} failed (exit {proc.returncode}): "
                f"{proc.stderr.strip()[:500]}"
            )
        return parse_opencode_events(proc.stdout)

    def _replay(self, slot: str) -> str:
        if self.replay_dir is None:
            raise ValueError("replay transport requires replay_dir")
        path = Path(self.replay_dir) / f"{slot}.raw.txt"
        if not path.exists():
            jpath = Path(self.replay_dir) / f"{slot}.json"
            if jpath.exists():
                text = jpath.read_text()
                if len(text) > MAX_MODEL_OUTPUT_CHARS:
                    raise ValueError(f"replay fixture exceeds {MAX_MODEL_OUTPUT_CHARS} characters: {jpath}")
                return text
            raise FileNotFoundError(f"no replay fixture for {slot!r}: {path}")
        text = path.read_text()
        if len(text) > MAX_MODEL_OUTPUT_CHARS:
            raise ValueError(f"replay fixture exceeds {MAX_MODEL_OUTPUT_CHARS} characters: {path}")
        return text

    def _real(self, source: str, prompt: str) -> InvokeResult:
        adapter = self.adapters.get(source)
        if adapter is None:
            raise ValueError(f"no adapter configured for panel source {source!r}")
        argv = _render_argv(adapter["argv"], prompt=prompt, cwd=self.cwd)
        try:
            proc = subprocess.run(
                argv, capture_output=True, text=True,
                timeout=float(adapter.get("timeout_seconds", self.timeout)), cwd=self.cwd,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(f"{source} CLI is not installed or not on PATH: {argv[0]}") from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"{source} CLI timed out after {exc.timeout} seconds") from exc
        if proc.returncode != 0:
            raise RuntimeError(
                f"{source} CLI failed (exit {proc.returncode}): {proc.stderr.strip()[:500]}"
            )
        out = proc.stdout
        fmt = adapter.get("format", "envelope" if adapter.get("result_path") else "plain")
        if fmt == "codex_jsonl":
            result = parse_codex_events(out)
            return InvokeResult(raw=result.text, usage=result.usage or None)
        if fmt == "envelope":
            try:
                envelope = json.loads(out)
            except json.JSONDecodeError:
                return InvokeResult(raw=out)
            if not isinstance(envelope, dict):
                return InvokeResult(raw=out)
            result_path = adapter.get("result_path")
            result = envelope.get(result_path) if result_path else None
            usage = envelope.get(adapter.get("usage_path", "usage"))
            return InvokeResult(
                raw=result if isinstance(result, str) else out,
                usage=usage if isinstance(usage, dict) else None,
            )
        return InvokeResult(raw=out)


def _walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def parse_opencode_events(raw: str) -> ModelResult:
    """Parse OpenCode NDJSON output without depending on one event revision."""
    events: list[dict] = []
    for line in raw.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)

    texts: list[str] = []
    cost = 0.0
    tokens = {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0, "cache_write": 0}
    found_usage = False
    for event in events:
        if event.get("type") == "text":
            part = event.get("part", event)
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                texts.append(part["text"])
        for obj in _walk(event):
            if isinstance(obj.get("cost"), (int, float)) and not isinstance(obj.get("cost"), bool):
                cost += float(obj["cost"])
                found_usage = True
            tok = obj.get("tokens")
            if not isinstance(tok, dict):
                continue
            found_usage = True
            tokens["input"] += int(tok.get("input", 0) or 0)
            tokens["output"] += int(tok.get("output", 0) or 0)
            tokens["reasoning"] += int(tok.get("reasoning", 0) or 0)
            cache = tok.get("cache", {}) if isinstance(tok.get("cache"), dict) else {}
            tokens["cache_read"] += int(cache.get("read", 0) or 0)
            tokens["cache_write"] += int(cache.get("write", 0) or 0)

    if not texts and events:
        for obj in _walk(events):
            if isinstance(obj.get("text"), str):
                texts.append(obj["text"])
    if not events:
        return ModelResult(text=raw, usage={}, raw_events=[])
    usage = {"cost_usd": cost, "tokens": tokens} if found_usage else {}
    return ModelResult(text="".join(texts), usage=usage, raw_events=events)


def parse_codex_events(raw: str) -> ModelResult:
    """Parse `codex exec --json` JSONL into final assistant text and token usage."""
    events: list[dict] = []
    texts: list[str] = []
    tokens = {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0}
    found_usage = False
    for line in raw.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        events.append(event)
        item = event.get("item")
        if event.get("type") == "item.completed" and isinstance(item, dict) and item.get("type") == "agent_message":
            text = item.get("text")
            if isinstance(text, str):
                texts.append(text)
        usage = event.get("usage")
        if event.get("type") == "turn.completed" and isinstance(usage, dict):
            found_usage = True
            tokens["input"] += int(usage.get("input_tokens", 0) or 0)
            tokens["output"] += int(usage.get("output_tokens", 0) or 0)
            tokens["reasoning"] += int(usage.get("reasoning_output_tokens", 0) or 0)
            tokens["cache_read"] += int(usage.get("cached_input_tokens", 0) or 0)
    if not texts:
        for event in events:
            if event.get("type") in {"message", "agent_message"} and isinstance(event.get("text"), str):
                texts.append(event["text"])
    if not events:
        return ModelResult(text=raw, usage={}, raw_events=[])
    return ModelResult(
        text="".join(texts),
        usage={"tokens": tokens} if found_usage else {},
        raw_events=events,
    )


def extract_json_value(text: str, expected_type: type, *, max_chars: int = MAX_MODEL_OUTPUT_CHARS):
    """Return the first bounded standalone JSON value of the requested type.

    `JSONDecoder.raw_decode` preserves nesting and quoted brackets. When a
    complete value of the wrong type is encountered, scanning resumes after
    that value so nested schema examples are not mistaken for top-level output.
    """
    if not isinstance(text, str):
        raise ValueError("model output must be text")
    if len(text) > max_chars:
        raise ValueError(f"model output exceeds {max_chars} characters")
    decoder = json.JSONDecoder()
    starts = 0
    index = 0
    while index < len(text):
        next_array = text.find("[", index)
        next_object = text.find("{", index)
        candidates = [pos for pos in (next_array, next_object) if pos >= 0]
        if not candidates:
            break
        start = min(candidates)
        starts += 1
        if starts > MAX_JSON_SCAN_STARTS:
            raise ValueError("model output contains too many JSON candidates")
        try:
            value, end = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            index = start + 1
            continue
        if isinstance(value, expected_type):
            return value
        index = max(end, start + 1)
    expected = "array" if expected_type is list else "object" if expected_type is dict else expected_type.__name__
    raise ValueError(f"no JSON {expected} found in model output")


def extract_json_array(text: str, *, max_chars: int = MAX_MODEL_OUTPUT_CHARS) -> list:
    return extract_json_value(text, list, max_chars=max_chars)


def extract_json_object(text: str, *, max_chars: int = MAX_MODEL_OUTPUT_CHARS) -> dict:
    return extract_json_value(text, dict, max_chars=max_chars)
