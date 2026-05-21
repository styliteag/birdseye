"""SMTP transport-mode resolution shared by the forwarder and the backup jobs.

Historically the project only supported STARTTLS on port 587 (`SMTP_STARTTLS=true`)
or plain SMTP (`SMTP_STARTTLS=false`). Some operators run relays that speak
implicit TLS on port 465 only; this helper exposes three modes:

  starttls — open SMTP, then upgrade with STARTTLS (submission, port 587)
  tls      — implicit TLS from the start (SMTPS, port 465)
  none     — plain SMTP (port 25, mostly internal MTAs)

`SMTP_STARTTLS=true|false` is still honoured as a fallback so existing
deployments do not have to change their `.env`. New deployments should set
`SMTP_TLS_MODE` directly.
"""

from __future__ import annotations

import smtplib
import ssl
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

VALID_MODES = ("starttls", "tls", "none")


@dataclass(frozen=True)
class SmtpConfig:
    """Resolved SMTP configuration shared by the forwarder and backup jobs.

    Immutable so it can be passed around and stashed in long-lived sinks
    without aliasing surprises. `sender` (not `from`) avoids the Python
    keyword; `to` is a list because most relays accept multiple
    recipients in one `RCPT TO`.
    """

    host: str
    port: int
    user: str
    password: str
    sender: str
    to: list[str]
    tls_mode: str


DEFAULT_PORT_BY_MODE: dict[str, int] = {
    "starttls": 587,
    "tls": 465,
    "none": 25,
}

_LEGACY_TRUE = {"1", "true", "yes", "on"}


def resolve_tls_mode(tls_mode_env: str, starttls_env: str) -> str:
    """Decide the transport mode.

    SMTP_TLS_MODE wins when set. Otherwise SMTP_STARTTLS=true/yes/on maps to
    "starttls" and anything else (incl. empty) maps to "none". An empty
    `starttls_env` is treated as the legacy default "true" so deployments
    that never set SMTP_STARTTLS continue to use STARTTLS.
    """
    mode = (tls_mode_env or "").strip().lower()
    if mode:
        if mode not in VALID_MODES:
            raise SystemExit(
                f"SMTP_TLS_MODE must be one of {'|'.join(VALID_MODES)}, got {tls_mode_env!r}"
            )
        return mode

    raw = (starttls_env or "").strip().lower()
    if not raw:
        # Preserve historical default: STARTTLS on when nothing is configured.
        return "starttls"
    return "starttls" if raw in _LEGACY_TRUE else "none"


def default_port(mode: str) -> int:
    return DEFAULT_PORT_BY_MODE[mode]


@contextmanager
def open_smtp(host: str, port: int, mode: str, timeout: float = 15.0) -> Iterator[smtplib.SMTP]:
    """Connected SMTP session honouring the resolved transport mode.

    Callers still handle authentication and message sending themselves;
    this only owns the transport (plain / STARTTLS / implicit TLS).
    """
    if mode not in VALID_MODES:
        raise ValueError(f"unknown SMTP transport mode {mode!r}")

    if mode == "tls":
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=timeout) as smtp:
            yield smtp
        return

    with smtplib.SMTP(host, port, timeout=timeout) as smtp:
        if mode == "starttls":
            smtp.starttls(context=ssl.create_default_context())
        yield smtp
