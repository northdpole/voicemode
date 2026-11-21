"""Adapter install: manifest checks, safe fetching, env wiring, CLI flow."""
import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from voice_mode import adapters as A
from voice_mode.cli_commands.adapter import adapter as adapter_cli

GOOD = {
    "schema": 1, "name": "demo-stt", "kind": "stt", "description": "demo", "port": 2099,
    "command": ["python", "server.py"], "leaves_machine": True, "notice": "Audio goes to Demo Corp.",
}


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    (tmp_path / "home").mkdir(exist_ok=True)
    monkeypatch.delenv("VOICEMODE_LOCAL_ONLY", raising=False)
    monkeypatch.setattr(A, "_build_venv", lambda name, src, m: (A.adapters_dir() / name / "venv").mkdir())
    monkeypatch.setattr(A, "start", lambda m: None)
    monkeypatch.setattr(A, "stop", lambda m: None)
    return tmp_path / "home"


def make_repo(tmp_path, manifest=GOOD, sub="adapters/demo"):
    repo = tmp_path / "repo"
    d = repo / sub if sub else repo
    d.mkdir(parents=True)
    (d / A.MANIFEST).write_text(json.dumps(manifest))
    (d / "server.py").write_text("print('hi')\n")
    for c in (["init", "-q"], ["add", "."], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x"]):
        subprocess.run(["git", *c], cwd=repo, check=True, capture_output=True)
    return repo


@pytest.mark.parametrize("patch", [
    {"schema": 2}, {"name": "../evil"}, {"name": "Bad Name"}, {"kind": "llm"}, {"port": 80},
    {"port": "2023"}, {"command": "python x"}, {"command": []}, {"health": "health"},
    {"notice": ""},  # leaves_machine without a notice
])
def test_manifest_rejects_bad_fields(patch):
    with pytest.raises(A.AdapterError):
        A.parse_manifest(json.dumps({**GOOD, **patch}))


def test_manifest_local_adapter_needs_no_notice():
    m = A.parse_manifest(json.dumps({**GOOD, "leaves_machine": False, "notice": ""}))
    assert not m.leaves_machine


def test_fetch_with_path_pins_commit(tmp_path):
    repo = make_repo(tmp_path)
    src, sha = A.fetch(str(repo), "adapters/demo", None, tmp_path / "clone")
    assert (src / A.MANIFEST).is_file()
    assert sha == subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()


def test_fetch_rejects_path_escape(tmp_path):
    repo = make_repo(tmp_path)
    with pytest.raises(A.AdapterError, match="inside the repo"):
        A.fetch(str(repo), "../..", None, tmp_path / "clone")


def test_fetch_requires_manifest(tmp_path):
    repo = make_repo(tmp_path)
    with pytest.raises(A.AdapterError, match="no voicemode-adapter.json"):
        A.fetch(str(repo), "adapters", None, tmp_path / "clone")


@pytest.mark.parametrize("bad", ["http://example.com/x.git", "ext::sh -c id", "file:///etc", "ftp://x/y"])
def test_repo_scheme_allowlist(bad, tmp_path):
    with pytest.raises(A.AdapterError):
        A.fetch(bad, None, None, tmp_path / "c")


def test_wire_puts_adapter_first_and_unwire_restores(home):
    m = A.parse_manifest(json.dumps(GOOD))
    A.set_env_var("VOICEMODE_STT_BASE_URLS", "http://127.0.0.1:2022/v1")
    A.set_env_var("VOICEMODE_SAVE_AUDIO", "false")
    A.wire(m)
    assert A._read_env_var("VOICEMODE_STT_BASE_URLS") == "http://127.0.0.1:2099/v1,http://127.0.0.1:2022/v1"
    A.wire(m)  # idempotent
    assert A._read_env_var("VOICEMODE_STT_BASE_URLS").count("2099") == 1
    A.unwire(m)
    assert A._read_env_var("VOICEMODE_STT_BASE_URLS") == "http://127.0.0.1:2022/v1"
    assert A._read_env_var("VOICEMODE_SAVE_AUDIO") == "false"  # untouched
    assert (A.env_file().stat().st_mode & 0o777) == 0o600


def test_cli_install_end_to_end(home, tmp_path):
    repo = make_repo(tmp_path)
    r = CliRunner().invoke(adapter_cli, ["install", str(repo), "--path", "adapters/demo", "--yes"])
    assert r.exit_code == 0, r.output
    assert "AUDIO LEAVES THIS MACHINE" in r.output and "Audio goes to Demo Corp." in r.output
    assert A.installed() == ["demo-stt"]
    assert A.is_wired(A.load("demo-stt"))
    assert not (A.adapters_dir() / "demo-stt" / "src" / ".git").exists()
    assert len(A.meta("demo-stt")["commit"]) == 40
    assert (A.adapters_dir() / "demo-stt").stat().st_mode & 0o777 == 0o700
    r = CliRunner().invoke(adapter_cli, ["remove", "demo-stt", "--yes"])
    assert r.exit_code == 0 and A.installed() == []
    assert not A.is_wired(A.parse_manifest(json.dumps(GOOD)))


def test_install_aborts_without_consent(home, tmp_path):
    repo = make_repo(tmp_path)
    r = CliRunner().invoke(adapter_cli, ["install", str(repo), "--path", "adapters/demo"], input="n\n")
    assert r.exit_code != 0 and A.installed() == []


def test_local_only_blocks_leaky_adapter_without_consent(home, tmp_path):
    A.set_env_var("VOICEMODE_LOCAL_ONLY", "true")
    repo = make_repo(tmp_path)
    r = CliRunner().invoke(adapter_cli, ["install", str(repo), "--path", "adapters/demo"], input="y\nn\n")
    assert "VOICEMODE_LOCAL_ONLY is on" in r.output
    assert A.installed() == ["demo-stt"] and not A.is_wired(A.load("demo-stt"))


def test_failed_install_leaves_nothing(home, tmp_path, monkeypatch):
    def boom(*a):
        raise RuntimeError("pip exploded")
    monkeypatch.setattr(A, "_build_venv", boom)
    repo = make_repo(tmp_path)
    r = CliRunner().invoke(adapter_cli, ["install", str(repo), "--path", "adapters/demo", "--yes"])
    assert r.exit_code != 0 and A.installed() == [] and not (A.adapters_dir() / "demo-stt").exists()
