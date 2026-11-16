"""Entry point for the macOS app bundle.

One binary, two modes, because an MCP client and a human need different things
from the same program:

    SocialMCP.app double-clicked   -> the setup flow, with native dialogs
    SocialMCP.app/... serve        -> the MCP server over stdio

Keeping them in one executable means the path written into a client's config
(`/Applications/SocialMCP.app/Contents/MacOS/SocialMCP serve`) is stable, and
there is nothing to install, unpack or keep in sync afterwards.

No GUI framework: macOS already has dialogs, and a progress window would be a
lie anyway — the work takes under two seconds. The AI client remains the real
interface, which is the whole premise of the project.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

# A bundled app inherits almost no environment: launched from Finder there is
# no shell, no PATH to speak of, and no working directory the user chose. Pin
# what we need before importing anything that reads config.
APP_SUPPORT = Path.home() / "Library" / "Application Support" / "SocialMCP"


def app_bundle_root() -> Path | None:
    """The .app directory, when running from a bundle."""
    if not getattr(sys, "frozen", False):
        return None
    # sys.executable is .../SocialMCP.app/Contents/MacOS/SocialMCP
    executable = Path(sys.executable).resolve()
    for parent in executable.parents:
        if parent.suffix == ".app":
            return parent
    return None


def launch_command() -> list[str]:
    """How a client should start the server: this same binary, plus `serve`."""
    return [str(Path(sys.executable).resolve()), "serve"]


def dialog(message: str, *, title: str = "SocialMCP", buttons: tuple[str, ...] = ("OK",)) -> str:
    """A native dialog via AppleScript, so no UI toolkit is bundled."""
    button_list = ", ".join(f'"{b}"' for b in buttons)
    script = (
        f'display dialog {_quote(message)} with title "{title}" '
        f"buttons {{{button_list}}} default button \"{buttons[-1]}\""
    )
    try:
        result = subprocess.run(
            ["osascript", "-e", script], capture_output=True, text=True, check=False, timeout=600
        )
    except (OSError, subprocess.SubprocessError):
        print(message)
        return buttons[-1]
    for part in (result.stdout or "").strip().split(", "):
        if part.startswith("button returned:"):
            return part.split(":", 1)[1]
    return buttons[-1]


def _quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def reveal(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["open", str(path)], check=False, timeout=30)


def embedding_available(config) -> bool:
    """Is a local embedding model reachable? Non-fatal either way.

    Checked so the setup dialog can say what the user is getting. Without it
    search still works, just literally — and someone who does not know that
    will read a thin result as a broken product.
    """
    import json
    import urllib.error
    import urllib.request

    url = config.embedding.ollama_url.rstrip("/")
    try:
        with urllib.request.urlopen(f"{url}/api/tags", timeout=2) as response:
            models = {m.get("name", "") for m in json.loads(response.read()).get("models", [])}
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return False
    wanted = config.embedding.model.split(":")[0]
    return any(name.split(":")[0] == wanted for name in models)


def build_setup_report(results, drop_folder: Path, *, embeddings: bool) -> tuple[str, tuple[str, ...]]:
    """The dialog text and buttons for a finished setup run.

    Separated from display so the wording can be tested. Every result status
    has to be accounted for here: a run where every client was `skipped` once
    reported "Connected to: none" and left the user with no idea why.
    """
    connected = [r for r in results if r.status in {"configured", "updated", "already"}]
    blocked = [r for r in results if r.status == "skipped"]
    failed = [r for r in results if r.status == "failed"]
    restart = [r for r in results if r.status in {"configured", "updated"}]

    lines: list[str] = []
    buttons: list[str] = []

    if connected:
        lines.append(f"Connected to: {', '.join(r.client for r in connected)}")
    if blocked:
        # Almost always a command-line install of SocialMCP already registered.
        lines += [
            "",
            f"Already set up by another copy: {', '.join(r.client for r in blocked)}",
            "",
            "That usually means SocialMCP was installed from the command line on",
            "this Mac. Replacing it points those apps at this app instead.",
        ]
        buttons.append("Replace it")
    if failed:
        lines += [
            "",
            f"Could not configure: {', '.join(r.client for r in failed)}",
            f"({failed[0].detail})",
        ]

    if not connected and not blocked:
        lines.append("No AI app could be configured.")

    lines += [
        "",
        "Next:",
        "",
        "1. Put your LinkedIn data export .zip in:",
        f"   {drop_folder}",
        "   (LinkedIn: Settings & Privacy -> Data Privacy -> Get a copy of your data,",
        "    and choose the larger archive.)",
        "",
        "2. Quit and reopen your AI app.",
        "",
        '3. Ask it: "what can you do with my network?"',
    ]
    if restart:
        lines += ["", f"Restart needed for: {', '.join(r.client for r in restart)}"]

    if not embeddings:
        lines += [
            "",
            "Optional: searches currently match words literally. Installing Ollama",
            "(free, runs on this Mac, nothing uploaded) lets you search by meaning.",
        ]
        buttons.append("Get Ollama")

    buttons.append("Open the folder")
    return "\n".join(lines), tuple(buttons)


def setup_flow() -> int:
    """What happens when a person double-clicks the app."""
    # Everything lives under Application Support, not next to the .app, so
    # moving or replacing the app never touches the user's data.
    os.environ.setdefault("SOCIALMCP_HOME", str(APP_SUPPORT))

    from socialmcp import clients as clients_module
    from socialmcp.clients import known_clients, setup_all
    from socialmcp.config import load_config

    config = load_config()
    config.ensure_dirs()
    drop = config.drop_dir / "linkedin"

    env = {"SOCIALMCP_HOME": str(config.home), "SOCIALMCP_DROP_DIR": str(config.drop_dir)}
    # The bundle's own executable is the launch command, so the config a client
    # receives keeps working with no Python installed on the machine.
    command = launch_command()
    clients_module.resolve_launch_command = lambda: clients_module.LaunchCommand(
        command=command[0], args=command[1:]
    )

    _, results = setup_all(env=env)

    if not results:
        choice = dialog(
            "No AI apps were found on this Mac.\n\n"
            "SocialMCP works with Claude Desktop, Cursor, Windsurf, VS Code and "
            "other apps that support MCP.\n\n"
            "Install one, then open SocialMCP again.\n\n"
            f"Apps checked: {', '.join(t.display_name for t in known_clients())}",
            buttons=("Get Claude Desktop", "OK"),
        )
        if choice == "Get Claude Desktop":
            subprocess.run(["open", "https://claude.ai/download"], check=False, timeout=30)
        return 1

    message, buttons = build_setup_report(
        results, drop, embeddings=embedding_available(config)
    )
    choice = dialog(message, buttons=buttons)

    if choice == "Replace it":
        # The user asked for this app to win, so overwrite the other entry.
        _, results = setup_all(env=env, force=True)
        message, buttons = build_setup_report(
            results, drop, embeddings=embedding_available(config)
        )
        choice = dialog(message, buttons=tuple(b for b in buttons if b != "Replace it"))

    if choice == "Get Ollama":
        subprocess.run(["open", "https://ollama.com"], check=False, timeout=30)

    reveal(drop)
    return 0


def main(argv: list[str]) -> int:
    if argv and argv[0] == "serve":
        # stdio transport: nothing may be written to stdout except protocol.
        os.environ.setdefault("SOCIALMCP_HOME", str(APP_SUPPORT))
        from socialmcp.mcp_server.server import main as serve

        serve()
        return 0

    if argv:
        # Any other argument runs the normal CLI, so the bundled binary is also
        # usable from a terminal (`SocialMCP doctor`) without installing Python.
        os.environ.setdefault("SOCIALMCP_HOME", str(APP_SUPPORT))
        from socialmcp.cli import main as cli

        return cli(argv)

    return setup_flow()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
