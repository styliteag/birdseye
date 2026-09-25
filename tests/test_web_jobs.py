import json
import os
import time

import pytest

import jobrun
from birdseye_web.jobs import JobError, load_jobs, request_run


@pytest.fixture
def jobs_dir(tmp_path):
    specs = jobrun.parse_registry_lines(
        [
            "cleanup\tcleanup\t*/15 * * * *\t/app/cleanup.py\t",
            "maintenance\tmaintenance (posture)\t0 * * * *\t/app/maint.py\t",
            "backup\tbackup\t0 3 * * 0\t/app/run_backup.sh\t",
            "mirror\tmirror\t\t\tCRON_MIRROR_ACCOUNT not set",
        ],
        triggers={"cleanup", "maintenance"},
    )
    jobrun.write_registry(tmp_path, specs)
    return tmp_path


def _state(d, key, **data):
    (d / "state" / f"{key}.json").write_text(json.dumps({"job": key, **data}))


def test_load_jobs_merges_registry_and_state(jobs_dir):
    _state(
        jobs_dir,
        "cleanup",
        running=False,
        exit_code=0,
        finished="2026-09-25T10:00:00+00:00",
        duration_s=1.5,
        trigger="cron",
        mode="run",
        log_tail=["ok"],
        history=[{"exit_code": 0}],
    )
    view = load_jobs(jobs_dir)
    by = {j.key: j for j in view.jobs}
    assert by["cleanup"].status == "ok" and by["cleanup"].log_tail == ("ok",)
    assert by["backup"].status == "never"
    assert by["mirror"].status == "disabled" and "not set" in by["mirror"].reason
    assert [j.key for j in view.jobs][:2] == ["cleanup", "maintenance"]


@pytest.mark.parametrize(
    "state,status",
    [
        (dict(running=True), "running"),
        (dict(running=False, exit_code=2), "failed"),
        (dict(running=False, exit_code=0), "ok"),
    ],
)
def test_status(jobs_dir, state, status):
    _state(jobs_dir, "backup", **state)
    assert {j.key: j for j in load_jobs(jobs_dir).jobs}["backup"].status == status


def test_missing_dir_is_unavailable(tmp_path):
    view = load_jobs(tmp_path / "nope")
    assert not view.available and view.jobs == ()


def test_forwarder_heartbeat(jobs_dir):
    now = time.time()
    (jobs_dir / "state" / "forwarder.json").write_text(
        json.dumps({"updated": now - 5, "last_poll_ok": True, "last_id": 42, "started": now - 99})
    )
    fw = load_jobs(jobs_dir, clock=lambda: now).forwarder
    assert fw.status == "ok" and fw.last_id == 42 and fw.age_s == 5


def test_forwarder_stale_and_outage(jobs_dir):
    now = time.time()
    path = jobs_dir / "state" / "forwarder.json"
    path.write_text(json.dumps({"updated": now - 3600, "last_poll_ok": True}))
    assert load_jobs(jobs_dir, clock=lambda: now).forwarder.status == "stale"
    path.write_text(json.dumps({"updated": now, "last_poll_ok": False, "error": "ConnectError"}))
    assert load_jobs(jobs_dir, clock=lambda: now).forwarder.status == "failing"


def test_request_run_writes_request_file(jobs_dir):
    rid = request_run(jobs_dir, "maintenance", "dry-run", by="Wim Bonis")
    [path] = list((jobs_dir / "requests").glob("*.json"))
    assert rid in path.name
    assert json.loads(path.read_text()) == {
        "job": "maintenance",
        "mode": "dry-run",
        "by": "Wim Bonis",
    }
    assert oct(os.stat(path).st_mode)[-3:] == "644"


@pytest.mark.parametrize(
    "key,mode,msg",
    [
        ("backup", "run", "not triggerable"),
        ("mirror", "run", "not triggerable"),
        ("nope", "run", "unknown"),
        ("cleanup", "delete", "mode"),
    ],
)
def test_request_run_validates(jobs_dir, key, mode, msg):
    with pytest.raises(JobError, match=msg):
        request_run(jobs_dir, key, mode, by="x")
    assert list((jobs_dir / "requests").glob("*")) == []


def test_request_run_marks_job_queued(jobs_dir):
    request_run(jobs_dir, "cleanup", "run", by="x")
    job = {j.key: j for j in load_jobs(jobs_dir).jobs}["cleanup"]
    assert job.queued
