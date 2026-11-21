"""voicemode-mcp-launcher -- VM-1314 Option A: smart stdio launcher.

The voicemode plugin ships ONE ``.mcp.json`` entry (``type: stdio``,
``command`` = this launcher).  The launcher -- not Claude Code's config layer
-- chooses the transport, driven by a single env var:

* ``VOICEMODE_MCP_URL`` **unset**  -> run the bundled local voicemode stdio
  server, unchanged.  This is load-bearing: most users never set the var, so
  the default path must be byte-for-byte the pre-VM-1314 behaviour and add
  zero new failure surface.  We call the server entrypoint in-process (no
  extra process, no proxy hop) -- the exact code path ``voicemode`` with no
  subcommand already takes.
* ``VOICEMODE_MCP_URL`` **set**  -> a native stdio<->Streamable-HTTP bridge to
  the remote endpoint (``VOICEMODE_MCP_TOKEN`` -> ``Authorization: Bearer``).
  No ``npx``/Node, no launch-time network fetch (see :mod:`voice_mode.mcp_bridge`).

Result: one server, one ``mcp__voicemode__*`` namespace, no second
``.mcp.json`` entry, no manual ``disabledMcpjsonServers`` step.  The
stdio<->http switch -- impossible in Claude Code's ``.mcp.json`` (a ``type``
field is not env-expandable; see VM-1314 research) -- lives in our own
process, where it is trivial.

VM-1314 defect 2 fix: this launcher is a *voicemode python* process, so it
calls ``load_voicemode_env()`` to fold ``~/.voicemode/voicemode.env`` into
``os.environ``.  Setting ``VOICEMODE_MCP_URL`` in ``voicemode.env`` therefore
now actually takes effect here (Claude Code's own ``.mcp.json`` never read
that file).  A real process env var still wins -- ``load_voicemode_env`` only
sets keys not already set.

Trust: ``VOICEMODE_MCP_URL``/``_TOKEN`` come only from the process env or the
user's own ``voicemode.env``; a project ``.voicemode.env`` cannot set them. A
token saved in ``voicemode.env`` is only sent to the URL saved beside it; to
send a token elsewhere, export it together with that URL.
"""
from __future__ import annotations

import os


def _selected_url() -> str:
    """Return the configured remote URL, or "" for the local default.

    Explicitly (re)loads ``~/.voicemode/voicemode.env`` so that, in the fresh
    launcher process, ``VOICEMODE_MCP_URL`` set in that file actually takes
    effect (VM-1314 defect 2). ``load_voicemode_env`` only sets keys not
    already in ``os.environ``, so a real exported env var still wins.
    """
    from voice_mode.config import load_voicemode_env

    load_voicemode_env()
    return os.environ.get("VOICEMODE_MCP_URL", "").strip()


def _token_for(url: str):
    """The bearer token to send to ``url``, or None.

    A token that came from voicemode.env is bound to the URL saved with it:
    if the URL now comes from somewhere else, the saved token is not sent.
    """
    import sys
    from voice_mode.config import user_config_values

    token = os.environ.get("VOICEMODE_MCP_TOKEN", "").strip() or None
    saved = user_config_values()
    saved_token = saved.get("VOICEMODE_MCP_TOKEN", "").strip()
    saved_url = saved.get("VOICEMODE_MCP_URL", "").strip()
    if token and token == saved_token and url.rstrip("/") != saved_url.rstrip("/"):
        print(
            f"voicemode: not sending the VOICEMODE_MCP_TOKEN saved in voicemode.env to "
            f"{url} (it is saved for {saved_url or 'no URL'}). Export the token "
            f"with this URL to use it here.",
            file=sys.stderr,
        )
        return None
    return token


def main() -> None:
    url = _selected_url()

    if not url:
        # LOCAL: in-process start of the bundled stdio server -- identical to
        # `voicemode` with no subcommand. No proxy, no extra process.
        from voice_mode.server import main as server_main

        server_main()
        return

    # REMOTE: native stdio<->Streamable-HTTP bridge (no npx/node).
    token = _token_for(url)
    from voice_mode.mcp_bridge import run_bridge

    run_bridge(url, token)


if __name__ == "__main__":
    main()
