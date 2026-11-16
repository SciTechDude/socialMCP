"""Plain-language health check.

Written for someone who has never opened a terminal before today. Every check
answers two questions: is this OK, and if not, what exactly do I type or click
to fix it. No stack traces, no jargon, no "see logs for details".
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .clients import SERVER_KEY, known_clients, resolve_launch_command
from .config import Config

OK = "ok"
WARN = "warn"
FAIL = "fail"


@dataclass(slots=True)
class Check:
    name: str
    status: str
    detail: str
    fix: str = ""


def run_checks(config: Config) -> list[Check]:
    checks: list[Check] = []
    checks.append(_python_check())
    checks.append(_fts_check())
    checks.append(_vector_check())
    checks.extend(_embedding_checks(config))
    checks.extend(_data_checks(config))
    checks.extend(_client_checks())
    return checks


def _python_check() -> Check:
    version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    # Kept despite requires-python: someone running run.py from a source
    # checkout with an old system python never goes through pip at all, and a
    # clear message beats a SyntaxError on modern type syntax.
    if sys.version_info < (3, 11):  # noqa: UP036
        return Check(
            "Python version",
            FAIL,
            f"Python {version} is too old",
            "SocialMCP needs Python 3.11 or newer. Install it from python.org, then re-run setup.",
        )
    return Check("Python version", OK, f"Python {version}")


def _fts_check() -> Check:
    try:
        con = sqlite3.connect(":memory:")
        con.execute("CREATE VIRTUAL TABLE t USING fts5(a)")
        con.close()
    except sqlite3.Error:
        return Check(
            "Keyword search (SQLite FTS5)",
            FAIL,
            "your Python's SQLite was built without FTS5",
            "Install Python from python.org rather than the system one; the official builds "
            "include FTS5.",
        )
    return Check("Keyword search (SQLite FTS5)", OK, "available")


def _vector_check() -> Check:
    try:
        import sqlite_vec
    except ImportError:
        return Check(
            "Smart search (sqlite-vec)",
            WARN,
            "not installed, so searches match words literally",
            install_hint("sqlite-vec") + "    (then: socialmcp reindex)",
        )
    del sqlite_vec
    return Check("Smart search (sqlite-vec)", OK, "installed")


def install_hint(package: str) -> str:
    """Advise installing into the environment that is running, not a global one.

    A bare `pip install` lands in whichever interpreter happens to be active,
    which on many systems is the one the operating system depends on. Naming
    `sys.executable` is unambiguous, and a bundled app should never be told to
    pip at all — it carries its own interpreter, and anything missing from it
    is a packaging bug rather than the user's problem.
    """
    if getattr(sys, "frozen", False):
        return (
            f"{package} should already be inside the app - install the latest "
            "version of SocialMCP rather than installing it yourself"
        )
    return f"Run: {sys.executable} -m pip install {package}"


def _embedding_checks(config: Config) -> list[Check]:
    provider = (config.embedding.provider or "none").lower()
    if provider == "none":
        return [
            Check(
                "Meaning-based search",
                WARN,
                "turned off, so searches match words literally",
                'Install Ollama from ollama.com, run "ollama pull nomic-embed-text", '
                'then set provider = "ollama" in your config.',
            )
        ]
    if provider == "openai":
        return [
            Check(
                "Meaning-based search",
                WARN,
                "set to OpenAI, which sends contact names and notes to a third party",
                'Switch to provider = "ollama" to keep everything on this computer.',
            )
        ]

    url = config.embedding.ollama_url.rstrip("/")
    try:
        with urllib.request.urlopen(f"{url}/api/tags", timeout=3) as response:
            models = {m.get("name", "") for m in json.loads(response.read()).get("models", [])}
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return [
            Check(
                "Meaning-based search (Ollama)",
                WARN,
                f"Ollama is not answering at {url}",
                "Open the Ollama app (download it from ollama.com). Searches still work "
                "without it, they just match words literally.",
            )
        ]

    wanted = config.embedding.model
    if not any(name.split(":")[0] == wanted.split(":")[0] for name in models):
        return [
            Check(
                "Meaning-based search (Ollama)",
                WARN,
                f"Ollama is running but the '{wanted}' model is missing",
                f"Run: ollama pull {wanted}",
            )
        ]
    return [Check("Meaning-based search (Ollama)", OK, f"running with '{wanted}'")]


def _data_checks(config: Config) -> list[Check]:
    checks: list[Check] = []
    drop = config.drop_dir / "linkedin"
    if drop.is_dir():
        waiting = sorted(p.name for p in config.drop_dir.rglob("*.zip*"))
        if waiting:
            checks.append(
                Check(
                    "Drop folder",
                    WARN,
                    f"{len(waiting)} file(s) waiting to be read: {', '.join(waiting[:3])}",
                    "Run: socialmcp ingest",
                )
            )
        else:
            checks.append(Check("Drop folder", OK, f"ready at {drop}"))
    else:
        checks.append(
            Check("Drop folder", WARN, "not created yet", "Run: socialmcp setup")
        )

    if not config.db_path.is_file():
        checks.append(
            Check(
                "Your contacts",
                WARN,
                "no data yet",
                f"Download your LinkedIn data (Settings & Privacy -> Data Privacy -> "
                f"Get a copy of your data), then put the .zip file in {drop}",
            )
        )
        return checks

    try:
        con = sqlite3.connect(f"file:{config.db_path}?mode=ro", uri=True)
        contacts = con.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
        clusters = con.execute("SELECT COUNT(*) FROM event_clusters").fetchone()[0]
        unnamed = con.execute(
            "SELECT COUNT(*) FROM event_clusters WHERE renamed_by_user = 0"
        ).fetchone()[0]
        pending = con.execute("SELECT COUNT(*) FROM contacts WHERE status = 'pending'").fetchone()[0]
        con.close()
    except sqlite3.Error as exc:
        checks.append(
            Check("Your contacts", FAIL, f"the database could not be read ({exc})",
                  f"Delete {config.db_path} and run: socialmcp ingest")
        )
        return checks

    if contacts == 0:
        checks.append(
            Check("Your contacts", WARN, "database is empty",
                  f"Put your LinkedIn export .zip in {drop}, then run: socialmcp ingest")
        )
    else:
        checks.append(Check("Your contacts", OK, f"{contacts} people, {pending} invites pending"))

    if clusters and unnamed:
        checks.append(
            Check(
                "Event names",
                WARN,
                f"{unnamed} of {clusters} events still have placeholder names",
                'Ask your AI assistant: "list my event clusters and help me name them"',
            )
        )
    elif clusters:
        checks.append(Check("Event names", OK, f"{clusters} events, all named"))
    return checks


def _client_checks() -> list[Check]:
    launch = resolve_launch_command()
    checks: list[Check] = []
    installed = [t for t in known_clients() if t.installed]

    if not installed:
        return [
            Check(
                "AI apps found",
                WARN,
                "none detected on this computer",
                "Install Claude Desktop (claude.ai/download), Cursor, or another MCP-compatible "
                "app, then run: socialmcp setup",
            )
        ]

    for target in installed:
        if target.cli and shutil.which(target.cli[0]):
            checks.append(
                Check(
                    target.display_name,
                    OK,
                    "installed (managed by its own command line tool)",
                    "" if _claude_cli_has_server() else "Run: socialmcp setup",
                )
            )
            continue
        try:
            raw = target.config_path.read_text(encoding="utf-8") if target.config_path.is_file() else "{}"
            config = json.loads(raw or "{}")
        except (OSError, json.JSONDecodeError):
            checks.append(
                Check(target.display_name, WARN, "its settings file could not be read",
                      "Run: socialmcp setup")
            )
            continue
        servers = config.get(target.servers_key) or {}
        if SERVER_KEY in servers:
            checks.append(Check(target.display_name, OK, "connected to SocialMCP"))
        else:
            checks.append(
                Check(target.display_name, WARN, "installed but not connected yet",
                      "Run: socialmcp setup")
            )

    checks.append(Check("Start command used by AI apps", OK, launch.as_display()))
    return checks


def _claude_cli_has_server() -> bool:
    import subprocess

    try:
        out = subprocess.run(
            ["claude", "mcp", "list"], capture_output=True, text=True, check=False, timeout=60
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return SERVER_KEY in out.stdout


def open_folder(path: Path) -> bool:
    """Open a folder in Finder / Explorer / the Linux file manager."""
    path.mkdir(parents=True, exist_ok=True)
    import subprocess

    if sys.platform == "darwin":
        command = ["open", str(path)]
    elif sys.platform.startswith("win"):
        command = ["explorer", str(path)]
    else:
        command = ["xdg-open", str(path)]
    try:
        subprocess.run(command, check=False, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return True
