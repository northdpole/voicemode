"""Tests for local-first hardening: loopback binds, owner-only files, config guard."""
import importlib
import os
import stat
from pathlib import Path

import pytest

from voice_mode.private_fs import ensure_private_dir, open_private_append


def mode(p):
    return stat.S_IMODE(os.stat(p).st_mode)


def test_ensure_private_dir_is_0700(tmp_path):
    d = ensure_private_dir(tmp_path / "a" / "b")
    assert mode(d) == 0o700
    assert mode(tmp_path / "a") == 0o700


def test_open_private_append_is_0600(tmp_path):
    f = tmp_path / "events.jsonl"
    with open_private_append(f) as fh:
        fh.write("x\n")
    assert mode(f) == 0o600


def test_whisper_script_binds_loopback_by_default():
    root = Path(__file__).resolve().parents[1] / "voice_mode"
    script = (root / "templates/scripts/start-whisper-server.sh").read_text()
    assert "--host 0.0.0.0" not in script
    assert script.count('--host "$WHISPER_HOST"') == 2
    assert "${VOICEMODE_WHISPER_HOST:-127.0.0.1}" in script
    installer = (root / "tools/whisper/install.py").read_text()
    assert "--host 0.0.0.0" not in installer
    assert "${{VOICEMODE_WHISPER_HOST:-127.0.0.1}}" in installer


@pytest.mark.parametrize("url,ok", [
    ("http://127.0.0.1:8880/v1", True),
    ("http://localhost:2022/v1", True),
    ("http://[::1]:8880/v1", True),
    ("https://api.openai.com/v1", False),
    ("http://192.168.1.5:8880/v1", False),
    ("http://127.0.0.1.evil.com/v1", False),
])
def test_is_loopback_url(url, ok):
    from voice_mode.config import _is_loopback_url
    assert _is_loopback_url(url) is ok


def test_apply_local_only(monkeypatch):
    from voice_mode.config import apply_local_only
    urls = ["http://127.0.0.1:8880/v1", "https://api.openai.com/v1"]
    monkeypatch.delenv("VOICEMODE_LOCAL_ONLY", raising=False)
    assert apply_local_only(urls) == urls
    monkeypatch.setenv("VOICEMODE_LOCAL_ONLY", "true")
    assert apply_local_only(urls) == ["http://127.0.0.1:8880/v1"]


@pytest.mark.parametrize("key", [
    "PATH", "LD_PRELOAD", "OPENAI_API_KEY",
    "VOICEMODE_TTS_BASE_URLS", "VOICEMODE_STT_BASE_URLS",
    "VOICEMODE_WHISPER_HOST", "VOICEMODE_LOCAL_ONLY", "VOICEMODE_SERVE_TOKEN",
])
def test_agent_guard_refuses(key):
    from voice_mode.tools.configuration_management import check_agent_may_set
    assert check_agent_may_set(key)


@pytest.mark.parametrize("key", ["VOICEMODE_VOICES", "VOICEMODE_VAD_AGGRESSIVENESS", "VOICEMODE_TTS_SPEED"])
def test_agent_guard_allows(key):
    from voice_mode.tools.configuration_management import check_agent_may_set
    assert check_agent_may_set(key) is None
