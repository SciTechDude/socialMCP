"""Choosing the export file.

The panel opens in Downloads, because that is where a browser puts the
archive, and it only offers archives — picking a PDF or a photo cannot be a
useful answer, and letting someone select one only to be told it failed is a
worse experience than not offering it.

Types are mapped per source rather than hardcoded. There is one source today,
and when there are several (Instagram, X, and so on) each brings its own
archive format: the mapping below is where that lives, and it should move
beside the adapters once more than one of them exists.

Worth recording why this is a plain `choose file` and not a window of our own.
A Tk file dialog was tried and reverted: it drew blank without a full event
loop, and bundling Tk cost 3 MB for a progress bar. The permission prompts
that appeared alongside it were never its fault — they came from
`claude mcp list`, which Claude Code answers by launching every configured MCP
server, and macOS attributes a child's file access to the responsible parent.
See socialmcp/clients.py.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

# What each source's export looks like on disk. The AppleScript panel takes
# uniform type identifiers; the suffixes are the second check, applied after a
# choice is made, because a panel filter is a convenience and not a guarantee.
# An NSOpenPanel, driven through the Objective-C bridge. AppleScript's
# `choose file` is simpler but exposes no control over folders: clicking a
# subfolder *selected* it, and the app then had to refuse it with an error —
# the user's click was treated as an answer rather than as navigation. Here
# `canChooseDirectories` is false, so folders open and only files can be
# chosen, which is how every Mac open panel behaves.
PANEL_SCRIPT = r"""
ObjC.import('Cocoa');
function run(argv) {
  if (argv.length === 1 && argv[0] === '__selftest__') return 'ok';
  const start = argv[0];
  const message = argv[1];
  const types = argv.slice(2);

  // Same activation dance as the alerts: a background tool cannot come to
  // the front, so the process adopts a regular policy and the panel is
  // raised above normal windows explicitly.
  const app = $.NSApplication.sharedApplication;
  app.setActivationPolicy(0);
  app.activateIgnoringOtherApps(true);

  const panel = $.NSOpenPanel.openPanel;
  panel.setCanChooseFiles(true);
  panel.setCanChooseDirectories(false);    // folders navigate, never select
  panel.setAllowsMultipleSelection(false);
  panel.setTreatsFilePackagesAsDirectories(true);
  panel.setShowsHiddenFiles(false);
  panel.setMessage(message);
  panel.setPrompt('Open');
  panel.setDirectoryURL($.NSURL.fileURLWithPath(start));
  panel.setAllowedFileTypes($(types));
  panel.setLevel(8);
  app.activateIgnoringOtherApps(true);

  if (Number(panel.runModal) !== 1) return '__cancelled__';   // NSModalResponseOK
  return ObjC.unwrap(panel.URL.path);
}
"""


SOURCE_ARCHIVES: dict[str, dict[str, object]] = {
    "linkedin": {
        "label": "LinkedIn",
        "types": ("public.zip-archive", "com.pkware.zip-archive", "zip"),
        "suffixes": (".zip",),
    },
}

DEFAULT_SOURCE = "linkedin"


def archive_suffixes(source: str = DEFAULT_SOURCE) -> tuple[str, ...]:
    """File endings a source's export may have."""
    entry = SOURCE_ARCHIVES.get(source, SOURCE_ARCHIVES[DEFAULT_SOURCE])
    return tuple(entry["suffixes"])  # type: ignore[arg-type]


def choose_export(
    initial_dir: Path | None = None, *, source: str = DEFAULT_SOURCE
) -> Path | None:
    """Open a file panel for one source's export. None if cancelled.

    `default location` must be a folder that exists. Given a file or a missing
    path the panel ignores it and restores whatever it showed last — usually
    Recents, a column view of `/private/var` temporary paths that tells the
    user nothing about where they are.
    """
    entry = SOURCE_ARCHIVES.get(source, SOURCE_ARCHIVES[DEFAULT_SOURCE])
    label = entry["label"]
    types = ", ".join(f'"{kind}"' for kind in entry["types"])  # type: ignore[union-attr]

    candidate = Path(initial_dir) if initial_dir else Path.home() / "Downloads"
    if candidate.is_file():
        candidate = candidate.parent
    if not candidate.is_dir():
        candidate = Path.home() / "Downloads"
    if not candidate.is_dir():
        candidate = Path.home()

    chosen = _panel_via_bridge(candidate, label, entry["types"])  # type: ignore[arg-type]
    if chosen is not None:
        return chosen if chosen != Path("__cancelled__") else None

    # Fallback: AppleScript. Folders are selectable here, which the caller has
    # to cope with, but a usable panel beats none.
    script = (
        # Activate first, or the panel can open behind the frontmost app.
        "activate\n"
        "set chosen to choose file with prompt "
        f'"Choose your {label} export (the .zip, exactly as you downloaded it)"'
        f' default location (POSIX file "{candidate}")'
        f" of type {{{types}}}\n"
        "POSIX path of chosen"
    )
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            check=False,
            timeout=900,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        # Cancelling raises AppleScript error -128, which is not a failure.
        return None
    path = (result.stdout or "").strip()
    return Path(path) if path else None


def _panel_via_bridge(
    start: Path, label: str, types: tuple[str, ...]
) -> Path | None:
    """NSOpenPanel via osascript. None if it could not run at all.

    Returns `Path("__cancelled__")` for an explicit cancel, so the caller can
    tell "the user said no" from "this mechanism is unavailable" — only the
    second should fall through to another implementation.
    """
    import tempfile

    message = f"Choose your {label} export (the .zip, exactly as you downloaded it)"
    try:
        with tempfile.NamedTemporaryFile(
            "w", suffix=".js", delete=False, encoding="utf-8"
        ) as handle:
            handle.write(PANEL_SCRIPT)
            script_path = handle.name
        try:
            result = subprocess.run(
                ["osascript", "-l", "JavaScript", script_path, str(start), message, *types],
                capture_output=True,
                text=True,
                check=False,
                timeout=900,
            )
        finally:
            Path(script_path).unlink(missing_ok=True)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    answer = (result.stdout or "").strip()
    if answer == "__cancelled__":
        return Path("__cancelled__")
    return Path(answer) if answer else None
