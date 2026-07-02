"""Shared CLI transport + JSON extraction for the panel and judge runners.

Two transports:

  * ``real``   - shells out to an agent CLI (claude / codex / opencode) in
                 headless mode and captures stdout.
  * ``replay`` - reads a recorded raw response from a fixtures directory, so
                 Eval golden cases and unit tests run with zero LLM dependency
                 and full determinism (P2: Eval executes top-level runs).

IDs are never taken from model output; the caller assigns them (P9/P11).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# argv templates per source; "{prompt}" is replaced with the actual prompt.
# claude emits a JSON envelope under --output-format json; ``result_path`` says
# which key holds the assistant text. Others print plain stdout.
DEFAULT_ADAPTERS: dict[str, dict] = {
    "claude": {
        "argv": ["claude", "-p", "{prompt}", "--output-format", "json"],
        "result_path": "result",
        "usage_path": "usage",
    },
    "codex": {
        "argv": ["codex", "exec", "{prompt}"],
        "result_path": None,
    },
    "opencode": {
        "argv": ["opencode", "run", "{prompt}"],
        "result_path": None,
    },
}


@dataclass(frozen=True)
class InvokeResult:
    raw: str
    usage: dict | None = None


@dataclass
class Transport:
    mode: str = "real"  # "real" | "replay"
    replay_dir: Path | None = None
    adapters: dict = field(default_factory=lambda: dict(DEFAULT_ADAPTERS))
    timeout: int = 600
    cwd: Path | None = None

    def invoke(self, source: str, prompt: str, slot: str) -> str:
        """Return the raw assistant text for ``source``.

        ``slot`` names the fixture file in replay mode (e.g. "panel/claude").
        """
        return self.invoke_result(source, prompt, slot).raw

    def invoke_result(self, source: str, prompt: str, slot: str) -> InvokeResult:
        """Return assistant text and provider usage, when available."""
        if self.mode == "replay":
            return InvokeResult(raw=self._replay(slot))
        return self._real(source, prompt)

    def invoke_model(self, stage: dict, prompt: str) -> "ModelResult":
        """Invoke one configured OpenCode model and retain metering data."""
        if self.mode == "replay":
            raw = self._replay(f"stages/{stage['id']}")
            usage_path = Path(self.replay_dir or ".") / "stages" / f"{stage['id']}.usage.json"
            usage = json.loads(usage_path.read_text()) if usage_path.exists() else {}
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

    # -- replay -----------------------------------------------------------
    def _replay(self, slot: str) -> str:
        if self.replay_dir is None:
            raise ValueError("replay transport requires replay_dir")
        path = Path(self.replay_dir) / f"{slot}.raw.txt"
        if not path.exists():
            # also accept a .json fixture (already-structured)
            jpath = Path(self.replay_dir) / f"{slot}.json"
            if jpath.exists():
                return jpath.read_text()
            raise FileNotFoundError(f"no replay fixture for {slot!r}: {path}")
        return path.read_text()

    # -- real -------------------------------------------------------------
    def _real(self, source: str, prompt: str) -> InvokeResult:
        adapter = self.adapters.get(source)
        if adapter is None:
            raise ValueError(f"no adapter configured for panel source {source!r}")
        argv = [a.replace("{prompt}", prompt) for a in adapter["argv"]]
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=self.timeout
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"{source} CLI failed (exit {proc.returncode}): {proc.stderr.strip()[:500]}"
            )
        out = proc.stdout
        result_path = adapter.get("result_path")
        if result_path:
            try:
                env = json.loads(out)
            except json.JSONDecodeError:
                return InvokeResult(raw=out)
            if not isinstance(env, dict):
                return InvokeResult(raw=out)
            result = env.get(result_path)
            usage = env.get(adapter.get("usage_path", "usage"))
            return InvokeResult(
                raw=result if isinstance(result, str) else out,
                usage=usage if isinstance(usage, dict) else None,
            )
        return InvokeResult(raw=out)


_ARRAY_RE = re.compile(r"\[.*\]", re.DOTALL)


@dataclass(frozen=True)
class ModelResult:
    text: str
    usage: dict
    raw_events: list[dict]


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
            if isinstance(obj.get("cost"), (int, float)):
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


def extract_json_array(text: str) -> list:
    """Pull the first JSON array out of possibly-prose-wrapped model output.

    Tolerant of markdown fences and surrounding chatter. Raises ValueError if
    no parseable array is found.
    """
    stripped = text.strip()
    # fast path: whole thing is an array
    try:
        val = json.loads(stripped)
        if isinstance(val, list):
            return val
    except json.JSONDecodeError:
        pass
    # strip markdown fences
    fenced = re.sub(r"^```(?:json)?|```$", "", stripped, flags=re.MULTILINE).strip()
    try:
        val = json.loads(fenced)
        if isinstance(val, list):
            return val
    except json.JSONDecodeError:
        pass
    # last resort: greedy bracket match
    m = _ARRAY_RE.search(text)
    if m:
        val = json.loads(m.group(0))
        if isinstance(val, list):
            return val
    raise ValueError("no JSON array found in model output")
