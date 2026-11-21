"""`voicemode serve` must not expose MCP tools beyond loopback without a credential.

Regression for: `voicemode serve --host 0.0.0.0` allowed every private-network
IP by default while the token and secret were empty, so any LAN peer could call
voice capture and service management with no credential. A private-CIDR
allowlist is a network restriction, not authentication.
"""

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

STRONG_TOKEN = "a" * 32  # meets the minimum length


def _invoke(args, serve_token="", serve_secret="", allowed_ips=""):
    """Run `voicemode serve <args>` with uvicorn and the MCP app mocked out.

    Config-derived defaults are pinned so the developer's voicemode.env cannot
    change the outcome. Returns (result, uvicorn_mock, app_mock).
    """
    from voice_mode.cli import voice_mode_main_cli

    mock_mcp = MagicMock()
    mock_app = MagicMock()
    mock_mcp.http_app.return_value = mock_app

    with patch.dict("sys.modules", {"voice_mode.server": MagicMock(mcp=mock_mcp)}), \
            patch("voice_mode.cli.SERVE_TOKEN", serve_token), \
            patch("voice_mode.cli.SERVE_SECRET", serve_secret), \
            patch("voice_mode.cli.SERVE_ALLOWED_IPS", allowed_ips), \
            patch("voice_mode.cli.SERVE_ALLOW_LOCAL", True), \
            patch("voice_mode.cli.SERVE_ALLOW_ANTHROPIC", False), \
            patch("voice_mode.cli.SERVE_ALLOW_TAILSCALE", False), \
            patch("uvicorn.run") as mock_uvicorn:
        result = CliRunner().invoke(voice_mode_main_cli, ["serve", *args])
    return result, mock_uvicorn, mock_app


def _middleware_names(mock_app):
    return [c.args[0].__name__ for c in mock_app.add_middleware.call_args_list]


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.5", "my-laptop.local"])
def test_non_loopback_bind_without_token_is_refused(host):
    result, mock_uvicorn, _ = _invoke(["--host", host])
    assert result.exit_code != 0
    assert not mock_uvicorn.called, f"server started on {host} with no credential"
    assert "--token" in result.output


def test_private_cidr_allowlist_does_not_count_as_authentication():
    result, mock_uvicorn, _ = _invoke(
        ["--host", "0.0.0.0", "--allow-ip", "192.168.1.0/24"]
    )
    assert result.exit_code != 0
    assert not mock_uvicorn.called


def test_secret_path_alone_does_not_permit_non_loopback_bind():
    result, mock_uvicorn, _ = _invoke(
        ["--host", "0.0.0.0", "--secret", "some-secret-path-segment"]
    )
    assert result.exit_code != 0
    assert not mock_uvicorn.called


def test_short_token_is_refused_for_non_loopback_bind():
    result, mock_uvicorn, _ = _invoke(["--host", "0.0.0.0", "--token", "a" * 31])
    assert result.exit_code != 0
    assert not mock_uvicorn.called
    assert "32" in result.output


def test_whitespace_token_is_refused():
    result, mock_uvicorn, _ = _invoke(["--host", "0.0.0.0", "--token", " " * 40])
    assert result.exit_code != 0
    assert not mock_uvicorn.called


def test_short_token_from_config_is_refused():
    result, mock_uvicorn, _ = _invoke(["--host", "0.0.0.0"], serve_token="short")
    assert result.exit_code != 0
    assert not mock_uvicorn.called


def test_strong_token_permits_non_loopback_bind_and_keeps_allowlist():
    result, mock_uvicorn, mock_app = _invoke(
        ["--host", "0.0.0.0", "--token", STRONG_TOKEN]
    )
    assert result.exit_code == 0, result.output
    assert mock_uvicorn.called
    names = _middleware_names(mock_app)
    assert "TokenAuthMiddleware" in names
    assert "IPAllowlistMiddleware" in names  # still an additional restriction
    assert STRONG_TOKEN not in result.output  # masked in the banner


def test_strong_token_from_config_permits_non_loopback_bind():
    result, mock_uvicorn, mock_app = _invoke(["--host", "0.0.0.0"], serve_token=STRONG_TOKEN)
    assert result.exit_code == 0, result.output
    assert mock_uvicorn.called
    assert "TokenAuthMiddleware" in _middleware_names(mock_app)


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "127.0.0.2"])
def test_loopback_bind_needs_no_token(host):
    result, mock_uvicorn, _ = _invoke(["--host", host])
    assert result.exit_code == 0, result.output
    assert mock_uvicorn.called


def test_default_bind_needs_no_token():
    result, mock_uvicorn, _ = _invoke([])
    assert result.exit_code == 0, result.output
    assert mock_uvicorn.called
