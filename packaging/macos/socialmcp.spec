# PyInstaller spec for the SocialMCP macOS app bundle.
#
# Goals, in order:
#   1. The target Mac needs no Python, no pip, no terminal.
#   2. onedir, not onefile: a onefile build unpacks itself to a temp directory
#      on every launch, which an MCP client starts repeatedly — slow, and it
#      leaves temp copies of a binary that reads private data.
#   3. Data files and the sqlite-vec native library must be collected
#      explicitly; neither is found by PyInstaller's import analysis.
#
# Build with packaging/macos/build.sh, which passes the right paths.

import tomllib
from pathlib import Path

from PyInstaller.utils.hooks import collect_dynamic_libs

REPO = Path(SPECPATH).resolve().parent.parent  # noqa: F821 - SPECPATH is injected

# Read the version rather than repeating it. A bundle whose Info.plist disagrees
# with the release it shipped in is the kind of mistake nobody notices until a
# user reports a version that does not exist.
VERSION = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"][
    "version"
]

datas = [
    # schema.sql is read at runtime by core/db.py; import analysis cannot see it.
    (str(REPO / "socialmcp" / "core" / "schema.sql"), "socialmcp/core"),
    (str(REPO / "socialmcp" / "core" / "schema_vec.sql"), "socialmcp/core"),
]

# sqlite-vec is a loadable SQLite extension shipped as vec0.dylib. Without this
# the app still runs, but semantic search silently degrades to keyword search.
binaries = collect_dynamic_libs("sqlite_vec")

hiddenimports = [
    # The first-launch progress window. Imported by name from app_main, which
    # PyInstaller's analysis of a sibling module does not always follow.
    "progress",
    "tkinter",
    "tkinter.ttk",
    # Adapters are imported by name from a registry, so nothing references them
    # statically and PyInstaller would otherwise leave them out.
    "socialmcp.adapters.linkedin",
    "socialmcp.adapters.linkedin.adapter",
    "socialmcp.adapters.linkedin.vocabulary",
    "socialmcp.adapters.linkedin.urls",
    # Optional at runtime, resolved dynamically.
    "sqlite_vec",
    "watchdog.observers",
    "watchdog.observers.fsevents",
]

analysis = Analysis(  # noqa: F821
    [str(REPO / "packaging" / "macos" / "app_main.py")],
    pathex=[str(REPO)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        # No scientific stack; excluding it keeps the bundle to a size people
        # will actually download.
        #
        # tkinter is NOT excluded, despite the size: first launch needs a
        # progress window before any slow work, because a double-click that
        # shows nothing reads as broken and a macOS permission prompt from an
        # app that has not introduced itself reads as suspicious. See
        # packaging/macos/progress.py.
        "numpy",
        "pandas",
        "matplotlib",
        "PIL",
        "pytest",
        "IPython",
    ],
    noarchive=False,
)

pyz = PYZ(analysis.pure)  # noqa: F821

executable = EXE(  # noqa: F821
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="SocialMCP",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX-packed binaries trip Gatekeeper and some AV scanners
    console=False,  # no terminal window when launched from Finder
    # Native arch, not universal2: a universal build requires every bundled
    # binary to be fat, and some dependencies (_cffi_backend, reached through
    # the MCP SDK) publish single-arch wheels only. Ship one dmg per
    # architecture instead — build.sh puts the arch in the filename.
    target_arch=None,
    codesign_identity=None,  # signing happens in build.sh, after bundling
    entitlements_file=None,
)

collection = COLLECT(  # noqa: F821
    executable,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    name="SocialMCP",
)

app = BUNDLE(  # noqa: F821
    collection,
    name="SocialMCP.app",
    icon=None,
    bundle_identifier="dev.scitechdude.socialmcp",
    version=VERSION,
    info_plist={
        "CFBundleName": "SocialMCP",
        "CFBundleDisplayName": "SocialMCP",
        "CFBundleShortVersionString": VERSION,
        "NSHumanReadableCopyright": "Apache-2.0",
        # Not a background app: it shows a dialog and quits.
        "LSBackgroundOnly": False,
        "LSMinimumSystemVersion": "11.0",
        # The app reads a .zip the user put in their own folder. Declaring this
        # keeps the Finder prompt comprehensible if the folder is in Downloads
        # or Documents, which macOS protects.
        "NSDesktopFolderUsageDescription": (
            "SocialMCP reads the data export you place in its folder."
        ),
        "NSDocumentsFolderUsageDescription": (
            "SocialMCP reads the data export you place in its folder."
        ),
        "NSDownloadsFolderUsageDescription": (
            "SocialMCP reads the data export you downloaded from LinkedIn."
        ),
    },
)
