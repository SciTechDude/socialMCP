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
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
MACOS = REPO / "packaging" / "macos"


def test_packaging_files_are_present():
    for name in ("app_main.py", "socialmcp.spec", "build.sh", "entitlements.plist", "README.md"):
        assert (MACOS / name).is_file(), f"packaging/macos/{name} is missing"
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
    assert "rm -rf build dist" in script, "a stale PyInstaller analysis gets silently reused"
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
