"""Tests for automatic MCP client registration.

This module edits configuration files that belong to *other applications*. If it
gets that wrong, a user's Claude Desktop or Cursor stops launching — a far worse
outcome than SocialMCP simply not working. So the properties under test are
mostly about restraint: merge, never replace; back up before writing; refuse to
overwrite someone else's entry unless explicitly forced.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from socialmcp.clients import (
    SERVER_KEY,
    ClientTarget,
    LaunchCommand,
    configure_client,
    known_clients,
    resolve_launch_command,
    server_entry,
)

LAUNCH = LaunchCommand(command="/opt/socialmcp/bin/socialmcp-server")


@pytest.fixture
def target(tmp_path: Path) -> ClientTarget:
    return ClientTarget(
        key="test-client",
        display_name="Test Client",
        config_path=tmp_path / "client" / "config.json",
        detect_paths=(tmp_path / "client",),
        notes="Restart Test Client.",
    )


def read(target: ClientTarget) -> dict:
    return json.loads(target.config_path.read_text())


def test_creates_config_when_the_client_has_none(target: ClientTarget):
    result = configure_client(target, LAUNCH)

    assert result.status == "configured"
    assert read(target) == {"mcpServers": {SERVER_KEY: {"command": LAUNCH.command}}}


def test_preserves_every_other_setting_and_server(target: ClientTarget):
    """The destructive failure mode: wiping a user's other MCP servers."""
    target.config_path.parent.mkdir(parents=True)
    target.config_path.write_text(
        json.dumps(
            {
                "theme": "dark",
                "mcpServers": {
                    "github": {"command": "gh-mcp", "args": ["--stdio"]},
                    "filesystem": {"command": "fs-mcp"},
                },
                "otherSetting": {"nested": [1, 2, 3]},
            }
        )
    )

    configure_client(target, LAUNCH)
    config = read(target)

    assert config["theme"] == "dark"
    assert config["otherSetting"] == {"nested": [1, 2, 3]}
    assert config["mcpServers"]["github"] == {"command": "gh-mcp", "args": ["--stdio"]}
    assert config["mcpServers"]["filesystem"] == {"command": "fs-mcp"}
    assert SERVER_KEY in config["mcpServers"]


def test_backs_up_before_modifying(target: ClientTarget):
    target.config_path.parent.mkdir(parents=True)
    original = json.dumps({"mcpServers": {"github": {"command": "gh-mcp"}}})
    target.config_path.write_text(original)

    result = configure_client(target, LAUNCH)

    assert result.backup, "an existing config must be backed up"
    assert json.loads(Path(result.backup).read_text()) == json.loads(original)


def test_rerunning_is_idempotent(target: ClientTarget):
    assert configure_client(target, LAUNCH).status == "configured"
    second = configure_client(target, LAUNCH)
    assert second.status == "already"
    assert not second.backup, "a no-op must not spew backup files"


def test_refuses_to_replace_a_different_entry_without_force(target: ClientTarget):
    target.config_path.parent.mkdir(parents=True)
    target.config_path.write_text(
        json.dumps({"mcpServers": {SERVER_KEY: {"command": "/somewhere/else/python"}}})
    )

    result = configure_client(target, LAUNCH)
    assert result.status == "skipped"
    assert "--force" in result.detail
    # Untouched.
    assert read(target)["mcpServers"][SERVER_KEY]["command"] == "/somewhere/else/python"

    forced = configure_client(target, LAUNCH, force=True)
    assert forced.status == "updated"
    assert read(target)["mcpServers"][SERVER_KEY]["command"] == LAUNCH.command


def test_broken_json_is_reported_not_overwritten(target: ClientTarget):
    target.config_path.parent.mkdir(parents=True)
    target.config_path.write_text("{ this is not json")

    result = configure_client(target, LAUNCH)

    assert result.status == "failed"
    assert "not valid JSON" in result.detail
    assert target.config_path.read_text() == "{ this is not json"


def test_dry_run_writes_nothing(target: ClientTarget):
    result = configure_client(target, LAUNCH, dry_run=True)
    assert result.status == "configured"
    assert not target.config_path.exists()


def test_respects_a_clients_own_servers_key(tmp_path: Path):
    """VS Code uses `servers`, Zed uses `context_servers`."""
    target = ClientTarget(
        key="vscode-like",
        display_name="VS Code",
        config_path=tmp_path / "mcp.json",
        servers_key="servers",
    )
    configure_client(target, LAUNCH)
    config = json.loads(target.config_path.read_text())
    assert "servers" in config and "mcpServers" not in config


def test_environment_is_pinned_into_the_entry(target: ClientTarget):
    """Clients launch servers with no shell, so paths can't be inherited."""
    configure_client(target, LAUNCH, env={"SOCIALMCP_HOME": "/Users/x/.socialmcp"})
    entry = read(target)["mcpServers"][SERVER_KEY]
    assert entry["env"]["SOCIALMCP_HOME"] == "/Users/x/.socialmcp"


def test_launch_command_is_absolute():
    """A bare "python" fails when a GUI app launches the server with a minimal PATH."""
    launch = resolve_launch_command()
    assert Path(launch.command).is_absolute(), launch.command
    for arg in launch.args:
        if arg.endswith(".py"):
            assert Path(arg).is_absolute(), arg


def test_server_entry_omits_empty_fields():
    assert server_entry(LaunchCommand(command="x")) == {"command": "x"}
    assert server_entry(LaunchCommand(command="x", args=["y"])) == {"command": "x", "args": ["y"]}


def test_known_clients_are_well_formed():
    clients = known_clients()
    assert {c.key for c in clients} >= {"claude-desktop", "claude-code", "cursor"}
    for client in clients:
        assert client.config_path.is_absolute()
        assert client.servers_key in {"mcpServers", "servers", "context_servers"}
        assert client.display_name
