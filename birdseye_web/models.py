"""Immutable view of one NetBird account, parsed from raw API dicts.

Only the fields the access resolver and the editors need are kept. Parsing
is tolerant: the API returns `null` for empty lists, and embedded group
objects (`{"id", "name", ...}`) are flattened to IDs.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
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
    hostname: str = ""
    version: str = ""
    last_seen: datetime | None = None
    dns_label: str = ""
    ssh_enabled: bool = False
    login_expiration_enabled: bool = False
    login_expired: bool = False
    inactivity_expiration_enabled: bool = False
    approval_required: bool = False


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
    description: str = ""


@dataclass(frozen=True)
class Router:
    """Routing peer (or peer groups) of a network."""

    id: str
    peer: str = ""
    peer_groups: tuple[str, ...] = ()
    metric: int = 9999
    masquerade: bool = False
    enabled: bool = True


@dataclass(frozen=True)
class Network:
    id: str
    name: str
    # from enabled routers only: what actually routes
    router_peer_ids: frozenset[str] = frozenset()
    router_group_ids: frozenset[str] = frozenset()
    description: str = ""
    routers: tuple[Router, ...] = ()


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
class SetupKey:
    """A setup key without its secret: the value is never parsed or kept."""

    id: str
    name: str
    type: str  # "one-off" | "reusable"
    expires: datetime | None
    revoked: bool
    used_times: int
    usage_limit: int  # 0 = unlimited
    auto_groups: frozenset[str]
    ephemeral: bool = False
    last_used: datetime | None = None
    api_state: str = ""  # NetBird's own: valid | expired | revoked | overused

    @property
    def reusable(self) -> bool:
        return self.type == "reusable"

    def state_at(self, now: datetime) -> str:
        """valid | revoked | expired | exhausted (usage limit reached)."""
        if self.revoked or self.api_state == "revoked":
            return "revoked"
        if self.api_state == "expired" or (self.expires is not None and self.expires <= now):
            return "expired"
        limit = self.usage_limit if self.reusable else 1
        if self.api_state == "overused" or (limit and self.used_times >= limit):
            return "exhausted"
        return "valid"


@dataclass(frozen=True)
class Snapshot:
    peers: Mapping[str, Peer] = field(default_factory=dict)
    groups: Mapping[str, Group] = field(default_factory=dict)
    users: Mapping[str, User] = field(default_factory=dict)
    resources: Mapping[str, Resource] = field(default_factory=dict)
    networks: Mapping[str, Network] = field(default_factory=dict)
    policies: Mapping[str, Policy] = field(default_factory=dict)
    posture_checks: Mapping[str, PostureCheck] = field(default_factory=dict)
    # only visible to roles that may read setup keys
    setup_keys: Mapping[str, SetupKey] = field(default_factory=dict)
    # groups that setup keys put new peers into (only visible to admins)
    setup_key_group_ids: frozenset[str] = frozenset()
    # account setting: a user's auto-group change also moves their existing
    # peers. None = account settings not readable for this user.
    groups_propagation: bool | None = None

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


def parse_time(value: Any) -> datetime | None:
    """RFC3339 timestamp (`Z` or offset, any fraction length) as aware UTC."""
    if not value or not isinstance(value, str):
        return None
    text = value.strip().replace("Z", "+00:00")
    # Go emits nanoseconds; Python parses at most microseconds.
    text = re.sub(r"(\.\d{6})\d+", r"\1", text)
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.year <= 1:  # Go zero time: "never"
        return None
    return dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)


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
        hostname=str(raw.get("hostname") or ""),
        version=str(raw.get("version") or ""),
        last_seen=parse_time(raw.get("last_seen")),
        dns_label=str(raw.get("dns_label") or ""),
        ssh_enabled=bool(raw.get("ssh_enabled", False)),
        login_expiration_enabled=bool(raw.get("login_expiration_enabled", False)),
        login_expired=bool(raw.get("login_expired", False)),
        inactivity_expiration_enabled=bool(raw.get("inactivity_expiration_enabled", False)),
        approval_required=bool(raw.get("approval_required", False)),
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
        description=str(raw.get("description") or ""),
    )


def parse_router(raw: Raw) -> Router:
    return Router(
        id=str(raw.get("id") or ""),
        peer=str(raw.get("peer") or ""),
        peer_groups=_ids(raw.get("peer_groups")),
        metric=int(raw.get("metric") or 9999),
        masquerade=bool(raw.get("masquerade", False)),
        enabled=bool(raw.get("enabled", True)),
    )


def parse_setup_key(raw: Raw) -> SetupKey:
    return SetupKey(
        id=str(raw["id"]),
        name=str(raw.get("name") or ""),
        type=str(raw.get("type") or ""),
        expires=parse_time(raw.get("expires")),
        revoked=bool(raw.get("revoked", False)),
        used_times=int(raw.get("used_times") or 0),
        usage_limit=int(raw.get("usage_limit") or 0),
        auto_groups=frozenset(_ids(raw.get("auto_groups"))),
        ephemeral=bool(raw.get("ephemeral", False)),
        last_used=parse_time(raw.get("last_used")),
        api_state=str(raw.get("state") or ""),
    )


def parse_network(raw: Raw, routers: Iterable[Raw]) -> Network:
    parsed = tuple(parse_router(r) for r in routers)
    active = [r for r in parsed if r.enabled]
    return Network(
        id=str(raw["id"]),
        name=str(raw.get("name") or ""),
        router_peer_ids=frozenset(r.peer for r in active if r.peer),
        router_group_ids=frozenset(g for r in active for g in r.peer_groups),
        description=str(raw.get("description") or ""),
        routers=parsed,
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
    accounts: Iterable[Raw] = (),
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
    keys = [parse_setup_key(k) for k in setup_keys]
    settings = next((a.get("settings") or {} for a in accounts), {})
    propagation = settings.get("groups_propagation_enabled")
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
        setup_keys={k.id: k for k in keys},
        setup_key_group_ids=frozenset(g for k in keys if not k.revoked for g in k.auto_groups),
        groups_propagation=None if propagation is None else bool(propagation),
    )
