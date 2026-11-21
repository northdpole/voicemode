"""Install and run third-party STT/TTS adapters from a git repo.

An adapter is a small loopback HTTP server that speaks the OpenAI audio API and
is described by a ``voicemode-adapter.json`` manifest. Installing one runs that
repo's code on this machine, so nothing here is silent: the caller shows the
repo, the pinned commit and the manifest first, and an adapter that sends audio
off the machine must say so (``leaves_machine``).

State lives in ``~/.voicemode/adapters/<name>/`` (mode 0700):
``src/`` (copy of the adapter), ``venv/`` (its own environment), ``install.json``.
"""

from __future__ import annotations

import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from voice_mode.private_fs import chmod_private_file, ensure_private_dir

MANIFEST = "voicemode-adapter.json"
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,31}$")
KINDS = {"stt": "VOICEMODE_STT_BASE_URLS", "tts": "VOICEMODE_TTS_BASE_URLS"}
LABEL_PREFIX = "com.voicemode.adapter."


class AdapterError(Exception):
    pass


def home() -> Path:
    return Path.home() / ".voicemode"


def adapters_dir() -> Path:
    return home() / "adapters"


def env_file() -> Path:
    return home() / "voicemode.env"


@dataclass
class Manifest:
    name: str
    kind: str
    description: str
    port: int
    command: list[str]
    health: str = "/health"
    leaves_machine: bool = False
    notice: str = ""
    requirements: list[str] | None = None
    setup: list[str] | None = None
    setup_check: list[str] | None = None


def parse_manifest(raw: str) -> Manifest:
    try:
        d = json.loads(raw)
    except ValueError as e:
        raise AdapterError(f"{MANIFEST} is not valid JSON: {e}")
    if not isinstance(d, dict) or d.get("schema") != 1:
        raise AdapterError(f"{MANIFEST}: unsupported or missing \"schema\" (expected 1)")

    def strs(key, required=False):
        v = d.get(key)
        if v is None:
            if required:
                raise AdapterError(f"{MANIFEST}: \"{key}\" is required")
            return None
        if not isinstance(v, list) or not v or not all(isinstance(x, str) and x for x in v):
            raise AdapterError(f"{MANIFEST}: \"{key}\" must be a non-empty list of strings")
        return v

    name = d.get("name", "")
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise AdapterError(f"{MANIFEST}: \"name\" must match {NAME_RE.pattern}")
    if d.get("kind") not in KINDS:
        raise AdapterError(f"{MANIFEST}: \"kind\" must be one of {sorted(KINDS)}")
    port = d.get("port")
    if not isinstance(port, int) or isinstance(port, bool) or not 1024 <= port <= 65535:
        raise AdapterError(f"{MANIFEST}: \"port\" must be an integer 1024-65535")
    health = d.get("health", "/health")
    if not isinstance(health, str) or not health.startswith("/"):
        raise AdapterError(f"{MANIFEST}: \"health\" must be a path starting with /")
    leaves = d.get("leaves_machine", False)
    if not isinstance(leaves, bool):
        raise AdapterError(f"{MANIFEST}: \"leaves_machine\" must be true or false")
    notice = d.get("notice", "")
    if leaves and not (isinstance(notice, str) and notice.strip()):
        raise AdapterError(f"{MANIFEST}: an adapter with leaves_machine=true must give a \"notice\" saying where audio goes")
    return Manifest(
        name=name, kind=d["kind"], description=str(d.get("description", ""))[:200],
        port=port, command=strs("command", required=True), health=health,
        leaves_machine=leaves, notice=str(notice)[:500],
        requirements=strs("requirements"), setup=strs("setup"), setup_check=strs("setup_check"),
    )


# --- fetching -------------------------------------------------------------

def _check_repo(repo: str) -> str:
    if Path(repo).expanduser().is_dir():
        return str(Path(repo).expanduser().resolve())
    u = urlparse(repo)
    if u.scheme in ("https", "ssh") or re.match(r"^[\w.-]+@[\w.-]+:[\w./~-]+$", repo):
        return repo
    raise AdapterError("repo must be an https/ssh git URL (or a local directory)")


def _git(*args: str, cwd: Path | None = None) -> str:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    r = subprocess.run(
        ["git", "-c", "protocol.ext.allow=never", "-c", "protocol.file.allow=user", *args],
        cwd=cwd, capture_output=True, text=True, env=env,
    )
    if r.returncode:
        raise AdapterError(f"git {args[0]} failed: {r.stderr.strip()[:300]}")
    return r.stdout.strip()


def fetch(repo: str, subpath: str | None, ref: str | None, dest: Path) -> tuple[Path, str]:
    """Clone ``repo`` into ``dest``; return (adapter dir, commit sha)."""
    repo = _check_repo(repo)
    args = ["clone", "--quiet", "--depth", "1"]
    if ref:
        args += ["--branch", ref]
    _git(*args, "--", repo, str(dest))
    sha = _git("rev-parse", "HEAD", cwd=dest)
    root = dest.resolve()
    src = (root / subpath).resolve() if subpath else root
    if src != root and root not in src.parents:
        raise AdapterError("--path must stay inside the repo")
    if not (src / MANIFEST).is_file():
        raise AdapterError(f"no {MANIFEST} in {subpath or 'the repo root'}")
    return src, sha


# --- install / remove -----------------------------------------------------

def _venv_python(name: str) -> Path:
    return adapters_dir() / name / "venv" / "bin" / "python"


def _build_venv(name: str, src: Path, m: Manifest) -> None:
    venv = adapters_dir() / name / "venv"
    uv = shutil.which("uv")
    if uv:
        subprocess.run([uv, "venv", "--quiet", str(venv)], check=True)
        pip = [uv, "pip", "install", "--quiet", "--python", str(venv / "bin" / "python")]
    else:
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
        pip = [str(venv / "bin" / "python"), "-m", "pip", "install", "--quiet"]
    # Own environment: the adapter and its requirements never touch voicemode's.
    # VIRTUAL_ENV is dropped so uv builds this venv, not the caller's.
    env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
    targets = list(m.requirements or [])
    if (src / "pyproject.toml").is_file():
        targets.append(str(src))
    if targets:
        subprocess.run([*pip, *targets], check=True, env=env)


def install(src: Path, sha: str, repo: str, subpath: str | None) -> Manifest:
    m = parse_manifest((src / MANIFEST).read_text())
    target = adapters_dir() / m.name
    if target.exists():
        raise AdapterError(f"{m.name} is already installed; run `voicemode adapter remove {m.name}` first")
    ensure_private_dir(target)
    try:
        shutil.copytree(src, target / "src", ignore=shutil.ignore_patterns(".git"), symlinks=False)
        _build_venv(m.name, target / "src", m)
        meta = target / "install.json"
        meta.write_text(json.dumps({"repo": repo, "path": subpath, "commit": sha,
                                    "installed": int(time.time())}, indent=2))
        chmod_private_file(meta)
    except BaseException:
        shutil.rmtree(target, ignore_errors=True)
        raise
    return m


def load(name: str) -> Manifest:
    p = adapters_dir() / name / "src" / MANIFEST
    if not NAME_RE.match(name) or not p.is_file():
        raise AdapterError(f"no adapter named {name!r}; see `voicemode adapter list`")
    return parse_manifest(p.read_text())


def installed() -> list[str]:
    d = adapters_dir()
    return sorted(p.name for p in d.iterdir() if (p / "install.json").is_file()) if d.is_dir() else []


def meta(name: str) -> dict:
    return json.loads((adapters_dir() / name / "install.json").read_text())


def remove(name: str) -> None:
    m = load(name)
    disable(m)
    shutil.rmtree(adapters_dir() / name)


# --- env wiring -----------------------------------------------------------

def _read_env_var(key: str) -> str | None:
    if env_file().exists():
        for line in env_file().read_text().splitlines():
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip()
    return None


def set_env_var(key: str, value: str) -> None:
    f = env_file()
    ensure_private_dir(f.parent)
    lines = f.read_text().splitlines() if f.exists() else []
    out, done = [], False
    for line in lines:
        if line.startswith(key + "="):
            out.append(f"{key}={value}")
            done = True
        else:
            out.append(line)
    if not done:
        out.append(f"{key}={value}")
    f.write_text("\n".join(out) + "\n")
    chmod_private_file(f)


def adapter_url(m: Manifest) -> str:
    return f"http://127.0.0.1:{m.port}/v1"


def _default_urls(kind: str) -> list[str]:
    return ["http://127.0.0.1:2022/v1"] if kind == "stt" else ["http://127.0.0.1:8880/v1"]


def _urls(m: Manifest) -> list[str]:
    cur = _read_env_var(KINDS[m.kind])
    return [u for u in cur.split(",") if u] if cur else _default_urls(m.kind)


def is_wired(m: Manifest) -> bool:
    return adapter_url(m) in _urls(m)


def wire(m: Manifest) -> None:
    urls = [u for u in _urls(m) if u != adapter_url(m)]
    set_env_var(KINDS[m.kind], ",".join([adapter_url(m), *urls]))  # adapter first, local engine as fallback


def unwire(m: Manifest) -> None:
    urls = [u for u in _urls(m) if u != adapter_url(m)] or _default_urls(m.kind)
    set_env_var(KINDS[m.kind], ",".join(urls))


def local_only() -> bool:
    v = _read_env_var("VOICEMODE_LOCAL_ONLY") or os.environ.get("VOICEMODE_LOCAL_ONLY", "")
    return v.strip().strip("\"'").lower() in ("1", "true", "yes", "on")


# --- running --------------------------------------------------------------

def _argv(m: Manifest) -> list[str]:
    py = str(_venv_python(m.name))
    return [py if a == "python" else a for a in m.command]


def _log(name: str) -> Path:
    ensure_private_dir(home() / "logs")
    return home() / "logs" / f"adapter-{name}.log"


def _plist(m: Manifest) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL_PREFIX}{m.name}.plist"


def _pidfile(m: Manifest) -> Path:
    return adapters_dir() / m.name / "pid"


def healthy(m: Manifest, timeout: float = 1.0) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{m.port}{m.health}", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def start(m: Manifest) -> None:
    stop(m)
    argv, cwd = _argv(m), str(adapters_dir() / m.name / "src")
    if sys.platform == "darwin":  # survives reboot, restarts on crash
        p = _plist(m)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(plistlib.dumps({
            "Label": LABEL_PREFIX + m.name, "ProgramArguments": argv, "WorkingDirectory": cwd,
            "RunAtLoad": True, "KeepAlive": True,
            "StandardOutPath": str(_log(m.name)), "StandardErrorPath": str(_log(m.name)),
        }))
        subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(p)], check=False, capture_output=True)
    else:
        log = open(_log(m.name), "ab")
        proc = subprocess.Popen(argv, cwd=cwd, stdout=log, stderr=log, start_new_session=True)
        _pidfile(m).write_text(str(proc.pid))
    for _ in range(40):
        if healthy(m):
            return
        time.sleep(0.5)
    stop(m)
    raise AdapterError(f"{m.name} did not become healthy; see {_log(m.name)}")


def stop(m: Manifest) -> None:
    if sys.platform == "darwin":
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL_PREFIX}{m.name}"],
                       check=False, capture_output=True)
        _plist(m).unlink(missing_ok=True)
    elif _pidfile(m).exists():
        try:
            os.kill(int(_pidfile(m).read_text()), 15)
        except (OSError, ValueError):
            pass
        _pidfile(m).unlink(missing_ok=True)


def enable(m: Manifest) -> None:
    start(m)
    wire(m)


def disable(m: Manifest) -> None:
    stop(m)
    unwire(m)


def _run_in_venv(m: Manifest, cmd: list[str], quiet: bool = False) -> int:
    exe = _venv_python(m.name).parent / cmd[0]
    argv = [str(exe) if exe.exists() else cmd[0], *cmd[1:]]
    out = subprocess.DEVNULL if quiet else None
    return subprocess.run(argv, cwd=adapters_dir() / m.name / "src", stdout=out, stderr=out).returncode


def needs_setup(m: Manifest) -> bool:
    if not m.setup:
        return False
    return not m.setup_check or _run_in_venv(m, m.setup_check, quiet=True) != 0


def run_setup(m: Manifest) -> int:
    return _run_in_venv(m, m.setup) if m.setup else 0


def tmp_clone_dir() -> tempfile.TemporaryDirectory:
    return tempfile.TemporaryDirectory(prefix="voicemode-adapter-")
