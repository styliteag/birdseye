# birdseye-web — TODO

Every write follows the house rules: CSRF (`context.csrf_protect`),
`context.log_change()` before/after, `cache.invalidate()`, preview via
`diff.access_delta()` where access can change, re-`GET` before `PUT`.
Pure logic in its own tested module; routes only do HTTP. TDD.

## 1. Peers page

- [x] `models.Peer`: add fields needed for the page (hostname, OS, client version,
      last seen, connected, login expiration enabled/expired, SSH enabled,
      approval required, user id, IP, DNS label)
- [x] `routes_peers.py` + `peers.html` / `_peer_rows.html`: list with search,
      filter (online/offline, user, group, OS, expired login), sort by last seen
- [x] Peer detail/editor `peer_edit.html`: name, SSH on/off, login expiration
      on/off, approval (`PUT /peers/{id}`, start from fresh `GET`)
- [x] Peer groups in the editor: add/remove via group `PUT` (keep `resources`),
      with live access preview
- [x] Delete peer with confirm step and preview of lost access
- [x] Peer detail shows effective access (reuse `access.edges()` filtered to the peer)
- [x] Link peers from matrix, anomalies, user editor and reachability to the editor
- [x] Nav entry, `docs/web-ui.md` table row, CHANGELOG

## 2. Audit log view

- [ ] `nbapi`: fetch `/api/events/audit` (not part of the cached snapshot;
      own short cache or none)
- [ ] `models.AuditEvent` frozen dataclass parsed from raw dict
- [ ] `routes_audit.py` + `audit.html`: table, newest first, paging or limit
- [ ] Filters: initiator (user), activity type, target object, time range, free text
- [ ] Resolve IDs to names via snapshot; deleted objects shown by meta name
- [ ] Link each target to its editor (group, policy, user, peer, resource)
- [ ] Contract: router `prefix="/audit"`, `?target=<id>` filters by target
      (peer and setup key editors already link there)
- [ ] "History" link on group/policy/user/peer editors → audit filtered to that object
- [ ] Highlight changes made through birdseye-web (same user, matching time) — optional
- [ ] Nav entry, docs, CHANGELOG

## 3. Networks, resources and routers editor

- [ ] `routes_networks.py`: list networks with resources and routers
- [ ] Network create/rename/delete (`/networks`)
- [ ] Resource create/edit/delete (`/networks/{id}/resources`): address/domain,
      groups, enabled; preview of gained/lost access
- [ ] Router create/edit/delete (`/networks/{id}/routers`): peer or peer group,
      metric, masquerade, enabled
- [ ] Payload builders in `payloads.py` (raw dicts, IDs not objects), tested
- [ ] Anomalies "resource nobody routes" / "resource no policy reaches" link
      to the new editors
- [ ] Group `PUT` paths that touch resource groups keep `resources` (regression test)
- [ ] Contract: `GET /resources/{rid}` redirects to the resource editor
      (`ui.obj_link("r:<id>")` already links there)
- [ ] Nav entry, docs, CHANGELOG

## 4. More anomalies

All from data already in the snapshot unless noted. Each one gets a test and,
where possible, a link to the editor that fixes it.

- [ ] Stale peers: last seen > `WEB_STALE_PEER_DAYS` (default 30) → peer editor
- [ ] Peers with expired login → peer editor
- [ ] Outdated client versions: below newest seen version (or `WEB_MIN_CLIENT_VERSION`)
- [ ] Peers waiting for approval
- [ ] Setup keys: valid but never used, no expiry, reusable without usage limit,
      expired but not revoked → setup key page (item 6)
- [ ] Users without any device (excluding service users)
- [ ] Posture checks not used by any policy
- [ ] Respect `WEB_ANOMALY_IGNORE` where group names are involved
- [ ] Docs: anomaly list and new env vars

## 6. Setup keys page

- [x] `models.SetupKey` with type, expiry, usage count/limit, revoked, auto-groups,
      ephemeral, last used
- [x] `routes_setup_keys.py` + templates: list with state badges (valid,
      expired, revoked, exhausted)
- [x] Create key: name, type, expiry, usage limit, auto-groups, ephemeral;
      show the key value **once** after creation, never log it
- [x] Edit auto-groups and revoke (`PUT /setup-keys/{id}` from fresh `GET`)
- [x] Delete revoked keys
- [x] Hide page/actions for roles that may not manage keys
- [x] Security review: key value not in logs, `log_change()` lines, or cache
- [x] Nav entry, docs, CHANGELOG

## 7. Config history and rollback

- [ ] Decide storage: snapshots written by the birdseye container
      (`export_objects.py` as a cron job into the shared jobs dir) and read
      by the web side — keeps the "web runs nothing itself" split
- [ ] Format: one JSON per object type per run, plus manifest; retention setting
- [ ] Pure `history.py`: diff two snapshots per object (added/removed/changed
      fields), tested
- [ ] `routes_history.py` + `history.html`: pick two dates, show diff grouped
      by object type; per-object history from item 2 links here
- [ ] Restore a single policy or group from a snapshot: build payload from old
      version, preview access delta against current state, confirm, write
- [ ] Handle restore of deleted objects (create) versus changed (update);
      referenced groups/posture checks that no longer exist → block with message
- [ ] Docs: setup of the snapshot job, env vars, CHANGELOG
