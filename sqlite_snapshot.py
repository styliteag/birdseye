"""Consistent hot copies of live SQLite files.

NetBird's management server keeps `store.db` (and the embedded IdP's `idp.db`,
`events.db`) open for writing the whole time it runs. Copying such a file with
`cp`/`tar` can catch it mid-transaction: pages from before and after a write
land in the same copy and the result is a database that opens fine and is
subtly wrong, or refuses to open at all.

SQLite's online backup API avoids that. It reads the source *through the SQLite
library* rather than through the filesystem, so it participates in the same
locking as the writer: the copy is a transactionally consistent point in time,
and the server never stops. `pages=-1` (the `Connection.backup()` default)
copies the whole database in one step, which means no writer can slip in
halfway through — for a database of this size that step takes milliseconds.

The source is opened `?mode=ro` so a bug here can never write to production,
and every snapshot is re-opened afterwards for `pragma integrity_check`.
"""

from __future__ import annotations

import os
import sqlite3

# Tables worth counting for a "does this snapshot look sane" report. Missing
# ones are skipped, so this stays harmless across NetBird schema versions.
INTERESTING_TABLES = (
    "accounts",
    "peers",
    "groups",
    "policies",
    "networks",
    "users",
    "setup_keys",
    "posture_checks",
    "personal_access_tokens",
)


def snapshot(src: str, dst: str) -> None:
    """Copy a live SQLite database to `dst`, then verify the copy."""
    if not os.path.exists(src):
        raise FileNotFoundError(src)
    source = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=30)
    try:
        out = sqlite3.connect(dst)
        try:
            with out:
                source.backup(out)
        finally:
            out.close()
    finally:
        source.close()
    os.chmod(dst, 0o600)

    check = sqlite3.connect(f"file:{dst}?mode=ro", uri=True)
    try:
        verdict = check.execute("pragma integrity_check").fetchone()[0]
    finally:
        check.close()
    if verdict != "ok":
        raise RuntimeError(f"{os.path.basename(src)}: integrity_check said {verdict!r}")


def table_counts(path: str, tables: tuple[str, ...] = INTERESTING_TABLES) -> dict[str, int]:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        present = {
            row[0] for row in con.execute("select name from sqlite_master where type='table'")
        }
        return {
            table: con.execute(f"select count(*) from {table}").fetchone()[0]  # noqa: S608
            for table in tables
            if table in present
        }
    finally:
        con.close()


def parse_min_rows(spec: str) -> dict[str, int]:
    """Parse `accounts:1,peers:1` into a {table: minimum} mapping."""
    wanted: dict[str, int] = {}
    for item in (s.strip() for s in spec.split(",")):
        if not item:
            continue
        table, _, minimum = item.partition(":")
        if not minimum.strip().isdigit():
            raise SystemExit(f"expected table:count, got {item!r}")
        wanted[table.strip()] = int(minimum)
    return wanted


def check_min_rows(counts: dict[str, int], wanted: dict[str, int]) -> list[str]:
    """Report tables that are missing or below their minimum row count.

    A table nobody could count is reported too: it usually means the snapshot
    is of the wrong file, which is exactly the mistake this gate exists for.
    """
    problems = []
    for table, minimum in wanted.items():
        if table not in counts:
            problems.append(f"table {table!r} not in the snapshot")
        elif counts[table] < minimum:
            problems.append(f"{table}={counts[table]}, expected at least {minimum}")
    return problems
