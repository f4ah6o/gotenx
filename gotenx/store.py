"""Transactional `.gotenx/` runtime store.

Runtime state is rooted at the consuming project. Run ids are reserved in a
separate directory, complete run artifacts are published atomically, and all
JSON writes replace the previous file only after fsync.
"""

from __future__ import annotations

import atexit
import contextlib
import fcntl
import json
import os
import re
import shutil
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from .validation import DataValidationError, read_json as read_json_strict, validate_run_artifacts

GOTENX_DIR = ".gotenx"
STALE_SECONDS = 24 * 60 * 60
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_OWNED_RESERVATIONS: set[tuple[int, str, str]] = set()


def project_root() -> Path:
    for name in ("GOTENX_PROJECT_DIR", "CLAUDE_PROJECT_DIR", "CODEX_PROJECT_DIR", "CODEX_WORKSPACE_ROOT"):
        value = os.environ.get(name)
        if value:
            return Path(value)
    return Path(os.getcwd())


def gotenx_dir(root: Path | None = None) -> Path:
    return (root or project_root()) / GOTENX_DIR


def runs_dir(root: Path | None = None) -> Path:
    return gotenx_dir(root) / "runs"


def proposals_dir(root: Path | None = None) -> Path:
    return gotenx_dir(root) / "proposals"


def migrations_dir(root: Path | None = None) -> Path:
    return gotenx_dir(root) / "migrations"


def locks_dir(root: Path | None = None) -> Path:
    return gotenx_dir(root) / "locks"


def reservations_dir(root: Path | None = None) -> Path:
    return gotenx_dir(root) / "run-reservations"


def policy_path(root: Path | None = None) -> Path:
    return gotenx_dir(root) / "policy.json"


def baseline_path(root: Path | None = None) -> Path:
    return gotenx_dir(root) / "baseline.json"


def adapters_path(root: Path | None = None) -> Path:
    return gotenx_dir(root) / "adapters.json"


def usage_ledger_path(root: Path | None = None) -> Path:
    return gotenx_dir(root) / "usage-ledger.json"


def ensure_layout(root: Path | None = None) -> Path:
    base = gotenx_dir(root)
    for path in (runs_dir(root), proposals_dir(root), locks_dir(root), reservations_dir(root)):
        path.mkdir(parents=True, exist_ok=True)
    return base


@contextlib.contextmanager
def file_lock(name: str, root: Path | None = None):
    """Exclusive cross-process lock for Linux/macOS using stdlib `flock`."""
    ensure_layout(root)
    path = locks_dir(root) / f"{name}.lock"
    with path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _cleanup_stale(root: Path | None = None, *, now: float | None = None) -> None:
    now = time.time() if now is None else now
    for directory in (reservations_dir(root), runs_dir(root)):
        if not directory.exists():
            continue
        for child in directory.iterdir():
            if directory == runs_dir(root) and not child.name.startswith(".tmp-"):
                continue
            try:
                age = now - child.stat().st_mtime
            except OSError:
                continue
            if age <= STALE_SECONDS:
                continue
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                try:
                    child.unlink()
                except OSError:
                    pass


def _reservation_owner(path: Path) -> int | None:
    try:
        return int((path / "owner-pid").read_text().strip())
    except (OSError, ValueError):
        return None


def _track_reservation(path: Path) -> None:
    _OWNED_RESERVATIONS.add((os.getpid(), str(path.parent.parent), path.name))


def _write_reservation_owner(path: Path) -> None:
    (path / "owner-pid").write_text(str(os.getpid()))
    _fsync_dir(path)
    _track_reservation(path)


def _reserve_specific(run_id: str, root: Path | None = None) -> Path:
    if not isinstance(run_id, str) or not _RUN_ID_RE.fullmatch(run_id):
        raise DataValidationError("contains unsupported characters", field="$.run_id")
    ensure_layout(root)
    reservation = reservations_dir(root) / run_id
    final = runs_dir(root) / run_id
    if final.exists():
        raise DataValidationError("run already exists", field="$.run_id", code="run_conflict")
    try:
        reservation.mkdir()
    except FileExistsError:
        raise DataValidationError("run id is already reserved", field="$.run_id", code="run_conflict")
    _write_reservation_owner(reservation)
    return reservation


def new_run_id(root: Path | None = None) -> str:
    """Atomically reserve and return a monotonic run id."""
    ensure_layout(root)
    with file_lock("run-id", root):
        _cleanup_stale(root)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        counter = 0
        while True:
            candidate = f"{stamp}-{counter:03d}"
            if not (runs_dir(root) / candidate).exists():
                try:
                    reservation = reservations_dir(root) / candidate
                    reservation.mkdir()
                    _write_reservation_owner(reservation)
                    return candidate
                except FileExistsError:
                    pass
            counter += 1


def release_run_id(run_id: str, root: Path | None = None, *, force: bool = False) -> None:
    reservation = reservations_dir(root) / run_id
    if reservation.exists() and (force or _reservation_owner(reservation) == os.getpid()):
        shutil.rmtree(reservation, ignore_errors=True)
    _OWNED_RESERVATIONS.discard((os.getpid(), str(gotenx_dir(root)), run_id))


def _release_owned_at_exit() -> None:
    pid = os.getpid()
    for owner_pid, base, run_id in list(_OWNED_RESERVATIONS):
        if owner_pid == pid:
            release_run_id(run_id, Path(base).parent)


atexit.register(_release_owned_at_exit)


def write_json(path: Path, data: object) -> None:
    """Compatibility name; all persisted JSON writes are atomic."""
    write_json_atomic(path, data)


def write_json_atomic(path: Path, data: object) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        payload = json.dumps(data, indent=2, sort_keys=True, allow_nan=False) + "\n"
    except (TypeError, ValueError) as exc:
        raise DataValidationError(str(exc), path=path, code="invalid_serialization") from exc
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        _fsync_dir(path.parent)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def read_json(path: Path) -> dict:
    value = read_json_strict(path)
    if not isinstance(value, dict):
        raise DataValidationError("root must be an object", path=path)
    return value


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
             root: Path | None = None, *, stages: list | None = None,
             usage: dict | None = None) -> Path:
    """Publish one complete run directory atomically.

    Existing callers that provide their own id are supported by reserving that
    id on entry. Readers see either no run or the complete run, never a partial
    set of files.
    """
    validate_run_artifacts(run_id, panel, judge, metrics, metadata, stages, usage)
    ensure_layout(root)
    reservation = reservations_dir(root) / run_id
    if not reservation.exists():
        _reserve_specific(run_id, root)
    elif _reservation_owner(reservation) != os.getpid():
        raise DataValidationError("run id is reserved by another process", field="$.run_id", code="run_conflict")
    final = runs_dir(root) / run_id
    temp = Path(tempfile.mkdtemp(prefix=f".tmp-{run_id}-", dir=runs_dir(root)))
    try:
        write_json_atomic(temp / "panel.json", panel)
        write_json_atomic(temp / "judge.json", judge)
        write_json_atomic(temp / "metrics.json", metrics)
        write_json_atomic(temp / "metadata.json", metadata)
        if stages is not None:
            write_json_atomic(temp / "stages.json", {"stages": stages})
        if usage is not None:
            write_json_atomic(temp / "usage.json", usage)
        if final.exists():
            raise DataValidationError("run already exists", field="$.run_id", code="run_conflict")
        os.replace(temp, final)
        _fsync_dir(final.parent)
        release_run_id(run_id, root)
        return final
    except Exception:
        shutil.rmtree(temp, ignore_errors=True)
        release_run_id(run_id, root)
        raise


def list_runs(root: Path | None = None) -> list[str]:
    rdir = runs_dir(root)
    if not rdir.exists():
        return []
    return sorted(
        p.name for p in rdir.iterdir()
        if p.is_dir() and not p.name.startswith(".")
    )
