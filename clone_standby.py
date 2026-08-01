"""Clone this NetBird controller's *data* onto a standby host you can fail over to.

    uv run clone_standby.py stage              # snapshot + ship the payload (no downtime)
    uv run clone_standby.py install [target]   # install it on the standby
    uv run clone_standby.py drill              # start the clone, verify it, stop it again
    uv run clone_standby.py verify [target]
    uv run clone_standby.py failover           # start the clone and leave it running
    uv run clone_standby.py status | rollback [target]
    uv run clone_standby.py run                # stage + install every target + drill (for cron)

Why this exists next to `mirror_account.py`: peers cannot be created through the
API — they enrol themselves. An object-level mirror can therefore never be a
failover target. A copy of the *database* can.

How it works
------------
The management server keeps its SQLite stores open the whole time it runs, so
they are copied with SQLite's online backup API (see `sqlite_snapshot.py`):
transactionally consistent, no downtime, and the source is opened read-only so
this job cannot write to the running controller. Snapshots, config files and
whatever extra paths you list are checksummed into a payload, rsynced to the
standby, and installed there by a generated `install.sh` that verifies every
checksum first and keeps the three previous states for rollback.

Everything is configuration; nothing about any particular deployment is baked
in. See `docker/.env.example` → "Standby clone" for the full list. The shape:

  CLONE_SSH_HOST=root@standby.example.com   the standby (empty disables the job)
  CLONE_DB_PATHS=/data/netbird/store.db,…   live SQLite files to snapshot
  CLONE_CONFIG_FILES=/data/stack/config.yaml,…   installed into each target root
  CLONE_TARGETS=real,test                   one or more stacks on the standby

Each target NAME is described by `CLONE_<NAME>_*` variables — root directory,
compose project, the hostname it answers for, whether it starts by itself.
Two targets is the useful arrangement:

  a **clone** with the primary's own identity and hostname, `AUTOSTART=false`,
  started only for a drill or a real failover; and

  a **smoke test** under the standby's own hostname, running all the time, which
  proves the data survived the trip without pretending to be the primary.

Anything the target's host differs from the primary's is rewritten inside the
shipped config files, so one payload serves both.

Certificates
------------
A standby cannot issue a certificate for the primary's hostname while DNS still
points at the primary — the CA would validate against the primary. A target with
`CERT_COPY=true` therefore gets a *copy* of the primary's certificate merged
into the standby's ACME store on every run (`CLONE_ACME_SOURCE` →
`CLONE_ACME_REMOTE`). Traefik reads that store only at startup and rewrites it
from memory afterwards, so the merge happens with the ingress stopped. After a
real failover the standby renews by itself over HTTP-01/TLS-ALPN — no DNS
provider account required.
"""

from __future__ import annotations

import argparse
import datetime
import glob
import hashlib
import http.client
import json
import os
import shutil
import socket
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import urlparse

from dotenv import load_dotenv

import checkmk
import sqlite_snapshot
from backup_common import env, env_int, env_list, make_log
from remote import Remote, RemoteError, compose_cmd

_log = make_log("clone_standby")

# Container-local on purpose: the payload is rebuilt from scratch every run,
# and a stage directory inside a mounted source directory recurses (see stage()).
DEFAULT_STAGE_DIR = "/var/tmp/birdseye-clone-stage"
DEFAULT_DB_SUBDIR = "data/netbird"
DEFAULT_MIN_ROWS = "accounts:1,peers:1"
DEFAULT_DOCKER_SOCKET = "/var/run/docker.sock"
CHECK_NAME = "NetBird_Standby_Clone"
SPOOL_FILE = "netbird_standby_clone"
# 9 h: with the suggested 6-hourly schedule one missed run is tolerated, two are not.
SPOOL_MAX_AGE = 32400

TRUTHY = ("1", "true", "yes", "on")


def _flag(name: str, default: bool = False) -> bool:
    raw = env(name)
    return raw.lower() in TRUTHY if raw else default


# --------------------------------------------------------------------- config


@dataclass(frozen=True)
class Target:
    """One NetBird stack on the standby that receives the payload."""

    name: str
    root: str
    project: str
    compose: str
    host: str
    autostart: bool
    cert_copy: bool
    db_dir: str

    @property
    def dc(self) -> str:
        return compose_cmd(self.root, self.project, self.compose)

    @property
    def description(self) -> str:
        if self.autostart:
            return f"runs continuously as {self.host}"
        return f"stopped; answers for {self.host} once started"


@dataclass
class PathSpec:
    """A local path and where it goes on the standby.

    mode "sync" replaces the destination (rsync --delete for directories);
    mode "seed" only writes files that are not there yet — for data a service
    keeps updating by itself, where the copy is just a starting point.
    """

    local: str
    dest: str
    mode: str = "sync"

    @property
    def is_dir(self) -> bool:
        return os.path.isdir(self.local)


@dataclass
class Config:
    remote: Remote
    targets: list[Target]
    failover: Target
    payload_dir: str
    stage_dir: str
    db_paths: list[str]
    db_subdir: str
    config_files: list[str]
    target_paths: list[PathSpec]
    shared_paths: list[PathSpec]
    acme_source: str
    acme_remote: str
    ingress_root: str
    ingress_project: str
    ingress_compose: str
    images: list[str]
    docker_socket: str
    min_rows: dict[str, int]
    primary_host: str
    nb_url: str
    nb_token: str
    standby_ip: str = ""
    keep_snapshots: int = 3
    extra_files: dict[str, str] = field(default_factory=dict)

    @property
    def ingress_dc(self) -> str:
        return compose_cmd(self.ingress_root, self.ingress_project, self.ingress_compose)

    def target(self, name: str) -> Target:
        for t in self.targets:
            if t.name == name:
                return t
        raise SystemExit(f"unknown target {name!r}; configured: {', '.join(self.names)}")

    @property
    def names(self) -> list[str]:
        return [t.name for t in self.targets]

    def ip(self) -> str:
        return self.standby_ip or self.remote.ip()


def _parse_paths(spec: str, *, absolute_dest: bool) -> list[PathSpec]:
    """Parse `local:dest[:mode]` entries. Bare `local` keeps the basename.

    A `local` containing a wildcard is expanded now and `dest` is then a
    *directory*: each match is installed under it by basename. That is what
    files whose name carries a date need — GeoIP databases and the like —
    since naming them individually means quietly shipping nothing the month
    the name changes.
    """
    out = []
    for item in (s.strip() for s in spec.split(",")):
        if not item:
            continue
        parts = item.split(":")
        if len(parts) == 1:
            local, dest, mode = parts[0], os.path.basename(parts[0].rstrip("/")), "sync"
        elif len(parts) == 2:
            local, dest, mode = parts[0], parts[1], "sync"
        elif len(parts) == 3:
            local, dest, mode = parts
        else:
            raise SystemExit(f"expected local:dest[:mode], got {item!r}")
        if mode not in ("sync", "seed"):
            raise SystemExit(f"mode must be sync|seed, got {mode!r} in {item!r}")
        if absolute_dest and not dest.startswith("/"):
            raise SystemExit(f"{item!r}: shared destinations must be absolute paths")
        if not absolute_dest and dest.startswith("/"):
            raise SystemExit(f"{item!r}: target destinations are relative to the target root")
        local, dest = local.rstrip("/"), dest.rstrip("/")
        if any(c in local for c in "*?["):
            if len(parts) == 1:
                raise SystemExit(f"{item!r}: a wildcard needs an explicit destination directory")
            matches = sorted(glob.glob(local))
            if not matches:
                # Silence here would mean shipping a payload that is quietly
                # missing something the operator asked for.
                raise SystemExit(f"{item!r}: matched no files")
            out += [PathSpec(m, f"{dest}/{os.path.basename(m)}", mode) for m in matches]
            continue
        out.append(PathSpec(local, dest, mode))
    return out


def _host_from_url(url: str) -> str:
    netloc = urlparse(url if "://" in url else f"https://{url}").netloc
    return netloc.split(":", 1)[0]


def load_config() -> Config:
    remote = Remote.from_env("CLONE")
    if remote is None:
        raise SystemExit("CLONE_SSH_HOST is not set — the standby clone job is disabled")

    nb_url = env("NB_URL")
    if not nb_url:
        raise SystemExit("NB_URL must be set (it names the primary this clone is made from)")
    primary_host = env("CLONE_PRIMARY_HOST") or _host_from_url(nb_url)

    names = env_list("CLONE_TARGETS")
    if not names:
        raise SystemExit("CLONE_TARGETS is not set — name at least one stack on the standby")

    targets = []
    for name in names:
        prefix = f"CLONE_{name.upper().replace('-', '_')}"
        root = env(f"{prefix}_ROOT")
        if not root:
            raise SystemExit(f"{prefix}_ROOT must be set for target {name!r}")
        targets.append(
            Target(
                name=name,
                root=root.rstrip("/"),
                project=env(f"{prefix}_PROJECT", name),
                compose=env(f"{prefix}_COMPOSE", "docker-compose.yml"),
                host=env(f"{prefix}_HOST", primary_host),
                autostart=_flag(f"{prefix}_AUTOSTART"),
                cert_copy=_flag(f"{prefix}_CERT_COPY"),
                db_dir=env(f"{prefix}_DB_DIR", DEFAULT_DB_SUBDIR).strip("/"),
            )
        )

    stopped = [t for t in targets if not t.autostart]
    wanted = env("CLONE_FAILOVER_TARGET")
    if wanted:
        failover = next((t for t in targets if t.name == wanted), None)
        if failover is None:
            raise SystemExit(f"CLONE_FAILOVER_TARGET={wanted!r} is not in CLONE_TARGETS")
    else:
        # The failover target is the one that carries the primary's identity: by
        # convention that is the stack which does *not* start by itself.
        failover = stopped[0] if stopped else targets[0]

    db_paths = env_list("CLONE_DB_PATHS")
    if not db_paths:
        raise SystemExit("CLONE_DB_PATHS is not set — nothing to clone")

    return Config(
        remote=remote,
        targets=targets,
        failover=failover,
        payload_dir=env("CLONE_PAYLOAD_DIR", f"{failover.root}/incoming").rstrip("/"),
        stage_dir=env("CLONE_STAGE_DIR", DEFAULT_STAGE_DIR).rstrip("/"),
        db_paths=db_paths,
        db_subdir=env("CLONE_DB_SUBDIR", DEFAULT_DB_SUBDIR).strip("/"),
        config_files=env_list("CLONE_CONFIG_FILES"),
        target_paths=_parse_paths(env("CLONE_TARGET_PATHS"), absolute_dest=False),
        shared_paths=_parse_paths(env("CLONE_SHARED_PATHS"), absolute_dest=True),
        acme_source=env("CLONE_ACME_SOURCE"),
        acme_remote=env("CLONE_ACME_REMOTE"),
        ingress_root=env("CLONE_INGRESS_ROOT"),
        ingress_project=env("CLONE_INGRESS_PROJECT", "traefik"),
        ingress_compose=env("CLONE_INGRESS_COMPOSE", "docker-compose.yml"),
        images=env_list("CLONE_IMAGES"),
        docker_socket=env("CLONE_DOCKER_SOCKET", DEFAULT_DOCKER_SOCKET),
        min_rows=sqlite_snapshot.parse_min_rows(env("CLONE_MIN_ROWS", DEFAULT_MIN_ROWS)),
        primary_host=primary_host,
        nb_url=nb_url.rstrip("/"),
        nb_token=env("NB_API_KEY"),
        standby_ip=env("CLONE_STANDBY_IP"),
        keep_snapshots=env_int("CLONE_KEEP_SNAPSHOTS", 3),
    )


# --------------------------------------------------------------------- helpers


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class _UnixHTTPConnection(http.client.HTTPConnection):
    """Just enough of a docker client to ask for image digests, no docker CLI needed."""

    def __init__(self, socket_path: str, timeout: float = 10) -> None:
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self.socket_path)
        self.sock = sock


def image_digests(repos: list[str], socket_path: str) -> dict[str, str]:
    """RepoDigest per image, so the standby runs the same binary as the primary.

    Skipped (with a note) when the docker socket is not mounted — the clone
    still works, it just installs whatever `:latest` resolves to over there.
    """
    if not repos:
        return {}
    if not os.path.exists(socket_path):
        _log(f"{socket_path} not mounted — shipping no image digests, standby keeps its own tags")
        return {}
    out = {}
    for repo in repos:
        try:
            conn = _UnixHTTPConnection(socket_path)
            conn.request("GET", f"/images/{urllib.parse.quote(repo, safe='')}:latest/json")
            resp = conn.getresponse()
            body = resp.read().decode()
            conn.close()
            if resp.status != 200:
                _log(f"docker socket: no local image {repo}:latest (HTTP {resp.status})")
                continue
            digests = json.loads(body).get("RepoDigests") or []
            if digests:
                out[repo] = digests[0].split("@", 1)[1]
        except (OSError, ValueError) as e:
            _log(f"could not read the digest of {repo}: {e}")
    return out


def _copy(src: str, dst: str, *, secret: bool = False) -> None:
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if os.path.isdir(src):
        shutil.copytree(src, dst, dirs_exist_ok=True)
    else:
        shutil.copy2(src, dst)
        os.chmod(dst, 0o600 if secret else 0o644)


# --------------------------------------------------------------------- staging


def stage(cfg: Config, args) -> int:
    missing = [p for p in cfg.db_paths + cfg.config_files if not os.path.exists(p)]
    missing += [p.local for p in cfg.target_paths + cfg.shared_paths if not os.path.exists(p.local)]
    if cfg.acme_source and not os.path.exists(cfg.acme_source):
        missing.append(cfg.acme_source)
    if missing:
        raise SystemExit(
            "these paths are configured but not present inside the container: " + ", ".join(missing)
        )

    # A stage directory inside one of the directories being copied makes
    # copytree descend into its own output until Python runs out of stack —
    # the traceback that produces says nothing about the actual mistake, and
    # it fills the disk on the way. Easy to hit when the container's state
    # volume happens to live under a mounted stack directory.
    stage_abs = os.path.abspath(cfg.stage_dir)
    for spec in cfg.target_paths + cfg.shared_paths:
        src = os.path.abspath(spec.local)
        if os.path.isdir(src) and (stage_abs == src or stage_abs.startswith(src + os.sep)):
            raise SystemExit(
                f"CLONE_STAGE_DIR ({stage_abs}) is inside {spec.local}, which this run has to "
                "copy — staging would recurse into its own payload. Point CLONE_STAGE_DIR "
                "at a directory outside every path listed in CLONE_TARGET_PATHS / "
                "CLONE_SHARED_PATHS (a container-local one such as /var/tmp/... is fine, "
                "the payload is rebuilt on every run)."
            )

    stage_dir = cfg.stage_dir
    if os.path.isdir(stage_dir):
        shutil.rmtree(stage_dir)
    os.makedirs(f"{stage_dir}/db")

    print("Snapshotting the live SQLite stores (the server keeps running) …")
    counts: dict[str, int] = {}
    for src in cfg.db_paths:
        dst = f"{stage_dir}/db/{os.path.basename(src)}"
        sqlite_snapshot.snapshot(src, dst)
        size = os.path.getsize(dst) / 1e6
        print(f"  {os.path.basename(src):<14} {size:7.1f} MB  integrity ok")
        for table, n in sqlite_snapshot.table_counts(dst).items():
            counts.setdefault(table, n)
    if counts:
        print("  holds: " + ", ".join(f"{v} {k}" for k, v in counts.items()))
    problems = sqlite_snapshot.check_min_rows(counts, cfg.min_rows)
    if problems:
        # A snapshot of the wrong file, or of a controller that lost its data, must
        # not be allowed to overwrite a good standby.
        raise SystemExit("snapshot fails CLONE_MIN_ROWS: " + "; ".join(problems))

    for src in cfg.config_files:
        _copy(src, f"{stage_dir}/files/{os.path.basename(src)}", secret=True)
    # One payload serves every target: a target answering under a different name
    # gets its own pre-rewritten copies, so the substitution is reviewable here
    # instead of hidden in a sed on the far side.
    for t in cfg.targets:
        if t.host == cfg.primary_host:
            continue
        for src in cfg.config_files:
            name = os.path.basename(src)
            with open(src) as fh:
                body = fh.read().replace(cfg.primary_host, t.host)
            out = f"{stage_dir}/files-{t.name}/{name}"
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with open(out, "w") as fh:
                fh.write(body)
            os.chmod(out, 0o600)

    for i, spec in enumerate(cfg.target_paths):
        _copy(spec.local, f"{stage_dir}/target/{i}")
    for i, spec in enumerate(cfg.shared_paths):
        _copy(spec.local, f"{stage_dir}/shared/{i}")
    if cfg.acme_source:
        _copy(cfg.acme_source, f"{stage_dir}/acme.json", secret=True)

    digests = image_digests(cfg.images, cfg.docker_socket)
    with open(f"{stage_dir}/images.txt", "w") as fh:
        for repo, digest in digests.items():
            fh.write(f"{repo} {digest}\n")

    files, total = {}, 0
    for base, _, names in os.walk(stage_dir):
        for n in names:
            path = os.path.join(base, n)
            size = os.path.getsize(path)
            total += size
            files[os.path.relpath(path, stage_dir)] = {"sha256": sha256(path), "bytes": size}
    manifest = {
        "created": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "source_host": socket.gethostname(),
        "primary": cfg.primary_host,
        "targets": {t.name: t.root for t in cfg.targets},
        "counts": counts,
        "images": digests,
        "files": files,
    }
    with open(f"{stage_dir}/MANIFEST.json", "w") as fh:
        json.dump(manifest, fh, indent=2)
    write_scripts(cfg, stage_dir)

    print(f"\nStaged {len(files) + 3} files, {total / 1e6:.1f} MB, in {stage_dir}")
    if args.dry_run:
        print(f"DRY RUN — would rsync to {cfg.remote.host}:{cfg.payload_dir}/")
        return 0

    cfg.remote.run(f"mkdir -p {cfg.payload_dir} && chmod 700 {cfg.payload_dir}")
    print(f"Shipping to {cfg.remote.host}:{cfg.payload_dir}/ …")
    stats = cfg.remote.push_dir(stage_dir, cfg.payload_dir)
    for line in stats.splitlines():
        if line.startswith(("Number of regular files transferred", "Total transferred file size")):
            print("  " + line.strip())
    cfg.remote.run(
        f"chmod 700 {cfg.payload_dir}/install.sh {cfg.payload_dir}/failover.sh; "
        f"install -m700 {cfg.payload_dir}/failover.sh {cfg.failover.root}/failover.sh"
    )
    print("\nStaged. Install it with:  clone_standby.py install")
    return 0


# ------------------------------------------------------------ shipped scripts

INSTALL_HEAD = r"""#!/bin/bash
# Install the staged payload into one target on this host.
# Generated by birdseye clone_standby.py — safe to run by hand:
#   bash @@PAYLOAD@@/install.sh <target>
# Editing it here is pointless: the next stage overwrites it.
set -euo pipefail
TARGET="${1:?usage: install.sh @@TARGET_LIST@@}"
IN=@@PAYLOAD@@
STAMP=$(date +%Y%m%d-%H%M%S)

case "$TARGET" in
@@TARGET_CASES@@
  *) echo "unknown target $TARGET (known: @@TARGET_LIST@@)"; exit 2 ;;
esac
SNAP=$ROOT/snapshots/$STAMP

echo "== [$TARGET] verifying the staged payload"
python3 - "$IN" <<'PY'
import hashlib, json, os, sys
inc = sys.argv[1]
m = json.load(open(os.path.join(inc, "MANIFEST.json")))
bad = []
for rel, meta in m["files"].items():
    p = os.path.join(inc, rel)
    if not os.path.exists(p):
        bad.append("missing " + rel); continue
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for c in iter(lambda: fh.read(1 << 20), b""):
            h.update(c)
    if h.hexdigest() != meta["sha256"]:
        bad.append("checksum " + rel)
if bad:
    sys.exit("payload is damaged: " + ", ".join(bad))
print("  %d files ok, staged %s from %s" % (len(m["files"]), m["created"], m["source_host"]))
if m["counts"]:
    print("  holds: " + ", ".join("%s %s" % (v, k) for k, v in m["counts"].items()))
PY
"""

INSTALL_ACME = r"""
if [ "$CERT_COPY" = yes ]; then
  # This target answers for @@PRIMARY@@, but it cannot *issue* that certificate
  # while DNS still points at the primary — the CA would validate against the
  # primary. So it serves a copy, refreshed on every run. Traefik reads its ACME
  # store only at startup and rewrites it from memory afterwards, so the merge
  # has to happen while the ingress is stopped.
  echo "== [$TARGET] merging the primary's @@PRIMARY@@ certificate into @@ACME_REMOTE@@"
  @@INGRESS_DC@@ stop >/dev/null 2>&1 || true
  python3 - "$IN/acme.json" "@@ACME_REMOTE@@" <<'PY'
import json, os, sys
src, dst_p = json.load(open(sys.argv[1])), sys.argv[2]
dst = {}
if os.path.exists(dst_p) and os.path.getsize(dst_p):
    try:
        dst = json.load(open(dst_p)) or {}
    except ValueError:
        dst = {}
for resolver, sdata in src.items():
    d = dst.setdefault(resolver, {})
    if not d.get("Account"):                      # keep the standby's own ACME account
        d["Account"] = sdata.get("Account")
    certs = dict((c["domain"]["main"], c) for c in (d.get("Certificates") or []))
    for c in (sdata.get("Certificates") or []):
        certs[c["domain"]["main"]] = c
    d["Certificates"] = list(certs.values())
    print("   %s: %s" % (resolver, ", ".join(sorted(certs))))
os.makedirs(os.path.dirname(dst_p), exist_ok=True)
with open(dst_p, "w") as fh:
    json.dump(dst, fh)
os.chmod(dst_p, 0o600)
PY
  @@INGRESS_DC@@ up -d >/dev/null
fi
"""

INSTALL_IMAGES = r"""
if [ -s "$IN/images.txt" ]; then
  echo "== pinning images to the digests the primary runs"
  while read -r repo digest; do
    [ -z "$repo" ] && continue
    docker pull -q "$repo@$digest" >/dev/null
    docker tag "$repo@$digest" "$repo:latest"
    echo "  $repo -> ${digest:0:19}…"
  done < "$IN/images.txt"
  # Retagging orphans the previous image. A standby usually has no watchtower to
  # tidy up after itself (deliberately — image versions come from the primary),
  # so without this it slowly fills its disk and then cannot install anything.
  # Only untagged images are removed.
  freed=$(docker image prune -f 2>/dev/null | tail -1)
  [ -n "$freed" ] && echo "  prune: $freed"
fi
"""

INSTALL_TAIL = r"""
if [ "$START" = yes ]; then
  echo "== [$TARGET] starting"
  $DC up -d
  sleep 5
  $DC ps --format 'table {{.Name}}\t{{.Status}}'
else
  echo "== [$TARGET] left stopped on purpose — 'clone_standby.py drill' or failover.sh starts it"
fi
echo "== [$TARGET] done; previous state kept in $SNAP"
"""

FAILOVER_SH = r"""#!/bin/bash
# EMERGENCY FAILOVER — run this on the standby when @@PRIMARY@@ is down.
#   bash @@ROOT@@/failover.sh
# Starts the clone, which then answers for @@PRIMARY@@ behind this host's ingress.
# Other stacks here can keep running; they do not collide.
#
# Then, in DNS:  A @@PRIMARY@@ -> @@STANDBY_IP@@
#                and DELETE any AAAA record unless this host has that same IPv6.
# A stale AAAA is the step people forget: dual-stack clients keep going to the
# dead machine and the failover looks broken.
set -euo pipefail
echo "== starting the clone (@@PRIMARY@@)"
@@DC@@ up -d
echo "== waiting for the management API"
code=""
for _ in $(seq 1 30); do
  code=$(curl -s -o /dev/null -w '%{http_code}' -k --resolve @@PRIMARY@@:443:127.0.0.1 \
         https://@@PRIMARY@@/api/accounts || true)
  if [ "$code" = "401" ] || [ "$code" = "200" ]; then break; fi
  sleep 2
done
echo "  API answers HTTP ${code:-none}"
sleep 2
@@DC@@ ps --format 'table {{.Name}}\t{{.Status}}'
echo
echo "Now change DNS:  A @@PRIMARY@@ -> @@STANDBY_IP@@"
echo "                 DELETE the AAAA record for @@PRIMARY@@ (unless it points here)"
"""


def _target_case(cfg: Config, t: Target) -> str:
    files = f"files-{t.name}" if t.host != cfg.primary_host else "files"
    return (
        f'  {t.name}) ROOT={t.root}; DC="{t.dc}"; FILES={files}; '
        f"DBDIR={t.db_dir}; START={'yes' if t.autostart else 'no'}; "
        f"CERT_COPY={'yes' if t.cert_copy else 'no'} ;;"
    )


def _shared_block(cfg: Config) -> str:
    if not cfg.shared_paths:
        return ""
    lines = ["", 'echo "== refreshing the shared assets on this host"']
    for i, spec in enumerate(cfg.shared_paths):
        src = f'"$IN/shared/{i}"'
        if spec.is_dir:
            if spec.mode == "seed":
                lines += [
                    f"mkdir -p {spec.dest}",
                    f"cp -an {src}/. {spec.dest}/ 2>/dev/null || true"
                    f"   # seed only, never overwrite",
                ]
            else:
                lines += [f"mkdir -p {spec.dest}", f"rsync -a --delete {src}/ {spec.dest}/"]
        elif spec.mode == "seed":
            lines += [
                f'mkdir -p "$(dirname {spec.dest})"',
                f"[ -f {spec.dest} ] || install -m 644 {src} {spec.dest}",
            ]
        else:
            lines += [
                f'mkdir -p "$(dirname {spec.dest})"',
                f"install -m 644 {src} {spec.dest}",
            ]
        lines.append(f'echo "  {spec.dest} ({spec.mode})"')
    return "\n".join(lines) + "\n"


def _target_paths_block(cfg: Config) -> str:
    if not cfg.target_paths:
        return ""
    lines = []
    for i, spec in enumerate(cfg.target_paths):
        src = f'"$IN/target/{i}"'
        dest = f'"$ROOT/{spec.dest}"'
        if spec.is_dir:
            if spec.mode == "seed":
                lines += [f"mkdir -p {dest}", f"cp -an {src}/. {dest}/ 2>/dev/null || true"]
            else:
                lines += [f"mkdir -p {dest}", f"rsync -a --delete {src}/ {dest}/"]
        elif spec.mode == "seed":
            lines += [
                f'mkdir -p "$(dirname {dest})"',
                f"[ -f {dest} ] || install -m 600 {src} {dest}",
            ]
        else:
            lines += [f'mkdir -p "$(dirname {dest})"', f"install -m 600 {src} {dest}"]
    return "\n".join(lines) + "\n"


def render_install(cfg: Config) -> str:
    body = INSTALL_HEAD
    body += _shared_block(cfg)
    body += rf"""
echo "== [$TARGET] stopping its stack"
$DC down --remove-orphans || true

echo "== [$TARGET] keeping the current state in $SNAP"
mkdir -p "$SNAP/db" "$ROOT/$DBDIR"
# `[ -f x ] && cp` as the last command of a loop makes the loop exit non-zero
# when the last file is missing, which under `set -e` would abort the very
# first install (nothing to snapshot yet). Hence if/fi.
for f in "$ROOT/$DBDIR"/*.db; do
  if [ -e "$f" ]; then cp -a "$f" "$SNAP/db/"; fi
done
for f in "$IN"/files/*; do
  b=$(basename "$f")
  if [ -f "$ROOT/$b" ]; then cp -a "$ROOT/$b" "$SNAP/$b"; fi
done
ls -1dt "$ROOT"/snapshots/*/ 2>/dev/null | tail -n +{cfg.keep_snapshots + 1} | xargs -r rm -rf

echo "== [$TARGET] installing"
for f in "$IN"/db/*; do install -m 600 "$f" "$ROOT/$DBDIR/$(basename "$f")"; done
for f in "$IN/$FILES"/*; do install -m 600 "$f" "$ROOT/$(basename "$f")"; done
"""
    body += _target_paths_block(cfg)
    if cfg.acme_source and cfg.acme_remote and cfg.ingress_root:
        body += INSTALL_ACME
    body += INSTALL_IMAGES
    body += INSTALL_TAIL

    subs = {
        "@@PAYLOAD@@": cfg.payload_dir,
        "@@TARGET_LIST@@": "|".join(cfg.names),
        "@@TARGET_CASES@@": "\n".join(_target_case(cfg, t) for t in cfg.targets),
        "@@PRIMARY@@": cfg.primary_host,
        "@@ACME_REMOTE@@": cfg.acme_remote,
        "@@INGRESS_DC@@": cfg.ingress_dc if cfg.ingress_root else "true",
    }
    for token, value in subs.items():
        body = body.replace(token, value)
    return body


def render_failover(cfg: Config) -> str:
    subs = {
        "@@PRIMARY@@": cfg.primary_host,
        "@@ROOT@@": cfg.failover.root,
        "@@DC@@": cfg.failover.dc,
        "@@STANDBY_IP@@": cfg.ip(),
    }
    body = FAILOVER_SH
    for token, value in subs.items():
        body = body.replace(token, value)
    return body


def write_scripts(cfg: Config, stage_dir: str) -> None:
    for name, body in (("install.sh", render_install(cfg)), ("failover.sh", render_failover(cfg))):
        path = os.path.join(stage_dir, name)
        with open(path, "w") as fh:
            fh.write(body)
        os.chmod(path, 0o700)


# ------------------------------------------------------------------- install


def staged_manifest(cfg: Config) -> dict:
    raw = cfg.remote.run(f"cat {cfg.payload_dir}/MANIFEST.json 2>/dev/null", check=False)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise SystemExit(
            "nothing staged on the standby — run 'clone_standby.py stage' first"
        ) from None


def install(cfg: Config, args) -> int:
    manifest = staged_manifest(cfg)
    targets = [cfg.target(args.target)] if args.target else cfg.targets
    print(f"Staged payload: {manifest['created']} from {manifest['source_host']}")
    if manifest.get("counts"):
        print("  " + ", ".join(f"{v} {k}" for k, v in manifest["counts"].items()))
    for t in targets:
        print(f"  -> {t.name:<8} {t.root}  ({t.description})")
    restarts = [t.name for t in targets if t.autostart]
    print(
        "\nThe primary is not touched."
        + (f" Restarts on the standby: {', '.join(restarts)}." if restarts else "")
    )
    if not args.yes and input("Proceed? [y/N] ").strip().lower() not in ("y", "yes"):
        print("aborted")
        return 1
    if args.dry_run:
        print(
            f"DRY RUN — would run: bash {cfg.payload_dir}/install.sh <{', '.join(t.name for t in targets)}>"
        )
        return 0
    for t in targets:
        print()
        if cfg.remote.run_live(f"bash {cfg.payload_dir}/install.sh {t.name}") != 0:
            print(
                f"\ninstall FAILED for {t.name} — roll back with: clone_standby.py rollback {t.name}",
                file=sys.stderr,
            )
            return 1
    running = [t for t in targets if t.autostart]
    if running and cfg.nb_token:
        print()
        return verify(cfg, argparse.Namespace(**{**vars(args), "target": running[0].name}))
    return 0


# --------------------------------------------------------------------- drill


def drill(cfg: Config, args) -> int:
    t = cfg.target(args.target) if args.target else cfg.failover
    others = [o.host for o in cfg.targets if o is not t and o.autostart]
    print(
        f"Drill: start {t.name} → verify the failover path → stop it again.\n"
        "The primary is not touched" + (f"; {', '.join(others)} keeps running." if others else ".")
    )
    if not args.yes and input("Proceed? [y/N] ").strip().lower() not in ("y", "yes"):
        print("aborted")
        return 1
    rc = 1
    try:
        print(f"== starting {t.name} ({t.host})")
        cfg.remote.run_live(
            f"{t.dc} up -d && sleep 6 && {t.dc} ps --format 'table {{{{.Name}}}}\t{{{{.Status}}}}'"
        )
        print()
        rc = verify(cfg, argparse.Namespace(**{**vars(args), "target": t.name}))
    finally:
        print(f"\n== stopping {t.name} again")
        cfg.remote.run_live(f"{t.dc} down --remove-orphans")
    print("\nDrill " + ("passed." if rc == 0 else "FAILED — see above."))
    return rc


def failover(cfg: Config, args) -> int:
    t = cfg.target(args.target) if args.target else cfg.failover
    print(f"FAILOVER: start {t.name} ({t.host}) and leave it running, then move DNS.")
    if not args.yes and input("Proceed? [y/N] ").strip().lower() not in ("y", "yes"):
        print("aborted")
        return 1
    return cfg.remote.run_live(f"bash {t.root}/failover.sh")


# --------------------------------------------------------------------- verify


def https_json(ip: str, host: str, path: str, token: str, timeout: int = 20):
    """GET https://<host><path> but connect to <ip>: the failover path, without touching DNS."""
    ctx = ssl.create_default_context()
    sock = ctx.wrap_socket(
        socket.create_connection((ip, 443), timeout=timeout), server_hostname=host
    )
    cert = sock.getpeercert()
    conn = http.client.HTTPConnection(host, 443, timeout=timeout)
    conn.sock = sock
    conn.request(
        "GET", path, headers={"Authorization": f"Token {token}", "Accept": "application/json"}
    )
    resp = conn.getresponse()
    body, status = resp.read().decode(), resp.status
    conn.close()
    return status, (json.loads(body) if body.strip().startswith(("{", "[")) else body), cert


def primary_json(cfg: Config, path: str):
    req = urllib.request.Request(f"{cfg.nb_url}/api/{path}")
    req.add_header("Authorization", f"Token {cfg.nb_token}")
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def verify(cfg: Config, args) -> int:
    t = cfg.target(args.target) if args.target else cfg.failover
    if not cfg.nb_token:
        raise SystemExit("NB_API_KEY must be set to verify the standby against the primary")
    ip = cfg.ip()
    print(f"Verifying [{t.name}]: https://{t.host} on {ip} — {t.description}")
    try:
        status, accounts, cert = https_json(ip, t.host, "/api/accounts", cfg.nb_token)
    except (OSError, ssl.SSLError) as e:
        print(f"  cannot reach it: {e}")
        print("  (is that stack running? clone_standby.py status)")
        return 1
    subject = dict(x[0] for x in cert.get("subject", ()))
    print(
        f"  TLS   {subject.get('commonName', '?')}  valid until {cert.get('notAfter')}"
        "  (chain verified against the system trust store)"
    )
    if status != 200:
        print(f"  API   HTTP {status} — it did not accept the primary's token")
        return 1

    endpoints = ("peers", "groups", "policies", "networks", "users", "setup-keys", "posture-checks")
    try:
        rows = [("account id", primary_json(cfg, "accounts")[0]["id"], accounts[0]["id"])]
        for ep in endpoints:
            rows.append(
                (
                    ep,
                    len(primary_json(cfg, ep)),
                    len(https_json(ip, t.host, f"/api/{ep}", cfg.nb_token)[1]),
                )
            )
    except (urllib.error.URLError, OSError) as e:
        print(f"  could not read the primary for comparison: {e}")
        return 1

    width = max(24, *(len(str(p)) for _, p, _ in rows))
    ok = True
    print(f"\n  {'':<16}{'primary':>{width}}{'standby':>{width}}")
    for name, p, s in rows:
        ok = ok and p == s
        print(f"  {name:<16}{str(p):>{width}}{str(s):>{width}}   {'ok' if p == s else 'DIFFERS'}")
    if t is cfg.failover:
        print(
            "\n"
            + (
                "The clone is faithful — moving the A record would just work."
                if ok
                else "The clone does NOT match the primary."
            )
        )
    else:
        print("\n" + ("Matches the primary." if ok else "Does NOT match the primary."))
    return 0 if ok else 1


# --------------------------------------------------------- status / rollback


def status(cfg: Config, args) -> int:
    ps = " ps --format 'table {{.Name}}\t{{.Status}}' 2>/dev/null | sed 's/^/  /'"
    print("--- staged payload")
    try:
        m = json.loads(
            cfg.remote.run(f"cat {cfg.payload_dir}/MANIFEST.json 2>/dev/null", check=False)
        )
        print(f"   {m['created']} from {m['source_host']}")
        if m.get("counts"):
            print("   counts: " + ", ".join(f"{v} {k}" for k, v in m["counts"].items()))
    except json.JSONDecodeError:
        print("   nothing staged")

    if cfg.ingress_root:
        print(f"\n--- ingress ({cfg.ingress_root})")
        if cfg.acme_remote:
            # grep only the domain names: the ACME store also holds private keys,
            # which must never travel back over this connection or into a log.
            doms = cfg.remote.run(
                f"""grep -o '"main": *"[^"]*"' {cfg.acme_remote} 2>/dev/null """
                f"""| sed 's/.*"\\(.*\\)"/\\1/' | sort -u | tr '\\n' ' '""",
                check=False,
            )
            print(f"   certificates: {doms or 'none'}")
        print(cfg.remote.run(cfg.ingress_dc + ps, check=False))

    for t in cfg.targets:
        print(f"\n--- {t.name}: {t.root} ({t.description})")
        print(
            cfg.remote.run(
                f"""ls -l --time-style=+%Y-%m-%d\\ %H:%M {t.root}/{t.db_dir}/*.db 2>/dev/null \
  | awk '{{print "  "$NF"  "$6" "$7"  "$5" bytes"}}'
ls -1dt {t.root}/snapshots/*/ 2>/dev/null | head -3 | sed 's/^/  rollback: /'
{t.dc}{ps}""",
                check=False,
            )
        )
    return 0


def rollback(cfg: Config, args) -> int:
    t = cfg.target(args.target) if args.target else cfg.failover
    snaps = cfg.remote.run(f"ls -1dt {t.root}/snapshots/*/ 2>/dev/null | head -3", check=False)
    if not snaps:
        raise SystemExit(f"no snapshots for {t.name} on the standby")
    latest = snaps.splitlines()[0].rstrip("/")
    print(f"Rolling {t.name} back to {latest}")
    if not args.yes and input("Proceed? [y/N] ").strip().lower() not in ("y", "yes"):
        print("aborted")
        return 1
    start = "up -d" if t.autostart else "ps"
    return cfg.remote.run_live(
        f"""set -euo pipefail
{t.dc} down --remove-orphans || true
for f in "{latest}"/db/*; do
  if [ -e "$f" ]; then install -m600 "$f" {t.root}/{t.db_dir}/$(basename "$f"); fi
done
for f in "{latest}"/*; do
  if [ -f "$f" ]; then install -m600 "$f" {t.root}/$(basename "$f"); fi
done
{t.dc} {start}
"""
    )


# ------------------------------------------------------------------ cron path


def run_cycle(cfg: Config, args) -> int:
    """stage → install every target → drill. What the CRON_CLONE_STANDBY schedule runs."""
    steps: list[tuple[str, Callable[[], int]]] = [("stage", lambda: stage(cfg, args))]
    for t in cfg.targets:
        steps.append(
            (
                f"install {t.name}",
                lambda name=t.name: install(
                    cfg, argparse.Namespace(**{**vars(args), "target": name, "yes": True})
                ),
            )
        )
    if cfg.nb_token:
        steps.append(
            ("drill", lambda: drill(cfg, argparse.Namespace(**{**vars(args), "yes": True})))
        )

    started = datetime.datetime.now()
    for name, step in steps:
        print(f"\n===== {name}")
        try:
            rc = step()
        # Deliberately broad: an unattended job that dies on something nobody
        # predicted must still alert. A bug that only prints a traceback into
        # the container log leaves the standby quietly rotting.
        except (SystemExit, Exception) as e:  # noqa: B014
            rc = 1
            detail = str(e) if isinstance(e, SystemExit) else f"{type(e).__name__}: {e}"
        else:
            detail = f"step '{name}' exited {rc}"
        if rc != 0:
            _log(f"FAILED at {name}: {detail}")
            checkmk.write(
                SPOOL_FILE,
                CHECK_NAME,
                checkmk.CRIT,
                f"refresh FAILED at {name} ({started:%Y-%m-%d %H:%M}): {detail[:160]}",
                SPOOL_MAX_AGE,
            )
            return 1
    took = (datetime.datetime.now() - started).seconds
    checkmk.write(
        SPOOL_FILE,
        CHECK_NAME,
        checkmk.OK,
        f"clone refreshed and drill passed at {started:%Y-%m-%d %H:%M} ({took}s)",
        SPOOL_MAX_AGE,
    )
    print(f"\n===== ok in {took}s")
    return 0


ACTIONS = {
    "stage": stage,
    "install": install,
    "drill": drill,
    "failover": failover,
    "verify": verify,
    "status": status,
    "rollback": rollback,
    "run": run_cycle,
}


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("action", nargs="?", default="stage", choices=sorted(ACTIONS))
    ap.add_argument("target", nargs="?", default=None, help="one of CLONE_TARGETS")
    ap.add_argument("--yes", "-y", action="store_true", help="do not ask for confirmation")
    ap.add_argument("--dry-run", action="store_true", help="stage locally, ship nothing")
    args = ap.parse_args(argv if argv is not None else sys.argv[1:])
    if args.action == "run":
        args.yes = True
        # The scheduled path must alert on a broken configuration too, not only
        # on a step that fails: an unmounted path or a renamed target would
        # otherwise disappear into the container log.
        try:
            return run_cycle(load_config(), args)
        except SystemExit as e:
            detail = str(e)
            _log(f"FAILED before the first step: {detail}")
            checkmk.write(
                SPOOL_FILE,
                CHECK_NAME,
                checkmk.CRIT,
                f"misconfigured: {detail[:160]}",
                SPOOL_MAX_AGE,
            )
            return 1
    cfg = load_config()
    try:
        return ACTIONS[args.action](cfg, args)
    except RemoteError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
