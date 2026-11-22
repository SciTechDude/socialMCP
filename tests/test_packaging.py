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
import sys
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
    """Import packaging/macos/app_main.py, which is outside the package."""
    import importlib.util

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
        [_Result("Claude Desktop", "skipped")], Path("/tmp/drop") if sys.platform != "win32" else Path("C:/drop"), embeddings=True
    )

    assert "Already set up by another copy" in message
    assert "Claude Desktop" in message
    assert "command line" in message, "the likely cause should be named"
    assert "Replace it" in buttons
    assert "Connected to:" not in message


def test_setup_report_lists_connected_clients_and_restarts():
    app = _load_app_main()
    message, buttons = app.build_setup_report(
        [_Result("Cursor", "configured"), _Result("Claude Code", "already")],
        Path("/tmp/drop") if sys.platform != "win32" else Path("C:/drop"),
        embeddings=True,
    )

    assert "Connected to: Cursor, Claude Code" in message
    # Only newly written configs need a restart; an existing one does not.
    assert "Restart needed for: Cursor" in message
    assert "Replace it" not in buttons
    # The default button must be the thing the user has to do next, not a step
    # towards it. "Open the folder" only reveals a folder they must then drag a
    # file into from another window; picking the export is the actual outcome.
    assert buttons[-1] == app.ACTION_IMPORT
    assert app.ACTION_FOLDER in buttons, "dragging into the folder stays available"


def test_setup_report_surfaces_a_failure_with_its_reason():
    app = _load_app_main()
    message, _ = app.build_setup_report(
        [_Result("Zed", "failed", "its config file is not valid JSON")],
        Path("/tmp/drop") if sys.platform != "win32" else Path("C:/drop"),
        embeddings=True,
    )
    assert "Could not configure: Zed" in message
    assert "not valid JSON" in message


def test_setup_report_offers_ollama_only_when_missing():
    """Literal-only search is not an error, but the user must know about it."""
    app = _load_app_main()

    without, buttons_without = app.build_setup_report(
        [_Result("Cursor", "configured")], Path("/tmp/drop") if sys.platform != "win32" else Path("C:/drop"), embeddings=False
    )
    assert "match words literally" in without
    assert "nothing uploaded" in without, "the privacy point is the reason to accept"
    assert "Get Ollama" in buttons_without

    with_model, buttons_with = app.build_setup_report(
        [_Result("Cursor", "configured")], Path("/tmp/drop") if sys.platform != "win32" else Path("C:/drop"), embeddings=True
    )
    assert "Ollama" not in with_model
    assert "Get Ollama" not in buttons_with


def test_setup_report_always_tells_the_user_where_the_folder_is():
    app = _load_app_main()
    for embeddings in (True, False):
        for status in ("configured", "already", "skipped", "failed"):
            message, buttons = app.build_setup_report(
                [_Result("Cursor", status)], Path("/tmp/the-drop-folder") if sys.platform != "win32" else Path("C:/the-drop-folder"), embeddings=embeddings
            )
            assert str(Path("/tmp/the-drop-folder") if sys.platform != "win32" else Path("C:/the-drop-folder")) in message, status
            assert buttons, status


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

    assert "run_with_progress(_run_setup" in flow, (
        "setup_flow must run setup inside the progress window"
    )
    # The slow calls belong in _run_setup, which the window drives. Split on
    # the call site, not the import — the import appears first and slicing
    # there would make this test unable to fail.
    body_before_window = flow.split("run_with_progress(_run_setup")[0]
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


def test_bundle_requests_no_folder_permissions():
    """The app must ask for no file access at all.

    It opens a file picker, which macOS runs out of process: the app receives
    the one chosen file and nothing else. Declaring a folder usage description
    makes macOS prompt for access to an entire folder — a frightening ask for
    software that reads your contacts, and one the code never needed.
    """
    spec = (
        Path(__file__).resolve().parent.parent / "packaging" / "macos" / "socialmcp.spec"
    ).read_text(encoding="utf-8")
    for key in (
        '"NSDesktopFolderUsageDescription":',
        '"NSDocumentsFolderUsageDescription":',
        '"NSDownloadsFolderUsageDescription":',
        '"NSFileProviderDomainUsageDescription":',
    ):
        assert key not in spec, (
            f"{key} makes macOS prompt for whole-folder access; the file picker "
            "needs no permission, so add this back only with a real reason"
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

    guard = flow.find("running_from_removable_location()")
    work = flow.find("run_with_progress(_run_setup")
    assert guard != -1, "setup_flow no longer checks where it is running from"
    assert 0 < guard < work, "the location check must precede any setup work"
