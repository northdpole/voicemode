"""Owner-only filesystem helpers for data that may hold speech or transcripts."""

import os
from pathlib import Path

PRIVATE_DIR_MODE = 0o700
PRIVATE_FILE_MODE = 0o600


def ensure_private_dir(path, *, parents: bool = True) -> Path:
    """Create ``path`` with mode 0700 and tighten it if it already exists.

    Best effort: a failure to chmod (read-only mount, foreign owner) is ignored
    so a permissions problem never stops voice from working.
    """
    path = Path(path)
    # Ancestors we are about to create get 0700 too; ones that already exist
    # (for example the user's home directory) are left alone.
    created = [p for p in (path, *path.parents) if not p.exists()]
    path.mkdir(mode=PRIVATE_DIR_MODE, parents=parents, exist_ok=True)
    for p in [*created, path]:
        try:
            os.chmod(p, PRIVATE_DIR_MODE)
        except OSError:
            pass
    return path


def open_private_append(path):
    """Open ``path`` for text append, creating it with mode 0600."""
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, PRIVATE_FILE_MODE)
    return os.fdopen(fd, "a")


def chmod_private_file(path) -> None:
    """Set an existing file to 0600, ignoring failure."""
    try:
        os.chmod(path, PRIVATE_FILE_MODE)
    except OSError:
        pass
