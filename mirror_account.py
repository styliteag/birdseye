"""Mirror one NetBird account's configuration onto a second controller. Dry-run by default.

    uv run mirror_account.py                      # show the full plan, change nothing
    uv run mirror_account.py --apply              # make the target match the source
    uv run mirror_account.py --only groups,policies --apply
    uv run mirror_account.py --no-prune           # never delete anything on the target

Source  NB_URL / NB_API_KEY          — opened through a client that refuses every
                                       method except GET, so a bug cannot write to it
Target  MIRROR_URL / MIRROR_API_KEY  — the controller being written to

The run aborts if the two URLs resolve to the same host. Tokens and setup-key
values are never printed.

What "everything" can mean here
-------------------------------
Peers cannot be created through the API — they enrol themselves with a setup
key. Anything pointing at a *peer* therefore cannot be mirrored until a peer of
the same name exists on the target. Objects are matched **by name**, not by ID
(IDs are per-instance), so the sync is idempotent and can be re-run: as peers
appear on the target, group memberships, network routers, routes and peer
settings fill in by themselves.

| Object            | Mirrored | Notes                                                        |
|-------------------|----------|--------------------------------------------------------------|
| posture checks    | full     |                                                              |
| groups            | full     | membership only for peers that exist on the target           |
| networks          | full     |                                                              |
| network resources | full     | subnet/host resources need no peer                           |
| network routers   | partial  | needs the router peer (or its peer group) on the target      |
| policies          | full     | group / resource / posture references remapped by name       |
| routes (legacy)   | partial  | same peer limitation as routers                              |
| setup keys        | partial  | recreated by name — the **key value is necessarily new**     |
| DNS nameservers   | full     |                                                              |
| DNS settings      | full     | disabled_management_groups remapped                          |
| users             | partial  | auto_groups/role for users that exist on both sides;         |
|                   |          | `--create-users` also creates the missing ones               |
| peers             | partial  | settings only, for peers matched by name                     |
| account settings  | full     | group-valued settings remapped                               |

Prune (delete target objects the source does not have) is ON by default — this
is a mirror, not an additive import. `MIRROR_PROTECTED_GROUPS` (default `All`)
is never created or deleted; neither is the target's own account or owner.

An object-level mirror is **not** a failover target: without peers, the target
cannot take over. Use `clone_standby.py` for that. This job is for keeping a
second controller's *configuration* in step — a lab, a staging instance, or a
second region you enrol peers into separately.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from dotenv import load_dotenv

from backup_common import env, env_list, make_log

_log = make_log("mirror_account")

SECTIONS = [
    "posture-checks",
    "groups",
    "networks",
    "resources",
    "routers",
    "policies",
    "routes",
    "setup-keys",
    "nameservers",
    "dns-settings",
    "users",
    "peers",
    "account",
]

# (section, phase) in dependency order: dependants are deleted first and created last.
PLAN = [
    ("policies", "delete"),
    ("routes", "delete"),
    ("routers", "delete"),
    ("resources", "delete"),
    ("networks", "delete"),
    ("setup-keys", "delete"),
    ("nameservers", "delete"),
    ("posture-checks", "upsert"),
    ("groups", "upsert"),
    ("networks", "upsert"),
    ("resources", "upsert"),
    ("routers", "upsert"),
    ("policies", "upsert"),
    ("routes", "upsert"),
    ("setup-keys", "upsert"),
    ("nameservers", "upsert"),
    ("dns-settings", "upsert"),
    ("users", "upsert"),
    ("peers", "upsert"),
    ("account", "upsert"),
    ("groups", "delete"),
    ("posture-checks", "delete"),
]

DEFAULT_PROTECTED_GROUPS = "All"
DEFAULT_SNAPSHOT_DIR = "/var/lib/birdseye/mirror"


class ApiError(RuntimeError):
    pass


class Api:
    """Minimal NetBird API client. read_only=True hard-blocks anything that could write.

    Deliberately not the `netbird` SDK: this walks endpoints the SDK models
    reject on write (see CLAUDE.md, "Critical write-path gotcha") and needs raw
    dicts on both sides to diff them.
    """

    def __init__(self, base: str, token: str, label: str, read_only: bool = False) -> None:
        base = base.rstrip("/")
        if base.endswith("/api"):
            base = base[:-4]
        self.base = base
        self.host = urllib.parse.urlsplit(base).netloc.lower()
        self._token = token
        self.label = label
        self.read_only = read_only

    def request(self, method: str, path: str, body=None):
        method = method.upper()
        if self.read_only and method != "GET":
            raise ApiError(f"BUG: {method} attempted against read-only {self.label}")
        url = f"{self.base}/api/{path.lstrip('/')}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Token {self._token}")
        req.add_header("Accept", "application/json")
        if data:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                payload = resp.read().decode()
        except urllib.error.HTTPError as e:
            detail = e.read().decode()[:400]
            raise ApiError(f"{method} {self.label}/{path} -> HTTP {e.code}: {detail}") from e
        except urllib.error.URLError as e:
            raise ApiError(f"{method} {self.label}/{path} -> {e.reason}") from e
        if not payload.strip():
            return None
        return json.loads(payload)

    def get(self, path):
        return self.request("GET", path)


def name_of(obj):
    return obj["name"]


def by_name(items):
    return {i["name"]: i for i in items}


class Mirror:
    def __init__(self, src: Api, dst: Api, args) -> None:
        self.src = src
        self.dst = dst
        self.apply = args.apply
        self.prune = args.prune
        self.verbose = args.verbose
        self.create_users = args.create_users
        self.sections = args.sections
        self.protected = args.protected
        self.snapshot_dir = args.snapshot_dir
        self.created = self.updated = self.deleted = 0
        self.skipped: list[tuple[str, str, str]] = []
        self.errors: list[tuple[str, str, str]] = []
        self.snapshot_written = False
        self._new_ids: dict[tuple[str, str], str] = {}
        self.s: dict = {}
        self.d: dict = {}

    # ---------------------------------------------------------------- plumbing

    def log(self, mark, section, text):
        print(f"  {mark} {section:<14} {text}")

    def skip(self, section, name, reason):
        self.skipped.append((section, name, reason))
        self.log("!", section, f"{name}  — skipped: {reason}")

    def error(self, section, name, msg):
        self.errors.append((section, name, msg))
        self.log("E", section, f"{name}  — FAILED: {msg}")

    def enabled(self, section):
        return section in self.sections

    def snapshot(self):
        """Dump the whole target account before the first write."""
        if self.snapshot_written or not self.apply or not self.snapshot_dir:
            return
        try:
            os.makedirs(self.snapshot_dir, exist_ok=True)
        except OSError as e:
            _log(f"cannot write {self.snapshot_dir} ({e}) — continuing without a pre-write dump")
            self.snapshot_written = True
            return
        stamp = time.strftime("%Y%m%d-%H%M%S")
        path = os.path.join(self.snapshot_dir, f"target-before-{stamp}.json")
        snap = {"url": self.dst.base, "taken": stamp}
        for ep in (
            "accounts",
            "users",
            "peers",
            "groups",
            "policies",
            "networks",
            "posture-checks",
            "setup-keys",
            "routes",
            "dns/nameservers",
            "dns/settings",
        ):
            try:
                snap[ep] = self.dst.get(ep)
            except ApiError as e:
                snap[ep] = {"error": str(e)}
        nets = snap.get("networks") or []
        snap["network_resources"], snap["network_routers"] = {}, {}
        for n in nets if isinstance(nets, list) else []:
            try:
                snap["network_resources"][n["name"]] = self.dst.get(f"networks/{n['id']}/resources")
                snap["network_routers"][n["name"]] = self.dst.get(f"networks/{n['id']}/routers")
            except ApiError as e:
                snap["network_resources"][n["name"]] = {"error": str(e)}
        with open(path, "w") as fh:
            json.dump(snap, fh, indent=2)
        os.chmod(path, 0o600)
        self.snapshot_written = True
        print(f"\nTarget snapshot before the first write: {path}\n")

    def write(self, section, method, path, body=None):
        """Perform a write against the target (no-op in dry-run)."""
        if not self.apply:
            return {"id": self._placeholder(section, body)}
        self.snapshot()
        return self.dst.request(method, path, body)

    def _placeholder(self, section, body):
        return f"NEW:{section}:{(body or {}).get('name', '?')}"

    # ------------------------------------------------------------ state loading

    def load(self):
        s, d = self.s, self.d
        for key, ep in (
            ("groups", "groups"),
            ("policies", "policies"),
            ("networks", "networks"),
            ("posture", "posture-checks"),
            ("peers", "peers"),
            ("users", "users"),
            ("keys", "setup-keys"),
            ("routes", "routes"),
            ("nameservers", "dns/nameservers"),
        ):
            s[key] = self.src.get(ep) or []
            d[key] = self.dst.get(ep) or []
        s["dns"] = self.src.get("dns/settings") or {}
        d["dns"] = self.dst.get("dns/settings") or {}
        s["account"] = (self.src.get("accounts") or [{}])[0]
        d["account"] = (self.dst.get("accounts") or [{}])[0]
        for api, st in ((self.src, s), (self.dst, d)):
            st["resources"], st["routers"] = {}, {}
            for n in st["networks"]:
                st["resources"][n["name"]] = api.get(f"networks/{n['id']}/resources") or []
                st["routers"][n["name"]] = api.get(f"networks/{n['id']}/routers") or []
        self.reindex()

    def reindex(self):
        """(Re)build the target name->object indexes. Called after every upsert phase."""
        self.d_groups = by_name(self.d["groups"])
        self.d_posture = by_name(self.d["posture"])
        self.d_networks = by_name(self.d["networks"])
        self.d_peers = by_name(self.d["peers"])
        self.d_resources = {
            (net, r["name"]): r for net, rs in self.d["resources"].items() for r in rs
        }
        self.s_group_name = {g["id"]: g["name"] for g in self.s["groups"]}
        self.s_peer_name = {p["id"]: p["name"] for p in self.s["peers"]}
        self.s_posture_name = {p["id"]: p["name"] for p in self.s["posture"]}
        self.s_resource_name = {
            r["id"]: (net, r["name"]) for net, rs in self.s["resources"].items() for r in rs
        }

    def refresh(self, *kinds):
        """Re-read parts of the target after writing them."""
        eps = {
            "groups": "groups",
            "posture": "posture-checks",
            "networks": "networks",
            "policies": "policies",
            "peers": "peers",
            "users": "users",
            "keys": "setup-keys",
            "routes": "routes",
            "nameservers": "dns/nameservers",
        }
        for k in kinds:
            if k in eps:
                self.d[k] = self.dst.get(eps[k]) or []
            elif k == "resources":
                self.d["resources"] = {
                    n["name"]: self.dst.get(f"networks/{n['id']}/resources") or []
                    for n in self.d["networks"]
                }
            elif k == "routers":
                self.d["routers"] = {
                    n["name"]: self.dst.get(f"networks/{n['id']}/routers") or []
                    for n in self.d["networks"]
                }
        self.reindex()

    # ------------------------------------------------------------ id mapping

    def _new_id(self, kind, name):
        return self._new_ids.setdefault((kind, name), f"NEW:{kind}:{name}")

    def map_group(self, src_id):
        name = self.s_group_name.get(src_id)
        if name is None:
            return None, f"unknown source group {src_id}"
        g = self.d_groups.get(name)
        if g:
            return g["id"], None
        if not self.apply and ("group", name) in self._new_ids:
            return self._new_id("group", name), None
        return None, f"group {name!r} missing on target"

    def map_groups(self, src_ids):
        out, problems = [], []
        for i in src_ids or []:
            gid, err = self.map_group(i)
            out.append(gid) if gid else problems.append(err)
        return out, problems

    def map_posture(self, src_id):
        name = self.s_posture_name.get(src_id)
        p = self.d_posture.get(name) if name else None
        if p:
            return p["id"], None
        if not self.apply and ("posture", name) in self._new_ids:
            return self._new_id("posture", name), None
        return None, f"posture check {name!r} missing on target"

    def map_peer(self, src_id):
        name = self.s_peer_name.get(src_id)
        if name is None:
            return None, f"stale reference to a peer that no longer exists on the source ({src_id})"
        p = self.d_peers.get(name)
        return (p["id"], None) if p else (None, f"peer {name!r} not enrolled on target")

    def map_resource(self, src_id):
        key = self.s_resource_name.get(src_id)
        if key is None:
            return None, f"unknown source resource {src_id}"
        r = self.d_resources.get(key)
        if r:
            return r["id"], None
        if not self.apply and ("resource", key[1]) in self._new_ids:
            return self._new_id("resource", key[1]), None
        return None, f"resource {key[1]!r} missing on target"

    # ------------------------------------------------------------ diff helper

    def upsert(self, section, name, want, current, create, update):
        """Compare payloads, then create or update. create()/update() do the API call."""
        if current is None:
            self.log("+", section, f"create {name}")
            create()
            self.created += 1
            return
        changed = [
            k
            for k in want
            if json.dumps(want[k], sort_keys=True) != json.dumps(current.get(k), sort_keys=True)
        ]
        if not changed:
            if self.verbose:
                self.log("=", section, f"{name} already in sync")
            return
        self.log("~", section, f"update {name}  ({', '.join(changed)})")
        if self.verbose:
            for k in changed:
                print(f"      - {k}: {json.dumps(current.get(k))[:200]}")
                print(f"      + {k}: {json.dumps(want[k])[:200]}")
        update()
        self.updated += 1

    def delete_extras(self, section, extras, path_of, label=name_of):
        for obj in extras:
            self.log("-", section, f"delete {label(obj)}")
            try:
                self.write(section, "DELETE", path_of(obj))
                self.deleted += 1
            except ApiError as e:
                self.error(section, label(obj), str(e))

    # ------------------------------------------------------------ sections

    def s_posture_checks(self, phase):
        want = {
            p["name"]: {
                "name": p["name"],
                "description": p.get("description") or "",
                "checks": p["checks"],
            }
            for p in self.s["posture"]
        }
        if phase == "delete":
            if self.prune:
                self.delete_extras(
                    "posture-checks",
                    [p for p in self.d["posture"] if p["name"] not in want],
                    lambda p: f"posture-checks/{p['id']}",
                )
            return
        for name, body in sorted(want.items()):
            cur = self.d_posture.get(name)
            curp = (
                None
                if cur is None
                else {
                    "name": cur["name"],
                    "description": cur.get("description") or "",
                    "checks": cur["checks"],
                }
            )
            self.upsert(
                "posture-checks",
                name,
                body,
                curp,
                lambda b=body, n=name: (
                    self.write("posture-checks", "POST", "posture-checks", b),
                    self._new_id("posture", n),
                ),
                lambda b=body, c=cur: self.write(
                    "posture-checks", "PUT", f"posture-checks/{c['id']}", b
                ),
            )

    def s_groups(self, phase):
        want = {g["name"]: g for g in self.s["groups"]}
        if phase == "delete":
            if self.prune:
                extras = [
                    g
                    for g in self.d["groups"]
                    if g["name"] not in want and g["name"] not in self.protected
                ]
                self.delete_extras("groups", extras, lambda g: f"groups/{g['id']}")
            return
        for name, g in sorted(want.items()):
            cur = self.d_groups.get(name)
            peers, problems = [], []
            for p in g.get("peers") or []:
                pid, err = self.map_peer(p["id"])
                peers.append(pid) if pid else problems.append(err)
            if name in self.protected:
                if cur is None:
                    self.skip("groups", name, "auto-maintained group missing on target")
                continue
            body = {"name": name, "peers": sorted(peers)}
            if cur is not None:
                # resource membership is owned by the resources section — preserve it verbatim
                body["resources"] = [
                    {"id": r["id"], "type": r["type"]} for r in (cur.get("resources") or [])
                ]
            curp = (
                None
                if cur is None
                else {
                    "name": cur["name"],
                    "peers": sorted(p["id"] for p in (cur.get("peers") or [])),
                    "resources": body.get("resources", []),
                }
            )
            if problems and self.verbose:
                self.log(" ", "groups", f"{name}: {len(problems)} peer(s) not on target")
            self.upsert(
                "groups",
                name,
                body,
                curp,
                lambda b=body, n=name: (
                    self.write("groups", "POST", "groups", b),
                    self._new_id("group", n),
                ),
                lambda b=body, c=cur: self.write("groups", "PUT", f"groups/{c['id']}", b),
            )

    def s_networks(self, phase):
        want = {
            n["name"]: {"name": n["name"], "description": n.get("description") or ""}
            for n in self.s["networks"]
        }
        if phase == "delete":
            if self.prune:
                self.delete_extras(
                    "networks",
                    [n for n in self.d["networks"] if n["name"] not in want],
                    lambda n: f"networks/{n['id']}",
                )
            return
        for name, body in sorted(want.items()):
            cur = self.d_networks.get(name)
            curp = (
                None
                if cur is None
                else {"name": cur["name"], "description": cur.get("description") or ""}
            )
            self.upsert(
                "networks",
                name,
                body,
                curp,
                lambda b=body, n=name: (
                    self.write("networks", "POST", "networks", b),
                    self._new_id("network", n),
                ),
                lambda b=body, c=cur: self.write("networks", "PUT", f"networks/{c['id']}", b),
            )

    def dest_network(self, name):
        """The target network, or its dry-run placeholder if this run would create it."""
        n = self.d_networks.get(name)
        if n is None and not self.apply and ("network", name) in self._new_ids:
            return {"id": self._new_ids[("network", name)], "name": name}
        return n

    def _resource_payload(self, r):
        gids, problems = self.map_groups([g["id"] for g in r.get("groups") or []])
        return {
            "name": r["name"],
            "description": r.get("description") or "",
            "address": r["address"],
            "enabled": r["enabled"],
            "groups": sorted(gids),
        }, problems

    def s_resources(self, phase):
        want = {(net, r["name"]): r for net, rs in self.s["resources"].items() for r in rs}
        if phase == "delete":
            if not self.prune:
                return
            src_nets = {n["name"] for n in self.s["networks"]}
            for net, rs in self.d["resources"].items():
                if net not in src_nets:
                    continue  # the whole network is pruned anyway
                nid = self.d_networks[net]["id"]
                self.delete_extras(
                    "resources",
                    [r for r in rs if (net, r["name"]) not in want],
                    lambda r, nid=nid: f"networks/{nid}/resources/{r['id']}",
                    label=lambda r, net=net: f"{net}/{r['name']}",
                )
            return
        for (net, rname), r in sorted(want.items()):
            dnet = self.dest_network(net)
            if dnet is None:
                self.skip("resources", f"{net}/{rname}", f"network {net!r} missing on target")
                continue
            body, problems = self._resource_payload(r)
            if problems:
                self.skip("resources", f"{net}/{rname}", "; ".join(sorted(set(problems))))
                continue
            cur = self.d_resources.get((net, rname))
            curp = (
                None
                if cur is None
                else {
                    "name": cur["name"],
                    "description": cur.get("description") or "",
                    "address": cur["address"],
                    "enabled": cur["enabled"],
                    "groups": sorted(g["id"] for g in cur.get("groups") or []),
                }
            )
            self.upsert(
                "resources",
                f"{net}/{rname}",
                body,
                curp,
                lambda b=body, d=dnet, n=rname: (
                    self.write("resources", "POST", f"networks/{d['id']}/resources", b),
                    self._new_id("resource", n),
                ),
                lambda b=body, d=dnet, c=cur: self.write(
                    "resources", "PUT", f"networks/{d['id']}/resources/{c['id']}", b
                ),
            )

    def _router_payload(self, rt):
        problems = []
        body = {
            "metric": rt["metric"],
            "masquerade": rt["masquerade"],
            "enabled": rt["enabled"],
            "peer": "",
            "peer_groups": [],
        }
        if rt.get("peer"):
            pid, err = self.map_peer(rt["peer"])
            if err:
                problems.append(err)
            else:
                body["peer"] = pid
        if rt.get("peer_groups"):
            gids, probs = self.map_groups(rt["peer_groups"])
            problems += probs
            body["peer_groups"] = sorted(gids)
        return body, problems

    def _router_key(self, rt, side):
        """Routers have no name — identify them by what they route through."""
        if rt.get("peer"):
            names = (
                [self.s_peer_name.get(rt["peer"], rt["peer"])]
                if side == "s"
                else [
                    next((p["name"] for p in self.d["peers"] if p["id"] == rt["peer"]), rt["peer"])
                ]
            )
        else:
            src = {g["id"]: g["name"] for g in self.s["groups"]}
            dst = {g["id"]: g["name"] for g in self.d["groups"]}
            table = src if side == "s" else dst
            names = sorted(table.get(g, g) for g in rt.get("peer_groups") or [])
        return ",".join(names)

    def s_routers(self, phase):
        want = {
            (net, self._router_key(rt, "s")): rt
            for net, rts in self.s["routers"].items()
            for rt in rts
        }
        if phase == "delete":
            if not self.prune:
                return
            src_nets = {n["name"] for n in self.s["networks"]}
            for net, rts in self.d["routers"].items():
                if net not in src_nets:
                    continue
                nid = self.d_networks[net]["id"]
                extras = [rt for rt in rts if (net, self._router_key(rt, "d")) not in want]
                self.delete_extras(
                    "routers",
                    extras,
                    lambda rt, nid=nid: f"networks/{nid}/routers/{rt['id']}",
                    label=lambda rt, net=net: f"{net}/{self._router_key(rt, 'd')}",
                )
            return
        for (net, key), rt in sorted(want.items()):
            dnet = self.dest_network(net)
            if dnet is None:
                self.skip("routers", f"{net}/{key}", f"network {net!r} missing on target")
                continue
            body, problems = self._router_payload(rt)
            if problems:
                self.skip("routers", f"{net}/{key}", "; ".join(sorted(set(problems))))
                continue
            cur = next(
                (r for r in self.d["routers"].get(net, []) if self._router_key(r, "d") == key), None
            )
            curp = (
                None
                if cur is None
                else {
                    "metric": cur["metric"],
                    "masquerade": cur["masquerade"],
                    "enabled": cur["enabled"],
                    "peer": cur.get("peer") or "",
                    "peer_groups": sorted(cur.get("peer_groups") or []),
                }
            )
            self.upsert(
                "routers",
                f"{net}/{key}",
                body,
                curp,
                lambda b=body, d=dnet: self.write(
                    "routers", "POST", f"networks/{d['id']}/routers", b
                ),
                lambda b=body, d=dnet, c=cur: self.write(
                    "routers", "PUT", f"networks/{d['id']}/routers/{c['id']}", b
                ),
            )

    def _policy_payload(self, p):
        problems = []
        checks = []
        for c in p.get("source_posture_checks") or []:
            cid, err = self.map_posture(c)
            checks.append(cid) if cid else problems.append(err)
        rules = []
        for r in p["rules"]:
            src_ids, probs = self.map_groups([g["id"] for g in r.get("sources") or []])
            problems += probs
            rule = {
                "name": r["name"],
                "description": r.get("description") or "",
                "enabled": r["enabled"],
                "action": r["action"],
                "bidirectional": r["bidirectional"],
                "protocol": r["protocol"],
                "sources": sorted(src_ids),
            }
            if r.get("ports"):
                rule["ports"] = r["ports"]
            if r.get("port_ranges"):
                rule["port_ranges"] = [
                    {"start": pr["start"], "end": pr["end"]} for pr in r["port_ranges"]
                ]
            if r.get("destinationResource"):
                dr = r["destinationResource"]
                # a rule may point straight at a peer instead of at a network resource
                rid, err = (
                    self.map_peer(dr["id"]) if dr["type"] == "peer" else self.map_resource(dr["id"])
                )
                if err:
                    problems.append(err)
                else:
                    rule["destinationResource"] = {"id": rid, "type": dr["type"]}
            else:
                dst_ids, probs = self.map_groups([g["id"] for g in r.get("destinations") or []])
                problems += probs
                rule["destinations"] = sorted(dst_ids)
            rules.append(rule)
        body = {
            "name": p["name"],
            "description": p.get("description") or "",
            "enabled": p["enabled"],
            "source_posture_checks": sorted(checks),
            "rules": rules,
        }
        return body, problems

    def _policy_current(self, p):
        rules = []
        for r in p["rules"]:
            rule = {
                "name": r["name"],
                "description": r.get("description") or "",
                "enabled": r["enabled"],
                "action": r["action"],
                "bidirectional": r["bidirectional"],
                "protocol": r["protocol"],
                "sources": sorted(g["id"] for g in r.get("sources") or []),
            }
            if r.get("ports"):
                rule["ports"] = r["ports"]
            if r.get("port_ranges"):
                rule["port_ranges"] = [
                    {"start": pr["start"], "end": pr["end"]} for pr in r["port_ranges"]
                ]
            if r.get("destinationResource"):
                rule["destinationResource"] = {
                    "id": r["destinationResource"]["id"],
                    "type": r["destinationResource"]["type"],
                }
            else:
                rule["destinations"] = sorted(g["id"] for g in r.get("destinations") or [])
            rules.append(rule)
        return {
            "name": p["name"],
            "description": p.get("description") or "",
            "enabled": p["enabled"],
            "source_posture_checks": sorted(p.get("source_posture_checks") or []),
            "rules": rules,
        }

    def s_policies(self, phase):
        want = {p["name"]: p for p in self.s["policies"]}
        d_index = by_name(self.d["policies"])
        if phase == "delete":
            if self.prune:
                self.delete_extras(
                    "policies",
                    [p for p in self.d["policies"] if p["name"] not in want],
                    lambda p: f"policies/{p['id']}",
                )
            return
        for name, p in sorted(want.items()):
            body, problems = self._policy_payload(p)
            if problems:
                self.skip("policies", name, "; ".join(sorted(set(problems))))
                continue
            cur = d_index.get(name)
            self.upsert(
                "policies",
                name,
                body,
                None if cur is None else self._policy_current(cur),
                lambda b=body: self.write("policies", "POST", "policies", b),
                lambda b=body, c=cur: self.write("policies", "PUT", f"policies/{c['id']}", b),
            )

    def _route_payload(self, r):
        problems = []
        gids, probs = self.map_groups(r.get("groups"))
        problems += probs
        acl, probs = self.map_groups(r.get("access_control_groups"))
        problems += probs
        body = {
            "network_id": r["network_id"],
            "description": r.get("description") or "",
            "enabled": r["enabled"],
            "metric": r["metric"],
            "masquerade": r["masquerade"],
            "keep_route": r.get("keep_route", False),
            "groups": sorted(gids),
            "access_control_groups": sorted(acl),
            "peer": "",
            "peer_groups": [],
        }
        if r.get("network"):
            body["network"] = r["network"]
        if r.get("domains"):
            body["domains"] = r["domains"]
        if r.get("peer"):
            pid, err = self.map_peer(r["peer"])
            if err:
                problems.append(err)
            else:
                body["peer"] = pid
        if r.get("peer_groups"):
            pg, probs = self.map_groups(r["peer_groups"])
            problems += probs
            body["peer_groups"] = sorted(pg)
        return body, problems

    def s_routes(self, phase):
        want = {r["network_id"]: r for r in self.s["routes"]}
        d_index = {r["network_id"]: r for r in self.d["routes"]}
        if phase == "delete":
            if self.prune:
                self.delete_extras(
                    "routes",
                    [r for r in self.d["routes"] if r["network_id"] not in want],
                    lambda r: f"routes/{r['id']}",
                    label=lambda r: r["network_id"],
                )
            return
        for nid, r in sorted(want.items()):
            body, problems = self._route_payload(r)
            if problems:
                self.skip("routes", nid, "; ".join(sorted(set(problems))))
                continue
            cur = d_index.get(nid)
            curp = None
            if cur is not None:
                curp = {k: cur.get(k) for k in body}
                curp["groups"] = sorted(cur.get("groups") or [])
                curp["access_control_groups"] = sorted(cur.get("access_control_groups") or [])
                curp["peer"] = cur.get("peer") or ""
                curp["peer_groups"] = sorted(cur.get("peer_groups") or [])
                curp["description"] = cur.get("description") or ""
            self.upsert(
                "routes",
                nid,
                body,
                curp,
                lambda b=body: self.write("routes", "POST", "routes", b),
                lambda b=body, c=cur: self.write("routes", "PUT", f"routes/{c['id']}", b),
            )

    def s_setup_keys(self, phase):
        want = {k["name"]: k for k in self.s["keys"]}
        d_index = by_name(self.d["keys"])
        if phase == "delete":
            if self.prune:
                self.delete_extras(
                    "setup-keys",
                    [k for k in self.d["keys"] if k["name"] not in want],
                    lambda k: f"setup-keys/{k['id']}",
                )
            return
        now = time.time()
        for name, k in sorted(want.items()):
            gids, problems = self.map_groups(k.get("auto_groups"))
            if problems:
                self.skip("setup-keys", name, "; ".join(sorted(set(problems))))
                continue
            cur = d_index.get(name)
            if cur is None:
                expires_in = 0
                exp = k.get("expires", "")
                if exp and not exp.startswith("0001-01-01"):
                    left = int(_iso(exp) - now)
                    if left <= 0:
                        self.skip("setup-keys", name, "expired on the source — not recreated")
                        continue
                    expires_in = max(left, 86400)
                body = {
                    "name": name,
                    "type": k["type"],
                    "expires_in": expires_in,
                    "auto_groups": sorted(gids),
                    "usage_limit": k.get("usage_limit", 0),
                    "ephemeral": k.get("ephemeral", False),
                    "allow_extra_dns_labels": k.get("allow_extra_dns_labels", False),
                }
                self.log(
                    "+", "setup-keys", f"create {name}  (new secret — value differs from source)"
                )
                try:
                    self.write("setup-keys", "POST", "setup-keys", body)
                    self.created += 1
                except ApiError as e:
                    self.error("setup-keys", name, str(e))
                continue
            body = {"name": name, "auto_groups": sorted(gids), "revoked": k.get("revoked", False)}
            curp = {
                "name": cur["name"],
                "auto_groups": sorted(cur.get("auto_groups") or []),
                "revoked": cur.get("revoked", False),
            }
            self.upsert(
                "setup-keys",
                name,
                body,
                curp,
                lambda: None,
                lambda b=body, c=cur: self.write("setup-keys", "PUT", f"setup-keys/{c['id']}", b),
            )

    def s_nameservers(self, phase):
        want = {n["name"]: n for n in self.s["nameservers"]}
        d_index = by_name(self.d["nameservers"])
        if phase == "delete":
            if self.prune:
                self.delete_extras(
                    "nameservers",
                    [n for n in self.d["nameservers"] if n["name"] not in want],
                    lambda n: f"dns/nameservers/{n['id']}",
                )
            return
        for name, n in sorted(want.items()):
            gids, problems = self.map_groups(n.get("groups"))
            if problems:
                self.skip("nameservers", name, "; ".join(sorted(set(problems))))
                continue
            body = {
                "name": name,
                "description": n.get("description") or "",
                "nameservers": n["nameservers"],
                "enabled": n["enabled"],
                "groups": sorted(gids),
                "primary": n.get("primary", False),
                "domains": n.get("domains") or [],
                "search_domains_enabled": n.get("search_domains_enabled", False),
            }
            cur = d_index.get(name)
            curp = (
                None
                if cur is None
                else {
                    **{k: cur.get(k) for k in body},
                    "groups": sorted(cur.get("groups") or []),
                    "description": cur.get("description") or "",
                    "domains": cur.get("domains") or [],
                }
            )
            self.upsert(
                "nameservers",
                name,
                body,
                curp,
                lambda b=body: self.write("nameservers", "POST", "dns/nameservers", b),
                lambda b=body, c=cur: self.write(
                    "nameservers", "PUT", f"dns/nameservers/{c['id']}", b
                ),
            )

    def s_dns_settings(self, phase):
        if phase != "upsert":
            return
        gids, problems = self.map_groups(self.s["dns"].get("disabled_management_groups"))
        for p in sorted(set(problems)):
            self.skip("dns-settings", "disabled_management_groups", p)
        body = {"disabled_management_groups": sorted(gids)}
        cur = {
            "disabled_management_groups": sorted(
                self.d["dns"].get("disabled_management_groups") or []
            )
        }
        self.upsert(
            "dns-settings",
            "dns/settings",
            body,
            cur,
            lambda: None,
            lambda b=body: self.write("dns-settings", "PUT", "dns/settings", b),
        )

    def s_users(self, phase):
        if phase != "upsert":
            return
        d_by_mail = {u["email"]: u for u in self.d["users"] if u.get("email")}
        d_by_name = {u["name"]: u for u in self.d["users"] if u.get("is_service_user")}
        for u in sorted(self.s["users"], key=lambda x: x.get("email") or x["name"]):
            label = u.get("email") or u["name"]
            cur = d_by_mail.get(u.get("email")) if u.get("email") else d_by_name.get(u["name"])
            gids, problems = self.map_groups(u.get("auto_groups"))
            if problems:
                self.skip("users", label, "; ".join(sorted(set(problems))))
                continue
            if cur is None:
                if not self.create_users:
                    self.skip(
                        "users", label, "no such user on target (use --create-users to create it)"
                    )
                    continue
                body = {
                    "email": u.get("email") or "",
                    "name": u["name"],
                    "role": u["role"],
                    "auto_groups": sorted(gids),
                    "is_service_user": u.get("is_service_user", False),
                }
                self.log("+", "users", f"create {label} ({u['role']})")
                try:
                    self.write("users", "POST", "users", body)
                    self.created += 1
                except ApiError as e:
                    self.error("users", label, str(e))
                continue
            role = cur["role"] if cur["role"] == "owner" or u["role"] == "owner" else u["role"]
            body = {
                "role": role,
                "auto_groups": sorted(gids),
                "is_blocked": u.get("is_blocked", False),
            }
            curp = {
                "role": cur["role"],
                "auto_groups": sorted(cur.get("auto_groups") or []),
                "is_blocked": cur.get("is_blocked", False),
            }
            self.upsert(
                "users",
                label,
                body,
                curp,
                lambda: None,
                lambda b=body, c=cur: self.write("users", "PUT", f"users/{c['id']}", b),
            )

    def s_peers(self, phase):
        if phase != "upsert":
            return
        for p in sorted(self.s["peers"], key=name_of):
            cur = self.d_peers.get(p["name"])
            if cur is None:
                self.skip("peers", p["name"], "not enrolled on target (peers cannot be created)")
                continue
            body = {
                "name": p["name"],
                "ssh_enabled": p.get("ssh_enabled", False),
                "login_expiration_enabled": p.get("login_expiration_enabled", False),
                "inactivity_expiration_enabled": p.get("inactivity_expiration_enabled", False),
            }
            curp = {k: cur.get(k, False) for k in body}
            curp["name"] = cur["name"]
            self.upsert(
                "peers",
                p["name"],
                body,
                curp,
                lambda: None,
                lambda b=body, c=cur: self.write("peers", "PUT", f"peers/{c['id']}", b),
            )

    # settings whose values are group IDs and therefore need remapping
    GROUP_SETTINGS = ("ipv6_enabled_groups", "peer_expose_groups")

    def s_account(self, phase):
        if phase != "upsert":
            return
        src = self.s["account"].get("settings") or {}
        dst = self.d["account"].get("settings") or {}
        if not src or not dst:
            self.skip("account", "settings", "could not read account settings on both sides")
            return
        body, problems = {}, []
        for k, v in src.items():
            if k not in dst:
                continue  # field the target version does not know
            if k in self.GROUP_SETTINGS:
                gids, probs = self.map_groups(v)
                problems += probs
                body[k] = sorted(gids)
            elif k == "extra" and isinstance(v, dict):
                extra = dict(v)
                if extra.get("network_traffic_logs_groups"):
                    gids, probs = self.map_groups(extra["network_traffic_logs_groups"])
                    problems += probs
                    extra["network_traffic_logs_groups"] = sorted(gids)
                body[k] = extra
            else:
                body[k] = v
        for p in sorted(set(problems)):
            self.skip("account", "settings", p)
        curp = {}
        for k in body:
            v = dst.get(k)
            curp[k] = sorted(v or []) if k in self.GROUP_SETTINGS else v
            if k == "extra" and isinstance(v, dict) and v.get("network_traffic_logs_groups"):
                curp[k] = {
                    **v,
                    "network_traffic_logs_groups": sorted(v["network_traffic_logs_groups"]),
                }
        aid = self.d["account"]["id"]
        self.upsert(
            "account",
            "settings",
            body,
            curp,
            lambda: None,
            lambda b=body: self.write("account", "PUT", f"accounts/{aid}", {"settings": b}),
        )

    # ------------------------------------------------------------ driver

    HANDLERS = {
        "posture-checks": s_posture_checks,
        "groups": s_groups,
        "networks": s_networks,
        "resources": s_resources,
        "routers": s_routers,
        "policies": s_policies,
        "routes": s_routes,
        "setup-keys": s_setup_keys,
        "nameservers": s_nameservers,
        "dns-settings": s_dns_settings,
        "users": s_users,
        "peers": s_peers,
        "account": s_account,
    }

    # sections that must be re-read after being written, so later sections see real IDs
    REFRESH_AFTER = {
        "posture-checks": ("posture",),
        "groups": ("groups",),
        "networks": ("networks", "resources", "routers"),
        "resources": ("resources",),
        "routers": ("routers",),
        "policies": ("policies",),
        "routes": ("routes",),
        "setup-keys": ("keys",),
        "nameservers": ("nameservers",),
        "users": ("users",),
        "peers": ("peers",),
    }

    def run(self):
        for section, phase in PLAN:
            if not self.enabled(section):
                continue
            if phase == "delete" and not self.prune:
                continue
            before = (self.created, self.updated, self.deleted)
            self.HANDLERS[section](self, phase)
            touched = (self.created, self.updated, self.deleted) != before
            if self.apply and touched and section in self.REFRESH_AFTER:
                self.refresh(*self.REFRESH_AFTER[section])

    def verify(self):
        """Re-plan against fresh target state; anything left to do is a real difference."""
        print("\nVerifying …")
        checker = Mirror(
            self.src,
            self.dst,
            _CheckArgs(
                sections=self.sections,
                prune=self.prune,
                create_users=self.create_users,
                protected=self.protected,
                snapshot_dir=self.snapshot_dir,
            ),
        )
        checker.load()
        checker.run()
        left = checker.created + checker.updated + checker.deleted
        if left == 0:
            print("  target matches the source for every selected section")
        else:
            print(f"  {left} difference(s) still outstanding (see the plan above)")
        blocked = [
            s
            for s in checker.skipped
            if "not enrolled" not in s[2] and "peers cannot be created" not in s[2]
        ]
        peer_blocked = len(checker.skipped) - len(blocked)
        if peer_blocked:
            print(f"  {peer_blocked} object(s) waiting for peers to enrol on the target")
        for section, name, reason in blocked:
            print(f"  UNRESOLVED {section}: {name} — {reason}")
        return left == 0 and not blocked


class _CheckArgs:
    """Argument stand-in for the verification pass (always a dry run)."""

    def __init__(self, sections, prune, create_users, protected, snapshot_dir):
        self.apply = False
        self.verbose = False
        self.sections = sections
        self.prune = prune
        self.create_users = create_users
        self.protected = protected
        self.snapshot_dir = snapshot_dir


def _iso(ts: str) -> float:
    ts = ts.replace("Z", "+00:00")
    if "." in ts:  # python <3.11 chokes on >6 fractional digits
        head, rest = ts.split(".", 1)
        frac, _, tz = rest.partition("+")
        ts = f"{head}.{frac[:6]}+{tz}" if tz else f"{head}.{frac[:6]}"
    return datetime.datetime.fromisoformat(ts).timestamp()


def _parse_args(argv):
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--apply", action="store_true", help="execute (default: dry run)")
    ap.add_argument(
        "--no-prune", dest="prune", action="store_false", help="never delete on the target"
    )
    ap.add_argument("--only", help=f"comma-separated sections ({', '.join(SECTIONS)})")
    ap.add_argument("--skip", help="comma-separated sections to leave out")
    ap.add_argument(
        "--create-users",
        action="store_true",
        help="also create users missing on the target (may send invitations)",
    )
    ap.add_argument("--verbose", "-v", action="store_true", help="show field-level differences")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = _parse_args(argv if argv is not None else sys.argv[1:])

    sections = env_list("MIRROR_SECTIONS") or list(SECTIONS)
    if args.only:
        sections = [s.strip() for s in args.only.split(",") if s.strip()]
    if args.skip:
        drop = {s.strip() for s in args.skip.split(",")}
        sections = [s for s in sections if s not in drop]
    bad = [s for s in sections if s not in SECTIONS]
    if bad:
        raise SystemExit(f"unknown section(s): {bad}\nknown: {', '.join(SECTIONS)}")
    # keep the dependency order of SECTIONS regardless of how they were listed
    args.sections = [s for s in SECTIONS if s in set(sections)]
    args.protected = set(env_list("MIRROR_PROTECTED_GROUPS", DEFAULT_PROTECTED_GROUPS))
    args.snapshot_dir = env("MIRROR_SNAPSHOT_DIR", DEFAULT_SNAPSHOT_DIR)
    if env("MIRROR_PRUNE", "true").lower() in ("0", "false", "no", "off"):
        args.prune = False
    # The cron entry runs this script with no arguments, so the *scheduled* run
    # has to opt into writing explicitly. Without MIRROR_APPLY a schedule just
    # reports the drift it would fix.
    if env("MIRROR_APPLY").lower() in ("1", "true", "yes", "on"):
        args.apply = True

    src_url, src_token = env("NB_URL"), env("NB_API_KEY")
    dst_url, dst_token = env("MIRROR_URL"), env("MIRROR_API_KEY")
    missing = [
        name
        for name, value in (
            ("NB_URL", src_url),
            ("NB_API_KEY", src_token),
            ("MIRROR_URL", dst_url),
            ("MIRROR_API_KEY", dst_token),
        )
        if not value
    ]
    if missing:
        raise SystemExit("mirror_account requires " + ", ".join(missing) + " to be set")

    src = Api(src_url, src_token, "source", read_only=True)
    dst = Api(dst_url, dst_token, "target")
    if src.host == dst.host:
        raise SystemExit(f"REFUSING: source and target are the same host ({src.host})")

    print(f"source   {src.base}   (read-only)")
    print(f"target   {dst.base}")
    print(f"mode     {'APPLY' if args.apply else 'DRY RUN'}{'' if args.prune else ' (prune off)'}")
    print(f"sections {', '.join(args.sections)}\n")

    mirror = Mirror(src, dst, args)
    try:
        mirror.load()
    except ApiError as e:
        print(f"could not read both sides: {e}", file=sys.stderr)
        return 1
    for label, state in (("source", mirror.s), ("target", mirror.d)):
        print(
            f"{label:<8} {len(state['groups'])} groups, {len(state['policies'])} policies, "
            f"{len(state['networks'])} networks, {len(state['peers'])} peers"
        )
    print()

    try:
        mirror.run()
    except ApiError as e:
        print(f"\nABORTED: {e}", file=sys.stderr)
        return 1

    print(
        f"\n{'applied' if args.apply else 'planned'}: {mirror.created} created, "
        f"{mirror.updated} updated, {mirror.deleted} deleted, "
        f"{len(mirror.skipped)} skipped, {len(mirror.errors)} failed"
    )
    if not args.apply:
        print("\nRe-run with --apply to execute.")
        return 0
    ok = mirror.verify()
    if mirror.errors:
        print(f"\n{len(mirror.errors)} write(s) failed — see the E lines above")
    return 0 if ok and not mirror.errors else 1


if __name__ == "__main__":
    sys.exit(main())
