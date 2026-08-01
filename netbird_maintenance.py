"""Run the account reconcilers on a schedule: posture attachment, then ICMP companions.

    uv run netbird_maintenance.py             # run the configured steps
    uv run netbird_maintenance.py --dry-run   # preview both, write nothing

`manage_posture.py` and `allow_ping.py` are reconcilers: they compare the account
against a convention and fix the difference. Run by hand they only take effect
when someone remembers, so the account drifts between runs — a policy created on
Tuesday has no posture check and no `ZPING:` companion until the next manual
pass. This wrapper is what a cron entry calls
(`CRON_NETBIRD_MAINTENANCE`) so the convention holds by itself.

Order matters. Posture runs **first**: `allow_ping.py` copies each policy's
`source_posture_checks` into its companion, so attaching a posture check after
the companions were built leaves them one cycle behind.

  MAINTENANCE_POSTURE_CHECK=Posture-Europe   attach this check (empty = skip the step)
  MAINTENANCE_POSTURE_RULE=<policy name>     limit to one policy (empty = --all)
  MAINTENANCE_ALLOW_PING=true                reconcile ICMP companions (empty = skip)
  MAINTENANCE_DRY_RUN=true                   preview both steps
  MAINTENANCE_EMAIL_TO=…                     failure mail; falls back to BACKUP_EMAIL_TO, SMTP_TO
  MAINTENANCE_SPOOL_MAX_AGE=7200             Checkmk staleness, matched to your schedule

Both markers the scripts honour still apply, and are the way to exempt something
from an automated run: `PING_IGNORE` in a policy description keeps `allow_ping`
from giving it a companion, `POSTURE_IGNORE` keeps `manage_posture` from
attaching a check to it.

The steps are independent — a failure in one does not stop the other, and the
run reports every failure at the end. Each step's own summary line lands in the
Checkmk check, so a reconciler that suddenly starts changing things on every run
is visible rather than buried in a container log.
"""

from __future__ import annotations

import argparse
import datetime
import subprocess
import sys

from dotenv import load_dotenv

import checkmk
from backup_common import env, env_int, make_log, notify_failure

_log = make_log("netbird_maintenance")

PYTHON = "/app/.venv/bin/python"
CHECK_NAME = "NetBird_Account_Maintenance"
SPOOL_FILE = "netbird_account_maintenance"
# 2 h: one missed run on the suggested hourly schedule is tolerated, two are not.
DEFAULT_SPOOL_MAX_AGE = 7200

TRUTHY = ("1", "true", "yes", "on")


def _flag(name: str) -> bool:
    return env(name).lower() in TRUTHY


def plan(dry_run: bool) -> list[tuple[str, list[str]]]:
    """The steps to run, in the order they have to run in."""
    steps: list[tuple[str, list[str]]] = []
    posture = env("MAINTENANCE_POSTURE_CHECK")
    if posture:
        scope = (
            ["--rule", env("MAINTENANCE_POSTURE_RULE")]
            if env("MAINTENANCE_POSTURE_RULE")
            else ["--all"]
        )
        cmd = [PYTHON, "/app/manage_posture.py", *scope, "--add-posture", posture]
        steps.append(("posture", cmd + (["--dry-run"] if dry_run else [])))
    if _flag("MAINTENANCE_ALLOW_PING"):
        cmd = [PYTHON, "/app/allow_ping.py"]
        steps.append(("allow-ping", cmd + (["--dry-run"] if dry_run else [])))
    return steps


def run_step(name: str, cmd: list[str]) -> tuple[int, str]:
    """Run one reconciler; return its exit code and its last output line."""
    proc = subprocess.run(cmd, text=True, capture_output=True, check=False)
    output = (proc.stdout or "") + (proc.stderr or "")
    print(output, end="" if output.endswith("\n") else "\n")
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    # Both scripts end with a counts line ("Summary: created=… " / "Done. changed=…"),
    # which is exactly what belongs in the monitoring summary.
    return proc.returncode, lines[-1] if lines else "(no output)"


def _notify_failure(detail: str) -> None:
    notify_failure(
        recipient_env="MAINTENANCE_EMAIL_TO",
        subject_prefix="NetBird maintenance",
        what="The scheduled account maintenance",
        detail=detail,
        log=_log,
    )


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--dry-run", action="store_true", help="preview every step, write nothing")
    args = ap.parse_args(argv if argv is not None else sys.argv[1:])
    dry_run = args.dry_run or _flag("MAINTENANCE_DRY_RUN")

    steps = plan(dry_run)
    if not steps:
        raise SystemExit(
            "nothing to do — set MAINTENANCE_POSTURE_CHECK and/or MAINTENANCE_ALLOW_PING"
        )

    started = datetime.datetime.now()
    summaries, failures = [], []
    for name, cmd in steps:
        print(f"\n===== {name}{' (dry run)' if dry_run else ''}")
        try:
            rc, last = run_step(name, cmd)
        except OSError as e:  # the script is missing or not executable
            rc, last = 1, f"{type(e).__name__}: {e}"
        summaries.append(f"{name}: {last}")
        if rc != 0:
            failures.append(f"{name} exited {rc}: {last}")

    took = (datetime.datetime.now() - started).seconds
    summary = "; ".join(summaries)
    if failures:
        detail = "\n".join(failures)
        _log(f"FAILED: {detail}")
        _notify_failure(detail)
        checkmk.write(
            SPOOL_FILE,
            CHECK_NAME,
            checkmk.CRIT,
            f"maintenance FAILED: {detail[:160]}",
            env_int("MAINTENANCE_SPOOL_MAX_AGE", DEFAULT_SPOOL_MAX_AGE),
        )
        return 1

    _log(f"ok in {took}s — {summary}")
    if not dry_run:
        checkmk.write(
            SPOOL_FILE,
            CHECK_NAME,
            checkmk.OK,
            f"{started:%Y-%m-%d %H:%M} ok in {took}s; {summary}"[:400],
            env_int("MAINTENANCE_SPOOL_MAX_AGE", DEFAULT_SPOOL_MAX_AGE),
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
