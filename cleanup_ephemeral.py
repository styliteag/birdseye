"""Delete stale ephemeral NetBird peers.

NetBird is supposed to auto-purge ephemeral peers (those joined via a setup key
flagged 'ephemeral', e.g. the WASM/browser client) ~10 min after they go
offline, but the self-hosted cleanup ticker is unreliable. This script does
that sweep externally.

A peer is deleted when ALL of:
  * ephemeral == true
  * connected == false
  * last_seen is older than --min-age-minutes (default 60)

Examples:
    uv run cleanup_ephemeral.py --dry-run
    uv run cleanup_ephemeral.py
    uv run cleanup_ephemeral.py --min-age-minutes 10
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

from nb_client import client_from_env

Json = dict[str, Any]
DEFAULT_MIN_AGE_MINUTES = 60


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--dry-run", action="store_true", help="preview deletions without writing")
    parser.add_argument(
        "--min-age-minutes",
        type=int,
        default=DEFAULT_MIN_AGE_MINUTES,
        help=(
            "skip ephemeral peers whose last_seen is more recent than this "
            f"(default {DEFAULT_MIN_AGE_MINUTES} minutes)"
        ),
    )
    return parser.parse_args()


def _parse_last_seen(raw: str | None) -> datetime | None:
    if not raw:
        return None
    # NetBird returns RFC3339 with nanoseconds + 'Z'. Truncate to microseconds.
    cleaned = raw.rstrip("Z")
    if "." in cleaned:
        head, frac = cleaned.split(".", 1)
        cleaned = f"{head}.{frac[:6]}"
    try:
        return datetime.fromisoformat(cleaned).replace(tzinfo=UTC)
    except ValueError:
        return None


def _should_delete(peer: Json, cutoff: datetime) -> tuple[bool, str]:
    if not peer.get("ephemeral"):
        return False, "not ephemeral"
    if peer.get("connected"):
        return False, "still connected"
    last_seen = _parse_last_seen(peer.get("last_seen"))
    if last_seen is None:
        return False, "missing last_seen"
    if last_seen > cutoff:
        return False, f"last_seen too recent ({last_seen.isoformat()})"
    return True, f"last_seen={last_seen.isoformat()}"


def main() -> int:
    args = _parse_args()
    client = client_from_env(key="admin")
    cutoff = datetime.now(UTC) - timedelta(minutes=args.min_age_minutes)

    peers: list[Json] = client.get("peers")
    ephemerals = [p for p in peers if p.get("ephemeral")]
    print(
        f"Found {len(ephemerals)} ephemeral peer(s) of {len(peers)} total. "
        f"Cutoff: last_seen < {cutoff.isoformat()} (--min-age-minutes={args.min_age_minutes})."
    )

    deleted = 0
    skipped = 0
    for peer in ephemerals:
        ok, reason = _should_delete(peer, cutoff)
        label = f"{peer.get('name') or '?'} (id={peer.get('id')}, ip={peer.get('ip')})"
        if not ok:
            print(f"  skip   {label}: {reason}")
            skipped += 1
            continue
        if args.dry_run:
            print(f"  would delete {label}: {reason}")
        else:
            print(f"  delete {label}: {reason}")
            client.delete(f"peers/{peer['id']}")
            print(f"  deleted {label}")
        deleted += 1

    verb = "would delete" if args.dry_run else "deleted"
    print(f"Done. {verb}={deleted}, skipped={skipped}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
