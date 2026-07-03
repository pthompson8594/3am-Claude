#!/usr/bin/env python3
"""
Session utilities for 3am-claude.

Git root detection → stable project_id hash, session ID generation.
"""

import hashlib
import os
import subprocess
import time
import uuid
from pathlib import Path
from typing import Optional


def get_git_root(cwd: Optional[str] = None) -> Optional[str]:
    """Return the absolute path to the git root, or None if not in a repo."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            cwd=cwd or os.getcwd(),
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


def project_id_from_path(path: str) -> str:
    """
    Compute a stable project_id from a filesystem path.
    Returns sha256[:16] of the canonical absolute path.
    """
    canonical = str(Path(path).resolve())
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


# Files/dirs that mark a project root when there's no git repo. Lets non-git
# folders get a stable project_id instead of collapsing into the general pool.
# `.3am-project` is an explicit escape hatch: drop one to anchor a root by hand.
_PROJECT_MARKERS = (
    ".3am-project", ".git", "CLAUDE.md", "pyproject.toml", "package.json",
    "Cargo.toml", "go.mod", "platformio.ini", "CMakeLists.txt", "Makefile",
    ".hg", ".svn", "requirements.txt",
)


def find_project_root(cwd: Optional[str] = None) -> Optional[str]:
    """
    Resolve the project root for `cwd`, git or not:
      1. the git root (canonical, stable), else
      2. the nearest ancestor containing a project marker (so a subdirectory
         still maps to the project, not to itself), else
      3. the working directory itself.
    Returns None for non-project locations (home dir, filesystem root, /tmp) so
    those fall through to the general pool.
    """
    start = Path(cwd or os.getcwd()).resolve()

    git_root = get_git_root(str(start))
    if git_root:
        return git_root

    home = Path.home().resolve()
    non_projects = {home, Path("/"), Path("/tmp")}

    for d in [start, *start.parents]:
        if d in non_projects:
            break
        if any((d / m).exists() for m in _PROJECT_MARKERS):
            return str(d)
        if d == d.parent:  # filesystem root
            break

    if start in non_projects:
        return None
    return str(start)


def get_project_id(cwd: Optional[str] = None) -> Optional[str]:
    """
    Stable project_id for the current location — works for non-git projects too
    (falls back to a marker-detected root, then the directory itself). Returns
    None only for non-project locations (home/root/tmp), which use the general
    pool. Git projects keep the same id as before (git root wins).
    """
    root = find_project_root(cwd)
    if root is None:
        return None
    return project_id_from_path(root)


def new_session_id() -> str:
    """Generate a unique session ID for use with episodic memory tagging."""
    return f"ses_{int(time.time())}_{uuid.uuid4().hex[:8]}"


if __name__ == "__main__":
    pid = get_project_id()
    sid = new_session_id()
    git_root = get_git_root()
    print(f"git_root:   {git_root}")
    print(f"project_id: {pid}")
    print(f"session_id: {sid}")
