"""Local-first hardening for the Kokoro-FastAPI install.

The upstream start scripts bind ``0.0.0.0`` and leave CORS open to ``*``.
Rather than run them as shipped, the installer derives a hardened copy
(``start-voicemode.sh``) that binds loopback and restricts CORS, and refuses to
produce one if it cannot prove the wildcard bind is gone.
"""

import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger("voicemode")

HARDENED_SCRIPT_NAME = "start-voicemode.sh"

# Tags are mutable; the commit they must resolve to is not.
KOKORO_PINNED_COMMITS = {
    "v0.2.4": "6b1e9d9128b9021f8b2716e213d3834be0371e1c",
    "v0.2.4-master": "6c1b8bf36c1da8ddb4aeb6fe694a4f5db2d6805a",
}

_HOST_RE = re.compile(r"--host\s+(?:0\.0\.0\.0|\"?\$\{?[A-Za-z_]+[^\s]*\"?)")
_PORT_RE = re.compile(r"--port\s+\d+")


def harden_start_script(content: str, port: int) -> str:
    """Return ``content`` bound to loopback on ``port`` with CORS restricted.

    Raises ValueError if no ``--host`` flag is found to rewrite, or if a
    wildcard bind survives, so a changed upstream script fails closed.
    """
    host_expr = '--host "${VOICEMODE_KOKORO_HOST:-127.0.0.1}"'
    hardened, n_host = _HOST_RE.subn(host_expr, content)
    if n_host == 0:
        raise ValueError("no --host flag found in Kokoro start script; refusing to run it unmodified")
    hardened, n_port = _PORT_RE.subn(f"--port {int(port)}", hardened)
    if n_port == 0:
        raise ValueError("no --port flag found in Kokoro start script")
    if "0.0.0.0" in hardened:
        raise ValueError("Kokoro start script still contains a 0.0.0.0 bind after hardening")

    origins = f'["http://127.0.0.1:{int(port)}","http://localhost:{int(port)}"]'
    preamble = (
        "# --- VoiceMode hardening: loopback bind, no wildcard CORS ---\n"
        f"export CORS_ORIGINS='{origins}'\n"
        "# Never install into whatever virtualenv the caller's shell has active.\n"
        "unset VIRTUAL_ENV\n"
        "# ------------------------------------------------------------\n"
    )
    lines = hardened.splitlines(keepends=True)
    insert_at = 1 if lines and lines[0].startswith("#!") else 0
    lines.insert(insert_at, preamble)
    return "".join(lines)


def write_hardened_start_script(install_dir: str, stock_script: str, port: int) -> str:
    """Write ``start-voicemode.sh`` next to the stock script and return its path."""
    with open(stock_script, "r") as f:
        content = f.read()
    hardened = harden_start_script(content, port)
    target = os.path.join(install_dir, HARDENED_SCRIPT_NAME)
    with open(target, "w") as f:
        f.write(hardened)
    os.chmod(target, 0o755)
    return target


def preferred_start_script(kokoro_dir) -> Optional[Path]:
    """The hardened script if the installer wrote one, else None."""
    candidate = Path(kokoro_dir) / HARDENED_SCRIPT_NAME
    return candidate if candidate.exists() else None


def verify_pinned_commit(install_dir: str, version: str) -> Tuple[bool, str]:
    """Check HEAD is the commit pinned for ``version``.

    Unknown versions fail unless VOICEMODE_KOKORO_ALLOW_UNPINNED=true.
    """
    expected = KOKORO_PINNED_COMMITS.get(version)
    if expected is None:
        if os.environ.get("VOICEMODE_KOKORO_ALLOW_UNPINNED", "").lower() in ("1", "true", "yes"):
            return True, f"{version} is not pinned; allowed by VOICEMODE_KOKORO_ALLOW_UNPINNED"
        return False, (
            f"Kokoro {version} has no pinned commit. Set VOICEMODE_KOKORO_ALLOW_UNPINNED=true "
            "to install it anyway."
        )
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=install_dir,
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except subprocess.CalledProcessError as e:
        return False, f"could not read Kokoro HEAD: {e}"
    if head != expected:
        return False, f"Kokoro {version} resolved to {head}, expected pinned {expected}"
    return True, f"{version} verified at {head}"
