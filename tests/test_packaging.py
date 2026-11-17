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
    assert buttons[-1] == "Open the folder"


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
