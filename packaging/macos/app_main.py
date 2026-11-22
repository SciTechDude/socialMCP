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
MAX_DIALOG_BUTTONS = 3  # AppleScript's `display dialog` hard limit

ACTION_LOAD = "Choose my export…"
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
    """A native dialog via AppleScript, so no UI toolkit is bundled.

    The leading `activate` matters more than it looks. A dialog shown by
    osascript belongs to the osascript process, which is not frontmost when
    this app is launched from Finder — so the window opened *behind* whatever
    the user was looking at. Someone who knows the app is running finds it in
    the Dock and clicks; everyone else concludes that double-clicking did
    nothing. `activate` brings the dialog to the front, and needs no
    permission because the process is only activating itself.
    """
    answer = _alert_via_bridge(message, title=title, buttons=buttons)
    if answer is not None:
        return answer

    # Fallback: AppleScript. Its dialog can open behind other windows, which
    # is exactly the problem the bridge solves, but a dialog the user has to
    # hunt for in the Dock still beats no dialog at all.
    button_list = ", ".join(f'"{b}"' for b in buttons)
    script = (
        "activate\n"
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


def trace(event: str) -> None:
    """Record a step in the app's log. See applog.py for why this exists."""
    import applog  # type: ignore[import-not-found]

    applog.write(event)


ALERT_SCRIPT = r"""
ObjC.import('Cocoa');
// Which button a response code means. NSAlert returns 1000 for the first
// button added, and buttons are added in reverse, so this undoes both.
function pick(buttons, response) {
  const index = buttons.length - 1 - (response - 1000);
  return buttons[index] !== undefined ? buttons[index] : buttons[buttons.length - 1];
}

function run(argv) {
  if (argv.length === 1 && argv[0] === '__selftest__') return 'ok';
  // Exercises the mapping above with no window, so the arithmetic is tested
  // rather than assumed: getting it wrong silently returns the wrong button.
  if (argv.length > 1 && argv[0] === '__maptest__') {
    const buttons = argv.slice(1);
    const out = [];
    for (let i = 0; i < buttons.length; i++) out.push(pick(buttons, 1000 + i));
    return out.join('|');
  }
  const title = argv[0];
  const message = argv[1];
  const buttons = argv.slice(2);

  // Without this, the alert belongs to a background tool and opens behind
  // whatever the user was looking at. AppleScript's bare `activate` cannot
  // fix that — osascript is not allowed to become frontmost — but setting a
  // regular activation policy through the bridge makes it a real foreground
  // app for the life of the dialog.
  const app = $.NSApplication.sharedApplication;
  app.setActivationPolicy(0);
  app.activateIgnoringOtherApps(true);

  const alert = $.NSAlert.alloc.init;
  alert.setMessageText(title);
  alert.setInformativeText(message);
  // NSAlert draws the first button added as the rightmost default, which is
  // the opposite of the order callers pass, so they go on in reverse.
  for (let i = buttons.length - 1; i >= 0; i--) alert.addButtonWithTitle(buttons[i]);

  // Activation alone loses to whatever is already frontmost: a terminal in
  // the foreground kept this behind it. So the window is also lifted above
  // normal windows (NSModalPanelWindowLevel = 8) and made key explicitly,
  // and activation is requested again immediately before the modal runs —
  // by which point the window exists and can actually be ordered front.
  const win = alert.window;
  win.setLevel(8);
  win.setHidesOnDeactivate(false);
  app.activateIgnoringOtherApps(true);
  win.makeKeyAndOrderFront($());

  return pick(buttons, Number(alert.runModal));
}
"""


CHECKBOX_SCRIPT = r"""
ObjC.import('Cocoa');
function run(argv) {
  const names = argv;
  // Lets a test compile and execute this script without drawing anything:
  // a syntax error here is otherwise only discoverable by a user, because
  // the caller quietly falls back to asking one app at a time.
  if (names.length === 1 && names[0] === '__selftest__') return 'ok';
  const app = $.NSApplication.sharedApplication;
  app.setActivationPolicy(0);               // Regular, so the alert can appear
  app.activateIgnoringOtherApps(true);

  const alert = $.NSAlert.alloc.init;
  alert.setMessageText('Connect SocialMCP to which apps?');
  alert.setInformativeText(
    'Each app ticked below gets a socialmcp entry added to its settings, and '
    + 'the file is backed up first. Nothing else in it is changed. '
    + 'Untick anything you would rather leave alone.\n\n'
    // Said here, not only in the summary afterwards: this is the first moment
    // anything is written, so the terms belong before the decision rather
    // than after it. No agreement gate — Apache-2.0 disclaims warranty
    // already, and `SocialMCP license` prints the full text.
    + 'SocialMCP is free and open source under Apache-2.0, provided as-is '
    + 'with no warranty. SocialMCP uploads nothing and collects no analytics, '
    + 'though answers your assistant gives you travel to whoever runs it.'
  );
  alert.addButtonWithTitle('Connect');
  alert.addButtonWithTitle('Cancel');

  const rowH = 26, width = 360;
  const view = $.NSView.alloc.initWithFrame($.NSMakeRect(0, 0, width, rowH * names.length));
  const boxes = [];
  for (let i = 0; i < names.length; i++) {
    const y = rowH * (names.length - 1 - i);
    const box = $.NSButton.alloc.initWithFrame($.NSMakeRect(0, y, width, rowH));
    box.setButtonType(3);                   // NSButtonTypeSwitch, i.e. a checkbox
    box.setTitle(names[i]);
    box.setState(1);                        // ticked: connecting all is the common case
    view.addSubview(box);
    boxes.push(box);
  }
  alert.setAccessoryView(view);

  // Activation alone loses to whatever is already frontmost: a terminal in
  // the foreground kept this behind it. So the window is also lifted above
  // normal windows (NSModalPanelWindowLevel = 8) and made key explicitly,
  // and activation is requested again immediately before the modal runs —
  // by which point the window exists and can actually be ordered front.
  const win = alert.window;
  win.setLevel(8);
  win.setHidesOnDeactivate(false);
  app.activateIgnoringOtherApps(true);
  win.makeKeyAndOrderFront($());

  if (Number(alert.runModal) !== 1000) return '__cancelled__';   // 1000 = first button
  const picked = [];
  for (let i = 0; i < boxes.length; i++) {
    if (Number(boxes[i].state) === 1) picked.push(names[i]);
  }
  return picked.length ? picked.join('\n') : '__none__';
}
"""


def choose_clients(names: list[str]) -> list[str] | None:
    """Ask which AI apps to connect, with real checkboxes.

    Shown only when more than one app is found: writing into another
    application's settings is not something to do unasked, and which of
    someone's editors gets an entry is preference, not detection.

    Drawn as an `NSAlert` with checkbox subviews, driven through
    `osascript -l JavaScript` and the Objective-C bridge. That is the native
    control, needs no bundled toolkit, and asks for no permissions.

    Two earlier attempts are worth remembering. AppleScript's
    `choose from list` looks like the obvious fit but draws highlightable rows
    rather than checkboxes, so selecting a subset needs Command-click and
    behaves as all-or-nothing for anyone who does not know that. Replacing it
    with one question per app worked but asked three times for three apps.
    This is what a Mac application would actually show.

    Falls back to plain questions if the script cannot run, so an unusual
    environment degrades to something usable rather than nothing.
    """
    import tempfile

    try:
        with tempfile.NamedTemporaryFile(
            "w", suffix=".js", delete=False, encoding="utf-8"
        ) as handle:
            handle.write(CHECKBOX_SCRIPT)
            script_path = handle.name
        try:
            result = subprocess.run(
                ["osascript", "-l", "JavaScript", script_path, *names],
                capture_output=True,
                text=True,
                check=False,
                timeout=900,
            )
        finally:
            Path(script_path).unlink(missing_ok=True)
    except (OSError, subprocess.SubprocessError):
        trace("consent: checkbox dialog unavailable, asking one at a time")
        return _choose_clients_one_at_a_time(names)

    if result.returncode != 0:
        trace(f"consent: checkbox dialog failed ({result.stderr.strip()[:120]})")
        return _choose_clients_one_at_a_time(names)

    answer = (result.stdout or "").strip()
    if answer == "__cancelled__":
        return None
    if answer == "__none__":
        return []
    picked = [line.strip() for line in answer.splitlines() if line.strip()]
    return [name for name in names if name in picked]


def _choose_clients_one_at_a_time(names: list[str]) -> list[str] | None:
    """Fallback: one plain question per app, needing no scripting bridge."""
    answer = dialog(
        f"Found {len(names)} AI apps that can use SocialMCP:\n\n"
        f"  {', '.join(names)}\n\n"
        "Connecting adds a socialmcp entry to each one's settings, and the\n"
        "file is backed up first. Nothing else in them is changed.\n\n"
        "Connect all of them, or decide one at a time?",
        buttons=("Cancel", "Decide each", "Connect all"),
    )
    if answer == "Cancel":
        return None
    if answer == "Connect all":
        return list(names)

    picked: list[str] = []
    for name in names:
        if dialog(
            f"Connect SocialMCP to {name}?\n\n"
            f"({len(picked)} of {len(names)} connected so far.)",
            buttons=("Skip", "Connect"),
        ) == "Connect":
            picked.append(name)
    return picked


def _run_jxa(script: str, args: list[str], *, timeout: int = 900):
    """Run a JavaScript-for-Automation script with arguments. None on failure."""
    import tempfile

    try:
        with tempfile.NamedTemporaryFile(
            "w", suffix=".js", delete=False, encoding="utf-8"
        ) as handle:
            handle.write(script)
            path = handle.name
        try:
            return subprocess.run(
                ["osascript", "-l", "JavaScript", path, *args],
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout,
            )
        finally:
            Path(path).unlink(missing_ok=True)
    except (OSError, subprocess.SubprocessError):
        return None


def _alert_via_bridge(
    message: str, *, title: str, buttons: tuple[str, ...]
) -> str | None:
    """A native NSAlert that raises itself. None if it could not be shown."""
    result = _run_jxa(ALERT_SCRIPT, [title, message, *buttons])
    if result is None or result.returncode != 0:
        if result is not None:
            trace(f"alert: bridge failed ({result.stderr.strip()[:120]})")
        return None
    answer = (result.stdout or "").strip()
    return answer if answer in buttons else None


TERMS = """SocialMCP — Terms of Use

1.  Licence. SocialMCP is free and open-source software licensed under the
    Apache License, Version 2.0. The full text accompanies this software as
    LICENSE.txt and may be displayed at any time by running
    `SocialMCP license`.

2.  No warranty. THE SOFTWARE IS PROVIDED "AS IS" AND WITHOUT WARRANTY OR
    CONDITION OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING WITHOUT LIMITATION
    ANY WARRANTY OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE, OR
    NON-INFRINGEMENT.

3.  Limitation of liability. To the maximum extent permitted by applicable
    law, no author or contributor shall be liable for any claim, damages, or
    other liability arising from, out of, or in connection with the software
    or its use.

4.  Scope of processing. The software processes only the data export you
    select. The resulting database is stored locally on this computer. The
    software operates without any account, sign-in, or server; it transmits
    no analytics or telemetry; and it does not access any social network,
    credential, or browser cookie. Deleting the database removes all data it
    holds.

5.  Third-party AI services. Results the software returns to your AI
    assistant form part of your conversation with that assistant and are
    accordingly disclosed to its operator (for example Anthropic, OpenAI, or
    Google) under that operator's own terms and privacy policy. Such
    disclosure is outside the control of this software and its authors.

6.  Outbound requests. The only network request the software initiates is to
    an embedding service you configure. This defaults to a model running on
    this computer and may be disabled entirely.

7.  Configuration of other applications. Where you so direct, the software
    adds an entry named "socialmcp" to the settings of the AI applications
    you select, having first created a backup of each file it modifies. No
    such file is modified before you make that selection.

8.  Acceptance. Selecting "Agree and continue" records your acceptance of
    these terms on this computer. The record is not transmitted."""


def terms_fingerprint() -> str:
    """Identifies this wording, so changed terms are shown again."""
    import hashlib

    return hashlib.sha256(TERMS.encode("utf-8")).hexdigest()[:16]


def terms_accepted() -> bool:
    """Has this wording been accepted on this Mac?"""
    marker = APP_SUPPORT / "accepted-terms.json"
    try:
        import json

        record = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return record.get("fingerprint") == terms_fingerprint()


def record_terms_acceptance() -> None:
    """Remember the acceptance, so it is asked once rather than every launch."""
    import datetime
    import json

    try:
        APP_SUPPORT.mkdir(parents=True, exist_ok=True)
        (APP_SUPPORT / "accepted-terms.json").write_text(
            json.dumps(
                {
                    "fingerprint": terms_fingerprint(),
                    "accepted_at": datetime.datetime.now()
                    .astimezone()
                    .isoformat(timespec="seconds"),
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    except OSError:
        # Unable to record it: better to ask again next time than to fail.
        pass


def terms_flow() -> bool:
    """Show the terms once, before anything else happens. False means quit.

    Deliberately the first thing on screen, and deliberately only once: it
    comes before any app is detected or any file written, and the wording is
    fingerprinted so a future change is shown again rather than assumed.
    """
    if terms_accepted():
        trace("terms: already accepted")
        return True

    trace("terms: asking")
    answer = ask(TERMS, title="SocialMCP — terms", buttons=("Quit", "Agree and continue"))
    if answer != "Agree and continue":
        trace("terms: declined, nothing written")
        return False
    record_terms_acceptance()
    trace("terms: accepted")
    return True


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


def build_setup_report(
    results, *, embeddings: bool, first_run: bool = True
) -> tuple[str, tuple[str, ...]]:
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
        f'Next: click "{ACTION_LOAD}" and pick the .zip LinkedIn sent you.',
        "",
        "Keep it as the .zip — double-clicking a .zip unzips it, and the .zip",
        "is what SocialMCP reads. The chooser opens in Downloads, where",
        "browsers put it, but you can pick it from anywhere.",
        "",
        "Don't have it yet? LinkedIn -> Settings & Privacy -> Data Privacy ->",
        "Get a copy of your data. Choose the larger archive; it arrives by",
        "email, usually within a day.",
        "",
        "Then quit and reopen your AI app and ask it:",
        '  "what can you do with my network?"',
        "",
        # Said once, where someone will actually read it, rather than behind an
        # "I agree" gate before they have seen what the app does. Apache-2.0
        # already disclaims warranty; this is the plain-language version, and
        # `SocialMCP license` prints the full text.
        "Free and open source under Apache-2.0, provided as-is with no",
        "warranty. SocialMCP uploads nothing and collects no analytics — though",
        "answers your assistant gives you travel to whoever runs it.",
    ]
    if restart:
        lines += ["", f"Restart needed for: {', '.join(r.client for r in restart)}"]

    if not embeddings:
        lines += [
            "",
            "Optional: searches currently match words literally. Installing Ollama",
            "(free, runs on this Mac, nothing uploaded) lets you search by meaning.",
        ]
        if not first_run:
            buttons.append("Get Ollama")

    # Offered only when there is something to disconnect. A user who will
    # never open a terminal still needs a way out, and the app's second launch
    # is the only place they will look.
    # Not on a first run: offering to disconnect something the user connected
    # ten seconds ago is noise, and it competes for one of three button slots
    # with the only thing they actually need to do next. It returns when they
    # open the app again, which is where someone looks to undo an install.
    if connected and not blocked and not first_run:
        buttons.append("Disconnect")

    # One action. Every extra button here was a decision the user had to make
    # about something they had not done yet, and each alternative route cost
    # either another permission prompt or another place for the file to be.
    buttons.append(ACTION_LOAD)

    # `display dialog` takes three buttons and fails silently with more.
    if len(buttons) > MAX_DIALOG_BUTTONS:
        optional = [b for b in buttons if b != ACTION_LOAD]
        keep = optional[: MAX_DIALOG_BUTTONS - 1]
        dropped = [b for b in optional if b not in keep]
        if dropped:
            lines += [
                "",
                "Also available when you open SocialMCP again: "
                + ", ".join(dropped)
                + ".",
            ]
        buttons = [*keep, ACTION_LOAD]
    return "\n".join(lines), tuple(buttons)


def _run_setup(report) -> tuple[Any, Any, Any]:
    """The actual setup work, reporting each step as it goes.

    Separated from the window so it can be tested without a display, and so
    the ordering guarantee — UI first, work second — is visible in one place.
    """
    from socialmcp import clients as clients_module
    from socialmcp.clients import detected_clients
    from socialmcp.config import load_config

    trace("step: load_config + ensure_dirs")
    report.step("Creating your data folder")
    config = load_config()
    config.ensure_dirs()
    drop = config.drop_dir / "linkedin"
    report.done("Created your data folder")

    # The bundle's own executable is the launch command, so the config a client
    # receives keeps working with no Python installed on the machine.
    command = launch_command()
    clients_module.resolve_launch_command = lambda: clients_module.LaunchCommand(
        command=command[0], args=command[1:]
    )

    # Detection only: no file is touched until the user has said which apps
    # to connect. `installed` is a path check, so this is fast and reads
    # nothing but directory existence.
    trace("step: detecting AI apps")
    report.step("Looking for your AI apps")
    detected = detected_clients()
    trace(f"step: detected {[c.display_name for c in detected]}")
    found = ", ".join(c.display_name for c in detected) if detected else "none found"
    report.done(f"Found: {found}")

    trace("step: checking for a local embedding model")
    report.step("Checking for local AI search")
    embeddings = embedding_available(config)
    trace("step: embedding check - done")
    report.done("Checked for local AI search")

    return config, drop, (detected, embeddings)


def setup_flow() -> int:
    """What happens when a person double-clicks the app."""
    # Refuse before writing anything. Configuring an AI client to launch a
    # binary inside a mounted disk image produces an install that works until
    # the image is ejected and then fails silently, which is worse than not
    # installing at all.
    trace("launch: checking where the app is running from")
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

    # Terms first, before anything is detected or written.
    if not terms_flow():
        return 0

    # A window appears before any of the work below. A double-click that shows
    # nothing for several seconds reads as a broken app, and a permission
    # prompt from an application that has not introduced itself reads as
    # suspicious. See packaging/macos/progress.py.
    from progress import run_with_progress  # type: ignore[import-not-found]

    from socialmcp.clients import known_clients, setup_all

    config, drop, (detected, embeddings) = run_with_progress(
        _run_setup, steps=3, intro="Setting up… finding your AI apps."
    )
    env = {"SOCIALMCP_HOME": str(config.home), "SOCIALMCP_DROP_DIR": str(config.drop_dir)}

    # One app found: no decision to make, so do not manufacture one. Several:
    # ask, because which of someone's editors gets a new config entry is
    # their call, not a detection result.
    chosen_keys: list[str] | None = None
    if len(detected) > 1:
        names = [client.display_name for client in detected]
        trace(f"consent: asking about {names}")
        picked = choose_clients(names)
        if picked is None:
            trace("consent: cancelled, nothing written")
            return 0
        chosen_keys = [c.key for c in detected if c.display_name in picked]
        trace(f"consent: connecting {chosen_keys}")
        if not chosen_keys:
            dialog(
                "Nothing was selected, so no app was connected.\n\n"
                "Open SocialMCP again whenever you want to connect one.",
                buttons=("OK",),
            )
            return 0

    trace("configuring the chosen apps")
    _, results = setup_all(env=env, only=chosen_keys)
    trace(f"configured: {[(r.client, r.status) for r in results]}")

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
    trace("about to show the first dialog")
    first_run = not config.db_path.exists()
    message, buttons = build_setup_report(
        results, embeddings=embeddings, first_run=first_run
    )
    choice = ask(message, buttons=buttons)

    if choice == "Replace it":
        # The user asked for this app to win, so overwrite the other entry.
        _, results = setup_all(env=env, force=True)
        message, buttons = build_setup_report(
            results, embeddings=embeddings, first_run=first_run
        )
        choice = ask(message, buttons=tuple(b for b in buttons if b != "Replace it"))

    if choice == "Disconnect":
        return disconnect_flow(config)

    if choice == "Get Ollama":
        subprocess.run(["open", "https://ollama.com"], check=False, timeout=30)
        return 0

    return load_flow(config, drop)


def ask(message: str, *, buttons: tuple[str, ...], title: str = "SocialMCP") -> str:
    """Show a choice. AppleScript, so nothing is bundled and nothing is asked for.

    `display dialog` accepts at most three buttons and fails silently with
    more — osascript errors and the caller cannot tell that from a click — so
    the limit is enforced here rather than hoped for.
    """
    if len(buttons) > MAX_DIALOG_BUTTONS:
        raise ValueError(
            f"macOS dialogs take at most {MAX_DIALOG_BUTTONS} buttons, got {buttons}"
        )
    return dialog(message, title=title, buttons=buttons)


def downloads_exports() -> list[Path]:
    """Candidate exports sitting in ~/Downloads, newest first.

    Downloads is the only place SocialMCP looks, deliberately. A browser puts
    the export there, and every alternative — a file panel, a watched folder of
    our own — added a second way to do the same thing, more permission prompts,
    and a decision for the user to get wrong. One source, one button.

    LinkedIn names the archive something like
    `Complete_LinkedInDataExport_11-15-2026.zip`, so those sort first; any other
    .zip is still offered, because the name has changed before and will again.
    """
    downloads = Path.home() / "Downloads"
    try:
        entries = [path for path in downloads.iterdir() if path.is_file()]
    except OSError:
        # No permission, or no such folder. The caller explains what to do.
        return []

    archives = [path for path in entries if path.name.lower().endswith(".zip")]
    named = [path for path in archives if "linkedin" in path.name.lower()]
    return sorted(named or archives, key=lambda p: p.stat().st_mtime, reverse=True)


def load_flow(config, drop: Path) -> int:
    """Read the newest export out of ~/Downloads, and say what happened."""
    from progress import run_with_progress  # type: ignore[import-not-found]
    from ui import choose_export  # type: ignore[import-not-found]

    # Always Downloads. Pointing the panel at a computed path risked handing
    # it something that is not a folder, and the panel then shows Recents —
    # a column view of /private/var temporary paths, which is disorienting and
    # looks broken.
    start = Path.home() / "Downloads"
    trace(f"load: opening the chooser at {start}")

    target = choose_export(start)
    if target is None:
        trace("load: cancelled")
        return 0
    trace(f"load: chose {target.name}")

    # The panel is told to offer archives only, but that is a convenience and
    # not a guarantee — a filter can be bypassed, and a future source may be
    # mapped wrongly. Checking here means the failure is a sentence the user
    # can act on rather than a parser error.
    from ui import archive_suffixes  # type: ignore[import-not-found]

    allowed = archive_suffixes()
    if target.is_dir() or not target.name.lower().endswith(allowed):
        trace(f"load: rejected {target.name} (not {allowed})")
        dialog(
            f"{target.name} is not a LinkedIn export.\n\n"
            f"SocialMCP reads the {' or '.join(allowed)} archive LinkedIn emails you,"
            " exactly as\nyou downloaded it. Double-clicking it unzips it, which is easy"
            " to do\nby accident — in that case download it again, or pick the"
            " original.\n\n"
            "To request one: LinkedIn -> Settings & Privacy -> Data Privacy ->\n"
            "Get a copy of your data. Choose the larger archive.",
            buttons=("OK",),
        )
        return 1

    def work(report):
        import shutil

        report.step(f"Reading {target.name}")
        from socialmcp.core.db import open_database
        from socialmcp.core.ingest import ingest_path

        # Copied in first, so the original download is never moved or deleted:
        # it is the user's file, and ingest archives what it reads.
        drop.mkdir(parents=True, exist_ok=True)
        staged = drop / target.name
        if not staged.exists():
            shutil.copy2(target, staged)

        db = open_database(config)
        result = ingest_path(db, config, staged)
        report.done(f"Read {target.name}")
        return result, db.scalar("select count(*) from contacts")

    try:
        _result, contacts = run_with_progress(
            work, steps=1, intro=f"Reading {target.name} — this stays on your Mac."
        )
    except Exception as exc:  # shown to the user, not swallowed
        trace(f"load: failed on {target.name}: {exc!r}")
        dialog(
            f"Could not read {target.name}:\n\n{exc}\n\n"
            "If that is not your LinkedIn export, remove it from Downloads or\n"
            "download the export again, then open SocialMCP once more.",
            buttons=("OK",),
        )
        return 1

    trace(f"load: done, {contacts} contacts")
    dialog(
        f"Loaded {target.name}.\n\n"
        f"Your network now has {contacts} people.\n\n"
        "Quit and reopen your AI app, then ask it:\n"
        '  "what can you do with my network?"\n\n'
        "Next time: put a newer export in Downloads and open SocialMCP again.",
        buttons=("OK",),
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
    # Logging is set up before anything else, including the stdio server, so a
    # failure during startup leaves a record. `serve` writes to the same file,
    # which is how a client-launched server can be debugged at all.
    import applog  # type: ignore[import-not-found]

    from socialmcp import __version__

    applog.configure(APP_SUPPORT)
    applog.start_session(version=__version__, bundle=app_bundle_root(), argv=argv)
    if not (argv and argv[0] == "serve"):
        # The stdio protocol owns stdout; wrapping subprocess is safe either
        # way, but the server starts no children worth tracing.
        applog.instrument_subprocess()

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
