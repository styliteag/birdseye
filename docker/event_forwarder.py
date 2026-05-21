"""Forward NetBird audit events to stdout, Mattermost, and email.

Long-running process; polls NetBird's /events/audit endpoint, persists last-seen
id to a state file, and fans matching events out to three sinks. See README in
this directory for configuration.
"""

from __future__ import annotations

import fnmatch
import json
import os
import smtplib
import socket
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from netbird import APIClient
from netbird.exceptions import (
    NetBirdAPIError,
    NetBirdAuthenticationError,
    NetBirdNotFoundError,
    NetBirdRateLimitError,
    NetBirdServerError,
)

# The Dockerfile flattens this script next to nb_client.py and resolver.py at
# /app/, but during local `uv run docker/event_forwarder.py` those modules sit
# one level up.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nb_client import client_from_env  # noqa: E402
from resolver import InitiatorResolver, build_initiator_resolver, resolve_initiator  # noqa: E402
from smtp_helpers import SmtpConfig, default_port, open_smtp, resolve_tls_mode  # noqa: E402

Json = dict[str, Any]


# --- config ----------------------------------------------------------------


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    return int(raw) if raw else default


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    return float(raw) if raw else default


def _env_list(name: str, default: str) -> list[str]:
    raw = _env(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


def _is_truthy(raw: str) -> bool:
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# --- formatting ------------------------------------------------------------


def _format_timestamp(ts: str) -> str:
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return ts
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _format_meta(meta: Json | None) -> str:
    if not meta:
        return ""
    return " ".join(f"{k}={v!r}" for k, v in meta.items())


def _format_event_text(event: Json, resolver: InitiatorResolver) -> str:
    ts = _format_timestamp(event.get("timestamp", ""))
    code = f"{event.get('activity_code') or '':<28}"
    initiator = f"{resolve_initiator(event, resolver)[:20]:<20}"
    activity = event.get("activity") or ""
    target = event.get("target_id") or ""
    meta = _format_meta(event.get("meta"))
    parts = [ts, code, initiator, activity]
    if target:
        parts.append(f"target={target}")
    if meta:
        parts.append(meta)
    return "  ".join(parts)


# --- Mattermost-only rendering ---------------------------------------------
#
# Each event becomes a single one-liner shaped like:
#
#   `2026-05-21 10:58:43`  **Peer login expired**: chuckcybermac.local · `10.48.231.168` · Nuremberg, DE  _Andre Keller_
#
# The verb phrase, the subject, and the trailing initiator are all derived
# per-event-shape so the dump of raw meta keys (created_at, fqdn,
# location_geo_name_id, …) disappears for the codes we know about.


# activity_code → (verb phrase shown in bold, subject shape)
_ACTIVITY_DESCRIPTORS: dict[str, tuple[str, str]] = {
    # peer lifecycle
    "peer.login.expire": ("Peer login expired", "peer"),
    "peer.login.expired": ("Peer login expired", "peer"),
    "user.peer.login": ("Peer logged in", "peer"),
    "peer.delete": ("Peer deleted", "peer"),
    "peer.rename": ("Peer renamed", "peer"),
    "peer.ssh.enable": ("Peer SSH enabled", "peer"),
    "peer.ssh.disable": ("Peer SSH disabled", "peer"),
    "peer.approve": ("Peer approved", "peer"),
    "setupkey.peer.add": ("Peer joined via setup key", "peer"),
    # user
    "user.create": ("User created", "user"),
    "user.invite": ("User invited", "user"),
    "user.delete": ("User deleted", "user"),
    "user.update": ("User updated", "user"),
    "user.role.update": ("User role updated", "user"),
    "user.block": ("User blocked", "user"),
    "user.unblock": ("User unblocked", "user"),
    # group
    "group.add": ("Group created", "group"),
    "group.update": ("Group updated", "group"),
    "group.delete": ("Group deleted", "group"),
    # policy
    "policy.add": ("Policy created", "named"),
    "policy.update": ("Policy updated", "named"),
    "policy.delete": ("Policy deleted", "named"),
    # setup key
    "setupkey.add": ("Setup key created", "named"),
    "setupkey.update": ("Setup key updated", "named"),
    "setupkey.delete": ("Setup key deleted", "named"),
    "setupkey.revoke": ("Setup key revoked", "named"),
    # personal access token
    "personalaccesstoken.create": ("Personal token created", "named"),
    "personalaccesstoken.delete": ("Personal token deleted", "named"),
    # routes & DNS
    "route.add": ("Route created", "named"),
    "route.update": ("Route updated", "named"),
    "route.delete": ("Route deleted", "named"),
    "nameserver.add": ("Nameserver group created", "named"),
    "nameserver.update": ("Nameserver group updated", "named"),
    "nameserver.delete": ("Nameserver group deleted", "named"),
    # posture checks
    "posturecheck.add": ("Posture check created", "named"),
    "posturecheck.update": ("Posture check updated", "named"),
    "posturecheck.delete": ("Posture check deleted", "named"),
    # account
    "account.create": ("Account created", "generic"),
}


# Fallback category labels keyed by the first dotted segment of an unmapped code.
_CATEGORY_FALLBACK: dict[str, str] = {
    "peer": "Peer",
    "user": "User",
    "group": "Group",
    "policy": "Policy",
    "setupkey": "Setup key",
    "personalaccesstoken": "Personal token",
    "route": "Route",
    "nameserver": "Nameserver",
    "dns": "DNS",
    "posturecheck": "Posture check",
    "account": "Account",
    "service": "Service",
    "integration": "Integration",
}


# Meta keys to drop from the Mattermost rendering. Stdout/email keep the full
# meta for log fidelity.
_MM_DROPPED_META_KEYS = frozenset(
    {
        "location_geo_name_id",
        "location_connection_id",
        "location_connection_ip",
        "fqdn",
        "created_at",
        "issued",
    }
)


# Meta keys already used to build the subject for a given shape — skip them
# in the trailing meta dump so they don't appear twice.
_CONSUMED_BY_SHAPE: dict[str, frozenset[str]] = {
    "peer": frozenset({"name", "fqdn", "ip", "location_city_name", "location_country_code"}),
    "group": frozenset({"name", "new_name", "old_name"}),
    "user": frozenset({"username", "name", "email"}),
    "named": frozenset({"name", "new_name"}),
    "generic": frozenset(),
}


def _humanise_code(code: str) -> str:
    """`account.setting.peer.login.expiration.update` → `Account setting peer login expiration update`."""
    return code.replace(".", " ").replace("_", " ").capitalize()


def _describe_activity(code: str) -> tuple[str, str]:
    if code in _ACTIVITY_DESCRIPTORS:
        return _ACTIVITY_DESCRIPTORS[code]
    head = code.split(".", 1)[0]
    # account.* events (settings, billing, …) rarely have a friendly subject
    # in meta — fall through to "generic" so the opaque target id is hidden.
    if head == "account":
        return _humanise_code(code), "generic"
    if head in _CATEGORY_FALLBACK:
        return _humanise_code(code), "named"
    return _humanise_code(code) if code else "Event", "generic"


def _format_location(meta: Json) -> str:
    city = meta.get("location_city_name") or ""
    country = meta.get("location_country_code") or ""
    return ", ".join(p for p in (city, country) if p)


def _peer_subject(meta: Json, target_id: str) -> str:
    name = meta.get("name") or meta.get("fqdn") or target_id
    parts: list[str] = [str(name)]
    ip = meta.get("ip")
    if ip:
        # IPv4 is colon-free, IPv6 is not — backtick-wrap in both cases so the
        # output stays uniform and IPv6 never hits Mattermost's emoji parser.
        parts.append(f"`{ip}`")
    loc = _format_location(meta)
    if loc:
        parts.append(loc)
    return " · ".join(parts)


def _group_subject(meta: Json, target_id: str) -> str:
    new, old = meta.get("new_name"), meta.get("old_name")
    if new and old and new != old:
        return f'"{old}" → "{new}"'
    return str(meta.get("name") or new or old or target_id)


def _user_subject(meta: Json, target_id: str) -> str:
    name = meta.get("username") or meta.get("name") or ""
    email = meta.get("email") or ""
    if name and email and name != email:
        return f"{name} ({email})"
    return str(name or email or target_id)


def _named_subject(meta: Json, target_id: str) -> str:
    return str(meta.get("name") or meta.get("new_name") or target_id)


def _generic_subject(meta: Json, target_id: str) -> str:
    # The opaque target ID is rarely useful for catch-all events (e.g.
    # account.setting.*); the verb phrase already carries the meaning.
    return ""


_SUBJECT_FORMATTERS = {
    "peer": _peer_subject,
    "group": _group_subject,
    "user": _user_subject,
    "named": _named_subject,
    "generic": _generic_subject,
}


def _format_remaining_meta(meta: Json, consumed: frozenset[str]) -> str:
    parts = []
    for k, v in meta.items():
        if k in consumed or k in _MM_DROPPED_META_KEYS or v in (None, ""):
            continue
        s = str(v)
        # Mattermost parses ':a:', ':100:' and similar as emoji shortcodes,
        # which mangles IPv6 addresses and ISO timestamps. Wrap any colon-
        # bearing value in inline code so the emoji parser leaves it alone.
        rendered = f"`{s}`" if ":" in s else f"'{s}'"
        parts.append(f"{k}={rendered}")
    return " ".join(parts)


def _format_event_markdown(event: Json, resolver: InitiatorResolver) -> str:
    ts = _format_timestamp(event.get("timestamp", ""))
    code = event.get("activity_code") or ""
    initiator = resolve_initiator(event, resolver)
    target_id = event.get("target_id") or ""
    meta = event.get("meta") or {}

    label, shape = _describe_activity(code)
    subject = _SUBJECT_FORMATTERS[shape](meta, target_id)

    line = f"`{ts}`  **{label}**"
    if subject:
        line += f": {subject}"
    if initiator and initiator != "system":
        line += f"  _{initiator}_"
    extra = _format_remaining_meta(meta, _CONSUMED_BY_SHAPE[shape])
    if extra:
        line += f"  · {extra}"
    return line


# --- filtering -------------------------------------------------------------


def _matches(activity_code: str, patterns: list[str]) -> bool:
    if not patterns:
        return False
    return any(fnmatch.fnmatchcase(activity_code, p) for p in patterns)


def _allow(activity_code: str, include: list[str], exclude: list[str]) -> bool:
    """Two-stage filter: an event passes when include matches AND
    exclude does not. An empty exclude list short-circuits to "no
    deny" via `_matches`, preserving the historic include-only
    behaviour."""
    if not _matches(activity_code, include):
        return False
    return not _matches(activity_code, exclude)


def _event_sort_key(event: Json) -> int:
    raw = event.get("id", "0")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


# --- state -----------------------------------------------------------------


class State:
    def __init__(self, path: Path):
        self.path = path
        self.last_id = 0
        # Wall-clock seconds since epoch — not monotonic, because the
        # outage may straddle a process restart and monotonic resets.
        self.outage_started: float | None = None
        self.outage_alerted = False

    def load(self) -> bool:
        if not self.path.exists():
            return False
        try:
            data = json.loads(self.path.read_text())
            self.last_id = int(data.get("last_id", 0))
            started = data.get("outage_started")
            self.outage_started = float(started) if started is not None else None
            self.outage_alerted = bool(data.get("outage_alerted", False))
            return True
        except (ValueError, OSError) as e:
            _log_err(f"state load failed ({e!r}); starting fresh")
            return False

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {
                    "last_id": self.last_id,
                    "outage_started": self.outage_started,
                    "outage_alerted": self.outage_alerted,
                }
            )
        )
        tmp.replace(self.path)


# --- logging ---------------------------------------------------------------


def _log_err(msg: str) -> None:
    print(f"[forwarder] {msg}", file=sys.stderr, flush=True)


def _log_info(msg: str) -> None:
    print(f"[forwarder] {msg}", file=sys.stderr, flush=True)


# --- sinks -----------------------------------------------------------------


class MattermostSink:
    def __init__(
        self,
        webhook_url: str,
        username: str,
        batch_notice: str | None,
        resolver: InitiatorResolver,
    ):
        self.webhook_url = webhook_url
        self.username = username
        self.batch_notice = batch_notice
        self.resolver = resolver

    @property
    def enabled(self) -> bool:
        return bool(self.webhook_url)

    def post(self, text: str) -> bool:
        payload = json.dumps({"username": self.username, "text": text}).encode()
        req = urllib.request.Request(
            self.webhook_url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                if 200 <= resp.status < 300:
                    return True
                _log_err(f"mattermost POST failed: HTTP {resp.status}")
                return False
        except urllib.error.HTTPError as e:
            _log_err(f"mattermost POST failed: HTTP {e.code}")
            return False
        except (urllib.error.URLError, OSError) as e:
            _log_err(f"mattermost POST failed: {e.__class__.__name__}")
            return False

    def send_events(self, events: list[Json]) -> None:
        if not events:
            return
        lines = [_format_event_markdown(e, self.resolver) for e in events]
        notice = self.batch_notice
        if notice:
            lines.insert(0, notice)
        if self.post("\n".join(lines)) and notice:
            # Drop the notice only on a confirmed delivery — otherwise the
            # next attempt would silently lose the skip warning.
            self.batch_notice = None

    def send_alert(self, text: str) -> None:
        if not self.enabled:
            return
        self.post(f":rotating_light: **NetBird forwarder**: {text}")

    def send_startup_test(self) -> bool:
        """One-shot smoke probe gated by MATTERMOST_STARTUP_TEST.

        Posts a single canned message so the operator can confirm the
        webhook works without waiting for the next audit event. Returns
        True on a successful POST, False otherwise (also logged).
        """
        if not self.enabled:
            _log_info("mattermost startup test skipped — webhook empty")
            return False
        stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
        text = (
            ":white_check_mark: **birdseye startup** — "
            f"posted by `{socket.gethostname()}` at `{stamp}`. "
            "Webhook works."
        )
        ok = self.post(text)
        _log_info("mattermost startup test posted" if ok else "mattermost startup test failed")
        return ok


@dataclass(frozen=True)
class EmailSinkConfig:
    """Config for the audit-event email sink.

    `smtp` is None exactly when `mode == "off"` — that's how the sink
    represents disabled, instead of carrying empty strings around.
    """

    mode: str  # "off" | "immediate" | "digest"
    smtp: SmtpConfig | None
    digest_seconds: int


class EmailSink:
    def __init__(self, cfg: EmailSinkConfig, resolver: InitiatorResolver):
        self.cfg = cfg
        self.mode = cfg.mode
        self.resolver = resolver
        self.digest_buffer: list[Json] = []
        self.last_flush = time.monotonic()

    @property
    def enabled(self) -> bool:
        return self.mode in {"immediate", "digest"}

    def add(self, event: Json) -> None:
        if not self.enabled:
            return
        if self.mode == "immediate":
            self._send([event])
        else:
            self.digest_buffer.append(event)

    def tick(self) -> None:
        if self.mode != "digest" or not self.digest_buffer:
            return
        if time.monotonic() - self.last_flush < self.cfg.digest_seconds:
            return
        events = self.digest_buffer
        if self._send(events):
            self.digest_buffer = []
            self.last_flush = time.monotonic()

    def send_startup_test(self) -> bool:
        """One-shot smoke probe gated by EMAIL_STARTUP_TEST.

        Sends a self-describing message via the resolved SMTP transport
        so the operator can confirm credentials, TLS mode, and routing
        at container start instead of waiting for the first event.
        """
        smtp = self.cfg.smtp
        if smtp is None:
            _log_info("email startup test skipped — EMAIL_MODE=off")
            return False
        nb_url = os.environ.get("NB_URL", "").strip() or "<unset>"
        body = (
            "birdseye email sink startup.\n\n"
            f"  container host: {socket.gethostname()}\n"
            f"  netbird url:    {nb_url}\n"
            f"  time:           {datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S UTC')}\n"
            f"  email mode:     {self.mode}\n"
            f"  transport:      {smtp.tls_mode} {smtp.host}:{smtp.port}\n"
            f"  recipients:     {', '.join(smtp.to)}\n\n"
            "Delivery of this mail confirms host, port, TLS mode, and "
            "credentials are correct.\n"
            "Set EMAIL_STARTUP_TEST=false (or unset) to suppress on the "
            "next container start.\n"
        )
        msg = EmailMessage()
        msg["From"] = smtp.sender
        msg["To"] = ", ".join(smtp.to)
        msg["Subject"] = f"[NetBird] birdseye startup ({socket.gethostname()})"
        msg.set_content(body)
        try:
            with open_smtp(smtp.host, smtp.port, smtp.tls_mode, timeout=15) as conn:
                if smtp.user:
                    conn.login(smtp.user, smtp.password)
                conn.send_message(msg)
            _log_info(f"email startup test sent to {', '.join(smtp.to)}")
            return True
        except (smtplib.SMTPException, OSError) as e:
            _log_err(f"email startup test failed: {e.__class__.__name__}: {e}")
            return False

    def _send(self, events: list[Json]) -> bool:
        smtp = self.cfg.smtp
        if smtp is None:
            # Unreachable when the sink is enabled — _build_email_sink
            # only constructs smtp=None for mode="off".
            return False
        msg = EmailMessage()
        msg["From"] = smtp.sender
        msg["To"] = ", ".join(smtp.to)
        if self.mode == "immediate":
            e = events[0]
            subject = f"[NetBird] {e.get('activity_code')} — {resolve_initiator(e, self.resolver)}"
        else:
            stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
            subject = f"[NetBird] {len(events)} events ({stamp} digest)"
        msg["Subject"] = subject
        body = "\n".join(_format_event_text(e, self.resolver) for e in events) + "\n"
        msg.set_content(body)

        try:
            with open_smtp(smtp.host, smtp.port, smtp.tls_mode, timeout=15) as conn:
                if smtp.user:
                    conn.login(smtp.user, smtp.password)
                conn.send_message(msg)
            return True
        except (smtplib.SMTPException, OSError) as e:
            _log_err(f"smtp send failed: {e.__class__.__name__}")
            return False


# --- poll loop -------------------------------------------------------------


def _fetch_events(client: APIClient) -> list[Json]:
    events = client.events.get_audit_events()
    return sorted(events, key=_event_sort_key)


def _classify_error(exc: Exception) -> str:
    """Return 'fatal' (exit), 'transient' (backoff), or 'unknown' (treat as transient)."""
    if isinstance(exc, (NetBirdAuthenticationError, NetBirdNotFoundError)):
        return "fatal"
    if isinstance(exc, (NetBirdServerError, NetBirdRateLimitError)):
        return "transient"
    if isinstance(exc, NetBirdAPIError):
        return "transient"
    return "transient"


def _run(
    *,
    client: APIClient,
    state: State,
    resolver: InitiatorResolver,
    poll_interval: float,
    backoff_cap: float,
    stdout_include: list[str],
    mattermost_include: list[str],
    email_include: list[str],
    stdout_exclude: list[str],
    mattermost_exclude: list[str],
    email_exclude: list[str],
    max_catchup: int,
    outage_alert_seconds: float,
    backlog_warn_threshold: int,
    mattermost: MattermostSink,
    email: EmailSink,
) -> int:
    seeded = state.load()
    current_backoff = poll_interval
    # The NetBird audit endpoint has no since/cursor parameter, so every
    # poll re-downloads the full event list (see netbird PyPI SDK,
    # resources/events.py). Watch for unbounded growth so the operator
    # can rotate the state file or shorten the server-side retention.
    backlog_warn_active = False

    if not seeded:
        try:
            initial = _fetch_events(client)
        except Exception as e:
            _log_err(f"initial fetch failed: {e.__class__.__name__}: {e}")
            return 2
        state.last_id = _event_sort_key(initial[-1]) if initial else 0
        state.save()
        _log_info(f"first boot — seeded last_id={state.last_id}, no backlog forwarded")

    _log_info(
        f"polling every {poll_interval:.0f}s; "
        f"last_id={state.last_id}; "
        f"sinks: stdout=on mattermost={'on' if mattermost.enabled else 'off'} email={email.mode}"
    )

    while True:
        try:
            events = _fetch_events(client)
            if len(events) > backlog_warn_threshold and not backlog_warn_active:
                _log_err(
                    f"audit-events backlog: {len(events)} events returned in one poll "
                    f"(threshold={backlog_warn_threshold}). The NetBird audit endpoint "
                    "has no cursor, so each poll re-downloads everything. Consider "
                    "lowering server-side audit-event retention."
                )
                backlog_warn_active = True
            elif len(events) <= backlog_warn_threshold and backlog_warn_active:
                _log_info(
                    f"audit-events backlog back under {backlog_warn_threshold} ({len(events)})"
                )
                backlog_warn_active = False
            new_events = [e for e in events if _event_sort_key(e) > state.last_id]
        except KeyboardInterrupt:
            return 0
        except Exception as e:
            kind = _classify_error(e)
            if kind == "fatal":
                _log_err(f"fatal API error ({e.__class__.__name__}): {e} — exiting")
                return 3
            now = time.time()
            new_outage = state.outage_started is None
            if new_outage:
                state.outage_started = now
                state.save()
            duration = now - state.outage_started
            _log_err(
                f"poll failed ({e.__class__.__name__}); "
                f"outage={duration:.0f}s; backoff={current_backoff:.0f}s"
            )
            if not state.outage_alerted and duration >= outage_alert_seconds and mattermost.enabled:
                mattermost.send_alert(
                    f"NetBird API unreachable for {duration / 60:.0f} min ({e.__class__.__name__})"
                )
                state.outage_alerted = True
                state.save()
            try:
                time.sleep(current_backoff)
            except KeyboardInterrupt:
                return 0
            current_backoff = min(current_backoff * 2, backoff_cap)
            continue

        if state.outage_started is not None:
            if state.outage_alerted and mattermost.enabled:
                duration = time.time() - state.outage_started
                mattermost.send_alert(f"NetBird API recovered after {duration / 60:.0f} min outage")
            state.outage_started = None
            state.outage_alerted = False
            state.save()
            current_backoff = poll_interval

        if new_events:
            stdout_events = [
                e
                for e in new_events
                if _allow(e.get("activity_code") or "", stdout_include, stdout_exclude)
            ]
            for event in stdout_events:
                print(_format_event_text(event, resolver), flush=True)

            mm_events = [
                e
                for e in new_events
                if _allow(e.get("activity_code") or "", mattermost_include, mattermost_exclude)
            ]
            if len(mm_events) > max_catchup:
                skipped = len(mm_events) - max_catchup
                mm_events = mm_events[-max_catchup:]
                mattermost.batch_notice = f":warning: skipped {skipped} older events on catch-up"
            if mattermost.enabled and mm_events:
                mattermost.send_events(mm_events)

            mail_events = [
                e
                for e in new_events
                if _allow(e.get("activity_code") or "", email_include, email_exclude)
            ]
            if len(mail_events) > max_catchup:
                mail_events = mail_events[-max_catchup:]
            for event in mail_events:
                email.add(event)

            state.last_id = _event_sort_key(new_events[-1])
            state.save()

        email.tick()

        try:
            time.sleep(poll_interval)
        except KeyboardInterrupt:
            return 0


# --- main ------------------------------------------------------------------


def _build_email_sink(resolver: InitiatorResolver) -> EmailSink:
    mode = _env("EMAIL_MODE", "off").lower()
    if mode not in {"off", "immediate", "digest"}:
        raise SystemExit(f"EMAIL_MODE must be off|immediate|digest, got {mode!r}")
    digest_seconds = _env_int("EMAIL_DIGEST_MINUTES", 15) * 60

    if mode == "off":
        return EmailSink(
            EmailSinkConfig(mode=mode, smtp=None, digest_seconds=digest_seconds),
            resolver,
        )

    host = _env("SMTP_HOST")
    sender = _env("SMTP_FROM")
    to = _env_list("SMTP_TO", "")
    missing: list[str] = []
    if not host:
        missing.append("HOST")
    if not sender:
        missing.append("FROM")
    if not to:
        missing.append("TO")
    if missing:
        raise SystemExit(f"EMAIL_MODE={mode} requires SMTP_{'/'.join(missing)}")

    tls_mode = resolve_tls_mode(_env("SMTP_TLS_MODE"), _env("SMTP_STARTTLS"))
    smtp = SmtpConfig(
        host=host,
        port=_env_int("SMTP_PORT", default_port(tls_mode)),
        user=_env("SMTP_USER"),
        password=_env("SMTP_PASSWORD"),
        sender=sender,
        to=to,
        tls_mode=tls_mode,
    )
    return EmailSink(
        EmailSinkConfig(mode=mode, smtp=smtp, digest_seconds=digest_seconds),
        resolver,
    )


def main() -> int:
    load_dotenv()

    state_path = Path(_env("STATE_FILE", "/var/lib/birdseye/state.json"))
    poll_interval = _env_float("POLL_INTERVAL", 60.0)
    backoff_cap = _env_float("BACKOFF_CAP_SECONDS", 300.0)
    outage_alert_seconds = _env_float("OUTAGE_ALERT_MINUTES", 10.0) * 60
    max_catchup = _env_int("MAX_CATCHUP", 200)
    backlog_warn_threshold = _env_int("BACKLOG_WARN_THRESHOLD", 1000)

    stdout_include = _env_list("STDOUT_INCLUDE", "*")
    mattermost_include = _env_list("MATTERMOST_INCLUDE", "*")
    email_include = _env_list(
        "EMAIL_INCLUDE",
        "policy.*,user.*,setupkey.*,personalaccesstoken.*,account.*",
    )
    # _EXCLUDE patterns are subtracted from their respective _INCLUDE
    # set. An empty list keeps the historical include-only behaviour.
    stdout_exclude = _env_list("STDOUT_EXCLUDE", "")
    mattermost_exclude = _env_list("MATTERMOST_EXCLUDE", "")
    email_exclude = _env_list("EMAIL_EXCLUDE", "")

    client = client_from_env(key="user")
    try:
        resolver = build_initiator_resolver(client)
    except Exception as e:
        _log_err(f"initiator resolver build failed: {e.__class__.__name__}: {e}")
        return 2

    mattermost = MattermostSink(
        webhook_url=_env("MATTERMOST_WEBHOOK_URL"),
        username=_env("MATTERMOST_USERNAME", "birdseye"),
        batch_notice=None,
        resolver=resolver,
    )
    email = _build_email_sink(resolver)
    state = State(state_path)

    # One-shot health probes — run before the poll loop so the operator
    # sees the result in the first chunk of logs. Failures are logged
    # but never abort the forwarder; the probes are diagnostic, not gating.
    if _is_truthy(_env("MATTERMOST_STARTUP_TEST")):
        mattermost.send_startup_test()
    if _is_truthy(_env("EMAIL_STARTUP_TEST")):
        email.send_startup_test()

    return _run(
        client=client,
        state=state,
        resolver=resolver,
        poll_interval=poll_interval,
        backoff_cap=backoff_cap,
        stdout_include=stdout_include,
        mattermost_include=mattermost_include,
        email_include=email_include,
        stdout_exclude=stdout_exclude,
        mattermost_exclude=mattermost_exclude,
        email_exclude=email_exclude,
        max_catchup=max_catchup,
        outage_alert_seconds=outage_alert_seconds,
        backlog_warn_threshold=backlog_warn_threshold,
        mattermost=mattermost,
        email=email,
    )


if __name__ == "__main__":
    sys.exit(main())
