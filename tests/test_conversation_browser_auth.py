"""scripts/conversation_browser.py must not hand transcripts to the LAN.

It used to bind 0.0.0.0:5000 with no authentication, so anyone on the network
could read ``/``, ``/api/conversations`` and ``/audio/<filename>``. Now it binds
127.0.0.1 by default, refuses a non-loopback host unless
VOICEMODE_BROWSER_TOKEN is set, and enforces that token on every route.

No audio is opened: ``/audio/`` serves a few bytes from a temp directory.
"""

import importlib
import os
import runpy
import sys
from pathlib import Path

import pytest

flask = pytest.importorskip("flask")

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "conversation_browser.py"
sys.path.insert(0, str(ROOT))

TOKEN = "s3cret-token-0123456789abcdef"


@pytest.fixture
def captured_run(tmp_path, monkeypatch):
    """Run the script as __main__ with Flask.run captured, not served."""
    monkeypatch.setenv("VOICEMODE_BASE_DIR", str(tmp_path / "vm"))
    for var in ("VOICEMODE_BROWSER_HOST", "VOICEMODE_BROWSER_TOKEN", "VOICEMODE_BROWSER_DEBUG"):
        monkeypatch.delenv(var, raising=False)
    calls = []
    monkeypatch.setattr(flask.Flask, "run", lambda self, **kw: calls.append(kw))

    def run():
        runpy.run_path(str(SCRIPT), run_name="__main__")
        return calls

    return run


def test_binds_loopback_by_default(captured_run):
    calls = captured_run()
    assert len(calls) == 1
    assert calls[0]["host"] == "127.0.0.1"


def test_refuses_non_loopback_host_without_token(captured_run, monkeypatch):
    monkeypatch.setenv("VOICEMODE_BROWSER_HOST", "0.0.0.0")
    with pytest.raises(SystemExit) as exc:
        captured_run()
    assert exc.value.code not in (0, None)


def test_refuses_short_token_on_non_loopback_host(captured_run, monkeypatch):
    monkeypatch.setenv("VOICEMODE_BROWSER_HOST", "0.0.0.0")
    monkeypatch.setenv("VOICEMODE_BROWSER_TOKEN", "short")
    with pytest.raises(SystemExit):
        captured_run()


def test_refuses_debugger_on_non_loopback_host(captured_run, monkeypatch):
    monkeypatch.setenv("VOICEMODE_BROWSER_HOST", "0.0.0.0")
    monkeypatch.setenv("VOICEMODE_BROWSER_TOKEN", TOKEN)
    monkeypatch.setenv("VOICEMODE_BROWSER_DEBUG", "1")
    with pytest.raises(SystemExit):
        captured_run()


def test_non_loopback_host_starts_with_token(captured_run, monkeypatch):
    monkeypatch.setenv("VOICEMODE_BROWSER_HOST", "0.0.0.0")
    monkeypatch.setenv("VOICEMODE_BROWSER_TOKEN", TOKEN)
    calls = captured_run()
    assert calls[0]["host"] == "0.0.0.0"
    assert calls[0]["debug"] is False


@pytest.fixture
def browser(tmp_path, monkeypatch):
    monkeypatch.setenv("VOICEMODE_BASE_DIR", str(tmp_path / "vm"))
    monkeypatch.delenv("VOICEMODE_BROWSER_TOKEN", raising=False)
    import scripts.conversation_browser as cb

    cb = importlib.reload(cb)
    audio = tmp_path / "vm" / "audio"
    audio.mkdir(parents=True)
    (audio / "old_tts.wav").write_bytes(b"flat")
    (tmp_path / "vm" / "logs").mkdir()
    cb.app.config["TESTING"] = True
    # /api/conversations calls get_all_conversations, which the script never
    # defines (pre-existing, unrelated 500). Stub it so these tests judge auth only.
    monkeypatch.setattr(cb, "get_all_conversations", lambda: [], raising=False)
    return cb


ROUTES = ("/", "/api/conversations", "/audio/old_tts.wav")


@pytest.mark.parametrize("path", ROUTES)
def test_every_route_needs_the_token_when_configured(browser, monkeypatch, path):
    monkeypatch.setitem(browser.app.config, "BROWSER_TOKEN", TOKEN)
    client = browser.app.test_client()

    assert client.get(path).status_code == 401
    assert client.get(path, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get(path + "?token=wrong").status_code == 401

    ok = client.get(path, headers={"Authorization": f"Bearer {TOKEN}"})
    assert ok.status_code == 200


def test_query_token_sets_cookie_for_audio_elements(browser, monkeypatch):
    monkeypatch.setitem(browser.app.config, "BROWSER_TOKEN", TOKEN)
    client = browser.app.test_client()

    resp = client.get(f"/?token={TOKEN}")
    assert resp.status_code == 302
    assert "token=" not in resp.headers["Location"]
    cookie = resp.headers["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie

    # <audio src="/audio/..."> cannot send a header; the cookie carries it.
    assert client.get("/audio/old_tts.wav").data == b"flat"
    assert client.get("/api/conversations").status_code == 200


def test_loopback_without_token_stays_open(browser):
    client = browser.app.test_client()
    for path in ROUTES:
        assert client.get(path).status_code == 200
