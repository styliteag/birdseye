from change_trigger import ChangeTrigger


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _ev(code):
    return {"activity_code": code}


def _trigger(clock, outcomes=None):
    started = []

    def start(key, trigger):
        started.append((key, trigger))
        return (outcomes or {}).get(len(started), "started")

    t = ChangeTrigger("history", settle_s=60, exclude=["*login*"], clock=clock, start=start)
    return t, started


def test_waits_for_quiet_period_then_starts_once():
    clock = Clock()
    t, started = _trigger(clock)
    t.note([_ev("group.update")])
    clock.t += 30
    t.note([_ev("policy.update")])  # resets the wait
    clock.t += 59
    t.tick()
    assert started == []
    clock.t += 1
    t.tick()
    t.tick()
    assert started == [("history", "audit")]


def test_login_noise_does_not_trigger():
    clock = Clock()
    t, started = _trigger(clock)
    t.note([_ev("user.peer.login"), _ev("peer.login.expire")])
    clock.t += 999
    t.tick()
    assert started == []


def test_busy_job_is_retried_next_tick():
    clock = Clock()
    t, started = _trigger(clock, outcomes={1: "busy"})
    t.note([_ev("group.add")])
    clock.t += 60
    t.tick()
    t.tick()
    assert len(started) == 2


def test_disabled_job_gives_up():
    clock = Clock()
    t, started = _trigger(clock, outcomes={1: "disabled"})
    t.note([_ev("group.add")])
    clock.t += 60
    t.tick()
    t.tick()
    assert len(started) == 1
