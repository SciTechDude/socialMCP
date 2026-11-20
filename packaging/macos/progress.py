"""First-launch progress window.

Why this exists: setup does several seconds of work — probing for installed AI
apps, writing their config, creating folders — and all of it used to happen
before anything appeared on screen. A user who double-clicked the app saw
nothing at all, and macOS would sometimes raise a folder-permission prompt from
an application that had not yet explained itself. Silence reads as "broken",
and an unexplained permission request reads as "suspicious". Both are fatal
first impressions for software that asks to read your contacts.

So: a window appears within a second of launch, names each step as it happens,
and stays frontmost so any permission prompt arrives with visible context.

Tk is used because it ships with Python and needs no extra dependency. It is
treated as optional anyway: if the toolkit is missing or no display is
available, `run_with_progress` still runs the work and simply reports no
progress, because failing to draw a progress bar must never stop an install.
"""

from __future__ import annotations

import contextlib
import queue
import threading
from collections.abc import Callable
from typing import Any

WINDOW_TITLE = "Setting up SocialMCP"


class ProgressReporter:
    """What the work function is handed. Safe to call from a worker thread."""

    def __init__(self, sink: queue.Queue | None, total: int) -> None:
        self._sink = sink
        self._total = max(1, total)
        self._done = 0

    @property
    def total(self) -> int:
        return self._total

    def step(self, label: str) -> None:
        """Announce the step about to start."""
        self._emit(("step", label, self._done))

    def done(self, label: str) -> None:
        """Mark a step finished; advances the bar."""
        self._done += 1
        self._emit(("done", label, self._done))

    def _emit(self, message: tuple[str, str, int]) -> None:
        if self._sink is not None:
            self._sink.put(message)


def _tk_available() -> bool:
    try:
        import tkinter  # noqa: F401
    except Exception:
        return False
    return True


def run_with_progress(
    work: Callable[[ProgressReporter], Any],
    *,
    steps: int,
    title: str = WINDOW_TITLE,
    intro: str = "This takes a few seconds. Nothing leaves your computer.",
) -> Any:
    """Run `work` on a worker thread while showing progress.

    Returns whatever `work` returns, and re-raises whatever it raises, so the
    caller's error handling is unchanged by the presence of a window.
    """
    if not _tk_available():
        return work(ProgressReporter(None, steps))

    import tkinter as tk
    from tkinter import ttk

    sink: queue.Queue = queue.Queue()
    reporter = ProgressReporter(sink, steps)
    outcome: dict[str, Any] = {}

    def worker() -> None:
        try:
            outcome["value"] = work(reporter)
        except BaseException as exc:
            outcome["error"] = exc

    try:
        root = tk.Tk()
    except Exception:
        # No display, or Tk present but unusable. The install still matters.
        return work(ProgressReporter(None, steps))

    root.title(title)
    root.resizable(False, False)
    # Frontmost, so a macOS permission prompt triggered by the work below
    # appears over an app the user can see and identify.
    root.lift()
    root.attributes("-topmost", True)
    with contextlib.suppress(Exception):
        # Not all window managers allow forcing focus; the window is already
        # raised, so losing this is cosmetic.
        root.focus_force()

    frame = ttk.Frame(root, padding=20)
    frame.grid(sticky="nsew")

    ttk.Label(frame, text=title, font=("", 15, "bold")).grid(sticky="w")
    ttk.Label(frame, text=intro, wraplength=380, foreground="#555").grid(
        sticky="w", pady=(4, 12)
    )

    bar = ttk.Progressbar(frame, mode="determinate", maximum=steps, length=380)
    bar.grid(sticky="we")

    current = ttk.Label(frame, text="Starting…", wraplength=380)
    current.grid(sticky="w", pady=(8, 0))

    log = ttk.Label(frame, text="", wraplength=380, justify="left", foreground="#2e7d32")
    log.grid(sticky="w", pady=(8, 0))

    completed: list[str] = []

    def drain() -> None:
        try:
            while True:
                kind, label, count = sink.get_nowait()
                if kind == "step":
                    current.configure(text=f"{label}…")
                else:
                    completed.append(label)
                    bar.configure(value=count)
                    log.configure(text="\n".join(f"✓ {item}" for item in completed[-4:]))
        except queue.Empty:
            pass

        if thread.is_alive():
            root.after(80, drain)
        else:
            drain_final()

    def drain_final() -> None:
        bar.configure(value=steps)
        current.configure(text="Done")
        # Let the completed state register visually rather than vanishing.
        root.after(400, root.destroy)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    root.after(80, drain)

    try:
        root.mainloop()
    finally:
        thread.join(timeout=120)

    if "error" in outcome:
        raise outcome["error"]
    return outcome.get("value")
