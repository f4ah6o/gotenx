"""P18 .gotenx/ runtime store and metadata.json.

Layout, rooted at the project dir (``$CLAUDE_PROJECT_DIR`` or cwd):

    .gotenx/
      policy.json            # the currently-applied policy (P13 "current applied")
      baseline.json          # ratcheted baselines (P4/P15)
      runs/<run_id>/
        panel.json
        judge.json
        metrics.json
        metadata.json        # P18: run_id, mode, status, panels, warnings
      proposals/<id>.json

run_id is monotonic: ``<UTC-timestamp>-<counter>`` so runs sort chronologically
and never collide within a session.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

GOTENX_DIR = ".gotenx"


def project_root() -> Path:
    return Path(os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd()))


def gotenx_dir(root: Path | None = None) -> Path:
    return (root or project_root()) / GOTENX_DIR


def runs_dir(root: Path | None = None) -> Path:
    return gotenx_dir(root) / "runs"


def proposals_dir(root: Path | None = None) -> Path:
    return gotenx_dir(root) / "proposals"


def policy_path(root: Path | None = None) -> Path:
    return gotenx_dir(root) / "policy.json"


def baseline_path(root: Path | None = None) -> Path:
    return gotenx_dir(root) / "baseline.json"


def ensure_layout(root: Path | None = None) -> Path:
    base = gotenx_dir(root)
    runs_dir(root).mkdir(parents=True, exist_ok=True)
    proposals_dir(root).mkdir(parents=True, exist_ok=True)
    return base


def new_run_id(root: Path | None = None) -> str:
    """Generate a monotonic run id, disambiguated against existing runs."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    rdir = runs_dir(root)
    counter = 0
    while True:
        candidate = f"{stamp}-{counter:03d}"
        if not (rdir / candidate).exists():
            return candidate
        counter += 1


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def read_json(path: Path) -> dict:
    return json.loads(Path(path).read_text())


def make_metadata(
    run_id: str,
    mode: str,
    status: str,
    panels: list[str],
    warnings: list[str],
    *,
    human_override: bool = False,
    overridden_items: list[str] | None = None,
) -> dict:
    """Build a P18-compliant metadata dict."""
    meta = {
        "run_id": run_id,
        "mode": mode,
        "status": status,
        "panels": list(panels),
        "warnings": list(warnings),
    }
    if human_override:
        meta["human_override"] = True
    if overridden_items:
        meta["overridden_items"] = list(overridden_items)
    return meta


def save_run(run_id: str, panel: dict, judge: dict, metrics: dict, metadata: dict,
             root: Path | None = None) -> Path:
    rdir = runs_dir(root) / run_id
    write_json(rdir / "panel.json", panel)
    write_json(rdir / "judge.json", judge)
    write_json(rdir / "metrics.json", metrics)
    write_json(rdir / "metadata.json", metadata)
    return rdir


def list_runs(root: Path | None = None) -> list[str]:
    rdir = runs_dir(root)
    if not rdir.exists():
        return []
    return sorted(p.name for p in rdir.iterdir() if p.is_dir())
