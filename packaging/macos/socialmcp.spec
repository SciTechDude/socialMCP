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
    # The licence and notice travel with the binary. The bundle already carried
    # seventeen licence files belonging to dependencies and none for this
    # software, which is the wrong way round: the terms someone is actually
    # relying on should be inside the thing they downloaded. `socialmcp
    # --license` prints them, and they sit beside the app on the disk image.
    (str(REPO / "LICENSE"), "."),
    (str(REPO / "NOTICE"), "."),
]

# sqlite-vec is a loadable SQLite extension shipped as vec0.dylib. Without this
# the app still runs, but semantic search silently degrades to keyword search.
binaries = collect_dynamic_libs("sqlite_vec")

hiddenimports = [
    # The first-launch progress window. Imported by name from app_main, which
    # PyInstaller's analysis of a sibling module does not always follow.
    # app_main imports its siblings by bare name, as PyInstaller lays them out
    # in the bundle. Declared rather than left to import analysis: one of these
    # missing is an ImportError in a user's hands, not a build failure.
    "progress",
    "ui",
    "applog",
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
        # tkinter is excluded again, and deliberately. A Tk progress window
        # made macOS prompt for Downloads, Desktop and Documents the moment it
        # initialised — before any file picker existed — and the osascript-only
        # build never did. Three permission prompts is an absurd price for a
        # progress bar in an app whose claim is that it reads one file you hand
        # it. Tk also drew blank windows without a full mainloop, which does not
        # fit a step-by-step flow. See packaging/macos/progress.py.
        "tkinter",
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
        # One declaration, for the one folder the app reads: Downloads.
        #
        # That is where a browser puts the LinkedIn export, and it is now the
        # only place SocialMCP looks — no file panel, no watched folder of its
        # own. macOS asks once, this is the text it shows, and refusing it
        # simply means no export is found.
        #
        # Desktop and Documents are deliberately absent: nothing reads them.
        # They only ever appeared because a file panel enumerated its sidebar,
        # and removing the panel removed the reason to ask.
        "NSDownloadsFolderUsageDescription": (
            "SocialMCP reads the LinkedIn export you downloaded. It reads that "
            "one file, and nothing is uploaded anywhere."
        ),
    },
)
