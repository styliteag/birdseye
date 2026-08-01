"""Tar configured directories to a second host over SSH, with dated retention.

    uv run backup_offsite.py              # one archive, verified on the far side, then prune
    uv run backup_offsite.py --dry-run    # build it locally, ship nothing
    uv run backup_offsite.py --list       # what is on the remote right now

This is the *configuration* safety net, and it is deliberately different from
`backup_volumes.py` (which mails an encrypted 7z, capped at attachment size) and
from `clone_standby.py` (a running system that is at most one sync out of date).
This one is a dated archive on a machine you control: big enough to hold whole
stack directories, and the thing you dig an old compose file or an ACME store
out of three weeks later.

Consistency: live SQLite databases are **not** tarred from disk — a file being
written while tar reads it lands in the archive torn. List them in
`OFFSITE_DB_PATHS` and they are snapshotted with SQLite's online backup API
(see `sqlite_snapshot.py`) and appended to the archive at their normal path,
replacing what `OFFSITE_EXCLUDE` left out.

Everything is configuration:

  OFFSITE_SSH_HOST=root@backup.example.com   where it goes (empty disables the job)
  OFFSITE_REMOTE_DIR=/root/backups           directory there, created 0700
  OFFSITE_PATHS=/data/traefik,/data/netbird  what to pack (mount them read-only)
  OFFSITE_DB_PATHS=/data/netbird/store.db    live SQLite files, snapshotted first
  OFFSITE_EXCLUDE=*.mmdb,*.BIN               tar patterns for large regenerable files
  OFFSITE_KEEP=14                            archives retained on the remote

Archives are written 0600 and contain **secrets** (ACME private keys, `.env`
files, store encryption keys). Send them somewhere that already holds data of
the same sensitivity — a standby controller, not a general file dump.

Failures mail OFFSITE_EMAIL_TO (falling back to BACKUP_EMAIL_TO, then SMTP_TO)
and, if `CHECKMK_SPOOL_DIR` is set, mark the local check CRIT.
"""

from __future__ import annotations

import argparse
import datetime
import os
import shutil
import subprocess
import sys

from dotenv import load_dotenv

import checkmk
import sqlite_snapshot
from backup_common import (
    base_subject,
    env,
    env_int,
    env_list,
    error_mail,
    make_log,
    send_mail,
    smtp_config,
)
from remote import Remote, RemoteError, run_local

_log = make_log("backup_offsite")

DEFAULT_WORK_DIR = "/var/tmp/birdseye-offsite"
DEFAULT_PREFIX = "config-backup"
DEFAULT_KEEP = 14
CHECK_NAME = "NetBird_Config_Backup"
SPOOL_FILE = "netbird_config_backup"
# 48 h: with a daily schedule one missed run is tolerated, two are not.
SPOOL_MAX_AGE = 172800


def _base_dir(paths: list[str]) -> str:
    """The directory the archive is relative to.

    Explicit `OFFSITE_BASE_DIR` wins. Otherwise it is the common parent of every
    configured path, so `/data/traefik,/data/netbird` produces an archive holding
    `traefik/` and `netbird/` rather than a deep chain of empty directories.
    """
    explicit = env("OFFSITE_BASE_DIR")
    if explicit:
        return explicit.rstrip("/") or "/"
    common = os.path.commonpath([os.path.abspath(p) for p in paths])
    if common in (os.path.abspath(p) for p in paths):  # a single path, or nested ones
        common = os.path.dirname(common)
    return common or "/"


def _relative(path: str, base: str) -> str:
    rel = os.path.relpath(os.path.abspath(path), base)
    if rel.startswith(".."):
        raise SystemExit(f"{path} is outside OFFSITE_BASE_DIR={base}")
    return rel


def build_archive(paths: list[str], db_paths: list[str], base: str, work: str, stamp: str) -> str:
    prefix = env("OFFSITE_PREFIX", DEFAULT_PREFIX)
    excludes = [f"--exclude={pattern}" for pattern in env_list("OFFSITE_EXCLUDE")]
    tar = os.path.join(work, f"{prefix}-{stamp}.tar")

    run_local(["tar", "cf", tar, *excludes, "-C", base, *[_relative(p, base) for p in paths]])

    if db_paths:
        snap_root = os.path.join(work, "snapshots")
        members = []
        for src in db_paths:
            rel = _relative(src, base)
            dst = os.path.join(snap_root, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            sqlite_snapshot.snapshot(src, dst)
            members.append(rel)
            _log(f"snapshot {rel}: {os.path.getsize(dst) / 1e6:.1f} MB, integrity ok")
        # Appended after the plain tar pass, so these replace anything the
        # excludes did not already keep out.
        run_local(["tar", "rf", tar, "-C", snap_root, *members])

    run_local(["gzip", "-6", tar])
    gz = tar + ".gz"
    os.chmod(gz, 0o600)
    count = len(run_local(["tar", "tzf", gz]).splitlines())
    _log(f"archive {os.path.basename(gz)}: {os.path.getsize(gz) / 1e6:.1f} MB, {count} members")
    return gz


def ship(remote: Remote, gz: str, remote_dir: str, keep: int) -> str:
    """Copy the archive over, prove it arrived intact, then prune to `keep`.

    The remote answers in labelled lines: ssh merges stdout and stderr, so
    reading anything by position would be a race waiting to happen.
    """
    name = os.path.basename(gz)
    prefix = env("OFFSITE_PREFIX", DEFAULT_PREFIX)
    local_sha = run_local(["sha256sum", gz]).split()[0]
    remote.run(f"mkdir -p {remote_dir} && chmod 700 {remote_dir}")
    remote.push_file(gz, f"{remote_dir}/{name}")
    out = remote.run(
        f"""set -e
cd {remote_dir}
chmod 600 {name}
echo "SHA $(sha256sum {name} | cut -d' ' -f1)"
tar tzf {name} >/dev/null && echo TAR_OK
for old in $(ls -1t {prefix}-*.tar.gz | tail -n +{keep + 1}); do
  rm -f "$old" && echo "PRUNED $old"
done
echo "COUNT $(ls -1 {prefix}-*.tar.gz | wc -l)"
echo "SIZE $(du -sh . | cut -f1)"
"""
    )
    fields, pruned = {}, []
    for line in out.splitlines():
        key, _, value = line.strip().partition(" ")
        if key == "PRUNED":
            pruned.append(value)
        elif key in ("SHA", "COUNT", "SIZE"):
            fields[key] = value
    if fields.get("SHA") != local_sha:
        raise RemoteError(f"checksum mismatch after transfer ({name})")
    if "TAR_OK" not in out:
        raise RemoteError(f"{name} does not list cleanly on the remote")
    _log("verified on the remote: sha256 matches, archive lists cleanly")
    if pruned:
        _log(f"pruned {len(pruned)} beyond the last {keep}: {', '.join(pruned)}")
    return (
        f"{fields.get('COUNT', '?')} archives, {fields.get('SIZE', '?')}, "
        f"newest {name} ({os.path.getsize(gz) / 1e6:.1f} MB)"
    )


def _notify_failure(detail: str, remote_dir: str) -> None:
    try:
        cfg = smtp_config(
            recipient_env="OFFSITE_EMAIL_TO", fallback_env="BACKUP_EMAIL_TO", who="backup_offsite"
        )
    except SystemExit:
        try:
            cfg = smtp_config(recipient_env="SMTP_TO", who="backup_offsite")
        except SystemExit:
            _log("no SMTP configured — failure not mailed")
            return
    subject = base_subject("NetBird offsite backup", env("BACKUP_LABEL"))
    body = (
        f"{detail}\n\nThe archive should have landed in {remote_dir} on "
        f"{env('OFFSITE_SSH_HOST')}.\nCheck with:  backup_offsite.py --list\n"
    )
    try:
        send_mail(cfg, error_mail(cfg, subject, "The offsite config backup", body))
    except OSError as e:
        _log(f"could not mail the failure either: {e}")


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--dry-run", action="store_true", help="build the archive locally, ship nothing"
    )
    ap.add_argument("--list", action="store_true", help="show what is on the remote and exit")
    ap.add_argument("--keep", type=int, default=None, help="override OFFSITE_KEEP for this run")
    args = ap.parse_args(argv if argv is not None else sys.argv[1:])

    remote = Remote.from_env("OFFSITE")
    if remote is None:
        raise SystemExit("OFFSITE_SSH_HOST is not set — the offsite backup job is disabled")
    remote_dir = env("OFFSITE_REMOTE_DIR")
    if not remote_dir:
        raise SystemExit("OFFSITE_REMOTE_DIR is not set")

    if args.list:
        print(
            remote.run(
                f"ls -lht {remote_dir}/ 2>/dev/null | head -20; echo; du -sh {remote_dir} 2>/dev/null"
            )
        )
        return 0

    # Everything below alerts on failure, misconfiguration included: a path that
    # is no longer mounted is exactly the kind of breakage a scheduled run has to
    # shout about rather than log quietly into the container's stdout.
    try:
        paths = env_list("OFFSITE_PATHS")
        if not paths:
            raise SystemExit("OFFSITE_PATHS is not set — nothing to back up")
        missing = [p for p in paths + env_list("OFFSITE_DB_PATHS") if not os.path.exists(p)]
        if missing:
            raise SystemExit(
                "configured but not present inside the container: " + ", ".join(missing)
            )

        keep = args.keep if args.keep is not None else env_int("OFFSITE_KEEP", DEFAULT_KEEP)
        work = env("OFFSITE_WORK_DIR", DEFAULT_WORK_DIR)
        base = _base_dir(paths)
        started = datetime.datetime.now()
        stamp = started.strftime("%Y%m%d-%H%M%S")

        if os.path.isdir(work):
            shutil.rmtree(work)
        os.makedirs(work, exist_ok=True)
        os.chmod(work, 0o700)

        _log(f"packing {', '.join(paths)} relative to {base}")
        gz = build_archive(paths, env_list("OFFSITE_DB_PATHS"), base, work, stamp)
        if args.dry_run:
            _log(f"dry-run: keeping {gz}, not shipping it")
            return 0
        summary = ship(remote, gz, remote_dir, keep)
        took = (datetime.datetime.now() - started).seconds
        _log(f"ok in {took}s — {summary}")
        checkmk.write(
            SPOOL_FILE,
            CHECK_NAME,
            checkmk.OK,
            f"{started:%Y-%m-%d %H:%M} ok in {took}s; {summary}",
            SPOOL_MAX_AGE,
        )
        return 0
    except (RemoteError, subprocess.SubprocessError, OSError, RuntimeError, SystemExit) as e:
        detail = str(e) if isinstance(e, SystemExit) else f"{type(e).__name__}: {e}"
        _log(f"FAILED: {detail}")
        _notify_failure(detail, remote_dir)
        checkmk.write(
            SPOOL_FILE, CHECK_NAME, checkmk.CRIT, f"backup FAILED: {detail[:160]}", SPOOL_MAX_AGE
        )
        return 1
    finally:
        if not args.dry_run:
            shutil.rmtree(env("OFFSITE_WORK_DIR", DEFAULT_WORK_DIR), ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
