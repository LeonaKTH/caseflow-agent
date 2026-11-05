"""Public-export provenance that also works without Git and from an installed wheel."""

import hashlib
import subprocess
from pathlib import Path


def provenance():
    root = Path(__file__).resolve().parents[2]
    commit, dirty = None, None
    if (root / ".git").exists():
        try:
            commit = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=root, stderr=subprocess.DEVNULL, text=True
            ).strip()
            dirty = bool(
                subprocess.check_output(
                    ["git", "status", "--porcelain"], cwd=root, stderr=subprocess.DEVNULL, text=True
                ).strip()
            )
        except (OSError, subprocess.CalledProcessError):
            commit, dirty = None, None
    digest = hashlib.sha256()
    packages = Path(__file__).resolve().parent.parent
    for name in ("caseflow", "support_agent"):
        for path in sorted((packages / name).rglob("*.py")):
            digest.update(path.relative_to(packages).as_posix().encode())
            digest.update(path.read_bytes().replace(b"\r\n", b"\n"))
    return commit, dirty, digest.hexdigest()
