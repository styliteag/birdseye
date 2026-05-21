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
import os
import shutil
import smtplib
import socket
import ssl
import subprocess
import sys
import tempfile
from collections.abc import Iterable
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

DEFAULT_MAX_MB = 20
# Base64 grows binary payloads by 4/3; with header overhead ~1.4x is a safe
# upper bound on the SMTP-visible message size relative to the raw archive.
BASE64_OVERHEAD = 1.4


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as e:
        raise SystemExit(f"{name} must be an integer, got {raw!r}") from e


def _env_list(name: str, default: str = "") -> list[str]:
    raw = _env(name) or default
    return [item.strip() for item in raw.split(",") if item.strip()]


def _log(msg: str) -> None:
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{stamp}] backup_volumes: {msg}", file=sys.stderr, flush=True)


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


def _smtp_config() -> dict[str, object]:
    host = _env("SMTP_HOST")
    sender = _env("SMTP_FROM")
    recipients = _env_list("BACKUP_EMAIL_TO") or _env_list("SMTP_TO")
    missing = [n for n, v in [("SMTP_HOST", host), ("SMTP_FROM", sender)] if not v]
    if not recipients:
        missing.append("BACKUP_EMAIL_TO/SMTP_TO")
    if missing:
        raise SystemExit("backup_volumes requires " + ", ".join(missing) + " to be set")
    return {
        "host": host,
        "port": _env_int("SMTP_PORT", 587),
        "user": _env("SMTP_USER"),
        "password": _env("SMTP_PASSWORD"),
        "from": sender,
        "to": recipients,
        "starttls": _env("SMTP_STARTTLS", "true").lower() in {"1", "true", "yes"},
    }


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


def _build_archive(
    paths: list[Path], password: str, archive: Path
) -> subprocess.CompletedProcess[str]:
    if shutil.which("7z") is None:
        raise SystemExit("7z binary not found — install p7zip-full in the image")
    # -mhe=on encrypts filenames as well; -bso0/-bsp0 silence stdout/progress.
    cmd = [
        "7z",
        "a",
        "-t7z",
        "-mhe=on",
        "-mx=5",
        f"-p{password}",
        "-bso0",
        "-bsp0",
        str(archive),
        *[str(p) for p in paths],
    ]
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def _send_mail(cfg: dict[str, object], msg: EmailMessage) -> None:
    host = str(cfg["host"])
    port = int(cfg["port"])  # type: ignore[arg-type]
    user = str(cfg["user"])
    password = str(cfg["password"])
    starttls = bool(cfg["starttls"])
    with smtplib.SMTP(host, port, timeout=30) as smtp:
        if starttls:
            smtp.starttls(context=ssl.create_default_context())
        if user:
            smtp.login(user, password)
        smtp.send_message(msg)


def _subject_host() -> str:
    # Prefer the NetBird hostname from NB_URL — the container hostname
    # (often a random ID) is useless for telling multiple NetBird
    # deployments apart in a shared mailbox.
    nb_url = _env("NB_URL")
    if nb_url:
        netloc = urlparse(nb_url if "://" in nb_url else f"https://{nb_url}").netloc
        if netloc:
            return netloc.split(":", 1)[0]
    return socket.gethostname()


def _base_subject(label: str) -> str:
    host = _subject_host()
    stamp = datetime.now(UTC).strftime("%Y-%m-%d")
    tag = f" {label}" if label else ""
    return f"[NetBird backup{tag}] {host} {stamp}"


def _attachment_mail(
    cfg: dict[str, object], subject: str, archive: Path, paths: list[Path]
) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = str(cfg["from"])
    msg["To"] = ", ".join(cfg["to"])  # type: ignore[arg-type]
    msg["Subject"] = subject
    size_mb = archive.stat().st_size / (1024 * 1024)
    body = (
        f"Encrypted NetBird volume backup attached.\n\n"
        f"  archive: {archive.name}\n"
        f"  size:    {size_mb:.2f} MB\n"
        f"  paths:\n" + "\n".join(f"    - {p}" for p in paths) + "\n\n"
        "Decrypt with the password from BACKUP_ZIP_PASSWORD:\n"
        f"  7z x {archive.name}\n"
    )
    msg.set_content(body)
    data = archive.read_bytes()
    msg.add_attachment(
        data,
        maintype="application",
        subtype="x-7z-compressed",
        filename=archive.name,
    )
    return msg


def _error_mail(cfg: dict[str, object], subject: str, reason: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = str(cfg["from"])
    msg["To"] = ", ".join(cfg["to"])  # type: ignore[arg-type]
    msg["Subject"] = f"{subject} — FAILED"
    msg.set_content(f"NetBird backup did not produce a deliverable archive.\n\n{reason}\n")
    return msg


def main() -> int:
    load_dotenv()
    args = _parse_args()

    paths_raw = _env_list("BACKUP_PATHS")
    if not paths_raw:
        raise SystemExit("BACKUP_PATHS is not set")
    password = _env("BACKUP_ZIP_PASSWORD")
    if not password:
        raise SystemExit("BACKUP_ZIP_PASSWORD is not set")
    max_mb = _env_int("BACKUP_MAX_ATTACHMENT_MB", DEFAULT_MAX_MB)
    label = _env("BACKUP_LABEL")

    paths = _resolve_paths(paths_raw)
    cfg = _smtp_config()
    subject = _base_subject(label)

    with tempfile.TemporaryDirectory(prefix="netbird-backup-") as tmp:
        ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        name = f"netbird-{label or 'backup'}-{ts}.7z"
        archive = Path(tmp) / name
        _log(f"packing {len(paths)} path(s) into {archive.name}")
        result = _build_archive(paths, password, archive)
        if result.returncode != 0:
            stderr = (result.stderr or result.stdout or "").strip()
            _log(f"7z failed (rc={result.returncode}): {stderr[:400]}")
            if args.dry_run:
                return 1
            _send_mail(cfg, _error_mail(cfg, subject, f"7z exit {result.returncode}: {stderr}"))
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
            _send_mail(cfg, _error_mail(cfg, subject, reason))
            return 2

        if args.dry_run:
            _log("dry-run: skipping mail send")
            return 0

        _send_mail(cfg, _attachment_mail(cfg, subject, archive, paths))
        _log(f"sent {archive.name} ({size_mb:.2f} MB) to {', '.join(cfg['to'])}")  # type: ignore[arg-type]
    return 0


if __name__ == "__main__":
    sys.exit(main())
