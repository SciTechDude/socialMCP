"""Windows the app shows after setup: the summary, and picking an export.

Two reasons this is not AppleScript like the rest of the dialogs:

1. `display dialog` supports at most three buttons. The summary can legitimately
   offer four (replace another install, get Ollama, disconnect, open the
   folder), and when it does, osascript errors and the caller cannot tell —
   which read as a button pressing itself.

2. Getting a LinkedIn export into the app used to mean opening the drop folder
   and dragging a file in from Downloads, across two Finder windows. A file
   picker is one click, and because macOS picks files out of process, it needs
   no folder permission at all: the app receives the one file chosen and
   nothing else.

Tk is already bundled for the first-launch progress window, so this adds no
dependency. It is still optional: callers fall back to AppleScript if Tk
cannot start.
"""

from __future__ import annotations

import contextlib
from pathlib import Path


def tk_available() -> bool:
    try:
        import tkinter  # noqa: F401
    except Exception:
        return False
    return True


def choose_export(initial_dir: Path | None = None) -> Path | None:
    """Native open panel, starting wherever downloads land.

    Returns None if the user cancels or no toolkit is available. Only the
    chosen file becomes readable by this app, which is why this is preferred
    over asking for access to a whole folder.
    """
    if not tk_available():
        return None
    import tkinter as tk
    from tkinter import filedialog

    try:
        root = tk.Tk()
    except Exception:
        return None
    root.withdraw()
    with contextlib.suppress(Exception):
        root.attributes("-topmost", True)
    start = str(initial_dir or (Path.home() / "Downloads"))
    try:
        chosen = filedialog.askopenfilename(
            parent=root,
            title="Choose your LinkedIn data export",
            initialdir=start,
            # The export is a .zip as downloaded. Extracted folders work too,
            # but a picker cannot offer both, and the zip is what people have.
            filetypes=[("LinkedIn export (.zip)", "*.zip"), ("All files", "*")],
        )
    finally:
        with contextlib.suppress(Exception):
            root.destroy()
    return Path(chosen) if chosen else None


def summary_window(
    message: str,
    *,
    buttons: tuple[str, ...],
    title: str = "SocialMCP",
) -> str | None:
    """Show the setup summary. Returns the button pressed, or None if no Tk.

    Buttons are laid out in the order given, with the last as the default, to
    match the AppleScript behaviour this replaces.
    """
    if not tk_available():
        return None
    import tkinter as tk
    from tkinter import ttk

    try:
        root = tk.Tk()
    except Exception:
        return None

    root.title(title)
    root.resizable(False, False)
    root.lift()
    with contextlib.suppress(Exception):
        root.attributes("-topmost", True)
        root.focus_force()

    chosen: dict[str, str] = {}

    frame = ttk.Frame(root, padding=20)
    frame.grid(sticky="nsew")

    body = tk.Text(frame, wrap="word", width=62, height=min(22, message.count("\n") + 4),
                   relief="flat", highlightthickness=0, borderwidth=0)
    body.insert("1.0", message)
    body.configure(state="disabled", background=root.cget("background"))
    body.grid(sticky="we")

    row = ttk.Frame(frame)
    row.grid(sticky="e", pady=(16, 0))

    def pick(label: str) -> None:
        chosen["value"] = label
        root.destroy()

    for index, label in enumerate(buttons):
        style = "Accent.TButton" if index == len(buttons) - 1 else "TButton"
        ttk.Button(row, text=label, style=style, command=lambda b=label: pick(b)).grid(
            row=0, column=index, padx=(8, 0)
        )

    root.protocol("WM_DELETE_WINDOW", lambda: pick(buttons[-1]))
    root.bind("<Return>", lambda _event: pick(buttons[-1]))
    root.mainloop()
    return chosen.get("value", buttons[-1])
