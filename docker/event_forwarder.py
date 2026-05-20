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
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from dotenv import load_dotenv
from netbird import APIClient
from netbird.exceptions import (
    NetBirdAPIError,
    NetBirdAuthenticationError,
    NetBirdNotFoundError,
    NetBirdRateLimitError,
    NetBirdServerError,
)

# The Dockerfile flattens this script next to resolver.py at /app/, but during
# local `uv run docker/event_forwarder.py` resolver.py sits one level up.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from resolver import InitiatorResolver, build_initiator_resolver, resolve_initiator  # noqa: E402

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


def _host_from_url(url: str) -> str:
    parsed = urlparse(url if "://" in url else f"https://{url}")
    if not parsed.netloc:
        raise ValueError(f"Cannot parse host from NB_URL={url!r}")
    return parsed.netloc


def _client_from_env() -> APIClient:
    load_dotenv()
    url = _env("NB_URL")
    token = _env("NB_API_KEY")
    if not url or not token:
        raise SystemExit("NB_URL and NB_API_KEY must be set")
    return APIClient(host=_host_from_url(url), api_token=token)


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


def _format_event_markdown(event: Json, resolver: InitiatorResolver) -> str:
    ts = _format_timestamp(event.get("timestamp", ""))
    code = event.get("activity_code") or ""
    initiator = resolve_initiator(event, resolver)
    activity = event.get("activity") or ""
    target = event.get("target_id") or ""
    meta = _format_meta(event.get("meta"))
    line = f"`{ts}`  **{code}**  _{initiator}_  {activity}"
    if target:
        line += f"  → `{target}`"
    if meta:
        line += f"  {meta}"
    return line


# --- filtering -------------------------------------------------------------


def _matches(activity_code: str, patterns: list[str]) -> bool:
    if not patterns:
        return False
    return any(fnmatch.fnmatchcase(activity_code, p) for p in patterns)


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
        self.outage_started: float | None = None
        self.outage_alerted = False

    def load(self) -> bool:
        if not self.path.exists():
            return False
        try:
            data = json.loads(self.path.read_text())
            self.last_id = int(data.get("last_id", 0))
            return True
        except (ValueError, OSError) as e:
            _log_err(f"state load failed ({e!r}); starting fresh")
            return False

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"last_id": self.last_id}))
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
        if self.batch_notice:
            lines.insert(0, self.batch_notice)
            self.batch_notice = None
        self.post("\n".join(lines))

    def send_alert(self, text: str) -> None:
        if not self.enabled:
            return
        self.post(f":rotating_light: **NetBird forwarder**: {text}")


class EmailSink:
    def __init__(self, cfg: dict[str, Any], resolver: InitiatorResolver):
        self.cfg = cfg
        self.mode = cfg["mode"]
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
        if time.monotonic() - self.last_flush < self.cfg["digest_seconds"]:
            return
        events = self.digest_buffer
        if self._send(events):
            self.digest_buffer = []
            self.last_flush = time.monotonic()

    def _send(self, events: list[Json]) -> bool:
        cfg = self.cfg
        msg = EmailMessage()
        msg["From"] = cfg["from"]
        msg["To"] = ", ".join(cfg["to"])
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
            if cfg["starttls"]:
                with smtplib.SMTP(cfg["host"], cfg["port"], timeout=15) as smtp:
                    smtp.starttls(context=ssl.create_default_context())
                    if cfg["user"]:
                        smtp.login(cfg["user"], cfg["password"])
                    smtp.send_message(msg)
            else:
                with smtplib.SMTP(cfg["host"], cfg["port"], timeout=15) as smtp:
                    if cfg["user"]:
                        smtp.login(cfg["user"], cfg["password"])
                    smtp.send_message(msg)
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
    max_catchup: int,
    outage_alert_seconds: float,
    mattermost: MattermostSink,
    email: EmailSink,
) -> int:
    seeded = state.load()
    current_backoff = poll_interval

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
            new_events = [e for e in events if _event_sort_key(e) > state.last_id]
        except KeyboardInterrupt:
            return 0
        except Exception as e:
            kind = _classify_error(e)
            if kind == "fatal":
                _log_err(f"fatal API error ({e.__class__.__name__}): {e} — exiting")
                return 3
            now = time.monotonic()
            if state.outage_started is None:
                state.outage_started = now
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
            try:
                time.sleep(current_backoff)
            except KeyboardInterrupt:
                return 0
            current_backoff = min(current_backoff * 2, backoff_cap)
            continue

        if state.outage_started is not None:
            if state.outage_alerted and mattermost.enabled:
                duration = time.monotonic() - state.outage_started
                mattermost.send_alert(f"NetBird API recovered after {duration / 60:.0f} min outage")
            state.outage_started = None
            state.outage_alerted = False
            current_backoff = poll_interval

        if new_events:
            stdout_events = [
                e for e in new_events if _matches(e.get("activity_code") or "", stdout_include)
            ]
            for event in stdout_events:
                print(_format_event_text(event, resolver), flush=True)

            mm_events = [
                e for e in new_events if _matches(e.get("activity_code") or "", mattermost_include)
            ]
            if len(mm_events) > max_catchup:
                skipped = len(mm_events) - max_catchup
                mm_events = mm_events[-max_catchup:]
                mattermost.batch_notice = f":warning: skipped {skipped} older events on catch-up"
            if mattermost.enabled and mm_events:
                mattermost.send_events(mm_events)

            mail_events = [
                e for e in new_events if _matches(e.get("activity_code") or "", email_include)
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
    cfg = {
        "mode": mode,
        "host": _env("SMTP_HOST"),
        "port": _env_int("SMTP_PORT", 587),
        "user": _env("SMTP_USER"),
        "password": _env("SMTP_PASSWORD"),
        "from": _env("SMTP_FROM"),
        "to": _env_list("SMTP_TO", ""),
        "starttls": _env("SMTP_STARTTLS", "true").lower() in {"1", "true", "yes"},
        "digest_seconds": _env_int("EMAIL_DIGEST_MINUTES", 15) * 60,
    }
    if mode != "off":
        missing = [k for k in ("host", "from") if not cfg[k]]
        if not cfg["to"]:
            missing.append("to")
        if missing:
            raise SystemExit(
                f"EMAIL_MODE={mode} requires SMTP_{'/'.join(m.upper() for m in missing)}"
            )
    return EmailSink(cfg, resolver)


def main() -> int:
    load_dotenv()

    state_path = Path(_env("STATE_FILE", "/var/lib/birdseye/state.json"))
    poll_interval = _env_float("POLL_INTERVAL", 60.0)
    backoff_cap = _env_float("BACKOFF_CAP_SECONDS", 300.0)
    outage_alert_seconds = _env_float("OUTAGE_ALERT_MINUTES", 10.0) * 60
    max_catchup = _env_int("MAX_CATCHUP", 200)

    stdout_include = _env_list("STDOUT_INCLUDE", "*")
    mattermost_include = _env_list("MATTERMOST_INCLUDE", "*")
    email_include = _env_list(
        "EMAIL_INCLUDE",
        "policy.*,user.*,setupkey.*,personalaccesstoken.*,account.*",
    )

    client = _client_from_env()
    try:
        resolver = build_initiator_resolver(client)
    except Exception as e:
        _log_err(f"initiator resolver build failed: {e.__class__.__name__}: {e}")
        return 2

    mattermost = MattermostSink(
        webhook_url=_env("MATTERMOST_WEBHOOK_URL"),
        username=_env("MATTERMOST_USERNAME", "NetBird"),
        batch_notice=None,
        resolver=resolver,
    )
    email = _build_email_sink(resolver)
    state = State(state_path)

    return _run(
        client=client,
        state=state,
        resolver=resolver,
        poll_interval=poll_interval,
        backoff_cap=backoff_cap,
        stdout_include=stdout_include,
        mattermost_include=mattermost_include,
        email_include=email_include,
        max_catchup=max_catchup,
        outage_alert_seconds=outage_alert_seconds,
        mattermost=mattermost,
        email=email,
    )


if __name__ == "__main__":
    sys.exit(main())
