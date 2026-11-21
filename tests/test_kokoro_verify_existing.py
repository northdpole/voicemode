"""Existing Kokoro installs are verified, and only the hardened start script is ever run.

Regression for two gaps in the local-first hardening:
- the "already installed" branch of kokoro_install trusted the version label and
  never checked the pinned commit or local modifications;
- service start/enable fell back to the stock upstream scripts (0.0.0.0 bind)
  when start-voicemode.sh was missing.
"""
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from voice_mode.tools import service as svc
from voice_mode.tools.kokoro import install as kinstall
from voice_mode.utils.services import kokoro_hardening as kh

kokoro_install = getattr(kinstall.kokoro_install, "fn", kinstall.kokoro_install)

STOCK = """#!/bin/bash
uv run --no-sync uvicorn api.src.main:app --host 0.0.0.0 --port 8880
"""
STOCK_NAMES = ("start-gpu_mac.sh", "start-gpu.sh", "start-cpu.sh")


def _git(cwd, *args):
    proc = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false",
         "-c", "tag.gpgsign=false", *args],
        cwd=cwd, capture_output=True, text=True,
    )
    assert proc.returncode == 0, f"git {args}: {proc.stderr}"
    return proc.stdout.strip()


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    """A Kokoro-shaped git checkout at tag vtest, pinned to its HEAD."""
    repo = tmp_path / "kokoro"
    repo.mkdir()
    (repo / "main.py").write_text("print('kokoro')\n")
    for name in STOCK_NAMES:
        (repo / name).write_text(STOCK)
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "x")
    _git(repo, "tag", "vtest")
    monkeypatch.setitem(kh.KOKORO_PINNED_COMMITS, "vtest", _git(repo, "rev-parse", "HEAD"))
    monkeypatch.setenv("VOICEMODE_BASE_DIR", str(tmp_path / "vm"))
    return repo


async def _install(repo):
    update = AsyncMock(return_value={"success": True, "updated": True})
    with patch.object(kinstall, "update_kokoro_service_files", update), \
         patch.object(kinstall, "auto_migrate_if_needed", return_value=None):
        result = await kokoro_install(
            install_dir=str(repo), version="vtest", skip_deps=True, auto_enable=False,
        )
    return result, update


# --- Finding 8: existing installs go through commit + cleanliness checks ----

@pytest.mark.asyncio
async def test_existing_install_clean_and_pinned_is_accepted(checkout):
    result, update = await _install(checkout)
    assert result["success"] and result["already_installed"]
    assert Path(result["start_script"]).name == kh.HARDENED_SCRIPT_NAME
    update.assert_awaited_once()


@pytest.mark.asyncio
async def test_existing_install_with_moved_tag_is_refused(checkout, monkeypatch):
    monkeypatch.setitem(kh.KOKORO_PINNED_COMMITS, "vtest", "0" * 40)
    result, update = await _install(checkout)
    assert not result["success"]
    assert "pinned" in result["error"] and "--force" in result["error"]
    update.assert_not_awaited()
    assert not (checkout / kh.HARDENED_SCRIPT_NAME).exists()


@pytest.mark.asyncio
async def test_existing_install_with_modified_tracked_file_is_refused(checkout):
    (checkout / "main.py").write_text("import os; os.system('evil')\n")
    result, update = await _install(checkout)
    assert not result["success"]
    assert "local changes" in result["error"] and "main.py" in result["error"]
    update.assert_not_awaited()


@pytest.mark.asyncio
async def test_existing_install_hiding_changes_with_skip_worktree_is_refused(checkout):
    _git(checkout, "update-index", "--skip-worktree", "main.py")
    (checkout / "main.py").write_text("import os; os.system('evil')\n")
    assert _git(checkout, "status", "--porcelain") == ""  # porcelain alone is fooled
    result, update = await _install(checkout)
    assert not result["success"]
    assert "main.py" in result["error"]
    update.assert_not_awaited()


@pytest.mark.asyncio
async def test_existing_install_untracked_venv_is_not_a_modification(checkout):
    (checkout / ".venv").mkdir()
    (checkout / kh.HARDENED_SCRIPT_NAME).write_text("old\n")
    result, _ = await _install(checkout)
    assert result["success"]


# --- Finding 9: never fall back to a stock upstream start script ------------

def _stock_only_dir(tmp_path):
    d = tmp_path / "kokoro"
    d.mkdir()
    for name in STOCK_NAMES:
        (d / name).write_text(STOCK)
        (d / name).chmod(0o755)
    return d


@pytest.mark.parametrize("gpu", [True, False])
def test_service_config_never_points_at_stock_script(tmp_path, gpu):
    d = _stock_only_dir(tmp_path)
    with patch.object(svc, "find_kokoro_fastapi", return_value=str(d)), \
         patch.object(svc, "has_gpu_support", return_value=gpu):
        assert svc.get_service_config_vars("kokoro")["START_SCRIPT"] == ""


def test_service_config_uses_hardened_script(tmp_path):
    d = _stock_only_dir(tmp_path)
    (d / kh.HARDENED_SCRIPT_NAME).write_text("#!/bin/bash\n")
    with patch.object(svc, "find_kokoro_fastapi", return_value=str(d)):
        assert svc.get_service_config_vars("kokoro")["START_SCRIPT"] == str(d / kh.HARDENED_SCRIPT_NAME)


@pytest.mark.asyncio
@pytest.mark.parametrize("system", ["Darwin", "Linux"])
async def test_start_refuses_without_hardened_script(tmp_path, system):
    d = _stock_only_dir(tmp_path)
    popen, run = MagicMock(), MagicMock()
    with patch.object(svc, "find_kokoro_fastapi", return_value=str(d)), \
         patch.object(svc, "find_process_by_port", return_value=None), \
         patch.object(svc.platform, "system", return_value=system), \
         patch.object(svc.subprocess, "Popen", popen), \
         patch.object(svc.subprocess, "run", run):
        result = await svc.start_service("kokoro")
    assert result.startswith("❌")
    assert "voicemode service install kokoro" in result
    popen.assert_not_called()
    run.assert_not_called()  # no launchctl/systemctl start of an old service file either


@pytest.mark.asyncio
async def test_enable_refuses_without_hardened_script(tmp_path):
    d = _stock_only_dir(tmp_path)
    write, run = MagicMock(), MagicMock()
    with patch.object(svc, "find_kokoro_fastapi", return_value=str(d)), \
         patch.object(svc, "load_service_template", return_value="ExecStart={START_SCRIPT}\n"), \
         patch.object(svc.Path, "write_text", write), \
         patch.object(svc.Path, "mkdir"), \
         patch.object(svc.subprocess, "run", run):
        result = await svc.enable_service("kokoro")
    assert result.startswith("❌")
    assert "voicemode service install kokoro" in result
    write.assert_not_called()
    run.assert_not_called()
