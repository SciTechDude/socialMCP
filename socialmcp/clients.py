"""Automatic MCP client registration, for people who should never see JSON.

The target user is a salesperson or a founder, not a developer. Asking them to
locate `claude_desktop_config.json`, understand JSON merging, and paste an
absolute interpreter path is where adoption dies. `socialmcp setup` finds the
MCP clients they already have installed and writes the config for them.

Rules this module holds itself to:
  * Never clobber. Existing config is read, merged, and written back; any file
    we touch is backed up first with a timestamped copy.
  * Never guess that a client is installed. Detection is by the presence of the
    application's own config directory, so we don't create config for software
    the user doesn't have.
  * Never write a path that can move. The launch command is resolved to the
    absolute console script or `sys.executable` + run.py, because MCP clients
    start servers with no shell, no PATH and no working directory.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

SERVER_KEY = "socialmcp"

# How long to wait on another app's CLI before giving up and editing its config
# file instead. Deliberately short: this runs during first launch, where every
# second is a second of an apparently frozen app. The previous 120s meant a
# Finder launch could sit silent for two minutes and look broken.
CLIENT_PROBE_TIMEOUT = 10


@dataclass(slots=True)
class ClientTarget:
    """One MCP client we know how to configure."""

    key: str
    display_name: str
    config_path: Path
    # Clients disagree on the top-level key: mcpServers (most), servers (VS
    # Code), context_servers (Zed).
    servers_key: str = "mcpServers"
    # A directory whose existence means the app is installed. Without it we
    # would scatter config files for apps the user has never run.
    detect_paths: tuple[Path, ...] = ()
    # Some clients own their config and should be driven by their own CLI.
    cli: tuple[str, ...] = ()
    notes: str = ""

    @property
    def installed(self) -> bool:
        if self.cli and shutil.which(self.cli[0]):
            return True
        return any(p.exists() for p in self.detect_paths) or self.config_path.exists()


@dataclass(slots=True)
class SetupResult:
    client: str
    status: str          # 'configured' | 'already' | 'updated' | 'skipped' | 'failed'
    detail: str = ""
    config_path: str = ""
    backup: str = ""


@dataclass(slots=True)
class LaunchCommand:
    command: str
    args: list[str] = field(default_factory=list)

    def as_display(self) -> str:
        return " ".join([self.command, *self.args])


def resolve_launch_command() -> LaunchCommand:
    """How a client should start this server, as an absolute, stable command.

    Prefers the installed `socialmcp-server` console script. Falls back to the
    current interpreter plus run.py from a source checkout — using
    `sys.executable` rather than "python", because a client launched from the
    macOS Dock has a minimal PATH where "python" often does not exist at all.
    """
    # Look beside the running interpreter first. PATH is unreliable here: the
    # user may have launched us by absolute path out of a venv that was never
    # activated, and `which` would then miss the console script sitting right
    # next to sys.executable.
    bin_dir = Path(sys.executable).parent
    for name in ("socialmcp-server", "socialmcp-server.exe"):
        candidate = bin_dir / name
        if candidate.is_file():
            return LaunchCommand(command=str(candidate.resolve()))

    script = shutil.which("socialmcp-server")
    if script:
        return LaunchCommand(command=str(Path(script).resolve()))

    run_py = Path(__file__).resolve().parent.parent / "run.py"
    if run_py.is_file():
        return LaunchCommand(command=sys.executable, args=[str(run_py)])

    # Installed as a library but the console script is not on PATH.
    return LaunchCommand(command=sys.executable, args=["-m", "socialmcp.cli", "serve"])


def _mac() -> bool:
    return sys.platform == "darwin"


def _win() -> bool:
    return sys.platform.startswith("win")


def known_clients() -> list[ClientTarget]:
    """Config locations per client, per platform.

    Every client here keeps its MCP servers in JSON, which is what the merge
    below can edit safely. Deliberately absent, for that reason:

    * **OpenAI Codex CLI** — `~/.codex/config.toml`, TOML.
    * **Continue** — `~/.continue/config.yaml`, YAML.
    * **Goose** — `~/.config/goose/config.yaml`, YAML.

    Adding those means writing TOML or YAML while preserving a user's comments
    and formatting, which is a different and riskier job than merging a JSON
    object. Until then they are listed in the README as a manual step: the
    launch command is the same for every client, so pasting it by hand works.

    Detection is by whether the config directory exists, so a path that turns
    out to be wrong means the client is simply not detected. It can never cause
    a write to the wrong file.
    """
    home = Path.home()
    appdata = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))

    if _mac():
        app_support = home / "Library" / "Application Support"
        claude_desktop = app_support / "Claude" / "claude_desktop_config.json"
        claude_dir = app_support / "Claude"
        vscode = app_support / "Code" / "User" / "mcp.json"
        vscode_dir = app_support / "Code"
        zed = home / ".config" / "zed" / "settings.json"
        cline = (
            app_support / "Code" / "User" / "globalStorage"
            / "saoudrizwan.claude-dev" / "settings" / "cline_mcp_settings.json"
        )
    elif _win():
        claude_desktop = appdata / "Claude" / "claude_desktop_config.json"
        claude_dir = appdata / "Claude"
        vscode = appdata / "Code" / "User" / "mcp.json"
        vscode_dir = appdata / "Code"
        zed = home / "AppData" / "Roaming" / "Zed" / "settings.json"
        cline = (
            appdata / "Code" / "User" / "globalStorage"
            / "saoudrizwan.claude-dev" / "settings" / "cline_mcp_settings.json"
        )
    else:
        config = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
        claude_desktop = config / "Claude" / "claude_desktop_config.json"
        claude_dir = config / "Claude"
        vscode = config / "Code" / "User" / "mcp.json"
        vscode_dir = config / "Code"
        zed = config / "zed" / "settings.json"
        cline = (
            config / "Code" / "User" / "globalStorage"
            / "saoudrizwan.claude-dev" / "settings" / "cline_mcp_settings.json"
        )

    # Same on every platform: both of these keep config in the home directory.
    gemini = home / ".gemini" / "settings.json"

    return [
        ClientTarget(
            key="claude-desktop",
            display_name="Claude Desktop",
            config_path=claude_desktop,
            detect_paths=(claude_dir,),
            notes="Quit and reopen Claude Desktop to pick up the change.",
        ),
        ClientTarget(
            key="claude-code",
            display_name="Claude Code",
            config_path=home / ".claude.json",
            detect_paths=(home / ".claude", home / ".claude.json"),
            cli=("claude", "mcp", "add"),
            notes="Start a new Claude Code session to pick up the change.",
        ),
        ClientTarget(
            key="cursor",
            display_name="Cursor",
            config_path=home / ".cursor" / "mcp.json",
            detect_paths=(home / ".cursor",),
            notes="Restart Cursor, then check Settings -> MCP.",
        ),
        ClientTarget(
            key="windsurf",
            display_name="Windsurf",
            config_path=home / ".codeium" / "windsurf" / "mcp_config.json",
            detect_paths=(home / ".codeium" / "windsurf",),
            notes="Restart Windsurf to pick up the change.",
        ),
        ClientTarget(
            key="vscode",
            display_name="VS Code (Copilot)",
            config_path=vscode,
            servers_key="servers",
            detect_paths=(vscode_dir,),
            notes="Reload the VS Code window.",
        ),
        ClientTarget(
            key="gemini-cli",
            display_name="Gemini CLI",
            config_path=gemini,
            detect_paths=(gemini.parent,),
            notes="Restart the Gemini CLI session.",
        ),
        ClientTarget(
            key="cline",
            display_name="Cline (VS Code)",
            config_path=cline,
            detect_paths=(cline.parent,),
            notes="Reload the VS Code window.",
        ),
        ClientTarget(
            key="zed",
            display_name="Zed",
            config_path=zed,
            servers_key="context_servers",
            detect_paths=(zed.parent,),
            notes="Restart Zed to pick up the change.",
        ),
    ]


def server_entry(launch: LaunchCommand, env: dict[str, str] | None = None) -> dict[str, Any]:
    entry: dict[str, Any] = {"command": launch.command}
    if launch.args:
        entry["args"] = launch.args
    if env:
        entry["env"] = env
    return entry


def backup_file(path: Path) -> Path | None:
    """Timestamped copy, so a bad merge is always one `cp` away from undone."""
    if not path.is_file():
        return None
    # Local time on purpose: this string is read by a human looking for
    # "the backup from just now" in their own file manager.
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")  # noqa: DTZ005
    target = path.with_suffix(path.suffix + f".socialmcp-{stamp}.bak")
    shutil.copy2(path, target)
    return target


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return {}
    return json.loads(text)


def _launched_from_app_bundle() -> bool:
    """Is this code running inside the .app, rather than from a terminal?

    It decides whether other tools' CLIs may be invoked: a child process's
    file access is attributed to the responsible parent, so a CLI that reaches
    into protected folders produces permission prompts in the app's name. From
    a terminal the responsible parent is the terminal, which already has the
    access it needs, so the CLI is the better choice there.
    """
    import sys

    if not getattr(sys, "frozen", False):
        return False
    return ".app/Contents/" in str(Path(sys.executable).resolve())


def configure_client(
    target: ClientTarget,
    launch: LaunchCommand,
    *,
    env: dict[str, str] | None = None,
    force: bool = False,
    dry_run: bool = False,
) -> SetupResult:
    """Merge a socialmcp entry into one client's config file."""
    entry = server_entry(launch, env)

    # Claude Code owns ~/.claude.json, so its CLI used to be preferred for
    # writing there. That is no longer done from the app, and the reason is
    # measured rather than theoretical.
    #
    # `claude mcp list` takes about nine seconds and, while it runs, Claude
    # Code launches every configured MCP server to inspect it. Claude Code
    # touches Downloads, Documents and Desktop in the process — and since it
    # is a child of whatever started it, macOS attributes that access to the
    # *parent*. Launched from the Mac app, that parent is SocialMCP, so a user
    # installing a tool that reads one file was asked for three folders it
    # never touches. The log in packaging/macos/applog.py and the grant times
    # in the TCC database line up exactly.
    #
    # Writing the JSON directly costs the risk that Claude Code rewrites the
    # file, which is recoverable — reopening SocialMCP puts the entry back —
    # against three unexplained permission prompts, which are not.
    #
    # The CLI is still used when the *user* ran `socialmcp setup` themselves:
    # their terminal is then the responsible process, which already has
    # whatever access it needs, and the CLI keeps the file's ownership intact.
    if (
        target.key == "claude-code"
        and shutil.which("claude")
        and not _launched_from_app_bundle()
    ):
        via_cli = _configure_via_claude_cli(
            target, launch, env=env, force=force, dry_run=dry_run
        )
        if via_cli is not None:
            return via_cli

    try:
        config = _read_json(target.config_path)
    except json.JSONDecodeError as exc:
        return SetupResult(
            target.display_name,
            "failed",
            f"its config file is not valid JSON ({exc}); fix or remove it, then retry",
            str(target.config_path),
        )

    servers = config.get(target.servers_key)
    if not isinstance(servers, dict):
        servers = {}
    existing = servers.get(SERVER_KEY)

    if existing == entry and not force:
        return SetupResult(target.display_name, "already", "already set up correctly",
                           str(target.config_path))
    if existing is not None and not force:
        return SetupResult(
            target.display_name,
            "skipped",
            "a different socialmcp entry is already there; re-run with --force to replace it",
            str(target.config_path),
        )

    if dry_run:
        return SetupResult(target.display_name, "configured", "would be added (dry run)",
                           str(target.config_path))

    backup = backup_file(target.config_path)
    servers[SERVER_KEY] = entry
    config[target.servers_key] = servers
    target.config_path.parent.mkdir(parents=True, exist_ok=True)
    # Write via a temp file in the same directory so a crash cannot truncate a
    # client's config and leave the app unable to start.
    temp = target.config_path.with_suffix(target.config_path.suffix + ".socialmcp-tmp")
    temp.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    temp.replace(target.config_path)

    return SetupResult(
        target.display_name,
        "updated" if existing is not None else "configured",
        target.notes,
        str(target.config_path),
        str(backup) if backup else "",
    )


def _configure_via_claude_cli(
    target: ClientTarget,
    launch: LaunchCommand,
    *,
    env: dict[str, str] | None,
    force: bool,
    dry_run: bool,
) -> SetupResult:
    # `mcp get socialmcp`, not `mcp list`: the only question is whether our own
    # entry exists, and listing would enumerate every MCP server the user has
    # configured — none of this program's business, and a meaningful amount of
    # private information to pull into a setup routine. It is also far faster;
    # `list` launches each configured server to inspect it.
    #
    # Bounded and detached from stdin: launched from Finder the CLI waits on a
    # terminal it does not have, and the app has only this to show meanwhile.
    try:
        listed = subprocess.run(
            ["claude", "mcp", "get", SERVER_KEY],
            capture_output=True,
            text=True,
            check=False,
            stdin=subprocess.DEVNULL,
            timeout=CLIENT_PROBE_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError):
        # None means "the CLI is not usable here"; the caller then edits the
        # config file directly, which is second choice but far better than
        # reporting failure to someone who just wants the app connected.
        return None
    # Exit 0 means the server exists; anything else means it does not, which
    # is the normal first-run case rather than an error.
    already = listed.returncode == 0 and SERVER_KEY in listed.stdout
    if already and not force:
        return SetupResult(target.display_name, "already", "already registered", "claude mcp")

    command = ["claude", "mcp", "add", SERVER_KEY, "-s", "user"]
    for key, value in (env or {}).items():
        command += ["-e", f"{key}={value}"]
    command += ["--", launch.command, *launch.args]

    if dry_run:
        return SetupResult(target.display_name, "configured", " ".join(command), "claude mcp")
    if force:
        subprocess.run(["claude", "mcp", "remove", SERVER_KEY, "-s", "user"],
                       capture_output=True, text=True, check=False, timeout=120)

    result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=180)
    if result.returncode != 0:
        return SetupResult(
            target.display_name, "failed",
            (result.stderr or result.stdout or "claude mcp add failed").strip()[:300],
            "claude mcp",
        )
    return SetupResult(target.display_name, "configured", target.notes, "claude mcp")


def unregister_client(target: ClientTarget, *, dry_run: bool = False) -> SetupResult:
    """Remove only our own entry from one client's config.

    The mirror image of configure_client, and held to the same rule: touch
    nothing we did not create. The socialmcp key is deleted and everything else
    in the file — other MCP servers, unrelated settings — is written back
    unchanged, with a backup taken first.

    An empty `mcpServers` map is left in place rather than removed, because its
    presence may be what the client expects and its absence is not ours to
    decide.
    """
    if target.key == "claude-code" and shutil.which("claude"):
        if dry_run:
            return SetupResult(target.display_name, "would_remove", "claude mcp remove", "claude mcp")
        result = subprocess.run(
            ["claude", "mcp", "remove", SERVER_KEY, "-s", "user"],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            if "not found" in detail.lower() or "no mcp server" in detail.lower():
                return SetupResult(target.display_name, "absent", "was not registered", "claude mcp")
            return SetupResult(target.display_name, "failed", detail[:300], "claude mcp")
        return SetupResult(target.display_name, "removed", "", "claude mcp")

    if not target.config_path.is_file():
        return SetupResult(target.display_name, "absent", "no config file", str(target.config_path))

    try:
        config = _read_json(target.config_path)
    except json.JSONDecodeError as exc:
        return SetupResult(
            target.display_name,
            "failed",
            f"its config file is not valid JSON ({exc}); left untouched",
            str(target.config_path),
        )

    servers = config.get(target.servers_key)
    if not isinstance(servers, dict) or SERVER_KEY not in servers:
        return SetupResult(
            target.display_name, "absent", "was not registered", str(target.config_path)
        )
    if dry_run:
        return SetupResult(
            target.display_name, "would_remove", "would be removed", str(target.config_path)
        )

    backup = backup_file(target.config_path)
    del servers[SERVER_KEY]
    config[target.servers_key] = servers
    temp = target.config_path.with_suffix(target.config_path.suffix + ".socialmcp-tmp")
    temp.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    temp.replace(target.config_path)
    return SetupResult(
        target.display_name,
        "removed",
        target.notes,
        str(target.config_path),
        str(backup) if backup else "",
    )


def unregister_all(
    *, only: list[str] | None = None, dry_run: bool = False
) -> list[SetupResult]:
    """Unregister from every client, including ones not currently installed.

    Unlike setup, this does not skip uninstalled clients: an app the user has
    since deleted may still hold a config file pointing at us, and leaving that
    behind is exactly the mess an uninstall is supposed to prevent.
    """
    results: list[SetupResult] = []
    for target in known_clients():
        if only and target.key not in only:
            continue
        result = unregister_client(target, dry_run=dry_run)
        if result.status != "absent" or only:
            results.append(result)
    return results


def setup_all(
    *,
    only: list[str] | None = None,
    env: dict[str, str] | None = None,
    force: bool = False,
    dry_run: bool = False,
) -> tuple[LaunchCommand, list[SetupResult]]:
    launch = resolve_launch_command()
    results: list[SetupResult] = []
    for target in known_clients():
        if only and target.key not in only:
            continue
        if not only and not target.installed:
            continue
        results.append(configure_client(target, launch, env=env, force=force, dry_run=dry_run))
    return launch, results


def detected_clients() -> list[ClientTarget]:
    return [t for t in known_clients() if t.installed]
