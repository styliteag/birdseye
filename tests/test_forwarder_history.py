import importlib.util
import sys
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def forwarder():
    spec = importlib.util.spec_from_file_location(
        "event_forwarder", Path(__file__).parent.parent / "docker" / "event_forwarder.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["event_forwarder"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_history_trigger_on_by_default(forwarder, monkeypatch, tmp_path):
    monkeypatch.delenv("HISTORY_ON_CHANGE", raising=False)
    monkeypatch.setenv("HISTORY_SETTLE_SECONDS", "15")
    t = forwarder._history_trigger(tmp_path)
    assert t is not None and t.settle_s == 15 and t.exclude == ["*login*"]


def test_history_trigger_can_be_switched_off(forwarder, monkeypatch, tmp_path):
    monkeypatch.setenv("HISTORY_ON_CHANGE", "0")
    assert forwarder._history_trigger(tmp_path) is None


def test_trigger_inert_without_history_job(forwarder, monkeypatch, tmp_path):
    monkeypatch.delenv("HISTORY_ON_CHANGE", raising=False)
    monkeypatch.setenv("HISTORY_SETTLE_SECONDS", "0")
    t = forwarder._history_trigger(tmp_path)  # no registry.json: job unknown
    t.note([{"activity_code": "group.update"}])
    assert t.tick() == "disabled" and t.due is None
