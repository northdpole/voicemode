"""Path-traversal guards for clone profile names and ref_audio paths.

`voicemode clone add <name>` turns the name into a directory under
VOICES_DIR. A name like ``../../escape`` must never create or write files
outside it, and `clone remove` must never delete a file outside it.
"""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from voice_mode.tools.impressions import profiles
from voice_mode.tools.impressions.profiles import clone_add, clone_remove


@pytest.fixture
def env(tmp_path, monkeypatch):
    # Nest VOICES_DIR so "../../x" still lands inside tmp_path on pre-fix code.
    voices_dir = tmp_path / "home" / "vm" / "voices"
    voices_dir.mkdir(parents=True)
    voices_json = tmp_path / "home" / "vm" / "voices.json"
    monkeypatch.setattr(profiles, "VOICES_DIR", voices_dir)
    monkeypatch.setattr(profiles, "VOICES_JSON", voices_json)
    src = tmp_path / "in.wav"
    src.write_bytes(b"RIFF0000WAVE")
    return {"root": tmp_path, "voices_dir": voices_dir, "voices_json": voices_json, "src": src}


def _fake_normalise(src, dest):
    Path(dest).write_bytes(b"RIFF0000WAVE")


async def _add(name, src):
    with patch.object(profiles, "_validate_clip_length", return_value=5.0), \
         patch.object(profiles, "_normalise_audio", side_effect=_fake_normalise) as norm, \
         patch.object(profiles, "_probe_duration_seconds", return_value=5.0):
        result = await clone_add(name, str(src), ref_text="hello there")
    return result, norm


def _files_outside(root: Path, voices_dir: Path, src: Path):
    return sorted(
        p for p in root.rglob("*")
        if p.is_file() and p != src and voices_dir not in p.parents
    )


UNSAFE_NAMES = [
    "../escape",
    "../../escape",
    "..",
    ".",
    "a/b",
    "a\\b",
    "ABSOLUTE",  # replaced with an absolute path inside tmp_path
    "~/escape",
    ".hidden",
    "-dash",
    "a" * 65,
    "nul\x00byte",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("name", UNSAFE_NAMES)
async def test_clone_add_rejects_unsafe_name_and_writes_nothing(env, name):
    if name == "ABSOLUTE":
        # Keep it under tmp_path: pre-fix code really writes here.
        name = str(env["root"] / "abs-escape")
    result, norm = await _add(name, env["src"])

    assert result["success"] is False
    assert "Invalid voice name" in result["error"]
    norm.assert_not_called()
    assert _files_outside(env["root"], env["voices_dir"], env["src"]) == []
    assert list(env["voices_dir"].iterdir()) == []
    assert not env["voices_json"].exists()


@pytest.mark.asyncio
async def test_clone_add_rejects_symlink_escape(env):
    outside = env["root"] / "outside"
    outside.mkdir()
    (env["voices_dir"] / "evil").symlink_to(outside, target_is_directory=True)

    result, norm = await _add("evil", env["src"])

    assert result["success"] is False
    norm.assert_not_called()
    assert list(outside.iterdir()) == []
    assert not env["voices_json"].exists()


@pytest.mark.asyncio
async def test_clone_add_accepts_safe_slug(env):
    result, _ = await _add("  My-Voice_2  ", env["src"])

    assert result["success"] is True, result
    assert result["name"] == "my-voice_2"
    assert (env["voices_dir"] / "my-voice_2" / "default.wav").is_file()
    assert (env["voices_dir"] / "my-voice_2" / "default.txt").is_file()


@pytest.mark.asyncio
@pytest.mark.parametrize("ref", ["../../victim.wav", "ABSOLUTE"])
async def test_clone_remove_never_deletes_outside_voices_dir(env, ref):
    victim = env["root"] / "home" / "victim.wav"
    victim.write_bytes(b"keep me")
    ref_audio = str(victim) if ref == "ABSOLUTE" else ref
    env["voices_json"].write_text(json.dumps(
        {"voices": {"bad": {"ref_audio": ref_audio, "ref_text": "x"}}}
    ))

    result = await clone_remove("bad", remove_audio=True)

    assert result["success"] is True
    assert result["audio_removed"] is False
    assert victim.read_bytes() == b"keep me"


@pytest.mark.asyncio
async def test_clone_remove_resolves_relative_ref_audio_under_voices_dir(env, monkeypatch):
    """New-layout ref_audio ("name/default.wav") is relative to VOICES_DIR, not cwd."""
    cwd = env["root"] / "cwd"
    (cwd / "alice").mkdir(parents=True)
    decoy = cwd / "alice" / "default.wav"
    decoy.write_bytes(b"not a voice")
    monkeypatch.chdir(cwd)

    real = env["voices_dir"] / "alice" / "default.wav"
    real.parent.mkdir()
    real.write_bytes(b"voice")
    env["voices_json"].write_text(json.dumps(
        {"voices": {"alice": {"ref_audio": "alice/default.wav", "ref_text": "x"}}}
    ))

    result = await clone_remove("alice", remove_audio=True)

    assert result["audio_removed"] is True
    assert not real.exists()
    assert decoy.exists()
