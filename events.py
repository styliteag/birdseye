"""Stream NetBird audit events to stdout, nicely formatted.

NetBird only exposes a polling endpoint (GET /events/audit), so this script
fetches events on a fixed interval and prints anything newer than the last
event it saw. Output is colorized when stdout is a TTY.

Examples:
    uv run events.py
    uv run events.py --interval 10 --initial 20
    uv run events.py --once --initial 50
    uv run events.py --no-color > events.log
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from typing import Any

from netbird import APIClient

from nb_client import client_from_env
from resolver import InitiatorResolver, build_initiator_resolver, resolve_initiator

Json = dict[str, Any]


# --- setup -----------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--interval", type=float, default=60.0, help="polling interval in seconds (default 60)"
    )
    parser.add_argument(
        "--initial",
        type=int,
        default=10,
        help="how many existing events to print before streaming (default 10, 0 = none)",
    )
    parser.add_argument("--once", action="store_true", help="print and exit, do not poll")
    parser.add_argument("--no-color", action="store_true", help="disable ANSI colors")
    return parser.parse_args()


# --- formatting ------------------------------------------------------------


# ANSI helpers — applied only when colors are enabled.
_RESET = "\x1b[0m"
_STYLES = {
    "dim": "\x1b[2m",
    "bold": "\x1b[1m",
    "red": "\x1b[31m",
    "green": "\x1b[32m",
    "yellow": "\x1b[33m",
    "blue": "\x1b[34m",
    "magenta": "\x1b[35m",
    "cyan": "\x1b[36m",
    "white": "\x1b[37m",
}

# Color per activity_code prefix.
_CATEGORY_COLOR = {
    "policy": "cyan",
    "peer": "green",
    "user": "yellow",
    "account": "yellow",
    "group": "magenta",
    "posture": "blue",
    "setupkey": "blue",
    "setup_key": "blue",
    "dns": "blue",
    "personalaccesstoken": "blue",
    "personal_access_token": "blue",
    "service_user": "yellow",
}


def _color(use_color: bool, style: str, text: str) -> str:
    if not use_color or style not in _STYLES:
        return text
    return f"{_STYLES[style]}{text}{_RESET}"


def _category(activity_code: str) -> str:
    return activity_code.split(".", 1)[0] if activity_code else ""


def _format_timestamp(ts: str) -> str:
    """Render RFC3339 timestamps as local time, second precision."""
    try:
        # `datetime.fromisoformat` handles `+00:00` but not the bare `Z` until 3.11+;
        # we substitute to stay safe on older interpreters.
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return ts
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _format_meta(meta: Json | None) -> str:
    if not meta:
        return ""
    return " ".join(f"{k}={v!r}" for k, v in meta.items())


def _format_event(event: Json, resolver: InitiatorResolver, use_color: bool) -> str:
    ts = _color(use_color, "dim", _format_timestamp(event.get("timestamp", "")))

    code = event.get("activity_code") or ""
    code_color = _CATEGORY_COLOR.get(_category(code), "white")
    code_str = _color(use_color, code_color, f"{code:<28}")

    initiator = resolve_initiator(event, resolver)
    initiator_str = _color(use_color, "bold", f"{initiator[:20]:<20}")

    activity = event.get("activity") or ""
    target = event.get("target_id") or ""
    target_str = _color(use_color, "dim", f"target={target}") if target else ""

    meta = _format_meta(event.get("meta"))
    meta_str = _color(use_color, "dim", meta) if meta else ""

    parts = [ts, code_str, initiator_str, activity]
    if target_str:
        parts.append(target_str)
    if meta_str:
        parts.append(meta_str)
    return "  ".join(parts)


# --- streaming -------------------------------------------------------------


def _event_sort_key(event: Json) -> int:
    """Audit-event ids are stringified integers; treat them as ints for ordering."""
    raw = event.get("id", "0")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def _print_events(events: list[Json], resolver: InitiatorResolver, use_color: bool) -> None:
    for event in events:
        print(_format_event(event, resolver, use_color), flush=True)


def _stream(
    client: APIClient,
    *,
    interval: float,
    initial: int,
    once: bool,
    use_color: bool,
) -> int:
    resolver = build_initiator_resolver(client)
    events = sorted(client.events.get_audit_events(), key=_event_sort_key)

    historic = events[-initial:] if initial > 0 else []
    _print_events(historic, resolver, use_color)
    last_id = _event_sort_key(events[-1]) if events else 0

    if once:
        return 0

    print(_color(use_color, "dim", f"-- streaming new events every {interval}s --"), flush=True)
    while True:
        try:
            time.sleep(interval)
            fresh = [e for e in client.events.get_audit_events() if _event_sort_key(e) > last_id]
            if fresh:
                fresh.sort(key=_event_sort_key)
                _print_events(fresh, resolver, use_color)
                last_id = _event_sort_key(fresh[-1])
        except KeyboardInterrupt:
            print(file=sys.stderr)
            return 0


def main() -> int:
    args = _parse_args()
    use_color = sys.stdout.isatty() and not args.no_color
    return _stream(
        client_from_env(key="user"),
        interval=args.interval,
        initial=args.initial,
        once=args.once,
        use_color=use_color,
    )


if __name__ == "__main__":
    sys.exit(main())
