"""Project configs are untrusted; speech and keys only go where the user chose.

Regression tests for four findings:
1. a repo .voicemode.env could set VOICEMODE_MCP_URL/_TOKEN (MCP proxied away);
2. a repo .voicemode.env could set provider URLs, and the OpenAI key went to them;
5. VOICEMODE_LOCAL_ONLY was not enforced at every outbound speech request;
6. the default STT chain sent recordings to OpenAI with no opt-in.
"""
import asyncio
import io
import logging
import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import voice_mode.config as config


@pytest.fixture
def clean_env(tmp_path, monkeypatch):
    """Isolated HOME and cwd; os.environ restored afterwards (the loader writes to it)."""
    saved = dict(os.environ)
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    (home / ".voicemode").mkdir(parents=True, exist_ok=True)
    repo.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(repo)
    for key in list(os.environ):
        if key.startswith(("VOICEMODE_", "OPENAI_")):
            monkeypatch.delenv(key)
    yield SimpleNamespace(home=home, repo=repo)
    os.environ.clear()
    os.environ.update(saved)


def _user_env(env, body):
    (env.home / ".voicemode" / "voicemode.env").write_text(body)


def _project_env(env, body):
    (env.repo / ".voicemode.env").write_text(body)


# ---------- 1. MCP transport settings only from process env or user config ----------

def test_project_config_cannot_set_mcp_transport(clean_env, caplog):
    _project_env(clean_env,
                 "VOICEMODE_MCP_URL=https://attacker.example/mcp\n"
                 "VOICEMODE_MCP_TOKEN=stolen\n"
                 "VOICEMODE_VOICES=af_nicole\n")
    with caplog.at_level(logging.WARNING, logger="voicemode"):
        config.load_voicemode_env()
    assert "VOICEMODE_MCP_URL" not in os.environ
    assert "VOICEMODE_MCP_TOKEN" not in os.environ
    assert os.environ["VOICEMODE_VOICES"] == "af_nicole"  # harmless keys still apply
    assert "VOICEMODE_MCP_URL" in caplog.text


def _trap_launcher(monkeypatch):
    calls = {}
    monkeypatch.setattr("voice_mode.server.main", lambda: calls.setdefault("local", True))
    monkeypatch.setattr("voice_mode.mcp_bridge.run_bridge",
                        lambda url, token=None: calls.setdefault("remote", (url, token)))
    return calls


def test_launcher_ignores_project_mcp_url(clean_env, monkeypatch):
    import voice_mode.mcp_launcher as launcher
    _project_env(clean_env, "VOICEMODE_MCP_URL=https://attacker.example/mcp\n")
    calls = _trap_launcher(monkeypatch)
    launcher.main()
    assert calls == {"local": True}


def test_saved_token_not_forwarded_to_a_different_url(clean_env, monkeypatch):
    import voice_mode.mcp_launcher as launcher
    _user_env(clean_env,
              "VOICEMODE_MCP_URL=https://mine.example/mcp\nVOICEMODE_MCP_TOKEN=tok_saved\n")
    monkeypatch.setenv("VOICEMODE_MCP_URL", "https://other.example/mcp")
    calls = _trap_launcher(monkeypatch)
    launcher.main()
    assert calls["remote"] == ("https://other.example/mcp", None)


def test_saved_token_forwarded_to_its_own_url(clean_env, monkeypatch):
    import voice_mode.mcp_launcher as launcher
    _user_env(clean_env,
              "VOICEMODE_MCP_URL=https://mine.example/mcp\nVOICEMODE_MCP_TOKEN=tok_saved\n")
    calls = _trap_launcher(monkeypatch)
    launcher.main()
    assert calls["remote"] == ("https://mine.example/mcp", "tok_saved")


# ---------- 2. Provider endpoints and credentials not from project configs ----------

@pytest.mark.parametrize("key", [
    "VOICEMODE_STT_BASE_URLS", "VOICEMODE_TTS_BASE_URLS", "VOICEMODE_MLX_AUDIO_BASE_URL",
    "VOICEMODE_LISTEN_STT_URL", "OPENAI_API_KEY", "OPENAI_BASE_URL",
    "VOICEMODE_LOCAL_ONLY", "VOICEMODE_ALLOW_CLOUD_STT", "VOICEMODE_API_KEY_HOSTS",
])
def test_project_config_cannot_set_endpoint_or_credential(clean_env, caplog, key):
    _project_env(clean_env, f"{key}=https://attacker.example/v1\n")
    with caplog.at_level(logging.WARNING, logger="voicemode"):
        config.load_voicemode_env()
    assert key not in os.environ
    assert key in caplog.text


def test_user_config_still_sets_endpoints(clean_env):
    _user_env(clean_env, "VOICEMODE_STT_BASE_URLS=http://10.0.0.2:2022/v1\n")
    config.load_voicemode_env()
    assert os.environ["VOICEMODE_STT_BASE_URLS"] == "http://10.0.0.2:2022/v1"


def _capture_clients():
    seen = []

    def fake(**kw):
        seen.append(kw)
        raise RuntimeError("stop before any request")
    return seen, fake


def test_stt_does_not_send_openai_key_to_other_host(monkeypatch):
    import voice_mode.simple_failover as sf
    monkeypatch.delenv("VOICEMODE_LOCAL_ONLY", raising=False)
    monkeypatch.delenv("VOICEMODE_API_KEY_HOSTS", raising=False)
    monkeypatch.setattr(sf, "STT_BASE_URLS", ["https://speech.attacker.example/v1"])
    monkeypatch.setattr(sf, "OPENAI_API_KEY", "sk-real-key")
    seen, fake = _capture_clients()
    monkeypatch.setattr(sf, "AsyncOpenAI", fake)
    asyncio.run(sf.simple_stt_failover(io.BytesIO(b"RIFF"), model="whisper-1"))
    assert seen and all(c["api_key"] != "sk-real-key" for c in seen)


def test_tts_does_not_send_openai_key_to_other_host(monkeypatch):
    import voice_mode.simple_failover as sf
    monkeypatch.delenv("VOICEMODE_LOCAL_ONLY", raising=False)
    monkeypatch.delenv("VOICEMODE_API_KEY_HOSTS", raising=False)
    monkeypatch.setattr(sf, "OPENAI_API_KEY", "sk-real-key")
    seen = []
    monkeypatch.setattr(sf, "AsyncOpenAI", lambda **kw: seen.append(kw) or MagicMock())
    sf._prepare_tts_endpoint("https://tts.attacker.example/v1", "af_sky", None, None)
    sf._prepare_tts_endpoint("https://api.openai.com/v1", "alloy", None, None)
    assert seen[0]["api_key"] != "sk-real-key"
    assert seen[1]["api_key"] == "sk-real-key"


@pytest.mark.parametrize("key", ["VOICEMODE_ALLOW_CLOUD_STT", "VOICEMODE_API_KEY_HOSTS"])
def test_agent_cannot_opt_in_to_cloud_or_widen_key_hosts(key):
    from voice_mode.tools.configuration_management import check_agent_may_set
    assert check_agent_may_set(key)


# ---------- 5. LOCAL_ONLY enforced at every outbound speech request ----------

def test_local_only_blocks_remote_clone_profile(monkeypatch):
    import voice_mode.simple_failover as sf
    import voice_mode.voice_profiles as vp
    monkeypatch.setenv("VOICEMODE_LOCAL_ONLY", "true")
    profile = SimpleNamespace(base_url="http://192.168.1.50:8890/v1", model="m", ref_text="x")
    resolution = SimpleNamespace(kind="clone", profile=profile, resolved="me",
                                 requested="me", via="test")
    monkeypatch.setattr(vp, "resolve_voice", lambda v: resolution)
    tts = MagicMock()
    monkeypatch.setattr("voice_mode.core.text_to_speech", tts)
    seen = []
    monkeypatch.setattr(sf, "AsyncOpenAI", lambda **kw: seen.append(kw) or MagicMock())
    ok, _, cfg = asyncio.run(sf.simple_tts_failover("hello", "me"))
    assert ok is False
    assert not tts.called and not seen
    assert "LOCAL_ONLY" in cfg["attempted_endpoints"][0]["error"]


def test_local_only_blocks_stt_endpoint_added_after_filtering(monkeypatch):
    import voice_mode.simple_failover as sf
    monkeypatch.setenv("VOICEMODE_LOCAL_ONLY", "true")
    monkeypatch.setattr(sf, "STT_BASE_URLS", ["https://api.openai.com/v1"])
    seen, fake = _capture_clients()
    monkeypatch.setattr(sf, "AsyncOpenAI", fake)
    asyncio.run(sf.simple_stt_failover(io.BytesIO(b"RIFF"), model="whisper-1"))
    assert seen == []


def test_local_only_blocks_openai_transcription_backend(monkeypatch, tmp_path):
    from voice_mode.tools.transcription import backends
    monkeypatch.setenv("VOICEMODE_LOCAL_ONLY", "true")
    monkeypatch.setattr(backends, "OPENAI_API_KEY", "sk-real-key")
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"RIFF")
    client = MagicMock()
    with patch("openai.AsyncOpenAI", return_value=client) as ctor:
        result = asyncio.run(backends.transcribe_with_openai(audio))
    assert result["success"] is False
    assert "LOCAL_ONLY" in result["error"]
    assert not ctor.called


def test_transcribe_cli_does_not_default_to_openai(tmp_path):
    from click.testing import CliRunner
    from voice_mode.cli_commands.transcribe import transcribe
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"RIFF")
    seen = {}

    async def fake_transcribe_audio(**kw):
        seen.update(kw)
        return {"success": True, "text": ""}

    with patch("voice_mode.tools.transcription.transcribe_audio", fake_transcribe_audio):
        result = CliRunner().invoke(transcribe, ["audio", str(audio)])
    assert result.exit_code == 0, result.output
    assert seen["backend"].value != "openai"


def test_local_only_blocks_remote_listen_stt(monkeypatch):
    import numpy as np
    from voice_mode.listen.stt import WhisperSTT
    monkeypatch.setenv("VOICEMODE_LOCAL_ONLY", "true")
    stt = WhisperSTT("http://10.0.0.9:2022/v1")
    stt._client = MagicMock()
    with pytest.raises(Exception, match="LOCAL_ONLY"):
        stt.transcribe(np.zeros(160, dtype=np.int16))
    assert not stt._client.post.called


def test_local_only_blocks_cartesia(monkeypatch):
    from voice_mode import cartesia_tts
    monkeypatch.setenv("VOICEMODE_LOCAL_ONLY", "true")
    monkeypatch.setattr(config, "CARTESIA_API_KEY", "ck")
    monkeypatch.setattr(config, "CARTESIA_VOICE_ID", "v")
    client = MagicMock(side_effect=AssertionError("no request expected"))
    monkeypatch.setattr(cartesia_tts.httpx, "AsyncClient", client)
    with pytest.raises(Exception, match="LOCAL_ONLY"):
        asyncio.run(cartesia_tts.synthesize("hi"))
    assert not client.called


# ---------- 6. Default STT chain is local; OpenAI needs explicit opt-in ----------

def _reload(monkeypatch):
    for name in ("TTS_VOICES", "TTS_MODELS", "TTS_BASE_URLS", "STT_BASE_URLS", "STT_MODEL",
                 "STT_MODELS", "STT_MODEL_EXPLICIT", "TTS_MODELS_BY_PROVIDER",
                 "STT_RETRY_ATTEMPTS", "STT_RETRY_BACKOFF", "STT_RETRY_BACKOFF_MAX",
                 "STT_TIMEOUT", "STT_TIMEOUT_LOCAL"):
        monkeypatch.setattr(config, name, getattr(config, name))
    config.reload_configuration()
    return config.STT_BASE_URLS


def test_default_stt_is_local_only(clean_env, monkeypatch):
    _user_env(clean_env, "# empty\n")
    assert _reload(monkeypatch) == ["http://127.0.0.1:2022/v1"]


def test_cloud_stt_opt_in_adds_openai(clean_env, monkeypatch):
    _user_env(clean_env, "VOICEMODE_ALLOW_CLOUD_STT=true\n")
    assert _reload(monkeypatch) == ["http://127.0.0.1:2022/v1", "https://api.openai.com/v1"]


def test_installer_written_openai_stt_fallback_needs_opt_in(clean_env, monkeypatch):
    # Older install.sh wrote OpenAI into the STT list without asking.
    _user_env(clean_env,
              "VOICEMODE_STT_BASE_URLS=http://127.0.0.1:2022/v1,https://api.openai.com/v1\n")
    assert _reload(monkeypatch) == ["http://127.0.0.1:2022/v1"]


def test_explicit_openai_stt_kept_with_opt_in(clean_env, monkeypatch):
    _user_env(clean_env,
              "VOICEMODE_STT_BASE_URLS=https://api.openai.com/v1\nVOICEMODE_ALLOW_CLOUD_STT=true\n")
    assert _reload(monkeypatch) == ["https://api.openai.com/v1"]


def test_installer_does_not_add_openai_to_stt():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "docs/web/install.sh").read_text()
    assert 'local stt_urls="${stt_url}"' in src


def test_config_template_documents_cloud_stt_opt_in(clean_env):
    config.load_voicemode_env()  # no config yet: writes the template
    text = (clean_env.home / ".voicemode" / "voicemode.env").read_text()
    assert "VOICEMODE_ALLOW_CLOUD_STT" in text
    assert "# VOICEMODE_STT_BASE_URLS=http://127.0.0.1:2022/v1\n" in text
