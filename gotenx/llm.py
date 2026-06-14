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


@dataclass
class Transport:
    mode: str = "real"  # "real" | "replay"
    replay_dir: Path | None = None
    adapters: dict = field(default_factory=lambda: dict(DEFAULT_ADAPTERS))
    timeout: int = 600

    def invoke(self, source: str, prompt: str, slot: str) -> str:
        """Return the raw assistant text for ``source``.

        ``slot`` names the fixture file in replay mode (e.g. "panel/claude").
        """
        if self.mode == "replay":
            return self._replay(slot)
        return self._real(source, prompt)

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
    def _real(self, source: str, prompt: str) -> str:
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
                out = env.get(result_path, out)
            except json.JSONDecodeError:
                pass  # tolerate non-envelope output
        return out


_ARRAY_RE = re.compile(r"\[.*\]", re.DOTALL)


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
