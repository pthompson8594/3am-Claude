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


def get_project_id(cwd: Optional[str] = None) -> Optional[str]:
    """
    Detect current project_id from git root.
    Returns None if not in a git repo (memories stored as general/shared).
    """
    git_root = get_git_root(cwd)
    if git_root:
        return project_id_from_path(git_root)
    return None


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
