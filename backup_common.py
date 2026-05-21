"""Shared helpers for the weekly NetBird backup jobs.

Used by both `backup_volumes.py` (mounted-volume snapshot) and
`export_objects.py` (API config export). Each job picks its own subject
prefix and recipient env var; everything else — SMTP, 7z packaging,
base64-aware size check — comes from here.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urlparse

from smtp_helpers import default_port, open_smtp, resolve_tls_mode

DEFAULT_MAX_MB = 20
# Base64 grows binary payloads by 4/3; with header overhead ~1.4x is a
# safe upper bound on the SMTP-visible message size relative to the raw
# archive — that is what SMTP servers (Gmail 25 MB, Exchange ~10 MB)
# actually count.
BASE64_OVERHEAD = 1.4


# --- env helpers -----------------------------------------------------------


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def env_int(name: str, default: int) -> int:
    raw = env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as e:
        raise SystemExit(f"{name} must be an integer, got {raw!r}") from e


def env_list(name: str, default: str = "") -> list[str]:
    raw = env(name) or default
    return [item.strip() for item in raw.split(",") if item.strip()]


# --- logging ---------------------------------------------------------------


def make_log(component: str) -> Callable[[str], None]:
    def _log(msg: str) -> None:
        stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        print(f"[{stamp}] {component}: {msg}", file=sys.stderr, flush=True)

    return _log


# --- SMTP ------------------------------------------------------------------


def smtp_config(
    *,
    recipient_env: str,
    fallback_env: str = "SMTP_TO",
    who: str,
) -> dict[str, object]:
    """Build the SMTP config dict, falling back to SMTP_TO when the
    job-specific recipient env var is empty. `who` is the script name
    used in the SystemExit message so the operator knows which job
    is missing config."""
    host = env("SMTP_HOST")
    sender = env("SMTP_FROM")
    recipients = env_list(recipient_env) or env_list(fallback_env)
    missing = [n for n, v in [("SMTP_HOST", host), ("SMTP_FROM", sender)] if not v]
    if not recipients:
        missing.append(f"{recipient_env}/{fallback_env}")
    if missing:
        raise SystemExit(f"{who} requires " + ", ".join(missing) + " to be set")

    tls_mode = resolve_tls_mode(env("SMTP_TLS_MODE"), env("SMTP_STARTTLS"))
    # SMTP_PORT wins when set; otherwise the default depends on the mode
    # (587 for STARTTLS, 465 for implicit TLS, 25 for plain).
    port = env_int("SMTP_PORT", default_port(tls_mode))
    return {
        "host": host,
        "port": port,
        "user": env("SMTP_USER"),
        "password": env("SMTP_PASSWORD"),
        "from": sender,
        "to": recipients,
        "tls_mode": tls_mode,
    }


def send_mail(cfg: dict[str, object], msg: EmailMessage) -> None:
    host = str(cfg["host"])
    port = int(cfg["port"])  # type: ignore[arg-type]
    user = str(cfg["user"])
    password = str(cfg["password"])
    tls_mode = str(cfg["tls_mode"])
    with open_smtp(host, port, tls_mode, timeout=30) as smtp:
        if user:
            smtp.login(user, password)
        smtp.send_message(msg)


# --- subjects --------------------------------------------------------------


def subject_host() -> str:
    # Prefer the NetBird hostname from NB_URL — the container hostname
    # (often a random ID) is useless for telling multiple NetBird
    # deployments apart in a shared mailbox.
    nb_url = env("NB_URL")
    if nb_url:
        netloc = urlparse(nb_url if "://" in nb_url else f"https://{nb_url}").netloc
        if netloc:
            return netloc.split(":", 1)[0]
    return socket.gethostname()


def base_subject(prefix: str, label: str) -> str:
    host = subject_host()
    stamp = datetime.now(UTC).strftime("%Y-%m-%d")
    tag = f" {label}" if label else ""
    return f"[{prefix}{tag}] {host} {stamp}"


# --- archive ---------------------------------------------------------------


def build_archive(
    paths: list[Path],
    password: str,
    archive: Path,
    excludes: list[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    if shutil.which("7z") is None:
        raise SystemExit("7z binary not found — install p7zip-full in the image")
    # -mhe=on encrypts filenames as well; -bso0/-bsp0 silence
    # stdout/progress so the cron log stays readable. -xr! takes a
    # wildcard pattern matched recursively against file names; -ssc-
    # makes that match case-insensitive so "geo*" also catches "Geo*".
    cmd = [
        "7z",
        "a",
        "-t7z",
        "-mhe=on",
        "-mx=5",
        f"-p{password}",
        "-bso0",
        "-bsp0",
        "-ssc-",
        *[f"-xr!{pattern}" for pattern in (excludes or [])],
        str(archive),
        *[str(p) for p in paths],
    ]
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


# --- mail bodies -----------------------------------------------------------


def attachment_mail(
    cfg: dict[str, object],
    subject: str,
    archive: Path,
    body: str,
) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = str(cfg["from"])
    msg["To"] = ", ".join(cfg["to"])  # type: ignore[arg-type]
    msg["Subject"] = subject
    msg.set_content(body)
    msg.add_attachment(
        archive.read_bytes(),
        maintype="application",
        subtype="x-7z-compressed",
        filename=archive.name,
    )
    return msg


def error_mail(cfg: dict[str, object], subject: str, what: str, reason: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = str(cfg["from"])
    msg["To"] = ", ".join(cfg["to"])  # type: ignore[arg-type]
    msg["Subject"] = f"{subject} — FAILED"
    msg.set_content(f"{what} did not produce a deliverable archive.\n\n{reason}\n")
    return msg
