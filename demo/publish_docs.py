"""Copy the curated screenshots into docs/screenshots/, compressed.

Run after shoot.py, only when the UI visibly changed (every run adds to git):

    uv run --project .. python publish_docs.py

Uses pngquant + oxipng (brew install pngquant oxipng); falls back to
ImageMagick. 256 colours at full (2x) resolution keeps text sharp at
~70-100 KB per image.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
SRC = HERE / "screenshots"
DST = HERE.parent / "docs" / "screenshots"

# shoot.py name -> published name (referenced by docs/screenshots.md and README)
CURATED = {
    "01-matrix-groups": "matrix",
    "02-matrix-cell": "matrix-cell",
    "05-reachability": "reachability",
    "07-group-editor-preview": "group-editor",
    "11-user-editor": "user-editor",
    "14-resources": "resources",
    "16-policy-editor-preview": "policy-editor",
    "19-anomalies": "anomalies",
    "20-audit": "audit",
    "21-history-diff": "history",
    "23-history-restore": "history-restore",
    "24-jobs": "jobs",
}


def compress(src: Path, out: Path) -> None:
    if shutil.which("pngquant"):
        subprocess.run(
            ["pngquant", "--quality=70-90", "--strip", "--force", "-o", str(out), str(src)],
            check=True,
        )
        if shutil.which("oxipng"):
            subprocess.run(["oxipng", "-q", "-o", "4", "--strip", "safe", str(out)], check=True)
        return
    subprocess.run(
        [
            "magick",
            str(src),
            "-strip",
            "-colors",
            "256",
            "-define",
            "png:compression-level=9",
            f"PNG8:{out}",
        ],
        check=True,
    )


def main() -> None:
    if not (shutil.which("pngquant") or shutil.which("magick")):
        sys.exit("needs pngquant (brew install pngquant oxipng) or ImageMagick")
    DST.mkdir(parents=True, exist_ok=True)
    total = 0
    for src_name, name in CURATED.items():
        for theme in ("light", "dark"):
            src = SRC / theme / f"{src_name}.png"
            if not src.exists():
                sys.exit(f"missing {src} - run shoot.py first")
            out = DST / f"{name}-{theme}.png"
            compress(src, out)
            total += out.stat().st_size
    print(f"{len(CURATED) * 2} images, {total / 1e6:.1f} MB in {DST}")


if __name__ == "__main__":
    main()
