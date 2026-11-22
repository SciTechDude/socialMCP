"""Progress feedback during first launch, without a GUI toolkit.

Two findings drove this design, both from testing on a real Mac:

1. **Silence reads as broken.** Setup does several seconds of work — probing
   for installed AI apps, writing their config — and when all of it happened
   before anything appeared on screen, a double-click looked like it did
   nothing at all.

2. **Tk is what asked for your folders.** A Tk progress window was the first
   fix, and it immediately produced macOS prompts for Downloads, Desktop and
   Documents. Those prompts appeared *before any file picker existed*, and the
   earlier osascript-only build never produced them. Tk touches the standard
   folders when it initialises, so for an app whose entire claim is that it
   reads one file you hand it, bundling Tk costs three alarming permission
   requests that buy nothing. It also drew blank windows unless a full
   `mainloop` ran, which does not fit a step-by-step flow.

So progress is reported with `display notification`, which is non-blocking,
needs no toolkit, and asks for nothing. Each step replaces the last. It is
less pretty than a progress bar and it is honest about what is happening,
which is the property that matters here.
"""

from __future__ import annotations

import contextlib
import subprocess
from collections.abc import Callable
from typing import Any

TITLE = "SocialMCP"


def notify(message: str, *, title: str = TITLE, subtitle: str = "") -> None:
    """A banner, best-effort. Never raises, never blocks, never prompts."""
    script = f'display notification {_quote(message)} with title {_quote(title)}'
    if subtitle:
        script += f" subtitle {_quote(subtitle)}"
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            check=False,
            timeout=15,
        )


def _quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


class ProgressReporter:
    """Reports steps as notifications, and remembers them for the caller."""

    def __init__(self, total: int, *, quiet: bool = False) -> None:
        self._total = max(1, total)
        self._done = 0
        self._quiet = quiet
        self.steps: list[str] = []

    @property
    def total(self) -> int:
        return self._total

    def step(self, label: str) -> None:
        self.steps.append(f"step:{label}")
        if not self._quiet:
            notify(f"{label}…", subtitle=f"Step {self._done + 1} of {self._total}")

    def done(self, label: str) -> None:
        self._done += 1
        self.steps.append(f"done:{label}")


def run_with_progress(
    work: Callable[[ProgressReporter], Any],
    *,
    steps: int,
    title: str = TITLE,
    intro: str = "",
    quiet: bool = False,
) -> Any:
    """Run `work`, reporting each step it announces.

    Runs on the calling thread: there is no event loop to keep responsive, and
    the background thread the Tk version needed is what made Tcl abort the
    process when a second interpreter appeared.

    Returns whatever `work` returns and re-raises whatever it raises, so error
    handling is unaffected by how progress is displayed.
    """
    if not quiet:
        notify(intro or "Setting up… this takes a few seconds.", title=title)
    return work(ProgressReporter(steps, quiet=quiet))
