"""Write a dated JSON snapshot of the NetBird configuration for birdseye-web.

Each run fetches the same endpoints as `export_objects.py` and writes them,
unencrypted, into the shared jobs volume:

  <HISTORY_DIR>/<YYYYMMDDTHHMMSSZ>/<slug>.json   one file per endpoint
  <HISTORY_DIR>/<YYYYMMDDTHHMMSSZ>/manifest.json what was fetched, when

birdseye-web's History page reads these to diff two dates and to restore a
single policy or group. The web container only reads; writing stays here,
in the birdseye container, like every other job.

Required:
  NB_URL, NB_ADMIN_API_KEY   admin token, so the snapshot sees everything

Optional:
  HISTORY_DIR        default <JOBS_DIR>/history (JOBS_DIR default /var/lib/birdseye/jobs)
  HISTORY_KEEP_DAYS  default 90; older snapshots are deleted, the newest two never
  HISTORY_EMAIL_TO   failure mail (falls back to BACKUP_EMAIL_TO, then SMTP_TO)
  CHECKMK_SPOOL_DIR  Checkmk local check `birdseye_history`

  uv run config_history.py [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tempfile
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

import checkmk
from backup_common import env, env_int, make_log, notify_failure

_log = make_log("config_history")

STAMP = re.compile(r"^\d{8}T\d{6}Z$")  # birdseye_web/history.py uses the same format
STAMP_FORMAT = "%Y%m%dT%H%M%SZ"
DEFAULT_KEEP_DAYS = 90
KEEP_MIN = 2  # a diff needs two
CHECK_NAME = "birdseye_history"
SPOOL_FILE = "birdseye_history"
SPOOL_MAX_AGE = 2 * 86400 + 3600


def history_dir() -> Path:
    explicit = env("HISTORY_DIR")
    if explicit:
        return Path(explicit)
    return Path(env("JOBS_DIR") or "/var/lib/birdseye/jobs") / "history"


def snapshot_stamps(base: Path) -> list[str]:
    """Finished snapshots, oldest first (temporary `.tmp-*` dirs are not)."""
    if not base.is_dir():
        return []
    return sorted(p.name for p in base.iterdir() if p.is_dir() and STAMP.match(p.name))


def prune_plan(stamps: Iterable[str], now: datetime, keep_days: int) -> list[str]:
    """Stamps older than `keep_days`, never touching the newest KEEP_MIN."""
    ordered = sorted(stamps)
    candidates = ordered[:-KEEP_MIN] if len(ordered) > KEEP_MIN else []
    cutoff = now - timedelta(days=keep_days)
    return [
        s for s in candidates if datetime.strptime(s, STAMP_FORMAT).replace(tzinfo=UTC) < cutoff
    ]


def _strip_key_values(snapshot: Path) -> None:
    """Setup key values never land on the shared volume, masked or not."""
    path = snapshot / "setup_keys.json"
    if not path.is_file():
        return
    keys = json.loads(path.read_text())
    if isinstance(keys, list):
        clean = [{k: v for k, v in key.items() if k != "key"} for key in keys]
        path.write_text(json.dumps(clean, indent=2, sort_keys=True, ensure_ascii=False))


def take_snapshot(client, base: Path, now: datetime) -> tuple[Path, list[str]]:
    """Fetch into a temp dir next to the target, then rename: readers never
    see a half-written snapshot."""
    from export_objects import dump_objects, write_manifest

    stamp = now.strftime(STAMP_FORMAT)
    base.mkdir(parents=True, exist_ok=True)
    tmp = base / f".tmp-{stamp}"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(mode=0o755)
    try:
        summary, written = dump_objects(client, tmp)
        if not written:
            raise SystemExit(
                "no endpoint returned data — check NB_ADMIN_API_KEY and that the "
                "management API is reachable from this container"
            )
        _strip_key_values(tmp)
        write_manifest(tmp, summary, env("NB_URL"))
        final = base / stamp
        tmp.rename(final)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return final, written


def _prune(base: Path, now: datetime, keep_days: int, dry_run: bool) -> list[str]:
    doomed = prune_plan(snapshot_stamps(base), now, keep_days)
    for stamp in doomed:
        _log(f"{'would delete' if dry_run else 'deleting'} snapshot {stamp}")
        if not dry_run:
            shutil.rmtree(base / stamp)
    return doomed


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--dry-run", action="store_true", help="fetch into a temp dir, write nothing")
    args = ap.parse_args(argv if argv is not None else sys.argv[1:])

    base = history_dir()
    # Configuration errors alert too: a missing key or an unmounted volume must
    # not only show up in the container log.
    try:
        if not env("NB_URL") or not env("NB_ADMIN_API_KEY"):
            raise SystemExit("NB_URL and NB_ADMIN_API_KEY must be set")
        keep_days = env_int("HISTORY_KEEP_DAYS", DEFAULT_KEEP_DAYS)
        from nb_client import client_from_env

        client = client_from_env(key="admin")
        now = datetime.now(UTC)
        if args.dry_run:
            with tempfile.TemporaryDirectory(prefix="history-") as tmp:
                _, written = take_snapshot(client, Path(tmp), now)
            _log(f"dry-run: fetched {len(written)} endpoint(s), nothing written to {base}")
            _prune(base, now, keep_days, dry_run=True)
            return 0
        final, written = take_snapshot(client, base, now)
        removed = _prune(base, now, keep_days, dry_run=False)
        summary = (
            f"{final.name}: {len(written)} endpoint(s); {len(removed)} old snapshot(s) removed"
        )
        _log(summary)
        checkmk.write(SPOOL_FILE, CHECK_NAME, checkmk.OK, summary, SPOOL_MAX_AGE)
        return 0
    # Broad on purpose: an unattended job must mail and mark CRIT on anything.
    except (SystemExit, Exception) as e:  # noqa: B014
        detail = str(e) if isinstance(e, SystemExit) else f"{type(e).__name__}: {e}"
        _log(f"FAILED: {detail}")
        if not args.dry_run:
            notify_failure(
                recipient_env="HISTORY_EMAIL_TO",
                subject_prefix="NetBird config history",
                what="The config history snapshot",
                detail=f"{detail}\n\nSnapshots go to {base}.",
                log=_log,
            )
            checkmk.write(
                SPOOL_FILE, CHECK_NAME, checkmk.CRIT, f"FAILED: {detail[:160]}", SPOOL_MAX_AGE
            )
        return 1


if __name__ == "__main__":
    sys.exit(main())
