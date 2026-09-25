"""Read birdseye job status and queue manual runs, via the shared jobs volume.

The `birdseye` container owns the directory (see jobrun.py). This side only
reads `registry.json` and `state/*.json`, and drops request files into
`requests/`. It re-checks what jobrun will check anyway, so the UI can say
no before anything is written — but jobrun remains the authority.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

STALE_AFTER_S = 600  # forwarder polls every ~30 s; 10 min silence means it is down
MODES = ("run", "dry-run")


class JobError(ValueError):
    pass


@dataclass(frozen=True)
class Job:
    key: str
    label: str
    schedule: str
    enabled: bool
    reason: str
    triggerable: bool
    dry_run: bool
    status: str  # ok | failed | running | never | disabled
    started: str = ""
    finished: str = ""
    duration_s: float | None = None
    exit_code: int | None = None
    trigger: str = ""
    mode: str = ""
    log_tail: tuple[str, ...] = ()
    history: tuple[dict, ...] = ()


@dataclass(frozen=True)
class Forwarder:
    status: str  # ok | failing | stale | unknown
    age_s: int | None = None
    last_id: int | None = None
    error: str = ""
    outage_started: float | None = None
    forwarded: int = 0
    uptime_s: int | None = None


@dataclass(frozen=True)
class JobsView:
    available: bool
    jobs: tuple[Job, ...] = ()
    forwarder: Forwarder = Forwarder("unknown")
    registry_written: str = ""


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _status(enabled: bool, state: dict) -> str:
    if state.get("running"):
        return "running"
    if "exit_code" in state:
        return "ok" if state["exit_code"] == 0 else "failed"
    return "never" if enabled else "disabled"


def _forwarder(base: Path, now: float) -> Forwarder:
    hb = _read(base / "state" / "forwarder.json")
    if not hb.get("updated"):
        return Forwarder("unknown")
    age = int(now - float(hb["updated"]))
    healthy = "ok" if hb.get("last_poll_ok") else "failing"
    status = "stale" if age > STALE_AFTER_S else healthy
    started = hb.get("started")
    return Forwarder(
        status=status,
        age_s=age,
        last_id=hb.get("last_id"),
        error=str(hb.get("error") or ""),
        outage_started=hb.get("outage_started"),
        forwarded=int(hb.get("forwarded_since_start") or 0),
        uptime_s=int(now - float(started)) if started else None,
    )


def load_jobs(base: Path, clock: Callable[[], float] = time.time) -> JobsView:
    registry = _read(base / "registry.json")
    if not registry:
        return JobsView(available=False)
    jobs = []
    for item in registry.get("jobs", []):
        key = str(item.get("key", ""))
        enabled = bool(item.get("enabled"))
        state = _read(base / "state" / f"{key}.json")
        jobs.append(
            Job(
                key=key,
                label=str(item.get("label") or key),
                schedule=str(item.get("schedule") or ""),
                enabled=enabled,
                reason=str(item.get("reason") or ""),
                triggerable=bool(item.get("triggerable")) and enabled,
                dry_run=bool(item.get("dry_run_arg")),
                status=_status(enabled, state),
                started=str(state.get("started") or ""),
                finished=str(state.get("finished") or ""),
                duration_s=state.get("duration_s"),
                exit_code=state.get("exit_code"),
                trigger=str(state.get("trigger") or ""),
                mode=str(state.get("mode") or ""),
                log_tail=tuple(str(x) for x in state.get("log_tail") or ()),
                history=tuple(h for h in state.get("history") or () if isinstance(h, dict)),
            )
        )
    # what you can start first, then what runs on a schedule, then the rest
    jobs.sort(key=lambda j: (not j.triggerable, not j.enabled, j.key))
    return JobsView(
        available=True,
        jobs=tuple(jobs),
        forwarder=_forwarder(base, clock()),
        registry_written=str(registry.get("written") or ""),
    )


def request_run(base: Path, key: str, mode: str, *, by: str) -> str:
    """Drop a request file for jobrun.py serve; returns the request id."""
    job = next((j for j in load_jobs(base).jobs if j.key == key), None)
    if job is None:
        raise JobError(f"unknown job {key!r}")
    if not job.triggerable:
        raise JobError(f"{key} is not triggerable")
    if mode not in MODES or (mode == "dry-run" and not job.dry_run):
        raise JobError(f"mode {mode!r} is not available for {key}")
    rid = f"{int(time.time())}-{uuid.uuid4().hex[:8]}"
    requests = base / "requests"
    tmp = requests / f".{rid}.tmp"
    try:
        tmp.write_text(json.dumps({"job": key, "mode": mode, "by": by}))
        os.chmod(tmp, 0o644)
        os.replace(tmp, requests / f"{rid}.json")
    except OSError as exc:
        raise JobError(f"cannot queue the run: {exc.strerror or exc}") from exc
    return rid
