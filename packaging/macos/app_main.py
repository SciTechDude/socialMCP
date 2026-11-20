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
from typing import Any

# A bundled app inherits almost no environment: launched from Finder there is
# no shell, no PATH to speak of, and no working directory the user chose. Pin
# what we need before importing anything that reads config.
ACTION_IMPORT = "Choose my export…"
ACTION_FOLDER = "Open the folder"
ACTION_DONE = "Done"

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


def running_from_removable_location() -> Path | None:
    """The bundle's path if it will stop existing, else None.

    Opening the app from inside the downloaded .dmg is the most natural thing
    to do — the disk image window appears the moment you double-click the
    download, with the app right there. But setup writes this binary's absolute
    path into each AI client's config, and that path is
    `/Volumes/SocialMCP 0.1.2/...`, which vanishes the moment the image is
    ejected. The client then fails to start a server that no longer exists,
    with nothing to suggest why.

    Detected rather than guessed: a path under /Volumes on a read-only
    filesystem is a mounted image. A writable external disk is left alone,
    because someone may genuinely keep applications there.
    """
    bundle = app_bundle_root()
    if bundle is None or not str(bundle).startswith("/Volumes/"):
        return None
    try:
        read_only = bool(os.statvfs(bundle).f_flag & os.ST_RDONLY)
    except (OSError, AttributeError):
        # Cannot tell; /Volumes alone is suspicious enough to warn about.
        read_only = True
    return bundle if read_only else None


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
        '1. Click "Choose my export…" below and pick the .zip LinkedIn sent you.',
        "   Pick the .zip exactly as you downloaded it — do not unzip it first.",
        "   (Don't have it yet? LinkedIn: Settings & Privacy -> Data Privacy ->",
        "    Get a copy of your data. Choose the larger archive; it arrives by",
        "    email, usually within a day.)",
        "",
        "2. Quit and reopen your AI app.",
        "",
        '3. Ask it: "what can you do with my network?"',
        "",
        "You can also drop exports straight into this folder at any time:",
        f"   {drop_folder}",
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

    # Offered only when there is something to disconnect. A user who will
    # never open a terminal still needs a way out, and the app's second launch
    # is the only place they will look.
    if connected and not blocked:
        buttons.append("Disconnect")
    buttons.append(ACTION_FOLDER)
    # Last button is the default, and this is the one thing the user must do
    # next. Previously the default was "open a folder so you can drag a file
    # into it", which is a step, not an outcome.
    buttons.append(ACTION_IMPORT)
    return "\n".join(lines), tuple(buttons)


def _run_setup(report) -> tuple[Any, Any, Any]:
    """The actual setup work, reporting each step as it goes.

    Separated from the window so it can be tested without a display, and so
    the ordering guarantee — UI first, work second — is visible in one place.
    """
    from socialmcp import clients as clients_module
    from socialmcp.clients import setup_all
    from socialmcp.config import load_config

    report.step("Creating your data folder")
    config = load_config()
    config.ensure_dirs()
    drop = config.drop_dir / "linkedin"
    report.done("Created your data folder")

    env = {"SOCIALMCP_HOME": str(config.home), "SOCIALMCP_DROP_DIR": str(config.drop_dir)}
    # The bundle's own executable is the launch command, so the config a client
    # receives keeps working with no Python installed on the machine.
    command = launch_command()
    clients_module.resolve_launch_command = lambda: clients_module.LaunchCommand(
        command=command[0], args=command[1:]
    )

    # The slow step: probing for installed AI apps shells out to their CLIs,
    # and macOS may raise a folder-permission prompt during it. Both are why
    # the window has to exist before this runs.
    report.step("Looking for your AI apps")
    _, results = setup_all(env=env)
    found = ", ".join(r.client for r in results) if results else "none found"
    report.done(f"Checked for AI apps ({found})")

    report.step("Checking for local AI search")
    embeddings = embedding_available(config)
    report.done("Checked for local AI search")

    return config, drop, (results, embeddings)


def setup_flow() -> int:
    """What happens when a person double-clicks the app."""
    # Refuse before writing anything. Configuring an AI client to launch a
    # binary inside a mounted disk image produces an install that works until
    # the image is ejected and then fails silently, which is worse than not
    # installing at all.
    mounted = running_from_removable_location()
    if mounted is not None:
        choice = dialog(
            "Please move SocialMCP to your Applications folder first.\n\n"
            "It is running from the disk image you downloaded, which "
            "disappears when you eject it — and your AI app would be left "
            "pointing at software that is no longer there.\n\n"
            "Drag the SocialMCP icon onto the Applications shortcut in the "
            "window that opened, then open SocialMCP from Applications.",
            buttons=("Open Applications", "OK"),
        )
        if choice == "Open Applications":
            subprocess.run(["open", "/Applications"], check=False, timeout=30)
        reveal(mounted)
        return 1

    # Everything lives under Application Support, not next to the .app, so
    # moving or replacing the app never touches the user's data.
    os.environ.setdefault("SOCIALMCP_HOME", str(APP_SUPPORT))

    # A window appears before any of the work below. A double-click that shows
    # nothing for several seconds reads as a broken app, and a permission
    # prompt from an application that has not introduced itself reads as
    # suspicious. See packaging/macos/progress.py.
    from progress import run_with_progress  # type: ignore[import-not-found]

    from socialmcp.clients import known_clients, setup_all

    config, drop, (results, embeddings) = run_with_progress(_run_setup, steps=3)
    env = {"SOCIALMCP_HOME": str(config.home), "SOCIALMCP_DROP_DIR": str(config.drop_dir)}

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

    # `embeddings` was already probed inside the progress window; probing again
    # here would add a second silent delay after the window has closed.
    message, buttons = build_setup_report(results, drop, embeddings=embeddings)
    choice = ask(message, buttons=buttons)

    if choice == "Replace it":
        # The user asked for this app to win, so overwrite the other entry.
        _, results = setup_all(env=env, force=True)
        message, buttons = build_setup_report(results, drop, embeddings=embeddings)
        choice = ask(message, buttons=tuple(b for b in buttons if b != "Replace it"))

    if choice == "Disconnect":
        return disconnect_flow(config)

    if choice == "Get Ollama":
        subprocess.run(["open", "https://ollama.com"], check=False, timeout=30)
        return 0

    if choice == ACTION_IMPORT:
        return import_flow(config, drop)

    reveal(drop)
    return 0


def ask(message: str, *, buttons: tuple[str, ...], title: str = "SocialMCP") -> str:
    """Show a choice, in a real window when possible.

    AppleScript's `display dialog` accepts at most three buttons; the summary
    can legitimately offer four, and when it does, osascript fails and the
    caller cannot tell the difference between an error and a click. Tk is
    already bundled for the progress window, so prefer it and keep the
    AppleScript path as the fallback with the list trimmed to what it supports.
    """
    from ui import summary_window  # type: ignore[import-not-found]

    answer = summary_window(message, buttons=buttons, title=title)
    if answer is not None:
        return answer
    return dialog(message, title=title, buttons=buttons[-3:])


def import_flow(config, drop: Path) -> int:
    """Let the user pick their export, then ingest it.

    Replaces "open this folder and drag a file into it from another window",
    which needed two Finder windows and an instruction nobody read. The picker
    is also why the app asks for no folder permission: macOS hands over the one
    chosen file and nothing else.
    """
    import shutil

    from ui import choose_export  # type: ignore[import-not-found]

    chosen = choose_export()
    if chosen is None:
        reveal(drop)
        return 0

    if chosen.is_dir():
        dialog(
            f"{chosen.name} is a folder, not the .zip LinkedIn gave you.\n\n"
            "Double-clicking the download unzips it, which is easy to do by "
            "accident. Pick the original .zip instead — or drag the .zip into "
            "the folder that opens next.",
            buttons=("OK",),
        )
        reveal(drop)
        return 1

    drop.mkdir(parents=True, exist_ok=True)
    target = drop / chosen.name
    try:
        if chosen.resolve() != target.resolve():
            shutil.copy2(chosen, target)
    except OSError as exc:
        dialog(f"Could not copy {chosen.name}:\n\n{exc}", buttons=("OK",))
        return 1

    def work(report):
        report.step(f"Reading {chosen.name}")
        from socialmcp.core.db import connect
        from socialmcp.core.ingest import ingest_path

        db = connect(config)
        result = ingest_path(db, config, target)
        report.done(f"Read {chosen.name}")
        report.step("Counting your network")
        stats = db.execute("select count(*) from contacts").fetchone()[0]
        report.done("Counted your network")
        return result, stats

    from progress import run_with_progress  # type: ignore[import-not-found]

    try:
        _result, contacts = run_with_progress(
            work, steps=2, title="Reading your export", intro="This stays on your Mac."
        )
    except Exception as exc:
        dialog(
            f"Could not read {chosen.name}:\n\n{exc}\n\n"
            "If this was not a LinkedIn export, try the original .zip from "
            "LinkedIn: Settings & Privacy -> Data Privacy -> Get a copy of your data.",
            buttons=("OK",),
        )
        return 1

    ask(
        f"Imported {chosen.name}.\n\n"
        f"Your network now has {contacts} people.\n\n"
        "Quit and reopen your AI app, then ask it:\n"
        '  "what can you do with my network?"\n\n'
        "Next time, you can drop a new export straight into:\n"
        f"  {drop}\n"
        "or open SocialMCP again and choose it.",
        buttons=(ACTION_FOLDER, ACTION_DONE),
    )
    return 0


def disconnect_flow(config) -> int:
    """Remove SocialMCP from the AI apps, and ask separately about the data.

    Kept as two questions on purpose. Disconnecting is reversible — opening the
    app again undoes it. Deleting the database is not: the notes the user wrote
    and the event names they chose are not in any export and cannot be
    recovered from one.
    """
    from socialmcp.clients import unregister_all

    results = unregister_all()
    removed = [r for r in results if r.status == "removed"]

    data_paths = [p for p in (config.db_path, config.drop_dir, config.archive_dir) if p.exists()]
    summary = [f"Disconnected from: {', '.join(r.client for r in removed) or 'nothing'}", ""]
    summary += ["Restart those apps to finish.", ""]
    if data_paths:
        summary += [
            "Your data is still on this Mac:",
            *[f"   {p}" for p in data_paths],
            "",
            "Keeping it means reconnecting later picks up exactly where you left off.",
            "Deleting it also removes the notes you wrote and the event names you",
            "chose, which no new export can bring back.",
        ]
    summary += ["", "To remove the app itself, drag SocialMCP to the Trash."]

    buttons: tuple[str, ...] = ("Delete my data", "Keep my data") if data_paths else ("OK",)
    if dialog("\n".join(summary), buttons=buttons) != "Delete my data":
        return 0

    confirmed = dialog(
        "Delete the database, drop folder and processed archives?\n\n"
        "This cannot be undone.",
        buttons=("Cancel", "Delete"),
    )
    if confirmed != "Delete":
        return 0

    import shutil as _shutil

    for path in data_paths:
        try:
            _shutil.rmtree(path) if path.is_dir() else path.unlink()
            for suffix in ("-wal", "-shm"):
                sidecar = path.with_name(path.name + suffix)
                if sidecar.exists():
                    sidecar.unlink()
        except OSError:
            pass
    dialog("Your SocialMCP data has been deleted.\n\nDrag the app to the Trash to finish.")
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
