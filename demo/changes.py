"""Make a few config changes in two rounds so History and Audit have content.
Each round waits for the forwarder's automatic snapshot."""

import time
from pathlib import Path

from seed import SECRETS, Api, read_env

HIST = Path(__file__).parent / "data/birdseye/jobs/history"
api = Api(read_env(SECRETS)["NB_API_KEY"])
groups = {g["name"]: g for g in api.get("groups")}
peers = {p["name"]: p["id"] for p in api.get("peers")}
pols = {p["name"]: p for p in api.get("policies")}


def ids(items):
    return [i["id"] if isinstance(i, dict) else i for i in items or []]


def put_policy(p, **changes):
    body = {
        "name": p["name"],
        "description": p.get("description") or "",
        "enabled": p["enabled"],
        "source_posture_checks": ids(p.get("source_posture_checks")),
        "rules": [],
    }
    for r in p["rules"]:
        rule = {
            k: r[k]
            for k in ("id", "name", "description", "enabled", "action", "protocol", "bidirectional")
        }
        rule["sources"] = ids(r.get("sources"))
        if r.get("destinationResource"):
            rule["destinationResource"] = {
                "id": r["destinationResource"]["id"],
                "type": r["destinationResource"]["type"],
            }
        else:
            rule["destinations"] = ids(r.get("destinations"))
        if r.get("ports"):
            rule["ports"] = r["ports"]
        body["rules"].append(rule)
    body = changes.pop("fn", lambda b: b)(body)
    api.c.put(f"policies/{p['id']}", json={**body, **changes}).raise_for_status()
    print("  ~ policy", p["name"])


def put_group(name, add=()):
    g = api.get(f"groups/{groups[name]['id']}")
    body = {
        "name": g["name"],
        "peers": sorted(set(ids(g.get("peers"))) | {peers[x] for x in add}),
        "resources": [{"id": r["id"], "type": r["type"]} for r in g.get("resources") or []],
    }
    api.c.put(f"groups/{g['id']}", json=body).raise_for_status()
    print("  ~ group", name)


def wait_snapshot():
    before = len(list(HIST.iterdir()))
    for _ in range(60):
        time.sleep(2)
        if len(list(HIST.iterdir())) > before:
            print("  snapshot taken")
            return
    print("  (no snapshot yet)")


def add_port(port):
    def fn(body):
        for r in body["rules"]:
            r["ports"] = [*r.get("ports", []), port]
        return body

    return fn


def add_dest(group_id):
    def fn(body):
        for r in body["rules"]:
            r["destinations"] = [*r.get("destinations", []), group_id]
        return body

    return fn


def main():
    print("round 1")
    put_policy(pols["Office NAS"], fn=add_port("2049"), description="SMB and NFS")
    put_group("Support", add=["clara-laptop"])
    wait_snapshot()

    print("round 2")
    put_policy(pols["QA staging"], enabled=False, description="Paused until the next release")
    put_policy(pols["Admin SSH"], fn=add_dest(groups["Office-Router"]["id"]))
    wait_snapshot()


if __name__ == "__main__":
    main()
