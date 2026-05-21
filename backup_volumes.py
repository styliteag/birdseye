"""Pack configured directories into a password-protected 7z archive and email it.

Designed to run from cron inside the birdseye container alongside a self-hosted
NetBird stack. The NetBird Docker volumes (management, signal, ...) are mounted
read-only into this container; this script tars them up, encrypts the archive
with 7z (AES256, including filenames via `-mhe=on`), and ships it as an SMTP
attachment. If the resulting archive exceeds BACKUP_MAX_ATTACHMENT_MB, an
error mail is sent instead so the operator notices before the next run.

Required env vars:
  BACKUP_PATHS              comma-separated paths inside the container
  BACKUP_ZIP_PASSWORD       passphrase for the 7z archive
  SMTP_HOST, SMTP_FROM      see event_forwarder.py for the full SMTP block
  BACKUP_EMAIL_TO or SMTP_TO

Optional:
  BACKUP_MAX_ATTACHMENT_MB  default 20. Limit applies to the *base64-encoded*
                            SMTP payload, not the raw archive (so it matches
                            the size your SMTP server actually counts).
  BACKUP_LABEL              free-form tag, ends up in subject and filename
  BACKUP_EXCLUDE            comma-separated 7z wildcard patterns matched
                            case-insensitively against file names, recursive
                            (e.g. "geo*,*.tmp"). Useful for large derived
                            data files (GeoIP DBs, caches) that the operator
                            can re-download instead of mailing every week.
  SMTP_PORT, SMTP_STARTTLS, SMTP_USER, SMTP_PASSWORD

Notes:
  Active SQLite databases (e.g. NetBird management's store.db) may be in the
  middle of a write when the cron fires; the resulting archive can contain
  an inconsistent WAL state. For a hot-consistent backup either stop the
  NetBird container briefly, or run `sqlite3 store.db ".backup snap.db"`
  before invoking this script and back up `snap.db` instead.

Examples:
    uv run backup_volumes.py --dry-run
    BACKUP_PATHS=/backup/management,/backup/signal uv run backup_volumes.py
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv

from backup_common import (
    BASE64_OVERHEAD,
    DEFAULT_MAX_MB,
    attachment_mail,
    base_subject,
    build_archive,
    env,
    env_int,
    env_list,
    error_mail,
    make_log,
    send_mail,
    smtp_config,
)

_log = make_log("backup_volumes")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="build the archive and report size, but do not send mail",
    )
    return parser.parse_args()


def _resolve_paths(raw_paths: Iterable[str]) -> list[Path]:
    resolved: list[Path] = []
    missing: list[str] = []
    for raw in raw_paths:
        path = Path(raw)
        if path.exists():
            resolved.append(path)
        else:
            missing.append(raw)
    if missing:
        raise SystemExit(
            "BACKUP_PATHS contains paths that do not exist inside the container: "
            + ", ".join(missing)
        )
    if not resolved:
        raise SystemExit("BACKUP_PATHS is empty — nothing to back up")
    return resolved


def _body(archive: Path, paths: list[Path]) -> str:
    size_mb = archive.stat().st_size / (1024 * 1024)
    return (
        f"Encrypted NetBird volume backup attached.\n\n"
        f"  archive: {archive.name}\n"
        f"  size:    {size_mb:.2f} MB\n"
        f"  paths:\n" + "\n".join(f"    - {p}" for p in paths) + "\n\n"
        "Decrypt with the password from BACKUP_ZIP_PASSWORD:\n"
        f"  7z x {archive.name}\n"
    )


def main() -> int:
    load_dotenv()
    args = _parse_args()

    paths_raw = env_list("BACKUP_PATHS")
    if not paths_raw:
        raise SystemExit("BACKUP_PATHS is not set")
    password = env("BACKUP_ZIP_PASSWORD")
    if not password:
        raise SystemExit("BACKUP_ZIP_PASSWORD is not set")
    max_mb = env_int("BACKUP_MAX_ATTACHMENT_MB", DEFAULT_MAX_MB)
    label = env("BACKUP_LABEL")
    excludes = env_list("BACKUP_EXCLUDE")

    paths = _resolve_paths(paths_raw)
    cfg = smtp_config(recipient_env="BACKUP_EMAIL_TO", who="backup_volumes")
    subject = base_subject("NetBird backup", label)

    with tempfile.TemporaryDirectory(prefix="netbird-backup-") as tmp:
        ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        name = f"netbird-{label or 'backup'}-{ts}.7z"
        archive = Path(tmp) / name
        excluded_note = f" (excluding {', '.join(excludes)})" if excludes else ""
        _log(f"packing {len(paths)} path(s) into {archive.name}{excluded_note}")
        result = build_archive(paths, password, archive, excludes=excludes)
        if result.returncode != 0:
            stderr = (result.stderr or result.stdout or "").strip()
            _log(f"7z failed (rc={result.returncode}): {stderr[:400]}")
            if args.dry_run:
                return 1
            send_mail(
                cfg,
                error_mail(
                    cfg,
                    subject,
                    "NetBird volume backup",
                    f"7z exit {result.returncode}: {stderr}",
                ),
            )
            return 1

        size_mb = archive.stat().st_size / (1024 * 1024)
        encoded_mb = size_mb * BASE64_OVERHEAD
        _log(
            f"archive size {size_mb:.2f} MB raw / ~{encoded_mb:.2f} MB after "
            f"base64 (mail-size limit {max_mb} MB)"
        )

        if encoded_mb > max_mb:
            reason = (
                f"archive {archive.name} is {size_mb:.2f} MB raw "
                f"(~{encoded_mb:.2f} MB after base64), exceeds "
                f"BACKUP_MAX_ATTACHMENT_MB={max_mb}. Raise the limit, "
                "shrink BACKUP_PATHS, or switch to off-host storage. "
                "Note: SMTP servers count the encoded size — Gmail caps at "
                "25 MB, many corporate relays at 10 MB."
            )
            _log(reason)
            if args.dry_run:
                return 2
            send_mail(cfg, error_mail(cfg, subject, "NetBird volume backup", reason))
            return 2

        if args.dry_run:
            _log("dry-run: skipping mail send")
            return 0

        send_mail(cfg, attachment_mail(cfg, subject, archive, _body(archive, paths)))
        _log(f"sent {archive.name} ({size_mb:.2f} MB) to {', '.join(cfg.to)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
