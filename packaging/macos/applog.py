"""A real log file, written next to the app's data.

Why this is worth the code: macOS attributes a permission prompt to the
*responsible* process, which is this app even when the thing actually touching
a folder is a child process it launched. So "which step caused that prompt?"
cannot be answered by reading our source — it needs a timestamped record of
what ran and when, which can then be lined up against the grant times in the
TCC database.

It logs step names, child process commands, durations and exit codes. It does
not log anything from the export: no names, no companies, no file contents.
Paths are recorded, since knowing *which* folder was touched is the point.

The file lives at:
    ~/Library/Application Support/SocialMCP/socialmcp.log

which is the app's own folder — the same place its database lives — so the app
still writes nowhere else.
"""

from __future__ import annotations

import datetime
import os
import subprocess
import sys
import time
from pathlib import Path

MAX_BYTES = 1_000_000
_path: Path | None = None


def _stamp() -> str:
    return datetime.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def configure(folder: Path) -> Path:
    """Point the log at `folder`, trimming it if it has grown large."""
    global _path
    try:
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / "socialmcp.log"
        if target.exists() and target.stat().st_size > MAX_BYTES:
            # Keep the tail: recent launches are what anyone debugging needs.
            tail = target.read_text(encoding="utf-8", errors="ignore")[-MAX_BYTES // 2 :]
            target.write_text(f"[log trimmed]\n{tail}", encoding="utf-8")
        _path = target
    except OSError:
        _path = None
    return _path  # type: ignore[return-value]


def write(message: str) -> None:
    """One line, best effort. Logging must never break the app."""
    if _path is None:
        return
    try:
        with _path.open("a", encoding="utf-8") as handle:
            handle.write(f"{_stamp()}  {message}\n")
    except OSError:
        pass


def start_session(*, version: str, bundle: Path | None, argv: list[str]) -> None:
    write("")
    write("=" * 72)
    write(f"launch  version={version}  pid={os.getpid()}")
    write(f"  bundle={bundle or 'not a bundle'}")
    write(f"  argv={argv}  cwd={Path.cwd()}")
    write(f"  frozen={getattr(sys, 'frozen', False)}  tmpdir={os.environ.get('TMPDIR', '')}")
    write(
        "  note: macOS blames this app for folder access by any child process "
        "below, so compare these times with the TCC prompts you saw."
    )


def instrument_subprocess() -> None:
    """Log every child process this app starts, with timing and exit code.

    This is the measurement that matters: a permission prompt during setup is
    almost always a child process reaching for something, and the log then
    names which one and when.
    """
    original = subprocess.run

    def logged(*args, **kwargs):
        argv = args[0] if args else kwargs.get("args")
        shown = argv if isinstance(argv, str) else " ".join(str(a) for a in (argv or []))
        write(f"run: {shown[:200]}")
        began = time.monotonic()
        try:
            result = original(*args, **kwargs)
        except BaseException as exc:
            write(f"run failed after {time.monotonic() - began:.2f}s: {exc!r}")
            raise
        took = time.monotonic() - began
        code = getattr(result, "returncode", "?")
        write(f"run done in {took:.2f}s  exit={code}")
        return result

    subprocess.run = logged  # type: ignore[assignment]
