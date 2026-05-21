"""Inspect and modify NetBird setup-key auto_groups.

The dashboard hides auto_groups after a setup key is created, but the API
(`PUT /api/setup-keys/{id}`) supports updating both `auto_groups` and
`revoked`. This script wraps that for everyday use.

Modes (mutations require --key NAME or --all-keys):
    list                       (default: dump every key + its groups)
    --key NAME                 show a single key
    --add-group G [-g ...]     append group(s) to the key's auto_groups
    --remove-group G [-g ...]  remove group(s) from the key's auto_groups
    --remove-all-groups        clear auto_groups (set to [])
    --set-groups G1,G2         replace auto_groups verbatim
    --all-keys                 apply mutation to every setup key

Examples:
    uv run setup_keys.py
    uv run setup_keys.py --key "2026-06"
    uv run setup_keys.py --key "2026-06" --remove-all-groups --dry-run
    uv run setup_keys.py --key "2026-06" --add-group "New"
    uv run setup_keys.py --all-keys --add-group "New" --dry-run
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from netbird import APIClient

from nb_client import client_from_env

Json = dict[str, Any]


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    target = p.add_mutually_exclusive_group()
    target.add_argument("--key", help="setup key name to target")
    target.add_argument("--all-keys", action="store_true", help="target every setup key")

    p.add_argument(
        "--add-group",
        action="append",
        default=[],
        metavar="NAME",
        help="group name to add (repeatable)",
    )
    p.add_argument(
        "--remove-group",
        action="append",
        default=[],
        metavar="NAME",
        help="group name to remove (repeatable)",
    )
    p.add_argument("--remove-all-groups", action="store_true", help="clear auto_groups")
    p.add_argument(
        "--set-groups",
        metavar="G1,G2",
        help="replace auto_groups with the given comma-separated names",
    )
    p.add_argument("--dry-run", action="store_true", help="preview changes without writing")
    return p.parse_args()


def _index_groups(client: APIClient) -> tuple[dict[str, str], dict[str, str]]:
    """Return (name->id, id->name). Errors on duplicate group names."""
    name_to_id: dict[str, str] = {}
    id_to_name: dict[str, str] = {}
    for g in client.get("groups"):
        gid, name = g["id"], g["name"]
        if name in name_to_id:
            raise SystemExit(
                f"duplicate group name {name!r} (ids {name_to_id[name]} and {gid}); "
                "refusing to guess — use group IDs directly in the script if needed"
            )
        name_to_id[name] = gid
        id_to_name[gid] = name
    return name_to_id, id_to_name


def _select_keys(all_keys: list[Json], target_name: str | None, all_flag: bool) -> list[Json]:
    if all_flag:
        return all_keys
    if not target_name:
        return []
    matches = [k for k in all_keys if k["name"] == target_name]
    if not matches:
        raise SystemExit(f"no setup key found with name {target_name!r}")
    if len(matches) > 1:
        ids = ", ".join(k["id"] for k in matches)
        raise SystemExit(f"multiple setup keys named {target_name!r}: {ids}")
    return matches


def _new_auto_groups(key: Json, args: argparse.Namespace, name_to_id: dict[str, str]) -> list[str]:
    current: list[str] = list(key.get("auto_groups") or [])

    if args.remove_all_groups:
        return []
    if args.set_groups is not None:
        names = [n.strip() for n in args.set_groups.split(",") if n.strip()]
        return [_resolve(n, name_to_id) for n in names]

    result = list(current)
    for name in args.remove_group:
        gid = _resolve(name, name_to_id)
        result = [g for g in result if g != gid]
    for name in args.add_group:
        gid = _resolve(name, name_to_id)
        if gid not in result:
            result.append(gid)
    return result


def _resolve(name: str, name_to_id: dict[str, str]) -> str:
    if name not in name_to_id:
        raise SystemExit(f"unknown group name {name!r}")
    return name_to_id[name]


def _format_groups(ids: list[str], id_to_name: dict[str, str]) -> str:
    if not ids:
        return "[]"
    return "[" + ", ".join(id_to_name.get(i, i) for i in ids) + "]"


def _print_key(key: Json, id_to_name: dict[str, str]) -> None:
    groups = _format_groups(key.get("auto_groups") or [], id_to_name)
    print(
        f"  {key['name']:25}  id={key['id']}  "
        f"ephemeral={key.get('ephemeral')!s:5}  revoked={key.get('revoked')!s:5}  "
        f"groups={groups}"
    )


def _has_mutation(args: argparse.Namespace) -> bool:
    return bool(
        args.add_group or args.remove_group or args.remove_all_groups or args.set_groups is not None
    )


def main() -> int:
    args = _parse_args()
    client = client_from_env(key="admin", fallback_to_user=True)
    name_to_id, id_to_name = _index_groups(client)
    keys = client.get("setup-keys")

    if not _has_mutation(args):
        # read-only listing
        targets = (
            _select_keys(keys, args.key, args.all_keys) if (args.key or args.all_keys) else keys
        )
        print(f"setup keys ({len(targets)}):")
        for k in targets:
            _print_key(k, id_to_name)
        return 0

    if not (args.key or args.all_keys):
        raise SystemExit("mutation requires --key NAME or --all-keys")

    targets = _select_keys(keys, args.key, args.all_keys)
    changed = 0
    for key in targets:
        before_ids = list(key.get("auto_groups") or [])
        after_ids = _new_auto_groups(key, args, name_to_id)
        if before_ids == after_ids:
            print(
                f"  skip   {key['name']}: auto_groups unchanged "
                f"({_format_groups(before_ids, id_to_name)})"
            )
            continue
        before = _format_groups(before_ids, id_to_name)
        after = _format_groups(after_ids, id_to_name)
        verb = "would update" if args.dry_run else "update"
        print(f"  {verb} {key['name']}: {before} -> {after}")
        if not args.dry_run:
            client.put(
                f"setup-keys/{key['id']}",
                data={"auto_groups": after_ids, "revoked": bool(key.get("revoked"))},
            )
            print(f"  updated {key['name']}: now {after}")
        changed += 1

    verb = "would change" if args.dry_run else "changed"
    print(f"Done. {verb}={changed}, total={len(targets)}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
