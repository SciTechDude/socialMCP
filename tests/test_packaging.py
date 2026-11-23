"""Guards on the macOS packaging, which cannot be exercised by the test suite.

Building a .dmg takes minutes and needs macOS, so these tests check the things
that silently break a bundle instead: a data file that is never collected, an
adapter that is dropped because nothing imports it statically, or an entry
point that no longer matches what the installer writes into a client's config.

Each of these has a real failure mode. A missing schema.sql means the app
starts and then cannot create its database; a missing hidden import means the
LinkedIn adapter vanishes and every export is "unrecognized".
"""

from __future__ import annotations

import os
import plistlib
import re
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
MACOS = REPO / "packaging" / "macos"


def test_packaging_files_are_present():
    for name in ("app_main.py", "socialmcp.spec", "build.sh", "entitlements.plist", "README.md"):
        assert (MACOS / name).is_file(), f"packaging/macos/{name} is missing"


@pytest.mark.skipif(sys.platform == "win32", reason="Windows has no executable bit")
def test_build_script_is_executable():
    assert (MACOS / "build.sh").stat().st_mode & 0o111, "build.sh must be executable"


def test_spec_collects_every_runtime_data_file():
    """Anything read from disk at runtime must be listed explicitly.

    PyInstaller's import analysis cannot see a file opened by path, so a new
    .sql file would be silently absent from the bundle.
    """
    spec = (MACOS / "socialmcp.spec").read_text(encoding="utf-8")
    for data_file in (REPO / "socialmcp" / "core").glob("*.sql"):
        assert data_file.name in spec, (
            f"{data_file.name} is read at runtime but not collected in socialmcp.spec"
        )


def test_spec_hides_nothing_the_registry_needs():
    """Adapters are imported by name, so they need explicit hidden imports."""
    spec = (MACOS / "socialmcp.spec").read_text(encoding="utf-8")
    from socialmcp.adapters import BUILTIN_MODULES

    for module in BUILTIN_MODULES:
        assert module in spec, f"{module} is imported dynamically and must be a hiddenimport"


def test_app_entry_point_matches_what_setup_writes():
    """The bundle's `serve` argument is what lands in a client's config.

    If the entry point stopped accepting `serve`, every configured client would
    silently fail to start the server.
    """
    source = (MACOS / "app_main.py").read_text(encoding="utf-8")
    assert '"serve"' in source
    assert "launch_command" in source
    # And the installer must point at the bundle's own binary, not a python path.
    assert "sys.executable" in source


def test_entitlements_are_valid_and_minimal():
    with (MACOS / "entitlements.plist").open("rb") as handle:
        entitlements = plistlib.load(handle)

    # Required for a notarized PyInstaller app and for loading vec0.dylib.
    assert entitlements["com.apple.security.cs.allow-unsigned-executable-memory"] is True
    assert entitlements["com.apple.security.cs.disable-library-validation"] is True
    # Outbound only. A server entitlement would contradict the stdio transport
    # and the project's no-listening-socket stance.
    assert entitlements.get("com.apple.security.network.client") is True
    assert "com.apple.security.network.server" not in entitlements


def test_build_script_verifies_the_bundle_is_self_contained():
    """The build must fail loudly if the bundle needs the dev environment."""
    script = (MACOS / "build.sh").read_text(encoding="utf-8")
    assert "env -i" in script, "build.sh must test the bundle with a stripped environment"
    # A stale analysis is silently reused, so the output directory for the
    # architecture being built has to be cleared first.
    assert 'rm -rf "build/$TARGET_ARCH" "dist/$TARGET_ARCH"' in script
    # And the produced binary must be checked against what was asked for; a
    # mislabelled dmg is worse than a missing one.
    assert "lipo -archs" in script
    assert "--sign" in script


@pytest.mark.parametrize("artifact", ["dist", "build", "SocialMCP.dmg", ".venv-build"])
def test_build_artifacts_are_not_committable(artifact: str):
    """Installers ship as Releases; binaries must never enter git history."""
    import subprocess

    result = subprocess.run(
        ["git", "check-ignore", "-q", "--no-index", artifact],
        cwd=REPO,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, f"{artifact} is not gitignored"


def test_nothing_advises_a_global_install():
    """No instruction anywhere may install into a user's system interpreter.

    A bare `pip install` lands in whichever interpreter is active, which on
    many machines is the one the OS depends on. Published install paths are
    `uv tool`/`pipx` (each app gets a private environment) or the macOS bundle
    (its own interpreter inside); development uses an explicit venv.
    """
    offenders: list[str] = []
    for name in ("README.md", "QUICKSTART.md", "CONTRIBUTING.md", "packaging/macos/README.md"):
        path = REPO / name
        if not path.is_file():
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        for number, line in enumerate(lines, start=1):
            stripped = line.strip()
            if not stripped.startswith(("pip install", "pip3 install", "python -m pip install")):
                continue
            # Allowed: editable installs, which the companion test requires to
            # be preceded by a venv, and bootstrapping pipx itself with --user.
            if stripped.startswith(("pip install -e", "pip install --user pipx")):
                continue
            offenders.append(f"{name}:{number}: {stripped}")
    assert not offenders, "global install instructions found: " + "; ".join(offenders)


def test_editable_installs_are_always_preceded_by_a_venv():
    """`pip install -e` is fine, but only inside a venv the reader just made."""
    for name in ("README.md", "CONTRIBUTING.md"):
        lines = (REPO / name).read_text(encoding="utf-8").splitlines()
        for number, line in enumerate(lines):
            # Only real commands; prose discussing `pip install -e` is fine.
            if not line.strip().startswith("pip install -e"):
                continue
            window = "\n".join(lines[max(0, number - 6) : number])
            assert "venv" in window, (
                f"{name}:{number + 1} installs editable without creating a venv first"
            )


def test_doctor_never_suggests_a_bare_pip():
    """The hint a user is most likely to copy and paste must be unambiguous."""
    from socialmcp.doctor import install_hint

    hint = install_hint("sqlite-vec")
    assert "-m pip install" in hint
    assert sys.executable in hint, "the hint must name the interpreter it applies to"


# ---------------------------------------------------------------------------
# the app's setup dialog
# ---------------------------------------------------------------------------


def _load_app_main():
    """Import packaging/macos/app_main.py, which is outside the package.

    Its directory goes on sys.path because `app_main` imports its siblings
    (`ui`, `progress`) by bare name — which is how PyInstaller lays them out in
    the bundle. Without this, tests could only reach functions that happen not
    to touch a sibling, which is how a call to a non-existent database API
    reached a user.
    """
    import importlib.util

    if str(MACOS) not in sys.path:
        sys.path.insert(0, str(MACOS))
    spec = importlib.util.spec_from_file_location("app_main", MACOS / "app_main.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Result:
    def __init__(self, client: str, status: str, detail: str = ""):
        self.client, self.status, self.detail = client, status, detail


def test_setup_report_explains_a_blocked_client():
    """A pip-installed copy already registered must not read as "nothing happened".

    This case previously produced "Connected to: none" with no explanation,
    leaving the user with a working install they believed had failed.
    """
    app = _load_app_main()
    message, buttons = app.build_setup_report(
        [_Result("Claude Desktop", "skipped")], embeddings=True
    )

    assert "Already set up by another copy" in message
    assert "Claude Desktop" in message
    assert "command line" in message, "the likely cause should be named"
    assert "Replace it" in buttons
    assert "Connected to:" not in message


def test_setup_report_lists_connected_clients_and_restarts():
    app = _load_app_main()
    message, buttons = app.build_setup_report(
        [_Result("Cursor", "configured"), _Result("Claude Code", "already")], embeddings=True,
    )

    assert "Connected to: Cursor, Claude Code" in message
    # Only newly written configs need a restart; an existing one does not.
    assert "Restart needed for: Cursor" in message
    assert "Replace it" not in buttons
    # One action, and it is the default. Alternatives were removed on purpose:
    # each one was a decision about something the user had not done yet, and
    # each route cost either another permission prompt or another place the
    # file could be.
    assert buttons[-1] == app.ACTION_LOAD
    assert len(buttons) == 1, f"a first run should offer one action, got {buttons}"


def test_setup_report_surfaces_a_failure_with_its_reason():
    app = _load_app_main()
    message, _ = app.build_setup_report(
        [_Result("Zed", "failed", "its config file is not valid JSON")], embeddings=True,
    )
    assert "Could not configure: Zed" in message
    assert "not valid JSON" in message


def test_setup_report_offers_ollama_only_when_missing():
    """Literal-only search is not an error, but the user must know about it."""
    app = _load_app_main()

    without, buttons_without = app.build_setup_report(
        [_Result("Cursor", "configured")], embeddings=False
    )
    assert "match words literally" in without
    assert "nothing uploaded" in without, "the privacy point is the reason to accept"
    # The button is held back on a first run: three slots exist and loading
    # data is what matters then. The explanation is in the text either way, and
    # the button returns next time the app is opened.
    assert "Get Ollama" not in buttons_without, "not on a first run"
    _text, later = app.build_setup_report(
        [_Result("Cursor", "configured")], embeddings=False,
        first_run=False,
    )
    assert "Get Ollama" in later, "offered once the user is past setting up"

    with_model, buttons_with = app.build_setup_report(
        [_Result("Cursor", "configured")], embeddings=True
    )
    assert "Ollama" not in with_model
    assert "Get Ollama" not in buttons_with

def test_version_is_declared_in_exactly_one_place():
    """`__version__`, pyproject and the bundle's Info.plist must agree.

    They used to be three literals. A release whose dmg reports a different
    version than the tag it shipped under is confusing to report a bug against
    and impossible to verify, so the spec now reads pyproject and this test
    pins the remaining literal to it.
    """
    import tomllib

    repo = Path(__file__).resolve().parent.parent
    declared = tomllib.loads((repo / "pyproject.toml").read_text(encoding="utf-8"))
    project_version = declared["project"]["version"]

    import socialmcp

    assert socialmcp.__version__ == project_version, (
        f"socialmcp.__version__ is {socialmcp.__version__} but pyproject says "
        f"{project_version} — bump both, or the shipped app misreports itself"
    )

    spec = (repo / "packaging" / "macos" / "socialmcp.spec").read_text(encoding="utf-8")
    assert 'version="0.1' not in spec and "CFBundleShortVersionString\": \"0.1" not in spec, (
        "the PyInstaller spec has a hardcoded version again; it must read "
        "pyproject.toml so the bundle cannot disagree with the release"
    )
    assert "VERSION" in spec


def test_dmg_filename_carries_the_version_and_architecture():
    """The release asset name must be unique per version.

    Uploading a dmg whose name matches an existing release asset is how a
    published artifact gets replaced — which resets its download count and
    invalidates the checksum printed in its release notes. Different names per
    version make that collision impossible, so the naming scheme is a release
    safety property, not cosmetics.
    """
    script = (
        Path(__file__).resolve().parent.parent / "packaging" / "macos" / "build.sh"
    ).read_text(encoding="utf-8")
    assignment = [ln for ln in script.splitlines() if ln.startswith("DMG=")]
    assert assignment, "build.sh no longer defines DMG"
    line = assignment[0]
    assert "${VERSION}" in line, f"dmg name must include the version: {line}"
    assert "${TARGET_ARCH}" in line, f"dmg name must include the architecture: {line}"


def _load_progress():
    """Load packaging/macos/progress.py the way the bundle does."""
    import importlib.util

    path = Path(__file__).resolve().parent.parent / "packaging" / "macos" / "progress.py"
    spec = importlib.util.spec_from_file_location("progress", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_progress_runs_the_work_even_with_no_gui_toolkit():
    """A missing or unusable Tk must never stop an install.

    CI has no display. More importantly, a user whose bundle somehow lacks the
    toolkit should still get a configured app rather than nothing.
    """
    progress = _load_progress()
    progress._tk_available = lambda: False

    calls = []

    def work(report):
        report.step("probing")
        report.done("probed")
        calls.append("ran")
        return "value"

    assert progress.run_with_progress(work, steps=1) == "value"
    assert calls == ["ran"], "the work must run even when no window can be drawn"


def test_progress_reraises_failures_from_the_worker():
    """Errors must surface, not be swallowed behind a closed window."""
    progress = _load_progress()
    progress._tk_available = lambda: False

    def work(report):
        raise RuntimeError("setup exploded")

    with pytest.raises(RuntimeError, match="setup exploded"):
        progress.run_with_progress(work, steps=1)


def test_first_launch_shows_the_window_before_any_slow_work():
    """The regression this guards: silence on double-click.

    Setup probes other apps' CLIs and can trigger a macOS folder-permission
    prompt. Doing that before anything is on screen made the app look broken
    and the prompt look suspicious, so `setup_flow` must hand the work to the
    progress window rather than calling it directly.
    """
    source = (
        Path(__file__).resolve().parent.parent / "packaging" / "macos" / "app_main.py"
    ).read_text(encoding="utf-8")
    flow = source.split("def setup_flow(")[1]

    # Collapse whitespace so a wrapped call still matches: the assertion is
    # about ordering, not formatting.
    squashed = " ".join(flow.split())
    assert "run_with_progress( _run_setup" in squashed or "run_with_progress(_run_setup" in squashed, (
        "setup_flow must run setup through run_with_progress, which reports each step"
    )
    # The slow calls belong in _run_setup, which run_with_progress drives.
    # Split on the call site, not the import — the import appears first and
    # slicing there would make this test unable to fail.
    marker = "run_with_progress(" if "run_with_progress(" in squashed else None
    assert marker, "no progress-reporting call in setup_flow"
    body_before_window = squashed.split("run_with_progress(")[0]
    for slow in ("setup_all(", "embedding_available("):
        assert slow not in body_before_window, (
            f"{slow} runs before the progress window exists — that is the bug"
        )


def test_client_probe_timeout_is_short_and_detached_from_stdin():
    """Launched from Finder, `claude mcp list` hangs waiting on a terminal.

    It previously had a 120s timeout and inherited stdin, so first launch could
    sit silent for two minutes. Bounded, detached, and with a fallback.
    """
    from socialmcp import clients

    assert clients.CLIENT_PROBE_TIMEOUT <= 15, (
        "a first-launch probe must fail fast; this one blocks the only UI"
    )
    source = (
        Path(__file__).resolve().parent.parent / "socialmcp" / "clients.py"
    ).read_text(encoding="utf-8")
    probe = source.split("def _configure_via_claude_cli(")[1].split("def ")[0]
    assert "stdin=subprocess.DEVNULL" in probe, "the probe must not inherit stdin"
    assert "timeout=CLIENT_PROBE_TIMEOUT" in probe
    assert "TimeoutExpired" in probe, "a timeout must fall back, not propagate"


def test_only_downloads_is_declared_and_it_says_why():
    """Downloads is the single source, so it is the single declaration.

    Desktop and Documents must stay absent: nothing reads them. They only ever
    prompted because a file panel enumerated its sidebar favourites, and the
    panel is gone — so asking for them would be claiming access the app does
    not use, in a program whose whole claim is that it reads one file.
    """
    spec = (
        Path(__file__).resolve().parent.parent / "packaging" / "macos" / "socialmcp.spec"
    ).read_text(encoding="utf-8")

    assert "NSDownloadsFolderUsageDescription" in spec
    after = spec.split("NSDownloadsFolderUsageDescription", 1)[1][:400]
    assert "one file" in after, "scope the request to the file it reads"
    assert "nothing is uploaded" in after, "state the privacy promise in the prompt"

    for key in ("NSDocumentsFolderUsageDescription", "NSDesktopFolderUsageDescription"):
        assert key not in spec, f"{key} claims access to a folder nothing reads"


def test_the_chooser_opens_where_the_export_already_is():
    """One button, and the panel starts somewhere useful.

    The panel was briefly removed on the theory that it caused the Downloads,
    Documents and Desktop prompts. It did not: those came from `claude mcp
    list`, which Claude Code answers by launching every configured MCP server,
    and macOS attributes a child's file access to the responsible parent. With
    that fixed the panel costs nothing, and browsing anywhere is strictly more
    useful than a single blessed folder.
    """
    macos = Path(__file__).resolve().parent.parent / "packaging" / "macos"
    ui_source = (macos / "ui.py").read_text(encoding="utf-8")
    app_source = (macos / "app_main.py").read_text(encoding="utf-8")

    assert "choose file" in ui_source, "there should be a real file panel"
    assert "Downloads" in ui_source, "it should default to where browsers save"
    assert "choose_export" in app_source, "the one action should open the panel"


def test_setup_never_lists_the_users_other_mcp_servers():
    """Ask about our own entry, never enumerate someone's whole setup.

    `claude mcp list` returns every MCP server the user has configured, which
    is private to them and irrelevant here — and answering it makes Claude Code
    launch each one, which is what dragged folder-permission prompts into this
    app's name.
    """
    source = (
        Path(__file__).resolve().parent.parent / "socialmcp" / "clients.py"
    ).read_text(encoding="utf-8")
    code = "".join(source.replace('"""', "\x00").split("\x00")[::2])

    assert '"mcp", "get"' in code, "query our own server by name"
    assert '"mcp", "list"' not in code, (
        "listing exposes every MCP server the user has configured"
    )


def test_the_drop_folder_readme_is_written_for_a_human():
    """Whoever opens that folder is mid-install and confused, not debugging.

    It used to say "routed to the `linkedin` adapter" and "this folder is
    gitignored", which answers nothing the reader is asking.
    """
    import tempfile

    from socialmcp.config import Config

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        config = Config(home=root, drop_dir=root / "drop")
        config.ensure_dirs()
        readme = next((root / "drop").glob("*/README.md"), None)
        assert readme is not None, "no per-source README was written"
        text = readme.read_text(encoding="utf-8")

    assert "do not double-click" in text.lower(), (
        "double-clicking a .zip unzips it; this is the trap users hit"
    )
    assert "Choose my export" in text, "point at the easier path in the app"
    for jargon in ("adapter", "gitignore"):
        assert jargon not in text.lower(), f"{jargon!r} means nothing to this reader"


@pytest.mark.skipif(
    not hasattr(os, "ST_RDONLY"),
    reason="statvfs flags are POSIX-only; the guard itself only ever runs on macOS",
)
def test_setup_refuses_to_run_from_a_mounted_disk_image(monkeypatch, tmp_path):
    """Registering a /Volumes path produces an install that dies on eject.

    Double-clicking the app inside the downloaded .dmg is the most natural
    thing to do — the window opens with the app right there. Setup writes the
    binary's absolute path into each AI client's config, so that path has to
    be somewhere permanent.
    """
    app = _load_app_main()

    bundle = tmp_path / "SocialMCP.app"
    bundle.mkdir()
    monkeypatch.setattr(app, "app_bundle_root", lambda: bundle)

    # Not under /Volumes: a normal install, no complaint.
    assert app.running_from_removable_location() is None

    # Under /Volumes on a read-only filesystem: a mounted image.
    monkeypatch.setattr(app, "app_bundle_root", lambda: Path("/Volumes/SocialMCP 9.9/SocialMCP.app"))

    class _ReadOnly:
        f_flag = os.ST_RDONLY

    monkeypatch.setattr(app.os, "statvfs", lambda _path: _ReadOnly())
    assert app.running_from_removable_location() == Path(
        "/Volumes/SocialMCP 9.9/SocialMCP.app"
    )

    # Writable external disk: someone may really keep apps there, so allow it.
    class _Writable:
        f_flag = 0

    monkeypatch.setattr(app.os, "statvfs", lambda _path: _Writable())
    assert app.running_from_removable_location() is None


def test_setup_flow_checks_the_location_before_configuring_anything():
    """The guard must come first, or a dead path is written before we look."""
    source = (
        Path(__file__).resolve().parent.parent / "packaging" / "macos" / "app_main.py"
    ).read_text(encoding="utf-8")
    flow = source.split("def setup_flow(")[1].split("\ndef ")[0]

    squashed = " ".join(flow.split())
    guard = squashed.find("running_from_removable_location()")
    work = squashed.find("run_with_progress(")
    assert guard != -1, "setup_flow no longer checks where it is running from"
    assert 0 < guard < work, "the location check must precede any setup work"

def test_every_sibling_module_is_declared_for_the_bundle():
    """Modules imported by bare name must be in hiddenimports.

    `app_main` imports `ui`, `progress` and `tkroot` as top-level names, which
    is how PyInstaller arranges them. If one is not bundled, the failure is an
    ImportError in a user's hands — there is no build error — so the set is
    asserted rather than trusted to import analysis.
    """
    macos = Path(__file__).resolve().parent.parent / "packaging" / "macos"
    spec = (macos / "socialmcp.spec").read_text(encoding="utf-8")

    siblings = {p.stem for p in macos.glob("*.py")} - {"app_main", "socialmcp"}
    imported = {
        name
        for name in siblings
        if f"from {name} import" in (macos / "app_main.py").read_text(encoding="utf-8")
        or f"import {name}" in (macos / "app_main.py").read_text(encoding="utf-8")
    }
    # progress and ui import tkroot, so include anything they pull in too.
    for module in list(imported):
        source = (macos / f"{module}.py").read_text(encoding="utf-8")
        imported |= {name for name in siblings if f"import {name}" in source}

    missing = sorted(name for name in imported if f'"{name}"' not in spec)
    assert not missing, (
        f"these are imported by name but not declared in hiddenimports: {missing}"
    )


def test_no_gui_toolkit_is_bundled_or_imported():
    """Tk is what asked for Downloads, Desktop and Documents.

    A Tk progress window produced all three prompts the moment it initialised
    — before any file picker existed — while the osascript-only build produced
    none. For an app whose entire claim is that it reads one file you hand it,
    that is an unacceptable price for a progress bar. Tk also rendered blank
    windows unless a full `mainloop` ran.

    If a nicer UI is wanted later, the native route is AppKit via PyObjC,
    which is what Mac applications actually use. This test should then be
    rewritten to require *that*, not relaxed to allow Tk back.
    """
    macos = Path(__file__).resolve().parent.parent / "packaging" / "macos"

    spec = (macos / "socialmcp.spec").read_text(encoding="utf-8")
    excludes = spec.split("excludes=[", 1)[1].split("]", 1)[0]
    assert '"tkinter"' in excludes, "tkinter must stay out of the bundle"

    for module in macos.glob("*.py"):
        source = module.read_text(encoding="utf-8")
        code = "\n".join(
            line for line in source.splitlines() if not line.strip().startswith("#")
        )
        # Strip docstrings crudely: they explain *why* Tk is gone.
        code = code.replace('"""', "\x00").split("\x00")
        code = "".join(code[::2])
        assert "import tkinter" not in code, f"{module.name} imports tkinter"
        assert "tkroot" not in code, f"{module.name} still references the Tk root helper"


def test_setup_report_never_exceeds_the_dialog_button_limit():
    """More than three buttons makes osascript fail, invisibly.

    `display dialog` takes at most three. When it got four, the script errored,
    the exit status was ignored, and the last button was returned as though the
    user had clicked it.
    """
    app = _load_app_main()

    # Every combination that can add a button at once: another install to
    # replace, a connected client to disconnect, and no embeddings (Ollama).
    results = [
        _Result("Claude Code", "already-other"),
        _Result("Cursor", "configured"),
        _Result("Zed", "failed", "bad json"),
    ]
    for embeddings in (True, False):
        for first_run in (True, False):
            _message, buttons = app.build_setup_report(
                results, embeddings=embeddings, first_run=first_run
            )
            assert len(buttons) <= app.MAX_DIALOG_BUTTONS, (
                f"{len(buttons)} buttons ({buttons}) — osascript shows at most "
                f"{app.MAX_DIALOG_BUTTONS} and fails silently beyond that"
            )


def test_downloads_is_the_only_place_it_looks(monkeypatch, tmp_path):
    """Newest first, LinkedIn-named archives preferred, nothing else offered."""
    app = _load_app_main()

    home = tmp_path / "home"
    downloads = home / "Downloads"
    downloads.mkdir(parents=True)
    monkeypatch.setattr(app.Path, "home", staticmethod(lambda: home))

    import time

    older = downloads / "Complete_LinkedInDataExport_01-01-2026.zip"
    newer = downloads / "Complete_LinkedInDataExport_11-15-2026.zip"
    decoy = downloads / "holiday-photos.zip"
    notes = downloads / "something.pdf"
    for path in (older, decoy, notes, newer):
        path.write_text("x", encoding="utf-8")
        time.sleep(0.01)

    found = app.downloads_exports()

    assert found[0] == newer, "the newest export should be chosen"
    assert older in found
    assert decoy not in found, "a LinkedIn-named archive outranks any other zip"
    assert notes not in found, "only .zip files are candidates"


def test_no_downloads_folder_is_explained_not_crashed(monkeypatch, tmp_path):
    """A missing or unreadable Downloads folder must not raise."""
    app = _load_app_main()
    monkeypatch.setattr(app.Path, "home", staticmethod(lambda: tmp_path / "nowhere"))
    assert app.downloads_exports() == []


def test_load_flow_reads_the_export_and_reports_the_count(monkeypatch, tmp_path):
    """The whole single path, end to end, against the synthetic fixtures.

    Exercises what a user actually does: an export sits in Downloads, they
    click one button, and the app says how many people it found.
    """
    import zipfile

    app = _load_app_main()

    home = tmp_path / "home"
    downloads = home / "Downloads"
    downloads.mkdir(parents=True)
    monkeypatch.setattr(app.Path, "home", staticmethod(lambda: home))

    export = downloads / "Complete_LinkedInDataExport_11-15-2026.zip"
    fixtures = Path(__file__).resolve().parent / "fixtures" / "linkedin"
    with zipfile.ZipFile(export, "w") as archive:
        for csv in fixtures.glob("*.csv"):
            archive.write(csv, csv.name)

    said: list[str] = []
    monkeypatch.setattr(app, "dialog", lambda message, **_k: said.append(message) or "OK")

    # Stand in for the file panel: the user picks the export it offers.
    import sys as _sys

    module = type(_sys)("ui")
    module.choose_export = lambda _start=None: export
    module.archive_suffixes = lambda *_a, **_k: (".zip",)
    monkeypatch.setitem(_sys.modules, "ui", module)

    from socialmcp.config import Config

    appdata = tmp_path / "appdata"
    config = Config(home=appdata, drop_dir=appdata / "drop-archive")
    config.ensure_dirs()
    drop = config.drop_dir / "linkedin"

    code = app.load_flow(config, drop)

    assert code == 0, f"load failed; dialogs said: {said}"
    assert any("Your network now has" in m for m in said), said
    from socialmcp.core.db import open_database

    assert open_database(config).scalar("select count(*) from contacts") > 0
    assert export.exists(), "the user's download must not be moved or deleted"


def test_load_flow_does_nothing_quietly_when_the_chooser_is_cancelled(
    monkeypatch, tmp_path
):
    """Cancelling is a choice, not a failure: no dialog, no error, no changes."""
    app = _load_app_main()

    home = tmp_path / "home"
    (home / "Downloads").mkdir(parents=True)
    monkeypatch.setattr(app.Path, "home", staticmethod(lambda: home))

    said: list[str] = []
    monkeypatch.setattr(app, "dialog", lambda message, **_k: said.append(message) or "OK")

    import sys as _sys

    module = type(_sys)("ui")
    module.choose_export = lambda _start=None: None
    module.archive_suffixes = lambda *_a, **_k: (".zip",)
    monkeypatch.setitem(_sys.modules, "ui", module)

    from socialmcp.config import Config

    appdata = tmp_path / "appdata"
    config = Config(home=appdata, drop_dir=appdata / "drop-archive")
    config.ensure_dirs()

    assert app.load_flow(config, config.drop_dir / "linkedin") == 0
    assert not said, f"cancelling should say nothing, said: {said}"
    assert not config.db_path.exists(), "nothing should have been created"



def test_the_chooser_never_starts_somewhere_that_is_not_a_folder(monkeypatch, tmp_path):
    """The starting location must be an existing directory.

    Given a file or a missing path, an open panel ignores it and restores
    whatever it showed last — usually Recents, a column view of
    `/private/var/...` temporary paths that tells the user nothing about where
    they are. The panel takes the directory as an argument, so the argument is
    what gets checked.
    """
    import importlib.util

    macos = Path(__file__).resolve().parent.parent / "packaging" / "macos"
    spec = importlib.util.spec_from_file_location("ui_start", macos / "ui.py")
    ui = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ui)

    starts: list[str] = []

    class _Cancelled:
        returncode = 0
        stdout = "__cancelled__"
        stderr = ""

    def fake_run(argv, **_kwargs):
        # osascript -l JavaScript <script> <start> <message> <types...>
        starts.append(argv[4])
        return _Cancelled()

    monkeypatch.setattr(ui.subprocess, "run", fake_run)

    a_file = tmp_path / "export.zip"
    a_file.write_text("x", encoding="utf-8")
    ui.choose_export(a_file)
    ui.choose_export(tmp_path / "does-not-exist")
    ui.choose_export(None)

    assert starts, "the panel was never asked to open anywhere"
    for start in starts:
        assert Path(start).is_dir(), f"panel would open at a non-folder: {start}"

def test_nothing_is_configured_until_the_user_has_been_asked():
    """Detection comes first; writing waits for consent.

    Editing another application's settings is not something to do on someone's
    behalf unasked, and when several AI apps are installed, which ones to
    connect is a preference rather than something detection can decide.
    """
    source = (
        Path(__file__).resolve().parent.parent / "packaging" / "macos" / "app_main.py"
    ).read_text(encoding="utf-8")

    setup = source.split("def _run_setup(")[1].split("\ndef ")[0]
    assert "detected_clients()" in setup, "_run_setup should only detect"
    assert "setup_all(" not in setup, (
        "_run_setup must not configure anything: the user has not been asked yet"
    )

    flow = source.split("def setup_flow(")[1].split("\ndef ")[0]
    squashed = " ".join(flow.split())
    asked = squashed.find("choose_clients(")
    wrote = squashed.find("setup_all(")
    assert asked != -1, "setup_flow never asks which apps to connect"
    assert wrote != -1, "setup_flow never configures anything"
    assert asked < wrote, "the question must come before the first write"


def test_one_client_is_not_turned_into_a_question():
    """A single detected app needs no checklist — there is nothing to choose."""
    source = (
        Path(__file__).resolve().parent.parent / "packaging" / "macos" / "app_main.py"
    ).read_text(encoding="utf-8")
    flow = " ".join(source.split("def setup_flow(")[1].split("\ndef ")[0].split())
    assert "len(detected) > 1" in flow, (
        "asking about one app manufactures a decision the user does not have"
    )


def test_cancelling_the_checklist_writes_nothing(monkeypatch):
    """Cancel means cancel: no config touched, no error shown."""
    app = _load_app_main()

    monkeypatch.setattr(app, "choose_clients", lambda _names: None)
    wrote: list[str] = []
    monkeypatch.setattr(
        app, "dialog", lambda message, **_k: wrote.append(message) or "OK"
    )

    # The parsing helper itself: an empty selection is distinct from a cancel.
    assert app.choose_clients is not None


def test_connect_all_is_one_tap(monkeypatch):
    """The common case must not cost a decision per app."""
    app = _load_app_main()

    asked: list[str] = []

    def fake_dialog(message, **_kwargs):
        asked.append(message)
        return "Connect all"

    monkeypatch.setattr(app, "dialog", fake_dialog)

    names = ["Claude Code", "VS Code (Copilot)", "Zed"]
    assert app._choose_clients_one_at_a_time(names) == names
    assert len(asked) == 1, "connecting everything should be a single question"
    assert "backed up first" in asked[0], "say what connecting does to their files"


def test_deciding_each_asks_once_per_app_and_respects_skips(monkeypatch):
    """Selective choice needs no modifier keys — one plain question per app.

    `choose from list` was tried first and is the wrong control: it draws
    highlightable rows rather than checkboxes, and picking more than one
    requires Command-click, which a user cannot discover.
    """
    app = _load_app_main()

    answers = iter(["Decide each", "Connect", "Skip", "Connect"])
    seen: list[str] = []

    def fake_dialog(message, **_kwargs):
        seen.append(message)
        return next(answers)

    monkeypatch.setattr(app, "dialog", fake_dialog)

    picked = app._choose_clients_one_at_a_time(
        ["Claude Code", "VS Code (Copilot)", "Zed"]
    )

    assert picked == ["Claude Code", "Zed"], "skipped apps must stay untouched"
    assert len(seen) == 4, "one overview question, then one per app"
    assert "Connect SocialMCP to VS Code (Copilot)?" in seen[2]


def test_cancelling_the_question_connects_nothing(monkeypatch):
    """Cancel is distinct from skipping everything: nothing is written."""
    app = _load_app_main()
    monkeypatch.setattr(app, "dialog", lambda *_a, **_k: "Cancel")
    assert app._choose_clients_one_at_a_time(["Claude Code", "Cursor"]) is None


def test_an_unanswerable_question_connects_what_was_found(monkeypatch):
    """If no dialog can be shown, do what the app did before asking existed.

    `dialog` returns its last button when osascript cannot run, so an
    environment with no GUI ends up at "Connect all" rather than silently
    connecting nothing and leaving the user with an app that does not work.
    """
    app = _load_app_main()
    monkeypatch.setattr(app, "dialog", lambda _m, **k: k["buttons"][-1])
    names = ["Claude Code", "Cursor"]
    assert app._choose_clients_one_at_a_time(names) == names


def _stub_osascript(monkeypatch, app, *, stdout="", returncode=0, capture=None):
    """Replace subprocess.run so no test can ever open a real dialog."""

    class _Result:
        pass

    def fake_run(argv, **_kwargs):
        if capture is not None:
            capture["argv"] = argv
            script = Path(argv[3]) if len(argv) > 3 else None
            if script and script.exists():
                capture["script"] = script.read_text(encoding="utf-8")
        result = _Result()
        result.returncode = returncode
        result.stdout = stdout
        result.stderr = ""
        return result

    monkeypatch.setattr(app.subprocess, "run", fake_run)


def test_the_consent_dialog_is_a_real_checkbox_list(monkeypatch):
    """Checkboxes, ticked by default, passing the app names as arguments.

    `choose from list` was tried first and behaves as all-or-nothing unless
    the user knows to Command-click; one dialog per app worked but asked three
    times for three apps. An NSAlert with switch buttons is the control a Mac
    application would use, and osascript's JavaScript bridge reaches it with
    nothing bundled.
    """
    app = _load_app_main()
    captured: dict[str, object] = {}
    _stub_osascript(
        monkeypatch, app, stdout="Claude Code\nZed\n", capture=captured
    )

    picked = app.choose_clients(["Claude Code", "VS Code (Copilot)", "Zed"])

    assert picked == ["Claude Code", "Zed"], "returns the ticked ones, in order"

    argv = captured["argv"]
    assert argv[:3] == ["osascript", "-l", "JavaScript"], argv
    assert argv[4:] == ["VS Code (Copilot)", "Zed"] or "Claude Code" in argv, argv

    script = captured["script"]
    assert "NSButtonTypeSwitch" in script or "setButtonType(3)" in script, (
        "the rows must be checkboxes, not a selectable list"
    )
    assert "setState(1)" in script, "ticked by default: connecting all is usual"
    assert "backed up first" in script, "say what connecting does to their files"


def test_unticking_everything_is_not_the_same_as_cancelling(monkeypatch):
    """Empty selection means "connect nothing"; cancel means "do not proceed".

    The caller tells them apart: one explains that nothing was connected, the
    other leaves silently.
    """
    app = _load_app_main()

    _stub_osascript(monkeypatch, app, stdout="__none__")
    assert app.choose_clients(["Claude Code", "Zed"]) == []

    _stub_osascript(monkeypatch, app, stdout="__cancelled__")
    assert app.choose_clients(["Claude Code", "Zed"]) is None


def test_a_broken_checkbox_dialog_degrades_to_plain_questions(monkeypatch):
    """If the scripting bridge fails, ask the old way rather than give up."""
    app = _load_app_main()
    _stub_osascript(monkeypatch, app, returncode=1)
    monkeypatch.setattr(app, "dialog", lambda _m, **k: k["buttons"][-1])

    names = ["Claude Code", "Cursor"]
    assert app.choose_clients(names) == names


def test_the_consent_script_leaves_no_temp_file_behind(monkeypatch, tmp_path):
    """The script is written to a temp file; it must not accumulate."""
    app = _load_app_main()
    seen: dict[str, object] = {}
    _stub_osascript(monkeypatch, app, stdout="Claude Code", capture=seen)

    app.choose_clients(["Claude Code"])

    script_path = Path(seen["argv"][3])
    assert not script_path.exists(), f"left behind {script_path}"


@pytest.mark.skipif(sys.platform != "darwin", reason="osascript is macOS-only")
def test_the_checkbox_script_actually_compiles_and_runs():
    """Run the real script through osascript, without drawing anything.

    This exists because of a bug a user found. `CHECKBOX_SCRIPT` was not a raw
    Python string, so `join('\\n')` in the JavaScript became a literal newline
    inside a quoted string — a SyntaxError on line 38. The caller fell back to
    asking about each app one at a time, which looked like a design choice
    rather than a failure, and only the log revealed it.

    Executing the script is the only honest check: nothing short of a
    JavaScript engine catches a syntax error, and there is one available.
    """
    import subprocess
    import tempfile

    app = _load_app_main()
    with tempfile.NamedTemporaryFile(
        "w", suffix=".js", delete=False, encoding="utf-8"
    ) as handle:
        handle.write(app.CHECKBOX_SCRIPT)
        script = handle.name
    try:
        result = subprocess.run(
            ["osascript", "-l", "JavaScript", script, "__selftest__"],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
    finally:
        Path(script).unlink(missing_ok=True)

    assert result.returncode == 0, f"the script does not run: {result.stderr.strip()}"
    assert result.stdout.strip() == "ok", result.stdout


def test_the_checkbox_script_keeps_its_escapes():
    """A non-raw string silently rewrites the JavaScript it contains."""
    source = (
        Path(__file__).resolve().parent.parent / "packaging" / "macos" / "app_main.py"
    ).read_text(encoding="utf-8")
    assert 'CHECKBOX_SCRIPT = r"""' in source, (
        "the script must be a raw string, or Python turns JavaScript escapes "
        "such as join('\\\\n') into real newlines and breaks the syntax"
    )


def test_every_dialog_brings_itself_to_the_front():
    """A dialog behind another window is indistinguishable from no dialog.

    osascript's windows belong to osascript, which is not frontmost when this
    app is launched from Finder, so the first dialog opened behind whatever the
    user was looking at. They had to find SocialMCP in the Dock and click it —
    which the person who built it knows to do, and nobody else does.
    """
    macos = Path(__file__).resolve().parent.parent / "packaging" / "macos"

    app_source = (macos / "app_main.py").read_text(encoding="utf-8")
    dialog_body = app_source.split("def dialog(")[1].split("\ndef ")[0]
    assert '"activate\\n"' in dialog_body, (
        "dialog() must activate before showing, or it opens behind other windows"
    )

    ui_source = (macos / "ui.py").read_text(encoding="utf-8")
    assert '"activate\\n"' in ui_source, (
        "the file chooser must activate too, for the same reason"
    )

    # The checkbox dialog uses the Objective-C bridge, which has its own way.
    assert "activateIgnoringOtherApps" in app_source, (
        "the checkbox alert must raise itself as well"
    )


@pytest.mark.skipif(sys.platform != "darwin", reason="osascript is macOS-only")
def test_the_alert_returns_the_button_the_user_actually_clicked():
    """NSAlert's response codes run opposite to the caller's button order.

    The first button added is drawn rightmost and reports 1000, and buttons go
    on in reverse so the caller's last stays the default. Get that arithmetic
    wrong and the app silently acts on a button nobody pressed — a Cancel
    treated as Connect. Exercised through the real script, with no window.
    """
    import subprocess
    import tempfile

    app = _load_app_main()
    with tempfile.NamedTemporaryFile(
        "w", suffix=".js", delete=False, encoding="utf-8"
    ) as handle:
        handle.write(app.ALERT_SCRIPT)
        script = handle.name
    try:
        result = subprocess.run(
            [
                "osascript", "-l", "JavaScript", script,
                "__maptest__", "Cancel", "Decide each", "Connect all",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
    finally:
        Path(script).unlink(missing_ok=True)

    assert result.returncode == 0, result.stderr
    # Response 1000 is the rightmost (the caller's last button, the default),
    # then leftwards.
    assert result.stdout.strip() == "Connect all|Decide each|Cancel", result.stdout


def test_alerts_are_lifted_above_normal_windows():
    """Activation alone loses to the app that is already frontmost.

    With a terminal in the foreground the dialog stayed behind it, so the
    window is also raised to NSModalPanelWindowLevel and made key explicitly.
    A dialog the user cannot see is the same as no dialog, and this is the
    first thing they see after double-clicking the app.
    """
    source = (
        Path(__file__).resolve().parent.parent / "packaging" / "macos" / "app_main.py"
    ).read_text(encoding="utf-8")

    for name in ("ALERT_SCRIPT", "CHECKBOX_SCRIPT"):
        script = source.split(f"{name} = r\"\"\"", 1)[1].split('"""', 1)[0]
        assert "setLevel(8)" in script, f"{name} must float above normal windows"
        assert "makeKeyAndOrderFront" in script, f"{name} must take key focus"
        assert "activateIgnoringOtherApps" in script, f"{name} must activate"


def test_the_panel_offers_only_the_sources_archive_type():
    """Offering files that cannot work is a trap, not a kindness.

    A user who can pick a PDF will pick a PDF, and the only feedback is a
    failure after the fact. The panel is told the source's uniform type
    identifiers so everything else is unselectable.
    """
    import importlib.util

    macos = Path(__file__).resolve().parent.parent / "packaging" / "macos"
    spec = importlib.util.spec_from_file_location("ui_types", macos / "ui.py")
    ui = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ui)

    entry = ui.SOURCE_ARCHIVES["linkedin"]
    assert "public.zip-archive" in entry["types"], "use the system type, not just an extension"
    assert ui.archive_suffixes() == (".zip",)

    # The mapping is per source, so a second platform is a table entry.
    assert ui.DEFAULT_SOURCE in ui.SOURCE_ARCHIVES
    assert ui.archive_suffixes("a-source-that-does-not-exist") == (".zip",), (
        "an unknown source should fall back rather than crash"
    )

    seen: dict[str, str] = {}

    class _Cancelled:
        returncode = 1
        stdout = ""
        stderr = ""

    original = ui.subprocess.run
    try:
        ui.subprocess.run = lambda argv, **_k: (
            seen.update(script=argv[-1]) or _Cancelled()
        )
        ui.choose_export(Path.home() / "Downloads")
    finally:
        ui.subprocess.run = original

    assert "of type {" in seen["script"], "the panel must filter by type"
    assert "public.zip-archive" in seen["script"]


def test_a_file_that_is_not_an_archive_is_refused_with_an_explanation(
    monkeypatch, tmp_path
):
    """The filter is a convenience; the check is the guarantee."""
    app = _load_app_main()

    home = tmp_path / "home"
    (home / "Downloads").mkdir(parents=True)
    monkeypatch.setattr(app.Path, "home", staticmethod(lambda: home))

    wrong = home / "Downloads" / "holiday.pdf"
    wrong.write_text("not an archive", encoding="utf-8")

    said: list[str] = []
    monkeypatch.setattr(app, "dialog", lambda message, **_k: said.append(message) or "OK")

    import sys as _sys

    module = type(_sys)("ui")
    module.choose_export = lambda _start=None: wrong
    module.archive_suffixes = lambda *_a, **_k: (".zip",)
    monkeypatch.setitem(_sys.modules, "ui", module)

    from socialmcp.config import Config

    appdata = tmp_path / "appdata"
    config = Config(home=appdata, drop_dir=appdata / "drop-archive")
    config.ensure_dirs()

    assert app.load_flow(config, config.drop_dir / "linkedin") == 1
    assert any("is not a LinkedIn export" in m for m in said), said
    assert not config.db_path.exists(), "nothing should have been ingested"


def test_the_real_world_export_names_are_accepted():
    """Real exports have odd names, and the suffix check must not reject them.

    LinkedIn's archive arrives as `Complete_LinkedInDataExport_<date>.zip`, and
    a browser that unpacks the outer container leaves `....zip.zip` — which is
    what the maintainer's own export is called. A check written as
    `name.endswith(".zip")` happens to handle it; one written as
    `suffixes == name.split(".")[-2:]` would not.
    """
    import importlib.util

    macos = Path(__file__).resolve().parent.parent / "packaging" / "macos"
    spec = importlib.util.spec_from_file_location("ui_names", macos / "ui.py")
    ui = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ui)

    allowed = ui.archive_suffixes()
    for name in (
        "Complete_LinkedInDataExport_11-15-2026.zip",
        "Complete_LinkedInDataExport_11-15-2026.zip.zip",
        "Basic_LinkedInDataExport_09-29-2026.zip.zip",
        "my export (1).zip",
        "EXPORT.ZIP",
    ):
        assert name.lower().endswith(allowed), f"{name} should be accepted"

    for name in ("holiday.pdf", "Connections.csv", "export.zip.part", "notes.txt"):
        assert not name.lower().endswith(allowed), f"{name} should be refused"


def test_the_panel_prompt_names_the_source_it_wants():
    """A future source must not ask for a "LinkedIn export".

    The prompt is built from the mapping's label, so adding Instagram changes
    one table entry rather than a string buried in a script.
    """
    import importlib.util

    macos = Path(__file__).resolve().parent.parent / "packaging" / "macos"
    spec = importlib.util.spec_from_file_location("ui_prompt", macos / "ui.py")
    ui = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ui)

    ui.SOURCE_ARCHIVES["example"] = {
        "label": "Example Network",
        "types": ("public.zip-archive",),
        "suffixes": (".zip",),
    }

    seen: dict[str, str] = {}

    class _Cancelled:
        returncode = 1
        stdout = ""
        stderr = ""

    original = ui.subprocess.run
    try:
        ui.subprocess.run = lambda argv, **_k: (
            seen.update(script=argv[-1]) or _Cancelled()
        )
        ui.choose_export(Path.home(), source="example")
    finally:
        ui.subprocess.run = original

    assert "Choose your Example Network export" in seen["script"], seen["script"]
    assert "LinkedIn" not in seen["script"], "the label must come from the mapping"


def test_the_open_panel_refuses_to_select_folders():
    """Clicking a folder must navigate into it, never answer the question.

    With AppleScript's `choose file`, folders were selectable: a user clicking
    a subfolder had their click taken as the answer, and the app then refused
    it with an error. That is the app blaming the user for a control that
    behaved wrongly. NSOpenPanel states the rule outright.
    """
    import importlib.util

    macos = Path(__file__).resolve().parent.parent / "packaging" / "macos"
    spec = importlib.util.spec_from_file_location("ui_panel", macos / "ui.py")
    ui = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ui)

    script = ui.PANEL_SCRIPT
    assert "setCanChooseDirectories(false)" in script, (
        "folders must open, not be selectable as the answer"
    )
    assert "setCanChooseFiles(true)" in script
    assert "setAllowedFileTypes" in script, "only the source's archives on offer"
    assert "setAllowsMultipleSelection(false)" in script, "one export at a time"
    assert "setTreatsFilePackagesAsDirectories(true)" in script, (
        "a bundle-like folder should be navigable too"
    )
    # Same raising behaviour as the alerts, for the same reason.
    assert "setLevel(8)" in script and "activateIgnoringOtherApps" in script


@pytest.mark.skipif(sys.platform != "darwin", reason="osascript is macOS-only")
def test_the_open_panel_script_compiles_and_runs():
    """Execute the real script; a syntax error is otherwise a silent fallback."""
    import importlib.util
    import subprocess
    import tempfile

    macos = Path(__file__).resolve().parent.parent / "packaging" / "macos"
    spec = importlib.util.spec_from_file_location("ui_panel_run", macos / "ui.py")
    ui = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ui)

    with tempfile.NamedTemporaryFile(
        "w", suffix=".js", delete=False, encoding="utf-8"
    ) as handle:
        handle.write(ui.PANEL_SCRIPT)
        script = handle.name
    try:
        result = subprocess.run(
            ["osascript", "-l", "JavaScript", script, "__selftest__"],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
    finally:
        Path(script).unlink(missing_ok=True)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_cancelling_the_panel_is_not_confused_with_it_being_unavailable(monkeypatch):
    """Two different outcomes: the user said no, or the mechanism is missing.

    Only the second should fall through to another implementation; treating a
    cancel as a failure would reopen a second panel the user just dismissed.
    """
    import importlib.util

    macos = Path(__file__).resolve().parent.parent / "packaging" / "macos"
    spec = importlib.util.spec_from_file_location("ui_cancel", macos / "ui.py")
    ui = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ui)

    class _Cancelled:
        returncode = 0
        stdout = "__cancelled__"
        stderr = ""

    monkeypatch.setattr(ui.subprocess, "run", lambda *_a, **_k: _Cancelled())
    assert ui.choose_export(Path.home()) is None

    class _Chosen:
        returncode = 0
        stdout = "/Users/someone/Downloads/Export.zip"
        stderr = ""

    monkeypatch.setattr(ui.subprocess, "run", lambda *_a, **_k: _Chosen())
    assert ui.choose_export(Path.home()) == Path("/Users/someone/Downloads/Export.zip")


def test_the_app_ships_its_own_licence_and_notice():
    """The terms someone relies on belong inside what they downloaded.

    The bundle carried seventeen licence files belonging to dependencies and
    none for this software. A click-through "I agree" gate was considered and
    rejected: Apache-2.0 already disclaims warranty, and a gate before first
    use is friction for the audience this app exists for. Shipping the text,
    and saying the short version once, is the honest version of the same
    thing.
    """
    macos = Path(__file__).resolve().parent.parent / "packaging" / "macos"
    spec = (macos / "socialmcp.spec").read_text(encoding="utf-8")

    datas = spec.split("datas = [", 1)[1].split("]", 1)[0]
    assert 'REPO / "LICENSE"' in datas, "the licence must be inside the bundle"
    assert 'REPO / "NOTICE"' in datas, "the notice must be inside the bundle"

    build = (macos / "build.sh").read_text(encoding="utf-8")
    assert "LICENSE.txt" in build, "the licence should sit beside the app on the dmg"

    report = (macos / "app_main.py").read_text(encoding="utf-8")
    assert "provided as-is with no" in report, (
        "say it once in the first dialog rather than behind an agreement gate"
    )
    assert "collects no analytics" in report
    assert "travel to whoever runs it" in report, (
        "the dialog must not imply contacts never reach a cloud model: the "
        "assistant sends whatever it read to answer you"
    )


def test_the_licence_command_prints_the_terms():
    """`socialmcp license` must work, and say the things that matter."""
    from socialmcp.cli import _license_text

    text = _license_text()
    for phrase in ("Apache License", "AS IS", "WITHOUT WARRANTIES"):
        assert phrase in text, f"missing {phrase!r}"
    for phrase in ("no analytics", "never logs in", "Deleting the database file"):
        assert phrase in text, f"the privacy terms should state: {phrase!r}"


def test_the_notice_file_does_not_overpromise():
    """The notice must not claim anything the software does not do."""
    notice = (
        Path(__file__).resolve().parent.parent / "NOTICE"
    ).read_text(encoding="utf-8")

    # The one outbound request it can make has to be disclosed, not hidden
    # behind "nothing leaves your computer".
    assert "127.0.0.1:11434" in notice, (
        "the embedding provider is a real outbound request and must be named"
    )
    assert "can be switched off" in notice


def test_the_terms_are_shown_before_anything_is_detected_or_written():
    """Terms after the fact are decoration; they come first or not at all."""
    source = (
        Path(__file__).resolve().parent.parent / "packaging" / "macos" / "app_main.py"
    ).read_text(encoding="utf-8")
    flow = " ".join(source.split("def setup_flow(")[1].split("\ndef ")[0].split())

    asked = flow.find("terms_flow()")
    detected = flow.find("run_with_progress(")
    wrote = flow.find("setup_all(")
    assert asked != -1, "setup_flow never shows the terms"
    assert asked < detected < wrote, (
        "the terms must precede detection, which must precede any write"
    )


def test_declining_the_terms_writes_nothing(monkeypatch, tmp_path):
    """Quit means quit: no config touched, no database, no marker."""
    app = _load_app_main()
    monkeypatch.setattr(app, "APP_SUPPORT", tmp_path / "appdata")
    # Stub the rich sheet too, or the test opens a real window.
    monkeypatch.setattr(app, "_terms_sheet", lambda **_k: "Quit")
    monkeypatch.setattr(app, "ask", lambda *_a, **_k: "Quit")

    assert app.terms_flow() is False
    assert not (tmp_path / "appdata" / "accepted-terms.json").exists(), (
        "declining must not be recorded as acceptance"
    )


def test_consent_is_recorded_locally_and_asked_only_once(monkeypatch, tmp_path):
    """Saved on this Mac, in the app's own folder, and never sent anywhere."""
    app = _load_app_main()
    appdata = tmp_path / "appdata"
    monkeypatch.setattr(app, "APP_SUPPORT", appdata)

    asked: list[str] = []

    def once(message, **_kwargs):
        asked.append(message)
        return "Agree and continue"

    monkeypatch.setattr(app, "_terms_sheet", lambda **_k: None)
    monkeypatch.setattr(app, "ask", once)

    assert app.terms_flow() is True
    marker = appdata / "accepted-terms.json"
    assert marker.exists(), "acceptance should be remembered"

    import json

    record = json.loads(marker.read_text(encoding="utf-8"))
    assert record["fingerprint"] == app.terms_fingerprint()
    # Acceptances are kept as a list, so which wording was agreed and when
    # stays answerable after the terms change.
    assert record["accepted"][-1]["fingerprint"] == app.terms_fingerprint()
    assert "accepted_at" in record["accepted"][-1]
    # Nothing resembling an identifier, because this is never transmitted.
    assert set(record) == {"fingerprint", "accepted"}, record
    assert set(record["accepted"][-1]) == {"fingerprint", "accepted_at"}

    # Second launch: no question.
    assert app.terms_flow() is True
    assert len(asked) == 1, "the terms should be shown once, not every launch"


def test_changed_terms_are_shown_again(monkeypatch, tmp_path):
    """New wording is a new agreement, not one already given."""
    app = _load_app_main()
    appdata = tmp_path / "appdata"
    monkeypatch.setattr(app, "APP_SUPPORT", appdata)
    monkeypatch.setattr(app, "_terms_sheet", lambda **_k: "Agree and continue")
    monkeypatch.setattr(app, "ask", lambda *_a, **_k: "Agree and continue")

    assert app.terms_flow() is True
    assert app.terms_accepted() is True

    monkeypatch.setattr(app, "TERMS", app.TERMS + "\n\nAnd one new condition.")
    assert app.terms_accepted() is False, (
        "a fingerprint of the wording means changed terms are asked again"
    )


def test_the_terms_say_the_things_that_matter():
    """The disclaimers have to be present in the words the user reads."""
    app = _load_app_main()
    terms = app.TERMS

    for phrase in (
        "Apache License, Version 2.0",
        'PROVIDED "AS IS"',
        "WITHOUT WARRANTY",
        "Limitation of liability",
        "no analytics or telemetry",
        "does not access any social network",
        "Deleting the database removes all data",
    ):
        assert phrase in terms, f"the terms should state: {phrase!r}"
    # The single outbound request must be disclosed rather than glossed over.
    assert "embedding service you configure" in terms
    assert "may be disabled entirely" in terms
    # And the bigger one: results reach whoever operates the assistant. Saying
    # only that nothing is uploaded reads as "my contacts never reach a cloud
    # model", which is not true of any cloud assistant.
    assert "disclosed to its operator" in terms, "the terms must not overclaim"
    assert "outside the control of this software" in terms
    # Writing into another application's settings is a consequential act and
    # has to be stated, along with the fact that it waits for a choice.
    assert "Configuration of other applications" in terms
    assert "before you make that selection" in terms
    # Acceptance is local. The terms say so, because the file exists.
    assert "not transmitted" in terms


def test_dialog_prose_is_not_hard_wrapped():
    """Let the system wrap. Our own line breaks fight it.

    NSAlert wraps text to the dialog width, so a string already broken at 72
    characters gets broken twice: every one of our breaks becomes a premature
    line ending, leaving ragged gaps mid-sentence. Lines that are deliberately
    short — a path, a quoted question, a bullet — are fine; prose is not.
    """
    app = _load_app_main()

    # Each clause of the terms is one paragraph, however long.
    for line in app.TERMS.splitlines():
        if not line.strip():
            continue
        assert len(line) > 90 or line.rstrip().endswith((":", ".")), (
            f"terms line looks hand-wrapped: {line!r}"
        )

    # The summary's prose paragraphs likewise.
    message, _buttons = app.build_setup_report([], embeddings=True)
    suspects = [
        line
        for line in message.splitlines()
        # A wrapped prose line is long-ish, has no structure, and does not end
        # a sentence — the signature of a break mid-thought.
        if 55 < len(line) < 75
        and not line.rstrip().endswith((".", ":", "?", '"'))
        and not line.startswith((" ", "/", "-", "1.", "2.", "3."))
    ]
    assert not suspects, f"these look hand-wrapped: {suspects}"


def test_the_terms_heading_is_not_repeated_in_the_body():
    """The dialog title is the heading; the body must not say it again."""
    source = (
        Path(__file__).resolve().parent.parent / "packaging" / "macos" / "app_main.py"
    ).read_text(encoding="utf-8")
    app = _load_app_main()

    assert not app.TERMS.lstrip().lower().startswith("socialmcp"), (
        "the terms body repeats the window title"
    )
    assert 'title="SocialMCP — Terms of Use"' in source, (
        "the title should carry the heading instead"
    )


def test_each_clause_heading_is_bold_and_ends_with_a_colon():
    """Headings carry the structure, so they must be visually distinct.

    Bold is not reachable through plain dialog text: NSAlert's informative
    text is a plain string, and JavaScript for Automation does not expose the
    attributed-string initialisers (tried: initWithString:,
    initWithString:attributes:, and the NSDictionary constructors are all
    absent from the bridge). NSTextView's readRTFDFromFile: is exposed, so the
    clauses are generated as RTF instead.
    """
    app = _load_app_main()
    rtf = app.terms_rtf()

    assert len(app.TERMS_CLAUSES) >= 8, "clauses were lost"
    for number, (heading, _body) in enumerate(app.TERMS_CLAUSES, 1):
        marker = rf"\b {number}. {heading}:\b0 "
        assert marker in rtf, f"clause {number} is not a bold heading: {marker!r}"

    assert rtf.startswith(r"{\rtf1"), "must be a valid RTF document"
    assert rtf.rstrip().endswith("}"), "RTF document is unterminated"


def test_the_rtf_escapes_characters_that_would_corrupt_it():
    """Braces and backslashes are RTF syntax; non-ASCII needs spelling out."""
    app = _load_app_main()

    assert app._rtf_escape("a{b}c") == r"a\{b\}c"
    assert app._rtf_escape("back\\slash") == r"back\\slash"
    # An em dash or curly quote would otherwise render as mojibake.
    assert app._rtf_escape("an — dash") == r"an \u8212? dash", (
        "non-ASCII must become a numeric RTF escape with a fallback character"
    )


def test_the_terms_sheet_shrinks_the_icon_and_scrolls():
    """A large icon pushes the terms off screen; long terms need scrolling."""
    app = _load_app_main()
    script = app.TERMS_SCRIPT

    assert "NSMakeSize(40, 40)" in script, "the default icon is too tall"
    assert "setHasVerticalScroller(true)" in script, "long terms must scroll"
    assert "readRTFDFromFile" in script, "bold headings come from RTF"
    assert "setLevel(8)" in script and "activateIgnoringOtherApps" in script


@pytest.mark.skipif(sys.platform != "darwin", reason="osascript is macOS-only")
def test_the_terms_script_and_its_rtf_both_load():
    """Generate the real RTF, hand it to the real script, without drawing."""
    import subprocess
    import tempfile

    app = _load_app_main()
    with tempfile.NamedTemporaryFile(
        "w", suffix=".js", delete=False, encoding="utf-8"
    ) as handle:
        handle.write(app.TERMS_SCRIPT)
        script = handle.name
    try:
        result = subprocess.run(
            ["osascript", "-l", "JavaScript", script, "__selftest__"],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
    finally:
        Path(script).unlink(missing_ok=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"

    # And the RTF itself parses: TextEdit's converter is what the view uses.
    with tempfile.NamedTemporaryFile(
        "w", suffix=".rtf", delete=False, encoding="ascii", errors="backslashreplace"
    ) as handle:
        handle.write(app.terms_rtf())
        rtf = handle.name
    try:
        probe = subprocess.run(
            ["textutil", "-convert", "txt", "-stdout", rtf],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
    finally:
        Path(rtf).unlink(missing_ok=True)
    assert probe.returncode == 0, probe.stderr
    for heading, _body in app.TERMS_CLAUSES:
        assert heading in probe.stdout, f"{heading!r} missing once rendered"


def test_the_terms_scrollbar_is_always_visible():
    """A hidden scrollbar hides the fact that there are more clauses.

    macOS overlay scrollers appear only once someone scrolls, so a reader
    sees the first clauses and no indication that five more follow. For terms
    specifically, that is the difference between agreeing to what you read
    and agreeing to what you happened to be shown.
    """
    app = _load_app_main()
    script = app.TERMS_SCRIPT

    assert "setScrollerStyle(0)" in script, "legacy scrollers are always drawn"
    assert "setAutohidesScrollers(false)" in script, "it must not fade away"
    assert "setHasVerticalScroller(true)" in script


def test_a_user_is_told_when_the_terms_have_changed(monkeypatch, tmp_path):
    """Being asked again without explanation looks like the app forgot."""
    app = _load_app_main()
    appdata = tmp_path / "appdata"
    monkeypatch.setattr(app, "APP_SUPPORT", appdata)

    # Nothing accepted yet: a first run, not an update.
    assert app.terms_changed_since_last_accepted() is False

    monkeypatch.setattr(app, "_terms_sheet", lambda **_k: "Agree and continue")
    assert app.terms_flow() is True
    assert app.terms_changed_since_last_accepted() is False

    # Reword the terms: now it is an update.
    monkeypatch.setattr(app, "TERMS", app.TERMS + "\n\n9. New clause: something.")
    assert app.terms_accepted() is False
    assert app.terms_changed_since_last_accepted() is True

    assert "These terms have changed" in app.terms_rtf(updated=True)
    assert "These terms have changed" not in app.terms_rtf()


def test_every_acceptance_is_kept_not_overwritten(monkeypatch, tmp_path):
    """Which wording was accepted, and when, stays answerable afterwards."""
    app = _load_app_main()
    appdata = tmp_path / "appdata"
    monkeypatch.setattr(app, "APP_SUPPORT", appdata)
    monkeypatch.setattr(app, "_terms_sheet", lambda **_k: "Agree and continue")

    assert app.terms_flow() is True
    first = app.terms_fingerprint()

    monkeypatch.setattr(app, "TERMS", app.TERMS + "\n\n9. New clause: something.")
    assert app.terms_flow() is True

    import json

    record = json.loads((appdata / "accepted-terms.json").read_text(encoding="utf-8"))
    fingerprints = [entry["fingerprint"] for entry in record["accepted"]]
    assert first in fingerprints, "the earlier acceptance must survive"
    assert app.terms_fingerprint() in fingerprints
    assert len(record["accepted"]) == 2
    # Still local, still free of identifiers.
    for entry in record["accepted"]:
        assert set(entry) == {"fingerprint", "accepted_at"}, entry


def test_the_documented_button_is_the_button_the_app_shows():
    """A user who follows the docs must find the button they were told to click.

    Three files tell someone how to remove SocialMCP — the README, the
    quick-start, and the note left inside the data folder — and all three name
    a button. A rename that updates the code and not the instructions sends a
    nervous user hunting for a word that is no longer on screen, which is the
    worst moment to look untrustworthy. So the label lives in one constant and
    the instructions are checked against it.
    """
    app = _load_app_main()
    label = app.ACTION_REMOVE
    # The ellipsis is a UI convention, not something a user types or reads in
    # prose, so documents are allowed to name the button without it.
    plain = label.rstrip("…").strip()

    assert "remove" in plain.lower(), (
        "the removal button should say what it removes, not 'Disconnect'"
    )

    # "Disconnect" is still the right word for what the first step *does*, so
    # only the instruction to press it is wrong. Look for the verb.
    stale = re.compile(r"(?:click|press|choose|select)\s+\*{0,2}Disconnect", re.I)
    for name in ("README.md", "QUICKSTART.md"):
        text = (REPO / name).read_text(encoding="utf-8")
        found = stale.search(text)
        assert not found, f"{name} still says to press Disconnect: {found.group(0)!r}"
        assert plain in text, f"{name} should name the button as {plain!r}"

    config = (REPO / "socialmcp" / "config.py").read_text(encoding="utf-8")
    explainer = config.split("def _write_folder_explainer", 1)[1].split("\n    def ", 1)[0]
    assert plain in explainer.replace('"\n                "', ""), (
        "the leftover folder's note should name the button as the app shows it"
    )

    # And the branch behind it is driven by the same constant, so the button
    # cannot be renamed into a dialog that no longer does anything.
    source = (REPO / "packaging" / "macos" / "app_main.py").read_text(encoding="utf-8")
    assert source.count("ACTION_REMOVE") >= 3, (
        "the label, the button and the branch should all be the one constant"
    )
    assert '== "Disconnect"' not in source, "a stale literal branch would never match"


def test_load_flow_says_a_file_was_already_read_instead_of_claiming_it_loaded(
    monkeypatch, tmp_path
):
    """Picking the same export twice must not be reported as a fresh import.

    The archive is matched by content hash, so "already read" is a certainty,
    not a guess — and the app used to discard that fact and say "Loaded X.
    Your network now has N people" regardless. Someone who picked last
    month's file by mistake, or who expected new people and saw an unchanged
    count, was told it worked and given nothing to act on.
    """
    import zipfile

    app = _load_app_main()

    home = tmp_path / "home"
    downloads = home / "Downloads"
    downloads.mkdir(parents=True)
    monkeypatch.setattr(app.Path, "home", staticmethod(lambda: home))

    export = downloads / "Complete_LinkedInDataExport_11-15-2026.zip"
    fixtures = Path(__file__).resolve().parent / "fixtures" / "linkedin"
    with zipfile.ZipFile(export, "w") as archive:
        for csv in fixtures.glob("*.csv"):
            archive.write(csv, csv.name)

    said: list[str] = []
    monkeypatch.setattr(app, "dialog", lambda message, **_k: said.append(message) or "OK")

    import sys as _sys

    module = type(_sys)("ui")
    module.choose_export = lambda _start=None: export
    module.archive_suffixes = lambda *_a, **_k: (".zip",)
    monkeypatch.setitem(_sys.modules, "ui", module)

    from socialmcp.config import Config
    from socialmcp.core.db import open_database

    appdata = tmp_path / "appdata"
    config = Config(home=appdata, drop_dir=appdata / "drop-archive")
    config.ensure_dirs()
    drop = config.drop_dir / "linkedin"

    assert app.load_flow(config, drop) == 0, said
    first_count = open_database(config).scalar("select count(*) from contacts")
    assert first_count > 0
    said.clear()

    # The same file, offered again — exactly what the chooser defaults to.
    assert app.load_flow(config, drop) == 0, said
    message = "\n".join(said)

    assert "already been read" in message, f"should say it was already read: {message!r}"
    assert "Nothing changed" in message, f"should say nothing changed: {message!r}"
    assert not message.startswith("Loaded "), (
        f"must not claim a fresh import: {message!r}"
    )
    assert open_database(config).scalar("select count(*) from contacts") == first_count
    assert export.exists(), "the user's download must still not be moved or deleted"


def _dmg_settings():
    """Read the dmgbuild settings file, without dmgbuild and without caching.

    Deliberately `exec` on the source text rather than importlib: a
    .pyc written next to the file is reused when source and cache share an
    mtime to the second, so editing this file and re-running the tests in
    quick succession silently tested the previous contents. That cost real
    time to spot, because the failure looks like the edit not applying.
    """
    path = REPO / "packaging" / "macos" / "dmg_settings.py"
    namespace: dict[str, object] = {"__file__": str(path), "defines": {}}
    exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), namespace)
    return types.SimpleNamespace(**namespace)


def test_the_install_window_background_matches_the_window_it_is_shown_in():
    """A background sized differently from the window is cropped or tiled.

    Both numbers are chosen by hand in two different files and nothing at
    build time complains when they disagree — the image simply sits wrong and
    the arrow stops pointing at anything.
    """
    settings = _dmg_settings()
    (_origin, (width, height)) = settings.window_rect

    script = (REPO / "packaging" / "macos" / "dmg_background.js").read_text(encoding="utf-8")
    drawn_w = int(re.search(r"const W = (\d+);", script).group(1))
    drawn_h = int(re.search(r"const H = (\d+);", script).group(1))

    assert (drawn_w, drawn_h) == (width, height), (
        f"background is {drawn_w}x{drawn_h} but the window is {width}x{height}"
    )


def test_the_arrow_points_from_the_app_to_the_applications_folder():
    """The arrow is only instruction if it runs between the two icons.

    Icon positions live in the dmgbuild settings and the arrow is drawn in a
    separate script, so this is the one check that they describe the same
    picture. Without it, moving an icon leaves an arrow pointing at nothing.
    """
    settings = _dmg_settings()
    script = (REPO / "packaging" / "macos" / "dmg_background.js").read_text(encoding="utf-8")

    app_x, app_y = settings.icon_locations[settings.application]
    apps_x, apps_y = settings.icon_locations["Applications"]
    assert app_x < apps_x, "the app should sit left of the Applications folder"
    assert app_y == apps_y, "both icons should be at the same height"

    height = settings.window_rect[1][1]
    # The script works in a bottom-left origin and the icons are placed from
    # the top, so the arrow is drawn at `H - ICON_ROW1_TOP`. Checking the
    # expression rather than a number is the point: it is what keeps the two
    # files from drifting apart.
    assert "const arrowY = H - ICON_ROW1_TOP;" in script, (
        "the arrow should be pinned to the first icon row, not a loose number"
    )
    arrow_y = _background_constants()["ICON_ROW1_TOP"]
    assert arrow_y == app_y, (
        f"arrow drawn at {arrow_y} from the top, icons at {app_y}"
    )

    xs = [int(m) for m in re.findall(r"NSMakePoint\((\d+),", script)]
    assert xs, "expected the arrow geometry to be readable"
    assert min(xs) > app_x, f"arrow starts at {min(xs)}, left of the app icon at {app_x}"
    assert max(xs) < apps_x, (
        f"arrow ends at {max(xs)}, past the Applications icon at {apps_x}"
    )
    assert height == 400 or height > 0  # geometry sanity, not a magic number


@pytest.mark.skipif(sys.platform != "darwin", reason="draws with AppKit")
def test_the_background_image_actually_renders(tmp_path):
    """It is drawn by a script, so run it rather than trusting it.

    Two silent failures were found this way: an assignment to
    `currentContext` that the bridge ignores, producing a blank image, and a
    paragraph style that pushed the text off the edge. Neither raised.
    """
    import subprocess

    out = tmp_path / "background.png"
    script = REPO / "packaging" / "macos" / "dmg_background.js"
    result = subprocess.run(
        ["osascript", "-l", "JavaScript", str(script), str(out), "9.9.9"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert out.is_file() and out.stat().st_size > 5000, "suspiciously small image"

    header = out.read_bytes()[:24]
    assert header[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    width = int.from_bytes(header[16:20], "big")
    height = int.from_bytes(header[20:24], "big")

    settings = _dmg_settings()
    (_origin, (points_w, points_h)) = settings.window_rect
    # Drawn at 2x so it is sharp on a Retina display.
    assert (width, height) == (points_w * 2, points_h * 2), f"{width}x{height}"

    # Not blank: a uniform image compresses far smaller than a drawn one.
    assert out.stat().st_size > 20000, (
        "image is probably blank — drawing silently went nowhere once before"
    )


def test_the_disk_image_is_laid_out_without_driving_finder():
    """Automating Finder would make a release build ask for permission.

    That prompt appears mid-build, applies to the whole machine rather than
    this build, and is the same category of surprise the first-run work spent
    a long time removing. dmgbuild writes the .DS_Store directly instead.
    """
    build = (REPO / "packaging" / "macos" / "build.sh").read_text(encoding="utf-8")
    assert "dmgbuild" in build, "the dmg should be laid out, not just archived"
    assert "dmg_settings.py" in build
    assert "dmg_background.js" in build

    lowered = build.lower()
    for forbidden in ('tell application "finder"', "tell app \"finder\"", "osascript -e 'tell"):
        assert forbidden not in lowered, f"build.sh drives Finder: {forbidden!r}"

    pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    assert "dmgbuild" in pyproject, "the build dependency should be declared"
    # Build-time only: it must not reach the shipped application.
    runtime = pyproject.split("dependencies = [", 1)[1].split("]", 1)[0]
    assert "dmgbuild" not in runtime, "dmgbuild is not a runtime dependency"


def _background_constants():
    """The vertical positions declared in the background script, by name."""
    script = (REPO / "packaging" / "macos" / "dmg_background.js").read_text(encoding="utf-8")
    return {
        name: int(value)
        for name, value in re.findall(r"const ([A-Z][A-Z0-9_]+) = (\d+);", script)
    }


def test_the_two_icon_rows_clear_the_instruction_text():
    """Icons must not collide with the words, which is what shipped first.

    An icon is drawn centred on its position with its filename below, so a
    row is taller than `icon_size`. At a 400-point window height the second
    row reached y=394 and overlapped the instruction — found by opening the
    image, not by any check, which is why this test exists.
    """
    settings = _dmg_settings()
    constants = _background_constants()
    height = settings.window_rect[1][1]
    half = settings.icon_size / 2
    label = 18  # a filename under the icon, at the 13pt text_size set above

    row1 = constants["ICON_ROW1_TOP"]
    row2 = constants["ICON_ROW2_TOP"]
    headline = constants["HEADLINE_TOP"]
    subtitle = constants["SUBTITLE_TOP"]

    assert (row1, row2) == (settings.ICON_ROW1_TOP, settings.ICON_ROW2_TOP), (
        "the background and the icon layout disagree about where the rows are"
    )

    row1_bottom = row1 + half + label
    assert row1_bottom < headline, (
        f"first row reaches {row1_bottom}, headline starts at {headline}"
    )

    subtitle_bottom = subtitle + 16
    row2_top = row2 - half
    assert subtitle_bottom < row2_top, (
        f"text ends at {subtitle_bottom}, second row starts at {row2_top}"
    )

    row2_bottom = row2 + half + label
    assert row2_bottom <= height, (
        f"second row reaches {row2_bottom}, past the {height}-point window"
    )

    # The version label sits in a corner, clear of the first row.
    assert constants["VERSION_TOP"] + 14 < row1 - half, "version overlaps the icons"


@pytest.mark.parametrize(
    ("provider_up", "expect", "forbid"),
    [
        (False, "match words literally", "Indexing"),
        (True, "Indexing", "literally"),
    ],
)
def test_the_import_summary_says_whether_search_can_match_meaning(
    monkeypatch, tmp_path, provider_up, expect, forbid
):
    """Told at the moment it matters: right before the first question.

    Without an index, "who works on robotics" matches the word and misses
    everyone whose title says "autonomy". Nothing looks broken, so nobody
    asks — which is exactly why the app has to volunteer it.
    """
    import zipfile

    app = _load_app_main()

    home = tmp_path / "home"
    downloads = home / "Downloads"
    downloads.mkdir(parents=True)
    monkeypatch.setattr(app.Path, "home", staticmethod(lambda: home))

    export = downloads / "Complete_LinkedInDataExport_11-15-2026.zip"
    fixtures = Path(__file__).resolve().parent / "fixtures" / "linkedin"
    with zipfile.ZipFile(export, "w") as archive:
        for csv in fixtures.glob("*.csv"):
            archive.write(csv, csv.name)

    said: list[str] = []
    monkeypatch.setattr(app, "dialog", lambda message, **_k: said.append(message) or "OK")
    monkeypatch.setattr(app, "embedding_available", lambda _config: provider_up)

    import sys as _sys

    module = type(_sys)("ui")
    module.choose_export = lambda _start=None: export
    module.archive_suffixes = lambda *_a, **_k: (".zip",)
    monkeypatch.setitem(_sys.modules, "ui", module)

    from socialmcp.config import Config

    appdata = tmp_path / "appdata"
    config = Config(home=appdata, drop_dir=appdata / "drop-archive")
    config.ensure_dirs()

    assert app.load_flow(config, config.drop_dir / "linkedin") == 0, said
    message = "\n".join(said)

    assert expect in message, f"expected {expect!r} in: {message!r}"
    assert forbid not in message, f"did not expect {forbid!r} in: {message!r}"
    # Either way, never a bare instruction to run something by hand.
    assert "reindex" not in message.lower(), (
        "indexing is automatic; the user should not be told to trigger it"
    )
