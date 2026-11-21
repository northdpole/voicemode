"""Kokoro start script is loopback-only; installs are pinned and uv is never piped from curl."""
import subprocess
from pathlib import Path

import pytest

from voice_mode.utils.services import kokoro_hardening as kh

STOCK = """#!/bin/bash
export USE_GPU=true
uv pip install -e .
uv run --no-sync uvicorn api.src.main:app --host 0.0.0.0 --port 8880
"""


def test_bind_is_loopback_and_port_applied():
    out = kh.harden_start_script(STOCK, 9999)
    assert "0.0.0.0" not in out
    assert '--host "${VOICEMODE_KOKORO_HOST:-127.0.0.1}" --port 9999' in out
    assert out.startswith("#!/bin/bash\n")


def test_cors_restricted_to_loopback_origin():
    out = kh.harden_start_script(STOCK, 8880)
    assert "CORS_ORIGINS='[\"http://127.0.0.1:8880\",\"http://localhost:8880\"]'" in out
    assert '"*"' not in out


def test_fails_closed_without_host_flag():
    with pytest.raises(ValueError):
        kh.harden_start_script("#!/bin/bash\nuvicorn api.src.main:app --port 8880\n", 8880)


def test_fails_closed_if_wildcard_survives():
    with pytest.raises(ValueError):
        kh.harden_start_script(STOCK + "echo --bind 0.0.0.0\n", 8880)


def test_write_and_prefer_hardened_script(tmp_path):
    stock = tmp_path / "start-gpu_mac.sh"
    stock.write_text(STOCK)
    path = kh.write_hardened_start_script(str(tmp_path), str(stock), 8880)
    assert Path(path).name == kh.HARDENED_SCRIPT_NAME
    assert Path(path).stat().st_mode & 0o111
    assert kh.preferred_start_script(tmp_path) == Path(path)
    assert "0.0.0.0" in stock.read_text()  # stock untouched
    assert kh.preferred_start_script(tmp_path / "nope") is None


def _repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
                    "--allow-empty", "-m", "x"], cwd=tmp_path, check=True)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True,
                          text=True, check=True).stdout.strip()


def test_pin_matches_and_mismatches(tmp_path, monkeypatch):
    sha = _repo(tmp_path)
    monkeypatch.setitem(kh.KOKORO_PINNED_COMMITS, "vtest", sha)
    assert kh.verify_pinned_commit(str(tmp_path), "vtest")[0]
    monkeypatch.setitem(kh.KOKORO_PINNED_COMMITS, "vtest", "0" * 40)
    assert not kh.verify_pinned_commit(str(tmp_path), "vtest")[0]


def test_unknown_version_refused_unless_opted_in(tmp_path, monkeypatch):
    _repo(tmp_path)
    monkeypatch.delenv("VOICEMODE_KOKORO_ALLOW_UNPINNED", raising=False)
    assert not kh.verify_pinned_commit(str(tmp_path), "v9.9.9")[0]
    monkeypatch.setenv("VOICEMODE_KOKORO_ALLOW_UNPINNED", "true")
    assert kh.verify_pinned_commit(str(tmp_path), "v9.9.9")[0]


def test_installer_does_not_pipe_curl_to_shell():
    src = (Path(kh.__file__).parents[2] / "tools" / "kokoro" / "install.py").read_text()
    assert "| sh" not in src and "shell=True" not in src
    assert "write_hardened_start_script" in src and "verify_pinned_commit" in src
