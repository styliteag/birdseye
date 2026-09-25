"""Record cron-job runs and accept manual triggers, for the birdseye-web Jobs page.

Everything lives under JOBS_DIR (default /var/lib/birdseye/jobs):

  registry.json        every job the entrypoint knows, enabled or not, and why
  state/<job>.json     last run: start, end, exit code, log tail, short history
  state/forwarder.json heartbeat of the audit-event forwarder
  requests/*.json      "please run <job>" files written by birdseye-web

The web UI never gets a command line, only a job name. `serve` runs a request
only if the job is in the registry, enabled, and listed in JOB_TRIGGERS here
in this container — a compromised web container can at most start one of
those jobs early.

  jobrun.py run <job> [--trigger T] [--mode run|dry-run] -- <command...>
  jobrun.py register < lines          (from entrypoint.sh, tab-separated)
  jobrun.py serve                     (supervisord program)
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import shlex
import stat
import subprocess
import sys
import time
from collections import deque
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

DEFAULT_DIR = "/var/lib/birdseye/jobs"
DEFAULT_TRIGGERS = "cleanup,maintenance"
SKIPPED = 75  # EX_TEMPFAIL: another run of the same job holds the lock
MODES = ("run", "dry-run")
# Jobs whose script takes a dry-run flag; only these offer "dry-run" to the UI.
DRY_RUN_ARGS = {"cleanup": "--dry-run", "maintenance": "--dry-run"}
WRAPPER = "/app/cron_wrapper.sh"


@dataclass(frozen=True)
class JobSpec:
    key: str
    label: str
    schedule: str
    command: list[str] = field(default_factory=list)
    enabled: bool = True
    reason: str = ""  # why it is disabled
    triggerable: bool = False
    dry_run_arg: str = ""


def jobs_dir() -> Path:
    return Path(os.environ.get("JOBS_DIR") or DEFAULT_DIR)


def ensure_dirs(base: Path) -> None:
    (base / "state").mkdir(parents=True, exist_ok=True)
    (base / "requests").mkdir(parents=True, exist_ok=True)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _write_json(path: Path, data: object) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)  # readers never see a half-written file


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


# --- recording a run -------------------------------------------------------------


@contextlib.contextmanager
def job_lock(base: Path, key: str) -> Iterator[bool]:
    """Non-blocking per-job lock; yields False if another run holds it."""
    path = base / "state" / f"{key}.lock"
    with open(path, "a") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _stream(command: list[str], tail: deque[str]) -> int:
    try:
        proc = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
        )
    except OSError as exc:
        line = f"[jobrun] cannot start {command[0]}: {exc}"
        print(line, file=sys.stderr, flush=True)
        tail.append(line)
        return 127
    assert proc.stdout is not None
    for line in proc.stdout:
        sys.stdout.write(line)  # keep the container log as before
        sys.stdout.flush()
        tail.append(line.rstrip("\n"))
    return proc.wait()


def run_job(
    base: Path,
    key: str,
    command: list[str],
    *,
    trigger: str = "cron",
    mode: str = "run",
    tail_lines: int = 200,
    history: int = 20,
) -> int:
    ensure_dirs(base)
    state_path = base / "state" / f"{key}.json"
    with job_lock(base, key) as got:
        if not got:
            print(f"[jobrun] {key}: previous run still active, skipping", file=sys.stderr)
            return SKIPPED
        prev = _read_json(state_path)
        started, t0 = _now(), time.monotonic()
        run = {"job": key, "running": True, "started": started, "trigger": trigger, "mode": mode}
        _write_json(state_path, {**prev, **run})

        tail: deque[str] = deque(maxlen=tail_lines)
        rc = _stream(command, tail)

        summary = {
            "started": started,
            "finished": _now(),
            "duration_s": round(time.monotonic() - t0, 1),
            "exit_code": rc,
            "trigger": trigger,
            "mode": mode,
        }
        past = [summary, *prev.get("history", [])][:history]
        _write_json(
            state_path,
            {"job": key, "running": False, **summary, "log_tail": list(tail), "history": past},
        )
        return rc


# --- registry --------------------------------------------------------------------


def trigger_allowlist() -> set[str]:
    raw = os.environ.get("JOB_TRIGGERS")
    raw = DEFAULT_TRIGGERS if raw is None else raw
    return {k.strip() for k in raw.split(",") if k.strip()}


def parse_registry_lines(lines: Iterable[str], triggers: set[str]) -> list[JobSpec]:
    """`key<TAB>label<TAB>schedule<TAB>command<TAB>reason`; a reason means disabled."""
    specs = []
    for line in lines:
        if not line.strip():
            continue
        key, label, schedule, command, reason = (line.rstrip("\n").split("\t") + [""] * 5)[:5]
        enabled = not reason
        specs.append(
            JobSpec(
                key=key,
                label=label or key,
                schedule=schedule,
                command=shlex.split(command),
                enabled=enabled,
                reason=reason,
                triggerable=enabled and key in triggers,
                dry_run_arg=DRY_RUN_ARGS.get(key, ""),
            )
        )
    return specs


def write_registry(base: Path, specs: Iterable[JobSpec]) -> None:
    ensure_dirs(base)
    _write_json(
        base / "registry.json",
        {"written": _now(), "jobs": [asdict(s) for s in specs]},
    )


def load_registry(base: Path) -> dict[str, JobSpec]:
    raw = _read_json(base / "registry.json")
    out = {}
    for item in raw.get("jobs", []):
        try:
            spec = JobSpec(**item)
        except TypeError:
            continue
        out[spec.key] = spec
    return out


# --- manual triggers -------------------------------------------------------------


def pending_requests(base: Path) -> list[Path]:
    return sorted((base / "requests").glob("*.json"))


def _run_command(spec: JobSpec, mode: str, by: str) -> list[str]:
    job_cmd = [*spec.command, *([spec.dry_run_arg] if mode == "dry-run" else [])]
    this = [sys.executable, os.path.abspath(__file__), "run", spec.key]
    return [WRAPPER, *this, "--trigger", f"manual:{by}", "--mode", mode, "--", *job_cmd]


def handle_request(
    base: Path,
    path: Path,
    registry: Mapping[str, JobSpec],
    spawn: Callable[[list[str]], object],
) -> str:
    """Validate one request file, start the job if allowed, always remove the file."""
    try:
        # The directory is writable by the web container and this runs as root:
        # never follow a link it planted there, and never read anything but a
        # small regular file.
        st = path.lstat()
        if not stat.S_ISREG(st.st_mode) or st.st_size > 4096:
            return "rejected: not a regular request file"
        body = _read_json(path)
        key = str(body.get("job", ""))
        mode = str(body.get("mode", "run"))
        by = "".join(c for c in str(body.get("by", "?")) if c.isprintable())[:80] or "?"
        spec = registry.get(key)
        if spec is None:
            return f"rejected: unknown job {key!r}"
        if not (spec.enabled and spec.triggerable):
            return f"rejected: {key} is not triggerable"
        if mode not in MODES or (mode == "dry-run" and not spec.dry_run_arg):
            return f"rejected: mode {mode!r} not available for {key}"
        spawn(_run_command(spec, mode, by))
        return "accepted"
    finally:
        with contextlib.suppress(OSError):
            path.unlink()


def _spawn(cmd: list[str]) -> None:
    # Detached, so a long job does not block the next request. Output goes to
    # this process's stdout/stderr, which supervisord routes to the container log.
    subprocess.Popen(cmd, start_new_session=True)


def serve(base: Path, interval: float = 3.0) -> None:
    ensure_dirs(base)
    print(f"[jobrun] watching {base / 'requests'}", file=sys.stderr, flush=True)
    while True:
        registry = load_registry(base)
        for path in pending_requests(base):
            outcome = handle_request(base, path, registry, _spawn)
            print(f"[jobrun] request {path.name}: {outcome}", file=sys.stderr, flush=True)
        time.sleep(interval)


# --- CLI ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    command: list[str] = []
    if "--" in argv:
        i = argv.index("--")
        argv, command = argv[:i], argv[i + 1 :]
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("job")
    r.add_argument("--trigger", default="cron")
    r.add_argument("--mode", default="run", choices=MODES)
    sub.add_parser("register")
    sub.add_parser("serve")
    args = ap.parse_args(argv)

    base = jobs_dir()
    if args.cmd == "run":
        if not command:
            ap.error("run needs a command after --")
        return run_job(base, args.job, command, trigger=args.trigger, mode=args.mode)
    if args.cmd == "register":
        write_registry(base, parse_registry_lines(sys.stdin, trigger_allowlist()))
        return 0
    serve(base)
    return 0


if __name__ == "__main__":
    sys.exit(main())
