from datetime import UTC, datetime

from birdseye_web.history_view import Change, flat_changes, snapshot_status

T = lambda h, m=0: datetime(2026, 10, 1, h, m, tzinfo=UTC)  # noqa: E731


def test_scalar_and_nested_dict():
    assert flat_changes("name", "a", "b") == [Change(("name",), "value", "a", "b")]
    assert flat_changes("x", {"a": 1, "b": 2}, {"a": 1, "b": 3}) == [
        Change(("x", "b"), "value", 2, 3)
    ]


def test_scalar_list_shows_added_and_removed_items():
    [c] = flat_changes("ports", ["80", "443"], ["80", "443", "8443"])
    assert c.kind == "list" and c.added == ("8443",) and c.removed == () and c.kept == ("80", "443")


def test_list_of_objects_matched_by_id_and_labelled_by_name():
    old = [{"id": "r1", "name": "web", "ports": ["80"]}, {"id": "r2", "name": "gone"}]
    new = [{"id": "r1", "name": "web", "ports": ["80", "443"]}, {"id": "r3", "name": "new"}]
    out = flat_changes("rules", old, new)
    assert (
        Change(("rules", "web", "ports"), "list", ["80"], ["80", "443"], ("443",), (), ("80",))
        in out
    )
    assert Change(("rules", "gone"), "removed", old[1], None) in out
    assert Change(("rules", "new"), "added", None, new[1]) in out


def test_snapshot_status():
    # change after newest snapshot and after last run -> pending
    assert snapshot_status([T(17, 40)], T(17, 0), job_enabled=True).state == "pending"
    # last run started after the change (found nothing new) -> current
    st = snapshot_status([T(17, 40)], T(17, 0), job_enabled=True, last_run=T(17, 42))
    assert st.state == "current"
    assert snapshot_status([T(17, 40)], T(17, 0), job_enabled=True, running=True).state == "running"
    assert snapshot_status([T(17, 40)], T(17, 0), job_enabled=False).state == "unscheduled"
    assert snapshot_status([], T(17, 0), job_enabled=True).state == "current"
    assert snapshot_status([T(16, 0)], T(17, 0), job_enabled=True).state == "current"
