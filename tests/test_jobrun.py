import json
import sys

import pytest

import jobrun

PY = sys.executable


@pytest.fixture
def jobs(tmp_path):
    jobrun.ensure_dirs(tmp_path)
    return tmp_path


def _state(jobs, key):
    return json.loads((jobs / "state" / f"{key}.json").read_text())


def _spec(key="cleanup", command=None, enabled=True, triggerable=True, **kw):
    return jobrun.JobSpec(
        key=key,
        label=key,
        schedule="*/15 * * * *",
        command=command or [PY, "-c", "print('ok')"],
        enabled=enabled,
        reason="" if enabled else "need: X",
        triggerable=triggerable,
        **kw,
    )


# --- run ---------------------------------------------------------------------


def test_run_records_success_and_tail(jobs, capsys):
    rc = jobrun.run_job(jobs, "cleanup", [PY, "-c", "print('hello'); print('world')"])
    st = _state(jobs, "cleanup")
    assert rc == 0 and st["exit_code"] == 0 and not st["running"]
    assert st["log_tail"] == ["hello", "world"]
    assert st["trigger"] == "cron" and st["mode"] == "run"
    assert "hello" in capsys.readouterr().out  # still reaches the container log


def test_run_records_failure(jobs):
    rc = jobrun.run_job(jobs, "cleanup", [PY, "-c", "import sys; print('boom'); sys.exit(3)"])
    st = _state(jobs, "cleanup")
    assert rc == 3 and st["exit_code"] == 3 and st["log_tail"] == ["boom"]


def test_missing_command_is_failure_not_crash(jobs):
    rc = jobrun.run_job(jobs, "cleanup", ["/nonexistent/binary"])
    assert rc == 127 and _state(jobs, "cleanup")["exit_code"] == 127


def test_tail_is_capped(jobs):
    jobrun.run_job(jobs, "x", [PY, "-c", "[print(i) for i in range(500)]"], tail_lines=10)
    assert _state(jobs, "x")["log_tail"] == [str(i) for i in range(490, 500)]


def test_history_newest_first_and_capped(jobs):
    for i in range(5):
        jobrun.run_job(jobs, "x", [PY, "-c", f"import sys; sys.exit({i})"], history=3)
    hist = _state(jobs, "x")["history"]
    assert [h["exit_code"] for h in hist] == [4, 3, 2]


def test_manual_trigger_and_mode_recorded(jobs):
    jobrun.run_job(jobs, "x", [PY, "-c", "pass"], trigger="manual:alice", mode="dry-run")
    st = _state(jobs, "x")
    assert st["trigger"] == "manual:alice" and st["mode"] == "dry-run"


def test_concurrent_run_is_skipped(jobs):
    with jobrun.job_lock(jobs, "x") as got:
        assert got
        rc = jobrun.run_job(jobs, "x", [PY, "-c", "print('should not run')"])
    assert rc == jobrun.SKIPPED
    assert not (jobs / "state" / "x.json").exists()


# --- registry ----------------------------------------------------------------


def test_registry_roundtrip_and_triggerable_allowlist(jobs):
    lines = [
        "cleanup\tcleanup\t*/15 * * * *\t/app/.venv/bin/python /app/cleanup_ephemeral.py\t",
        "backup\tbackup (volumes)\t0 3 * * 0\t/app/run_backup.sh\t",
        "mirror\tmirror\t\t\tneed: MIRROR_URL",
    ]
    specs = jobrun.parse_registry_lines(lines, triggers={"cleanup", "mirror"})
    jobrun.write_registry(jobs, specs)
    reg = jobrun.load_registry(jobs)
    assert reg["cleanup"].triggerable and reg["cleanup"].dry_run_arg == "--dry-run"
    assert reg["cleanup"].command == ["/app/.venv/bin/python", "/app/cleanup_ephemeral.py"]
    assert not reg["backup"].triggerable  # not in allowlist
    assert not reg["mirror"].enabled and not reg["mirror"].triggerable  # disabled wins
    assert reg["mirror"].reason == "need: MIRROR_URL"


def test_triggers_env_default(monkeypatch):
    monkeypatch.delenv("JOB_TRIGGERS", raising=False)
    assert jobrun.trigger_allowlist() == {"cleanup", "maintenance"}
    monkeypatch.setenv("JOB_TRIGGERS", " cleanup , ")
    assert jobrun.trigger_allowlist() == {"cleanup"}


# --- requests ------------------------------------------------------------------


def _request(jobs, **body):
    path = jobs / "requests" / "r1.json"
    path.write_text(json.dumps(body))
    return path


def test_request_spawns_whitelisted_job(jobs):
    spawned = []
    reg = {"cleanup": _spec(command=["/app/x.py"], dry_run_arg="--dry-run")}
    path = _request(jobs, job="cleanup", mode="dry-run", by="alice")
    outcome = jobrun.handle_request(jobs, path, reg, spawned.append)
    assert outcome == "accepted" and not path.exists()
    [cmd] = spawned
    assert cmd[-2:] == ["/app/x.py", "--dry-run"]
    assert "manual:alice" in cmd and "dry-run" in cmd


@pytest.mark.parametrize(
    "body,reason",
    [
        ({"job": "backup", "mode": "run", "by": "a"}, "unknown"),
        (
            {"job": "cleanup", "mode": "run", "by": "a", "_reg": "not-triggerable"},
            "not triggerable",
        ),
        ({"job": "cleanup", "mode": "run", "by": "a", "_reg": "disabled"}, "not triggerable"),
        ({"job": "cleanup", "mode": "rm -rf", "by": "a"}, "mode"),
        ({"job": "cleanup", "mode": "dry-run", "by": "a", "_reg": "no-dry"}, "mode"),
    ],
)
def test_request_rejected(jobs, body, reason):
    variant = body.pop("_reg", "")
    spec = _spec(
        enabled=variant != "disabled",
        triggerable=variant not in ("not-triggerable", "disabled"),
        dry_run_arg="" if variant == "no-dry" else "--dry-run",
    )
    spawned = []
    path = _request(jobs, **body)
    outcome = jobrun.handle_request(jobs, path, {"cleanup": spec}, spawned.append)
    assert outcome.startswith("rejected") and reason in outcome
    assert spawned == [] and not path.exists()


def test_garbage_request_is_rejected_and_removed(jobs):
    path = jobs / "requests" / "bad.json"
    path.write_text("{not json")
    assert jobrun.handle_request(jobs, path, {}, lambda c: None).startswith("rejected")
    assert not path.exists()


def test_pending_requests_listed_oldest_first(jobs):
    for name in ("b.json", "a.json", "ignored.tmp"):
        (jobs / "requests" / name).write_text("{}")
    assert [p.name for p in jobrun.pending_requests(jobs)] == ["a.json", "b.json"]


def test_symlink_request_is_never_followed(jobs, tmp_path_factory):
    secret = tmp_path_factory.mktemp("s") / "secret.json"
    secret.write_text('{"job": "cleanup", "mode": "run", "by": "x"}')
    link = jobs / "requests" / "evil.json"
    link.symlink_to(secret)
    spawned = []
    reg = {"cleanup": _spec()}
    outcome = jobrun.handle_request(jobs, link, reg, spawned.append)
    assert outcome.startswith("rejected") and spawned == []
    assert not link.exists() and secret.exists()


def test_oversized_request_rejected(jobs):
    path = _request(jobs, job="cleanup", mode="run", by="x" * 10_000)
    assert jobrun.handle_request(jobs, path, {"cleanup": _spec()}, lambda c: None).startswith(
        "rejected"
    )


# --- security review fixes ------------------------------------------------------


def test_fifo_request_does_not_block(jobs):
    import os

    fifo = jobs / "requests" / "f.json"
    os.mkfifo(fifo)
    outcome = jobrun.handle_request(jobs, fifo, {"cleanup": _spec()}, lambda c: None)
    assert outcome.startswith("rejected") and not fifo.exists()


def test_json_bomb_is_rejected_not_raised(jobs):
    path = jobs / "requests" / "bomb.json"
    path.write_text("[" * 3000)
    assert jobrun.handle_request(jobs, path, {"cleanup": _spec()}, lambda c: None).startswith(
        "rejected"
    )


def test_directory_named_json_is_removed(jobs):
    d = jobs / "requests" / "dir.json"
    d.mkdir()
    jobrun.handle_request(jobs, d, {}, lambda c: None)
    assert not d.exists()


def test_allowlist_rechecked_at_request_time(jobs, monkeypatch):
    monkeypatch.setenv("JOB_TRIGGERS", "maintenance")
    spawned = []
    path = _request(jobs, job="cleanup", mode="run", by="a")
    outcome = jobrun.handle_request(jobs, path, {"cleanup": _spec()}, spawned.append)
    assert outcome.startswith("rejected") and spawned == []


def test_by_is_reduced_to_printable_ascii(jobs):
    spawned = []
    path = _request(jobs, job="cleanup", mode="run", by="evil‮\nname")
    jobrun.handle_request(jobs, path, {"cleanup": _spec()}, spawned.append)
    [cmd] = spawned
    assert "manual:evilname" in cmd


def test_serve_tick_one_spawn_per_job_and_capped(jobs):
    for i in range(10):
        (jobs / "requests" / f"{i:02d}.json").write_text(
            json.dumps({"job": "cleanup", "mode": "run", "by": "a"})
        )
    spawned = []
    jobrun.serve_tick(jobs, {"cleanup": _spec()}, spawned.append)
    assert len(spawned) == 1
    assert list((jobs / "requests").glob("*.json")) == []


def test_serve_tick_skips_job_already_running(jobs):
    (jobs / "requests" / "a.json").write_text(
        json.dumps({"job": "cleanup", "mode": "run", "by": "a"})
    )
    spawned = []
    with jobrun.job_lock(jobs, "cleanup"):
        jobrun.serve_tick(jobs, {"cleanup": _spec()}, spawned.append)
    assert spawned == []


def test_serve_tick_survives_bad_files(jobs):
    (jobs / "requests" / "a.json").write_text("[" * 3000)
    (jobs / "requests" / "b.json").write_text(
        json.dumps({"job": "cleanup", "mode": "run", "by": "a"})
    )
    spawned = []
    jobrun.serve_tick(jobs, {"cleanup": _spec()}, spawned.append)
    assert len(spawned) == 1


def test_run_still_executes_when_state_cannot_be_written(jobs, monkeypatch, capsys):
    def boom(*a, **k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(jobrun, "_write_json", boom)
    rc = jobrun.run_job(jobs, "x", [PY, "-c", "print('ran anyway')"])
    assert rc == 0 and "ran anyway" in capsys.readouterr().out


def test_unknown_job_name_never_touches_the_filesystem(jobs):
    evil = "../../escaped"
    (jobs / "requests" / "a.json").write_text(json.dumps({"job": evil, "mode": "run", "by": "a"}))
    jobrun.serve_tick(jobs, {"cleanup": _spec()}, lambda c: None)
    assert not (jobs / "escaped.lock").exists()
    assert not (jobs.parent / "escaped.lock").exists()
    assert list((jobs / "state").glob("*.lock")) == []
