"""`voicemode adapter` — install STT/TTS adapters from a git repo."""

import json
from pathlib import Path

import click

from voice_mode import adapters as A


def _fail(e: Exception):
    raise click.ClickException(str(e))


def _summary(m: A.Manifest, repo: str, subpath, sha: str) -> str:
    lines = [
        f"Adapter:  {m.name} ({m.kind}) — {m.description}",
        f"Repo:     {repo}" + (f"  path={subpath}" if subpath else ""),
        f"Commit:   {sha}",
        f"Runs:     {' '.join(m.command)} on 127.0.0.1:{m.port}",
        f"Installs: {', '.join(m.requirements) if m.requirements else '(its own package only)'}",
    ]
    if m.leaves_machine:
        lines.append(click.style(f"AUDIO LEAVES THIS MACHINE: {m.notice}", fg="red", bold=True))
    return "\n".join(lines)


@click.group()
def adapter():
    """Install and manage third-party STT/TTS adapters.

    \b
    voicemode adapter install REPO [--path SUBDIR] [--ref TAG_OR_BRANCH]
    Installing runs that repo's code on this machine. Review the commit first.
    """


@adapter.command()
@click.argument("repo")
@click.option("--path", "subpath", default=None, help="Directory in the repo that holds voicemode-adapter.json.")
@click.option("--ref", default=None, help="Tag or branch to install (default: the repo's default branch). The commit is pinned.")
@click.option("--no-enable", is_flag=True, help="Install only; do not start it or change voicemode's config.")
@click.option("--yes", "-y", is_flag=True, help="Skip the confirmation prompt.")
def install(repo, subpath, ref, no_enable, yes):
    """Fetch an adapter from REPO, install it in its own venv, and turn it on."""
    with A.tmp_clone_dir() as tmp:
        try:
            src, sha = A.fetch(repo, subpath, ref, Path(tmp) / "repo")
            m = A.parse_manifest((src / A.MANIFEST).read_text())
        except A.AdapterError as e:
            _fail(e)
        click.echo(_summary(m, repo, subpath, sha))
        if not yes and not click.confirm("Install this and run its code?", default=False):
            raise click.Abort()
        try:
            click.echo("Installing…")
            A.install(src, sha, repo, subpath)
        except (A.AdapterError, OSError) as e:
            _fail(e)
        except Exception as e:  # subprocess failure while building the venv
            _fail(f"install failed: {e}")
    if no_enable:
        click.echo(f"Installed. Turn on with: voicemode adapter on {m.name}")
        return
    _enable(m, yes=yes)


def _enable(m: A.Manifest, yes: bool):
    if m.leaves_machine and A.local_only() and not yes:
        click.echo(click.style(
            "VOICEMODE_LOCAL_ONLY is on. Voicemode itself will still refuse non-local URLs, "
            "but this adapter sends audio off the machine.", fg="yellow"))
        if not click.confirm(f"Enable {m.name} anyway?", default=False):
            click.echo(f"Left off. Enable later with: voicemode adapter on {m.name}")
            return
    if A.needs_setup(m):
        click.echo(f"{m.name} needs one-time setup (e.g. an API key): {' '.join(m.setup)}")
        if A.run_setup(m) != 0:
            raise click.ClickException(f"setup failed; re-run with: voicemode adapter setup {m.name}")
    try:
        A.enable(m)
    except A.AdapterError as e:
        _fail(e)
    click.echo(f"ON: {m.name} is running on 127.0.0.1:{m.port}, first in line, local engine as fallback.")
    if m.leaves_machine:
        click.echo(click.style("Audio is leaving this machine. `voicemode adapter off " + m.name + "` undoes it.", fg="red"))
    click.echo("Restart Claude Code so the voice server rereads its config.")


@adapter.command("list")
def list_():
    """List installed adapters."""
    names = A.installed()
    if not names:
        click.echo("No adapters installed.")
    for n in names:
        try:
            m, md = A.load(n), A.meta(n)
        except (A.AdapterError, ValueError) as e:
            click.echo(f"{n}: unreadable ({e})")
            continue
        state = "on" if A.is_wired(m) and A.healthy(m) else "off"
        warn = "  [audio leaves machine]" if m.leaves_machine else ""
        click.echo(f"{n:<16} {m.kind}  {state:<4} {md['commit'][:10]}{warn}")


@adapter.command()
@click.argument("name")
@click.option("--yes", "-y", is_flag=True)
def on(name, yes):
    """Start an installed adapter and put it first."""
    try:
        _enable(A.load(name), yes=yes)
    except A.AdapterError as e:
        _fail(e)


@adapter.command()
@click.argument("name")
def off(name):
    """Stop an adapter and restore the local-only endpoints."""
    try:
        A.disable(A.load(name))
    except A.AdapterError as e:
        _fail(e)
    click.echo(f"OFF: {name} stopped. Restart Claude Code to pick up the change.")


@adapter.command()
@click.argument("name")
def setup(name):
    """Re-run an adapter's one-time setup."""
    try:
        m = A.load(name)
    except A.AdapterError as e:
        _fail(e)
    if not m.setup:
        click.echo(f"{name} has no setup step.")
    elif A.run_setup(m) != 0:
        raise click.ClickException("setup failed")


@adapter.command()
@click.argument("name")
def status(name):
    """Show whether an adapter is wired in and healthy."""
    try:
        m, md = A.load(name), A.meta(name)
    except A.AdapterError as e:
        _fail(e)
    click.echo(json.dumps({
        "name": m.name, "kind": m.kind, "wired": A.is_wired(m), "healthy": A.healthy(m),
        "leaves_machine": m.leaves_machine, "commit": md["commit"], "repo": md["repo"],
    }, indent=2))


@adapter.command()
@click.argument("name")
@click.option("--yes", "-y", is_flag=True)
def remove(name, yes):
    """Stop and delete an adapter."""
    if not yes:
        click.confirm(f"Remove {name}?", abort=True)
    try:
        A.remove(name)
    except A.AdapterError as e:
        _fail(e)
    click.echo(f"Removed {name}.")
