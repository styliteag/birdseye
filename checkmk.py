"""Optional Checkmk local check for the unattended jobs. Off unless configured.

Mail tells you a run *failed*. It cannot tell you a run stopped happening —
a broken cron, a container that never came back, an ssh key that expired into
a hang. A Checkmk spool file covers that gap: the filename carries a maximum
age, and Checkmk turns the service stale by itself once no run refreshes it.

Enable by mounting the agent's spool directory into the container and setting

  CHECKMK_SPOOL_DIR=/var/lib/check_mk_agent/spool

Every job then writes `<max_age>_<name>.txt` there. With no Checkmk on the
host, leave it empty and nothing is written.
"""

from __future__ import annotations

import os

OK, WARN, CRIT = 0, 1, 2


def spool_dir() -> str:
    return (os.environ.get("CHECKMK_SPOOL_DIR") or "").strip()


def write(name: str, check: str, status: int, summary: str, max_age: int) -> None:
    """Write one local-check result. Never raises — monitoring must not break the job."""
    directory = spool_dir()
    if not directory or not os.path.isdir(directory):
        return
    # One line, Checkmk local-check format: <status> <service> - <summary>
    line = f"{status} {check} - {summary}".replace("\n", " ")
    path = os.path.join(directory, f"{max_age}_{name}.txt")
    tmp = f"{path}.tmp"
    try:
        with open(tmp, "w") as fh:
            fh.write(line + "\n")
        os.replace(tmp, path)  # atomic: the agent never reads a half-written file
    except OSError:
        pass
