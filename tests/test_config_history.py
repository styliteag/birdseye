import json
from datetime import UTC, datetime

import pytest

import config_history as ch

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def test_prune_plan_keeps_recent_and_newest_two():
    stamps = ["20260101T000000Z", "20260601T000000Z", "20260920T000000Z", "20260930T000000Z"]
    assert ch.prune_plan(stamps, NOW, 90) == ["20260101T000000Z", "20260601T000000Z"]
    # everything old: the newest two survive anyway
    assert ch.prune_plan(stamps, NOW, 1) == stamps[:2]
    assert ch.prune_plan(stamps[:2], NOW, 1) == []


def test_snapshot_stamps_ignores_temp_and_junk(tmp_path):
    for name in ("20260930T000000Z", ".tmp-20261001T000000Z", "notes", "20260901T000000Z"):
        (tmp_path / name).mkdir()
    (tmp_path / "20260101T000000Z").write_text("a file, not a snapshot")
    assert ch.snapshot_stamps(tmp_path) == ["20260901T000000Z", "20260930T000000Z"]
    assert ch.snapshot_stamps(tmp_path / "missing") == []


class FakeClient:
    def __init__(self, fail=()):
        self.fail = set(fail)

    def get(self, path):
        if path in self.fail:
            raise RuntimeError("boom")
        return [{"id": "x", "path": path}]


def test_take_snapshot_writes_files_and_manifest_atomically(tmp_path, monkeypatch):
    monkeypatch.setenv("NB_URL", "https://nb.test")
    final, written = ch.take_snapshot(FakeClient(fail={"routes"}), tmp_path, NOW)
    assert final.name == "20261001T120000Z" and "groups" in written and "routes" not in written
    assert json.loads((final / "groups.json").read_text()) == [{"id": "x", "path": "groups"}]
    manifest = json.loads((final / "manifest.json").read_text())
    assert manifest["endpoints"]["routes"]["status"] == "skipped"
    assert [p.name for p in tmp_path.iterdir()] == [final.name]  # no temp dir left


def test_take_snapshot_with_nothing_fetched_leaves_no_dir(tmp_path):
    from export_objects import ENDPOINTS

    with pytest.raises(SystemExit):
        ch.take_snapshot(FakeClient(fail={p for p, _ in ENDPOINTS}), tmp_path, NOW)
    assert list(tmp_path.iterdir()) == []


def test_main_alerts_on_missing_config(monkeypatch, tmp_path):
    for var in ("NB_URL", "NB_ADMIN_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(ch, "load_dotenv", lambda: None)
    monkeypatch.setenv("HISTORY_DIR", str(tmp_path))
    mails = []
    monkeypatch.setattr(ch, "notify_failure", lambda **kw: mails.append(kw))
    assert ch.main([]) == 1
    assert mails and "NB_ADMIN_API_KEY" in mails[0]["detail"]


def test_setup_key_values_are_stripped(tmp_path, monkeypatch):
    class Keys(FakeClient):
        def get(self, path):
            if path == "setup-keys":
                return [{"id": "k", "name": "n", "key": "SECRET-VALUE"}]
            return super().get(path)

    final, _ = ch.take_snapshot(Keys(), tmp_path, NOW)
    text = (final / "setup_keys.json").read_text()
    assert "SECRET-VALUE" not in text and '"name": "n"' in text


def test_unchanged_config_writes_no_new_snapshot(tmp_path, monkeypatch):
    class Live(FakeClient):
        def __init__(self, online):
            super().__init__()
            self.online = online

        def get(self, path):
            if path == "peers":
                return [
                    {
                        "id": "p",
                        "name": "p",
                        "connected": self.online,
                        "last_seen": str(self.online),
                    }
                ]
            return super().get(path)

    first, _ = ch.take_snapshot(Live(True), tmp_path, NOW)
    later = NOW.replace(hour=13)
    again, _ = ch.take_snapshot(Live(False), tmp_path, later)  # only runtime state differs
    assert first is not None and again is None
    assert ch.snapshot_stamps(tmp_path) == [first.name]
    forced, _ = ch.take_snapshot(Live(False), tmp_path, later, force=True)
    assert forced is not None and len(ch.snapshot_stamps(tmp_path)) == 2
