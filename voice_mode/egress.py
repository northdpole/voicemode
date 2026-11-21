"""Where speech and credentials may go.

Checked right before each outbound speech request, not when the URL lists are
built, so a destination that reaches a request by another route (a clone
profile, a CLI backend, an SDK default) is still caught.

Kept free of heavy imports: ``voice_mode.listen`` uses it without loading
``voice_mode.config``.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
from typing import Optional

from .url_match import is_openai_url, url_hostname

logger = logging.getLogger("voicemode")

# Settings only the user may set: an agent (update_config) and a project's
# .voicemode.env may not. They decide where speech, transcripts and credentials
# go, what listens on the network, or what gets executed.
TRUSTED_ONLY_KEY_PATTERN = re.compile(
    r"(BASE_URLS?|_URLS?$|_HOSTS?$|_ENDPOINTS?$|_TOKEN$|_SECRET$|_API_KEY$|_PASSWORD$"
    r"|LOCAL_ONLY|ALLOW_CLOUD|^VOICEMODE_SERVE_|^VOICEMODE_MCP_|_COMMAND$|_CMD$)"
)

# Placeholder sent to endpoints that are not trusted with the real key.
PLACEHOLDER_API_KEY = "dummy-key-for-local"


class RemoteSpeechBlocked(RuntimeError):
    """A speech request was about to leave the machine under VOICEMODE_LOCAL_ONLY."""


def _truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("true", "1", "yes", "on")


def local_only() -> bool:
    return _truthy("VOICEMODE_LOCAL_ONLY")


def is_loopback_url(url: Optional[str]) -> bool:
    """True when ``url`` points at this machine (localhost, 127.0.0.0/8, ::1)."""
    host = url_hostname(url) or ""
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def guard_speech_destination(url: Optional[str], purpose: str = "speech") -> None:
    """Fail closed if VOICEMODE_LOCAL_ONLY is on and ``url`` is not loopback."""
    if local_only() and not is_loopback_url(url):
        raise RemoteSpeechBlocked(
            f"VOICEMODE_LOCAL_ONLY is on: refusing to send {purpose} to {url!r} "
            f"(not a local address)."
        )


def _extra_key_hosts() -> set[str]:
    raw = os.environ.get("VOICEMODE_API_KEY_HOSTS", "")
    return {h.strip().lower().rstrip(".") for h in raw.split(",") if h.strip()}


def scoped_api_key(url: Optional[str], api_key: Optional[str]) -> Optional[str]:
    """Return ``api_key`` only for hosts trusted with it, else a placeholder.

    Trusted: OpenAI hosts, plus any host the user lists in
    VOICEMODE_API_KEY_HOSTS (a setting only the user can make).
    """
    if is_openai_url(url):
        return api_key  # None stays None: the SDK then fails fast, as before
    if api_key and (url_hostname(url) or "") in _extra_key_hosts():
        return api_key
    return PLACEHOLDER_API_KEY
