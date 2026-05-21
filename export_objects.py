"""Export NetBird configuration via the API into an encrypted 7z and mail it.

Companion to `backup_volumes.py`: where that ships the raw volume bytes,
this asks the management API for the same configuration in a portable,
human-readable form. The archive contains one JSON file per endpoint
(`peers.json`, `groups.json`, `policies.json`, …) plus a `manifest.json`
listing what was collected and the timestamp.

This is what you keep in your mailbox to know *what* was configured on
a given date; the volume backup is for *byte-identical* restore.

Required env vars:
  NB_URL, NB_ADMIN_API_KEY  admin token — needed to read users / setup-keys
  BACKUP_ZIP_PASSWORD       passphrase for the 7z archive (shared with the
                            volume backup so you only memorise one)
  SMTP_HOST, SMTP_FROM
  EXPORT_EMAIL_TO or BACKUP_EMAIL_TO or SMTP_TO  (first non-empty wins)

Optional:
  BACKUP_MAX_ATTACHMENT_MB  default 20. Same base64-aware semantics as the
                            volume backup.
  BACKUP_LABEL              free-form tag, ends up in subject and filename
  SMTP_PORT, SMTP_STARTTLS, SMTP_USER, SMTP_PASSWORD

Endpoints are queried best-effort: a 404 (endpoint disabled or not
available in this NetBird version) is logged and skipped, not fatal.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from netbird import APIClient
from netbird.exceptions import NetBirdAPIError, NetBirdNotFoundError

from backup_common import (
    BASE64_OVERHEAD,
    DEFAULT_MAX_MB,
    attachment_mail,
    base_subject,
    build_archive,
    env,
    env_int,
    error_mail,
    make_log,
    send_mail,
    smtp_config,
)
from nb_client import client_from_env

_log = make_log("export_objects")

# Endpoints exported on every run. Each is a GET against /api/<path>;
# the response is dumped verbatim into <slug>.json inside the archive.
# Order is informational — not a dependency.
_ENDPOINTS: list[tuple[str, str]] = [
    ("peers", "peers"),
    ("groups", "groups"),
    ("policies", "policies"),
    ("users", "users"),
    ("setup-keys", "setup_keys"),
    ("routes", "routes"),
    ("dns/nameservers", "dns_nameservers"),
    ("dns/settings", "dns_settings"),
    ("posture-checks", "posture_checks"),
    ("networks", "networks"),
    ("accounts", "accounts"),
]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="fetch + archive locally, do not send mail",
    )
    return parser.parse_args()


def _smtp_config_for_export() -> dict[str, object]:
    # Prefer EXPORT_EMAIL_TO so the operator can route the two mails
    # independently; fall back to BACKUP_EMAIL_TO (same job) and then
    # SMTP_TO (forwarder default).
    fallback = "BACKUP_EMAIL_TO" if env("BACKUP_EMAIL_TO") else "SMTP_TO"
    return smtp_config(
        recipient_env="EXPORT_EMAIL_TO",
        fallback_env=fallback,
        who="export_objects",
    )


def _fetch_endpoint(client: APIClient, path: str) -> Any | None:
    """Return parsed JSON, None on any failure (logged).

    Cron jobs must not crash on a single endpoint — a missing or
    misbehaving endpoint is reflected in manifest.json instead.
    """
    try:
        return client.get(path)
    except NetBirdNotFoundError:
        _log(f"  GET /{path} → 404, skipped")
        return None
    except NetBirdAPIError as e:
        _log(f"  GET /{path} → {e.__class__.__name__}: {e}")
        return None
    except Exception as e:
        _log(f"  GET /{path} → {e.__class__.__name__}: {e}")
        return None


def _dump_objects(client: APIClient, out_dir: Path) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Write one JSON file per endpoint into out_dir.

    Returns (manifest_summary, slugs_actually_written).
    """
    summary: dict[str, dict[str, Any]] = {}
    written: list[str] = []
    for path, slug in _ENDPOINTS:
        data = _fetch_endpoint(client, path)
        if data is None:
            summary[slug] = {"path": path, "status": "skipped"}
            continue
        target = out_dir / f"{slug}.json"
        target.write_text(json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False))
        count = len(data) if isinstance(data, list) else 1
        size_kb = target.stat().st_size / 1024
        summary[slug] = {
            "path": path,
            "status": "ok",
            "count": count,
            "bytes": target.stat().st_size,
        }
        written.append(slug)
        _log(f"  GET /{path} → {count} item(s), {size_kb:.1f} KB")
    return summary, written


def _write_manifest(out_dir: Path, summary: dict[str, dict[str, Any]], nb_url: str) -> None:
    manifest = {
        "exported_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "nb_url": nb_url,
        "endpoints": summary,
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False)
    )


def _body(archive: Path, written: list[str]) -> str:
    size_kb = archive.stat().st_size / 1024
    return (
        f"Encrypted NetBird API export attached.\n\n"
        f"  archive:   {archive.name}\n"
        f"  size:      {size_kb:.1f} KB\n"
        f"  endpoints: {len(written)} ({', '.join(written)})\n\n"
        "Decrypt with the password from BACKUP_ZIP_PASSWORD:\n"
        f"  7z x {archive.name}\n\n"
        "manifest.json inside the archive lists every endpoint queried,\n"
        "including the ones that returned 404 (and were skipped).\n"
    )


def main() -> int:
    load_dotenv()
    args = _parse_args()

    password = env("BACKUP_ZIP_PASSWORD")
    if not password:
        raise SystemExit("BACKUP_ZIP_PASSWORD is not set")
    max_mb = env_int("BACKUP_MAX_ATTACHMENT_MB", DEFAULT_MAX_MB)
    label = env("BACKUP_LABEL")

    cfg = _smtp_config_for_export()
    client = client_from_env(key="admin")
    subject = base_subject("NetBird API export", label)

    with tempfile.TemporaryDirectory(prefix="netbird-export-") as tmp:
        tmp_path = Path(tmp)
        json_dir = tmp_path / "export"
        json_dir.mkdir()
        _log(f"fetching {len(_ENDPOINTS)} endpoints from {env('NB_URL')}")
        summary, written = _dump_objects(client, json_dir)
        if not written:
            reason = (
                "no endpoints returned data — check NB_ADMIN_API_KEY scope "
                "and that the management API is reachable from this container"
            )
            _log(reason)
            if args.dry_run:
                return 1
            send_mail(cfg, error_mail(cfg, subject, "NetBird API export", reason))
            return 1

        _write_manifest(json_dir, summary, env("NB_URL"))

        ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        archive = tmp_path / f"netbird-{label or 'export'}-{ts}.7z"
        _log(f"packing {len(written)} object set(s) into {archive.name}")
        result = build_archive([json_dir], password, archive)
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
                    "NetBird API export",
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
                f"BACKUP_MAX_ATTACHMENT_MB={max_mb}."
            )
            _log(reason)
            if args.dry_run:
                return 2
            send_mail(cfg, error_mail(cfg, subject, "NetBird API export", reason))
            return 2

        if args.dry_run:
            _log("dry-run: skipping mail send")
            return 0

        send_mail(cfg, attachment_mail(cfg, subject, archive, _body(archive, written)))
        _log(f"sent {archive.name} ({size_mb:.2f} MB) to {', '.join(cfg['to'])}")  # type: ignore[arg-type]
    return 0


if __name__ == "__main__":
    sys.exit(main())
