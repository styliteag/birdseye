"""SSH/rsync transport for the jobs that push to a second host.

Used by `clone_standby.py` (ships a data payload to a standby controller) and
`backup_offsite.py` (ships a config archive to any SSH target). Both configure
themselves from an env prefix so a deployment can point them at different hosts:

  <PREFIX>_SSH_HOST         user@host — no default, empty disables the job
  <PREFIX>_SSH_PORT         default 22
  <PREFIX>_SSH_KEY          identity file inside the container (mount it read-only)
  <PREFIX>_SSH_KNOWN_HOSTS  known_hosts file; default lets ssh use its own
  <PREFIX>_SSH_STRICT       yes | accept-new (default) | no

`accept-new` is trust-on-first-use: it accepts an unknown host key once and
then pins it. That is the pragmatic default for a container with no
pre-seeded known_hosts; mount one and set `<PREFIX>_SSH_STRICT=yes` when the
deployment cares about the first connection too.

Nothing here shells out to `docker`: the compose commands this module runs are
executed **on the far side** over ssh, so the container needs no docker socket
for them.
"""

from __future__ import annotations

import os
import socket
import subprocess
from dataclasses import dataclass

STRICT_MODES = ("yes", "accept-new", "no")


class RemoteError(RuntimeError):
    """A remote command exited non-zero, with its output attached."""


def run_local(cmd: list[str], *, check: bool = True, stdin: str | None = None) -> str:
    proc = subprocess.run(
        cmd,
        text=True,
        input=stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    # rstrip only: the leading indentation of multi-line remote output is part
    # of how `status` formats itself.
    out = (proc.stdout or "").rstrip()
    if check and proc.returncode != 0:
        raise RemoteError(f"{' '.join(cmd[:4])}… exited {proc.returncode}\n{out}")
    return out


@dataclass(frozen=True)
class Remote:
    """One SSH destination, plus the few operations the jobs need on it."""

    host: str
    port: int = 22
    key: str = ""
    known_hosts: str = ""
    strict: str = "accept-new"
    connect_timeout: int = 15

    @classmethod
    def from_env(cls, prefix: str) -> Remote | None:
        """Build from <PREFIX>_SSH_* env vars, or None when no host is configured."""
        host = (os.environ.get(f"{prefix}_SSH_HOST") or "").strip()
        if not host:
            return None
        strict = (os.environ.get(f"{prefix}_SSH_STRICT") or "accept-new").strip().lower()
        if strict not in STRICT_MODES:
            raise SystemExit(
                f"{prefix}_SSH_STRICT must be one of {'|'.join(STRICT_MODES)}, got {strict!r}"
            )
        port_raw = (os.environ.get(f"{prefix}_SSH_PORT") or "22").strip()
        if not port_raw.isdigit():
            raise SystemExit(f"{prefix}_SSH_PORT must be a number, got {port_raw!r}")
        key = (os.environ.get(f"{prefix}_SSH_KEY") or "").strip()
        if key and not os.path.exists(key):
            raise SystemExit(f"{prefix}_SSH_KEY={key} does not exist inside the container")
        return cls(
            host=host,
            port=int(port_raw),
            key=key,
            known_hosts=(os.environ.get(f"{prefix}_SSH_KNOWN_HOSTS") or "").strip(),
            strict=strict,
        )

    # --- plumbing ----------------------------------------------------------

    @property
    def hostname(self) -> str:
        """The bare hostname, without the user@ part."""
        return self.host.split("@", 1)[-1]

    def _opts(self) -> list[str]:
        opts = [
            "-o",
            "BatchMode=yes",
            "-o",
            f"ConnectTimeout={self.connect_timeout}",
            "-o",
            f"StrictHostKeyChecking={self.strict}",
        ]
        if self.key:
            opts += ["-i", self.key, "-o", "IdentitiesOnly=yes"]
        if self.known_hosts:
            opts += ["-o", f"UserKnownHostsFile={self.known_hosts}"]
        return opts

    def ssh_cmd(self) -> list[str]:
        return ["ssh", "-p", str(self.port), *self._opts()]

    def rsync_shell(self) -> str:
        return " ".join(["ssh", "-p", str(self.port), *self._opts()])

    # --- operations --------------------------------------------------------

    def run(self, script: str, *, check: bool = True) -> str:
        """Run a bash script on the remote and return its combined output."""
        return run_local([*self.ssh_cmd(), self.host, "bash", "-s"], check=check, stdin=script)

    def run_live(self, script: str) -> int:
        """Same, but stream the output straight through to this process's stdout."""
        proc = subprocess.run(
            [*self.ssh_cmd(), self.host, "bash", "-s"],
            text=True,
            input=script,
            check=False,
        )
        return proc.returncode

    def push_dir(self, local_dir: str, remote_dir: str, *, delete: bool = True) -> str:
        cmd = ["rsync", "-a", "--stats", "--chmod=D700,F600"]
        if delete:
            cmd.append("--delete")
        cmd += ["-e", self.rsync_shell(), f"{local_dir.rstrip('/')}/", f"{self.host}:{remote_dir}/"]
        return run_local(cmd)

    def push_file(self, local: str, remote: str) -> str:
        return run_local(
            [
                "rsync",
                "-a",
                "--chmod=F600",
                "-e",
                self.rsync_shell(),
                local,
                f"{self.host}:{remote}",
            ]
        )

    def ip(self) -> str:
        """Resolve the remote's address — used to reach it without touching DNS."""
        return socket.gethostbyname(self.hostname)


def compose_cmd(root: str, project: str, compose_file: str = "docker-compose.yml") -> str:
    """A `docker compose` invocation that works from any cwd on the remote.

    Project name and project directory are both pinned: a stack started through
    a symlink otherwise ends up in a *second* compose project with duplicate
    container names.
    """
    return f"docker compose -p {project} --project-directory {root} -f {root}/{compose_file}"
