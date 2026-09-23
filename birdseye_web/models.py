"""Immutable view of one NetBird account, parsed from raw API dicts.

Only the fields the access resolver and the editors need are kept. Parsing
is tolerant: the API returns `null` for empty lists, and embedded group
objects (`{"id", "name", ...}`) are flattened to IDs.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

Raw = Mapping[str, Any]


@dataclass(frozen=True)
class PortRange:
    start: int
    end: int

    def label(self) -> str:
        return str(self.start) if self.start == self.end else f"{self.start}-{self.end}"


@dataclass(frozen=True)
class Service:
    """Protocol plus port set. Empty `ports` means every port."""

    protocol: str
    ports: tuple[PortRange, ...] = ()

    def label(self) -> str:
        if self.protocol == "all":
            return "all"
        if not self.ports:
            return self.protocol
        return f"{self.protocol}/" + ",".join(p.label() for p in self.ports)


@dataclass(frozen=True)
class Ref:
    """Endpoint that is not a group: a peer or a network resource."""

    kind: str  # "peer" | "resource"
    id: str


@dataclass(frozen=True)
class Peer:
    id: str
    name: str
    ip: str
    user_id: str
    group_ids: frozenset[str]
    connected: bool = False
    os: str = ""


@dataclass(frozen=True)
class Group:
    id: str
    name: str
    peer_ids: frozenset[str]
    resource_ids: frozenset[str]
    issued: str = "api"

    @property
    def is_all(self) -> bool:
        return self.name == "All"


@dataclass(frozen=True)
class User:
    id: str
    name: str
    email: str
    role: str
    auto_groups: frozenset[str]
    is_service_user: bool = False
    is_blocked: bool = False

    @property
    def label(self) -> str:
        return self.name or self.email or self.id


@dataclass(frozen=True)
class Resource:
    id: str
    name: str
    address: str
    type: str  # "host" | "subnet" | "domain"
    network_id: str
    group_ids: frozenset[str]
    enabled: bool = True


@dataclass(frozen=True)
class Network:
    id: str
    name: str
    router_peer_ids: frozenset[str] = frozenset()
    router_group_ids: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Rule:
    id: str
    name: str
    enabled: bool
    action: str
    bidirectional: bool
    service: Service
    source_group_ids: tuple[str, ...]
    destination_group_ids: tuple[str, ...]
    source_ref: Ref | None = None
    destination_ref: Ref | None = None
    description: str = ""


@dataclass(frozen=True)
class Policy:
    id: str
    name: str
    enabled: bool
    rules: tuple[Rule, ...]
    posture_check_ids: tuple[str, ...] = ()
    description: str = ""


@dataclass(frozen=True)
class PostureCheck:
    id: str
    name: str


@dataclass(frozen=True)
class Snapshot:
    peers: Mapping[str, Peer] = field(default_factory=dict)
    groups: Mapping[str, Group] = field(default_factory=dict)
    users: Mapping[str, User] = field(default_factory=dict)
    resources: Mapping[str, Resource] = field(default_factory=dict)
    networks: Mapping[str, Network] = field(default_factory=dict)
    policies: Mapping[str, Policy] = field(default_factory=dict)
    posture_checks: Mapping[str, PostureCheck] = field(default_factory=dict)
    # groups that setup keys put new peers into (only visible to admins)
    setup_key_group_ids: frozenset[str] = frozenset()

    def names(self) -> dict[str, str]:
        """Display name for any group, resource or peer ID."""
        return {
            **{g.id: g.name for g in self.groups.values()},
            **{r.id: r.name for r in self.resources.values()},
            **{p.id: p.name for p in self.peers.values()},
        }

    def endpoint_name(self, ref: Ref) -> str:
        if ref.kind == "peer":
            peer = self.peers.get(ref.id)
            return peer.name if peer else ref.id
        res = self.resources.get(ref.id)
        return res.name if res else ref.id


# --- parsing -----------------------------------------------------------------


def _ids(items: Iterable[Any] | None) -> tuple[str, ...]:
    """Flatten `[{"id": ...}, ...]` or `["id", ...]` to a tuple of IDs."""
    out: list[str] = []
    for item in items or ():
        if isinstance(item, Mapping):
            out.append(str(item["id"]))
        elif item:
            out.append(str(item))
    return tuple(out)


def _ref(raw: Raw | None) -> Ref | None:
    if not raw or not raw.get("id"):
        return None
    kind = "peer" if raw.get("type") == "peer" else "resource"
    return Ref(kind=kind, id=str(raw["id"]))


def parse_service(raw: Raw) -> Service:
    protocol = str(raw.get("protocol") or "all")
    if protocol == "all":
        return Service("all")
    ranges = [PortRange(int(p), int(p)) for p in raw.get("ports") or ()]
    ranges += [PortRange(int(r["start"]), int(r["end"])) for r in raw.get("port_ranges") or ()]
    return Service(protocol, tuple(sorted(set(ranges), key=lambda r: (r.start, r.end))))


def parse_rule(raw: Raw) -> Rule:
    return Rule(
        id=str(raw.get("id") or ""),
        name=str(raw.get("name") or ""),
        enabled=bool(raw.get("enabled", True)),
        action=str(raw.get("action") or "accept"),
        bidirectional=bool(raw.get("bidirectional", False)),
        service=parse_service(raw),
        source_group_ids=_ids(raw.get("sources")),
        destination_group_ids=_ids(raw.get("destinations")),
        source_ref=_ref(raw.get("sourceResource")),
        destination_ref=_ref(raw.get("destinationResource")),
        description=str(raw.get("description") or ""),
    )


def parse_policy(raw: Raw) -> Policy:
    return Policy(
        id=str(raw["id"]),
        name=str(raw.get("name") or ""),
        enabled=bool(raw.get("enabled", True)),
        rules=tuple(parse_rule(r) for r in raw.get("rules") or ()),
        posture_check_ids=_ids(raw.get("source_posture_checks")),
        description=str(raw.get("description") or ""),
    )


def parse_group(raw: Raw) -> Group:
    return Group(
        id=str(raw["id"]),
        name=str(raw.get("name") or ""),
        peer_ids=frozenset(_ids(raw.get("peers"))),
        resource_ids=frozenset(_ids(raw.get("resources"))),
        issued=str(raw.get("issued") or "api"),
    )


def parse_peer(raw: Raw) -> Peer:
    return Peer(
        id=str(raw["id"]),
        name=str(raw.get("name") or raw.get("hostname") or raw["id"]),
        ip=str(raw.get("ip") or ""),
        user_id=str(raw.get("user_id") or ""),
        group_ids=frozenset(_ids(raw.get("groups"))),
        connected=bool(raw.get("connected", False)),
        os=str(raw.get("os") or ""),
    )


def parse_user(raw: Raw) -> User:
    return User(
        id=str(raw["id"]),
        name=str(raw.get("name") or ""),
        email=str(raw.get("email") or ""),
        role=str(raw.get("role") or ""),
        auto_groups=frozenset(_ids(raw.get("auto_groups"))),
        is_service_user=bool(raw.get("is_service_user", False)),
        is_blocked=bool(raw.get("is_blocked", False)),
    )


def parse_resource(raw: Raw, network_id: str) -> Resource:
    return Resource(
        id=str(raw["id"]),
        name=str(raw.get("name") or ""),
        address=str(raw.get("address") or ""),
        type=str(raw.get("type") or ""),
        network_id=network_id,
        group_ids=frozenset(_ids(raw.get("groups"))),
        enabled=bool(raw.get("enabled", True)),
    )


def parse_network(raw: Raw, routers: Iterable[Raw]) -> Network:
    active = [r for r in routers if r.get("enabled", True)]
    return Network(
        id=str(raw["id"]),
        name=str(raw.get("name") or ""),
        router_peer_ids=frozenset(str(r["peer"]) for r in active if r.get("peer")),
        router_group_ids=frozenset(g for r in active for g in _ids(r.get("peer_groups"))),
    )


def build_snapshot(
    *,
    peers: Iterable[Raw] = (),
    groups: Iterable[Raw] = (),
    users: Iterable[Raw] = (),
    policies: Iterable[Raw] = (),
    posture_checks: Iterable[Raw] = (),
    networks: Iterable[tuple[Raw, Iterable[Raw], Iterable[Raw]]] = (),
    setup_keys: Iterable[Raw] = (),
) -> Snapshot:
    """`networks` items are `(network, resources, routers)` raw triples."""
    nets: dict[str, Network] = {}
    resources: dict[str, Resource] = {}
    for net_raw, res_raw, routers_raw in networks:
        net = parse_network(net_raw, list(routers_raw))
        nets[net.id] = net
        for r in res_raw:
            res = parse_resource(r, net.id)
            resources[res.id] = res
    return Snapshot(
        peers={p.id: p for p in map(parse_peer, peers)},
        groups={g.id: g for g in map(parse_group, groups)},
        users={u.id: u for u in map(parse_user, users)},
        resources=resources,
        networks=nets,
        policies={p.id: p for p in map(parse_policy, policies)},
        posture_checks={
            str(c["id"]): PostureCheck(str(c["id"]), str(c.get("name") or ""))
            for c in posture_checks
        },
        setup_key_group_ids=frozenset(
            g for k in setup_keys if not k.get("revoked") for g in _ids(k.get("auto_groups"))
        ),
    )
