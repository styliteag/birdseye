"""Start a job once the configuration has settled after a change.

The audit-event forwarder feeds every new event in; when a matching one
arrives, the job is due `settle_s` seconds later, and every further change
pushes that back. A burst of edits therefore gives one run, after the last
edit. Used for `config_history.py`, so a snapshot follows each change.
"""

from __future__ import annotations

import fnmatch
import time
from collections.abc import Callable, Iterable
from typing import Any

# start(job, trigger) -> "started" | "busy" | "disabled"
Starter = Callable[[str, str], str]
BUSY_RETRY_S = 10.0


class ChangeTrigger:
    def __init__(
        self,
        job: str,
        *,
        settle_s: float,
        start: Starter,
        include: Iterable[str] = ("*",),
        exclude: Iterable[str] = (),
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.job = job
        self.settle_s = settle_s
        self.include = list(include)
        self.exclude = list(exclude)
        self._start = start
        self._clock = clock
        self.due: float | None = None

    def _counts(self, code: str) -> bool:
        def hit(patterns: list[str]) -> bool:
            return any(fnmatch.fnmatchcase(code, p) for p in patterns)

        return bool(code) and hit(self.include) and not hit(self.exclude)

    def note(self, events: Iterable[dict[str, Any]]) -> None:
        if any(self._counts(str(e.get("activity_code") or "")) for e in events):
            self.due = self._clock() + self.settle_s

    def seconds_until_due(self) -> float | None:
        if self.due is None:
            return None
        return max(0.0, self.due - self._clock())

    def tick(self) -> str:
        """Start the job if it is due. A busy job is retried a little later,
        so a change made during a running snapshot still gets its own."""
        if self.due is None or self._clock() < self.due:
            return ""
        outcome = self._start(self.job, "audit")
        self.due = self._clock() + BUSY_RETRY_S if outcome == "busy" else None
        return outcome
