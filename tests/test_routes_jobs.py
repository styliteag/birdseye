import json
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

import jobrun
from birdseye_web.app import create_app
from tests.fakes import ME, SETTINGS, FakeNetBird, FakeOIDC
from tests.test_routes import csrf, login


@pytest.fixture
def jobs_dir(tmp_path):
    jobrun.write_registry(
        tmp_path,
        jobrun.parse_registry_lines(
            [
                "cleanup\tcleanup\t*/15 * * * *\t/app/cleanup.py\t",
                "backup\tbackup\t0 3 * * 0\t/app/run_backup.sh\t",
            ],
            triggers={"cleanup"},
        ),
    )
    (tmp_path / "state" / "cleanup.json").write_text(
        json.dumps(
            {
                "job": "cleanup",
                "running": False,
                "exit_code": 1,
                "finished": "2026-09-25T10:00:00+00:00",
                "log_tail": ["peer omd deleted"],
            }
        )
    )
    return tmp_path


def _client(nb, settings):
    oidc = FakeOIDC()
    c = TestClient(create_app(settings, oidc=oidc, api_factory=nb.api), follow_redirects=False)
    c.oidc = oidc
    login(c)
    c.headers["X-CSRF-Token"] = csrf(c)
    return c


@pytest.fixture
def nb():
    return FakeNetBird()


def test_unconfigured_page(nb):
    html = _client(nb, SETTINGS).get("/jobs").text
    assert "not configured" in html


def test_page_shows_status_logs_and_buttons_for_admin(nb, jobs_dir):
    html = _client(nb, replace(SETTINGS, jobs_dir=str(jobs_dir))).get("/jobs").text
    assert "exit 1" in html and "peer omd deleted" in html
    assert 'hx-post="/jobs/cleanup/run"' in html and "Dry run" in html
    assert 'hx-post="/jobs/backup/run"' not in html


def test_admin_can_queue_run(nb, jobs_dir):
    c = _client(nb, replace(SETTINGS, jobs_dir=str(jobs_dir)))
    r = c.post("/jobs/cleanup/run", data={"mode": "dry-run"}, headers={"HX-Target": "jobs-body"})
    assert r.status_code == 200 and "queued" in r.text
    [req] = list((jobs_dir / "requests").glob("*.json"))
    assert json.loads(req.read_text()) == {"job": "cleanup", "mode": "dry-run", "by": "Alice"}


def test_not_triggerable_job_rejected(nb, jobs_dir):
    c = _client(nb, replace(SETTINGS, jobs_dir=str(jobs_dir)))
    r = c.post("/jobs/backup/run", data={"mode": "run"})
    assert r.status_code == 400 and list((jobs_dir / "requests").glob("*")) == []


def test_plain_user_sees_no_logs_and_cannot_run(nb, jobs_dir):
    nb.data["users/current"] = {**ME, "role": "user"}
    c = _client(nb, replace(SETTINGS, jobs_dir=str(jobs_dir)))
    html = c.get("/jobs").text
    assert "peer omd deleted" not in html and "Run now" not in html
    r = c.post("/jobs/cleanup/run", data={"mode": "run"})
    assert r.status_code == 403 and list((jobs_dir / "requests").glob("*")) == []


def test_bad_job_key_rejected(nb, jobs_dir):
    c = _client(nb, replace(SETTINGS, jobs_dir=str(jobs_dir)))
    assert c.post("/jobs/..%2Fx/run", data={"mode": "run"}).status_code in (404, 422)
