from birdseye_web.sessions import PENDING_TTL, SessionStore


def _store(now):
    return SessionStore(max_age=3600, clock=lambda: now[0], max_pending=3)


def test_pending_session_expires_fast():
    now = [0.0]
    store = _store(now)
    s = store.create()
    now[0] = PENDING_TTL + 1
    assert store.get(s.sid) is None


def test_logged_in_session_lives_max_age():
    now = [0.0]
    store = _store(now)
    s = store.rotate(store.create(), access_token="t")
    now[0] = 3000
    assert store.get(s.sid) is not None
    now[0] = 3601
    assert store.get(s.sid) is None


def test_pending_sessions_are_capped():
    now = [0.0]
    store = _store(now)
    first = store.create()
    for _ in range(5):
        now[0] += 1
        store.create()
    assert store.get(first.sid) is None
    assert store.pending_count() == 3


def test_cap_never_evicts_logged_in_sessions():
    now = [0.0]
    store = _store(now)
    user = store.rotate(store.create(), access_token="t")
    for _ in range(10):
        store.create()
    assert store.get(user.sid) is not None


def test_update_does_not_resurrect_dropped_session():
    now = [0.0]
    store = _store(now)
    s = store.rotate(store.create(), access_token="t")
    store.drop(s.sid)
    store.update(s, access_token="t2")
    assert store.get(s.sid) is None
