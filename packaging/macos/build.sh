#!/usr/bin/env bash
# Build SocialMCP.app and SocialMCP.dmg for macOS.
#
#   ./packaging/macos/build.sh              # unsigned, for local testing
#   ./packaging/macos/build.sh --sign       # signed + notarized (needs credentials)
#
# Unsigned builds run fine on the machine that made them. On any other Mac,
# Gatekeeper will refuse to open them until they are signed and notarized —
# that refusal is the real first-run experience for your users, so test the
# unsigned build on a second machine deliberately, then sign.
#
# Signing needs, in the environment:
#   SOCIALMCP_SIGN_IDENTITY  "Developer ID Application: Your Name (TEAMID)"
#   SOCIALMCP_NOTARY_PROFILE  a profile stored via:
#       xcrun notarytool store-credentials <profile> \
#         --apple-id <email> --team-id <TEAMID> --password <app-specific-password>

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO"

SIGN=0
TARGET_ARCH="$(uname -m)"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --sign) SIGN=1 ;;
        --arch) TARGET_ARCH="${2:?--arch needs arm64 or x86_64}"; shift ;;
        --arch=*) TARGET_ARCH="${1#*=}" ;;
        -h|--help)
            sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
    shift
done

case "$TARGET_ARCH" in
    arm64|x86_64) ;;
    *) echo "error: --arch must be arm64 or x86_64, not $TARGET_ARCH" >&2; exit 2 ;;
esac

# Cross-building needs an interpreter of the target architecture; PyInstaller
# bundles the interpreter it is running under and cannot retarget. Fail with
# the actual remedy rather than quietly producing the wrong architecture.
HOST_ARCH="$(uname -m)"
ARCH_PREFIX=()
if [[ "$TARGET_ARCH" != "$HOST_ARCH" ]]; then
    if ! arch -"$TARGET_ARCH" /usr/bin/true 2>/dev/null; then
        cat >&2 <<EOF
error: cannot run $TARGET_ARCH binaries on this $HOST_ARCH Mac.

  Install Rosetta 2:  softwareupdate --install-rosetta

EOF
        exit 1
    fi
    CROSS_PYTHON="${SOCIALMCP_CROSS_PYTHON:-}"
    if [[ -z "$CROSS_PYTHON" ]]; then
        for candidate in \
            /usr/local/bin/python3.13 /usr/local/bin/python3.12 /usr/local/bin/python3.11 \
            /Library/Frameworks/Python.framework/Versions/3.13/bin/python3 \
            /Library/Frameworks/Python.framework/Versions/3.12/bin/python3 \
            /Library/Frameworks/Python.framework/Versions/3.11/bin/python3; do
            [[ -x "$candidate" ]] || continue
            if arch -"$TARGET_ARCH" "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)' 2>/dev/null; then
                CROSS_PYTHON="$candidate"
                break
            fi
        done
    fi
    if [[ -z "$CROSS_PYTHON" ]]; then
        cat >&2 <<EOF
error: no $TARGET_ARCH Python 3.11+ found on this Mac, so a $TARGET_ARCH build
       is not possible here. PyInstaller bundles the interpreter it runs under
       and cannot retarget one.

  Two ways forward:

  1. Build on a Mac of that architecture, natively.

  2. Install a universal2 Python from python.org (3.11 or newer). It lands in
     /Library/Frameworks and can run as $TARGET_ARCH under Rosetta, then:
         SOCIALMCP_CROSS_PYTHON=/usr/local/bin/python3.12 \\
           ./packaging/macos/build.sh --arch $TARGET_ARCH

  Note that the system /usr/bin/python3 does not work: it is 3.9 and is an
  xcrun stub that cannot run under Rosetta.

  GitHub-hosted macOS runners are not an option: this repository is never
  allocated one, so the jobs queue for 24 hours and are cancelled without
  running a step. Releases are built and signed on a maintainer's Mac, which
  is also where the signing certificate lives.
EOF
        exit 1
    fi
    echo "==> Cross-building $TARGET_ARCH using $CROSS_PYTHON under Rosetta"
    ARCH_PREFIX=(arch -"$TARGET_ARCH")
fi

APP="dist/$TARGET_ARCH/SocialMCP.app"
VERSION="$(python3 -c 'import tomllib,pathlib;print(tomllib.loads(pathlib.Path("pyproject.toml").read_text())["project"]["version"])')"
# The filename carries the architecture because one bundle cannot serve both:
# universal2 requires every bundled binary to be fat, and some dependencies
# publish single-arch wheels only.
#
# It carries the version so that uploading a new release cannot overwrite an
# older asset — different names, nothing to clobber. Replacing a published
# asset resets its download count and breaks the checksum in its release
# notes, which for software that reads private contact data looks exactly like
# tampering. It also means a user with two downloads can tell them apart, and
# a bug report that names the file names its version.
DMG="dist/SocialMCP-${VERSION}-${TARGET_ARCH}.dmg"

echo "==> Building SocialMCP $VERSION"

# Build in a dedicated virtualenv rather than whatever the developer's shell
# happens to have. Two reasons: the bundle then contains exactly the declared
# dependencies and nothing else from a dev environment, and a stray package
# (an obsolete `typing` backport, for instance) cannot break or pollute the
# build. This is also what CI does, so local and CI builds match.
BUILD_VENV="${SOCIALMCP_BUILD_VENV:-.venv-build-$TARGET_ARCH}"
if [[ ! -x "$BUILD_VENV/bin/python" ]]; then
    echo "==> Creating build environment in $BUILD_VENV"
    ${ARCH_PREFIX[@]+"${ARCH_PREFIX[@]}"} "${CROSS_PYTHON:-python3}" -m venv "$BUILD_VENV"
fi
PY_BIN="$BUILD_VENV/bin/python"
${ARCH_PREFIX[@]+"${ARCH_PREFIX[@]}"} "$PY_BIN" -m pip install --quiet --upgrade pip
${ARCH_PREFIX[@]+"${ARCH_PREFIX[@]}"} "$PY_BIN" -m pip install --quiet ".[package]"

# A stale build directory is the most common cause of "it works here but not
# there": PyInstaller happily reuses an old analysis. Only this architecture's
# output is cleared, so building both in turn does not discard the first.
rm -rf "build/$TARGET_ARCH" "dist/$TARGET_ARCH"

${ARCH_PREFIX[@]+"${ARCH_PREFIX[@]}"} "$PY_BIN" -m PyInstaller --noconfirm --clean \
    --distpath "dist/$TARGET_ARCH" --workpath "build/$TARGET_ARCH" \
    packaging/macos/socialmcp.spec

[[ -d "$APP" ]] || { echo "error: $APP was not produced" >&2; exit 1; }

echo "==> Verifying the bundle is self-contained"
# The whole point is that the target Mac has no Python. Run the bundled binary
# with an emptied environment so a stray PYTHONPATH or a dev virtualenv cannot
# make a broken bundle look healthy.
BIN="$APP/Contents/MacOS/SocialMCP"
env -i HOME="$HOME" ${ARCH_PREFIX[@]+"${ARCH_PREFIX[@]}"} "$BIN" --log-level ERROR doctor \
    >/tmp/socialmcp-bundle-check.txt 2>&1 || true
if ! grep -q "SocialMCP check-up" /tmp/socialmcp-bundle-check.txt; then
    echo "error: the bundled binary could not run. Output:" >&2
    cat /tmp/socialmcp-bundle-check.txt >&2
    exit 1
fi
grep -E "Python version|Keyword search|Smart search" /tmp/socialmcp-bundle-check.txt | sed 's/^/    /'

if [[ "$SIGN" == "1" ]]; then
    : "${SOCIALMCP_SIGN_IDENTITY:?set SOCIALMCP_SIGN_IDENTITY}"
    echo "==> Signing with $SOCIALMCP_SIGN_IDENTITY"
    # --deep is deprecated and unreliable; sign nested code first, then the app.
    find "$APP/Contents" -type f \( -name "*.dylib" -o -name "*.so" \) -print0 |
        xargs -0 -I{} codesign --force --timestamp --options runtime \
            --sign "$SOCIALMCP_SIGN_IDENTITY" {}
    codesign --force --timestamp --options runtime \
        --entitlements packaging/macos/entitlements.plist \
        --sign "$SOCIALMCP_SIGN_IDENTITY" "$APP"
    codesign --verify --deep --strict --verbose=2 "$APP"
fi

echo "==> Building $DMG"
rm -f "$DMG"
STAGE="$(mktemp -d)"
cp "$REPO/LICENSE" "$STAGE/LICENSE.txt"
cp "$REPO/NOTICE" "$STAGE/README-AND-PRIVACY.txt"

# The window the user actually sees. Dragging an app to Applications is a file
# copy, so macOS reports no progress and announces nothing when it finishes —
# a bare file listing therefore reads as "the download did nothing". The
# background carries the instruction and the arrow; dmg_settings.py places the
# icons at the two ends of that arrow.
echo "==> Drawing the install window background"
osascript -l JavaScript packaging/macos/dmg_background.js \
    "$STAGE/background.png" "$VERSION" >/dev/null

${ARCH_PREFIX[@]+"${ARCH_PREFIX[@]}"} "$PY_BIN" -m dmgbuild \
    -s packaging/macos/dmg_settings.py \
    -D app="$REPO/$APP" \
    -D licence="$STAGE/LICENSE.txt" \
    -D readme="$STAGE/README-AND-PRIVACY.txt" \
    -D background="$STAGE/background.png" \
    -D version="$VERSION" \
    "SocialMCP $VERSION" "$DMG"
rm -rf "$STAGE"
[[ -f "$DMG" ]] || { echo "error: $DMG was not produced" >&2; exit 1; }

if [[ "$SIGN" == "1" ]]; then
    : "${SOCIALMCP_NOTARY_PROFILE:?set SOCIALMCP_NOTARY_PROFILE}"
    codesign --force --timestamp --sign "$SOCIALMCP_SIGN_IDENTITY" "$DMG"
    echo "==> Notarizing (a few minutes)"
    # Submit, then poll, rather than `--wait`. notarytool 1.1.3 (the version
    # shipped with Command Line Tools) crashes with SIGBUS inside --wait after
    # the upload has already succeeded, which aborts the build and leaves a
    # perfectly good submission orphaned in Apple's queue. Submitting and
    # polling separately also means a crashed poll can be retried without
    # re-uploading 24 MB.
    SUBMIT_LOG="$(mktemp)"
    # `|| true` because notarytool 1.1.3 also crashes with SIGBUS under
    # --no-wait, *after* printing the submission id — the upload has already
    # succeeded by then. Letting `set -e` kill the build there is the worst
    # outcome available: it looks like a failure, so the natural response is to
    # rebuild and submit a second copy of a file Apple is already notarizing.
    #
    # So the exit status is deliberately ignored and the submission id is used
    # as the success signal instead. No id means nothing reached Apple.
    xcrun notarytool submit "$DMG" \
        --keychain-profile "$SOCIALMCP_NOTARY_PROFILE" --no-wait \
        2>&1 | tee "$SUBMIT_LOG" || true
    SUBMISSION_ID="$(awk '/^ *id: /{print $2; exit}' "$SUBMIT_LOG")"
    rm -f "$SUBMIT_LOG"
    [[ -n "$SUBMISSION_ID" ]] || { echo "error: no submission id returned" >&2; exit 1; }

    echo "==> Waiting for Apple to finish (submission $SUBMISSION_ID)"
    # Exponential backoff, capped at half an hour, giving up after three.
    #
    # Both numbers come from observed behaviour, not taste. Submissions here
    # have taken over two hours, so the previous one-hour ceiling declared a
    # perfectly healthy submission dead and sent the build down the "do NOT
    # rebuild" path for no reason. And the notary service is rate limited per
    # developer account: polling a two-hour submission every five minutes is
    # two dozen pointless requests that can earn throttling, which makes the
    # wait longer still. Nothing is gained by asking sooner — the answer
    # arrives when Apple is finished either way.
    NOTARY_STATUS="In Progress"
    NOTARY_DELAY=60
    NOTARY_WAITED=0
    while (( NOTARY_WAITED < 10800 )); do       # give up after three hours
        sleep "$NOTARY_DELAY"
        NOTARY_WAITED=$(( NOTARY_WAITED + NOTARY_DELAY ))
        # `|| true` for the same reason as the submit above: a crashed or
        # throttled poll must not end the build, because the submission it is
        # polling is still perfectly good. An empty status is read as "not
        # finished yet" and simply polled again.
        NOTARY_STATUS="$(xcrun notarytool info "$SUBMISSION_ID" \
            --keychain-profile "$SOCIALMCP_NOTARY_PROFILE" 2>/dev/null |
            awk '/^ *status: /{sub(/^ *status: /, ""); print; exit}' || true)"
        [[ "$NOTARY_STATUS" == "In Progress" || -z "$NOTARY_STATUS" ]] || break
        printf '.'
        NOTARY_DELAY=$(( NOTARY_DELAY * 2 > 1800 ? 1800 : NOTARY_DELAY * 2 ))
    done
    echo
    if [[ "$NOTARY_STATUS" == "In Progress" ]]; then
        cat >&2 <<EOF
error: still queued at Apple after three hours. The upload succeeded, so do
       NOT rebuild yet — that submits a second copy of the same file and
       consumes account capacity for nothing. Ask Apple first:

  xcrun notarytool info $SUBMISSION_ID --keychain-profile "\$SOCIALMCP_NOTARY_PROFILE"

       Then read the answer carefully, because it decides what to do next:

  "status: Accepted"        -> xcrun stapler staple $DMG
  "status: In Progress"     -> still working; wait and ask again
  "Submission does not
   exist or does not
   belong to your team"     -> Apple purged it. Observed more than once on
                              this account: a submission that stalls for
                              hours is dropped rather than finished, with no
                              log and no rejection reason. Rebuilding IS the
                              right move now — but bump the version first,
                              so the new binary cannot be confused with the
                              one this dmg already is.
EOF
        exit 1
    fi
    if [[ "$NOTARY_STATUS" != "Accepted" ]]; then
        echo "error: notarization ended as '${NOTARY_STATUS:-unknown}'" >&2
        echo "  xcrun notarytool log $SUBMISSION_ID --keychain-profile $SOCIALMCP_NOTARY_PROFILE" >&2
        exit 1
    fi
    echo "==> Accepted"

    # Stapling lets the dmg verify offline; without it the first open needs network.
    xcrun stapler staple "$DMG"
    spctl --assess --type open --context context:primary-signature -v "$DMG"
fi

SIZE="$(du -h "$DMG" | cut -f1)"
echo
ACTUAL_ARCH="$(lipo -archs "$BIN" 2>/dev/null | tr -d '\n' || echo unknown)"
echo "==> Done: $DMG ($SIZE)"
echo "    architecture: $ACTUAL_ARCH"
if [[ "$ACTUAL_ARCH" != *"$TARGET_ARCH"* ]]; then
    echo "    WARNING: expected $TARGET_ARCH. Do not publish this file." >&2
    exit 1
fi
echo "    This file runs only on $TARGET_ARCH Macs. Intel users install from"
echo "    the command line instead; see packaging/macos/README.md."
if [[ "$SIGN" == "0" ]]; then
    cat <<'NOTE'

This build is UNSIGNED. On this Mac it will open normally. On any other Mac,
macOS will refuse it with "cannot be opened because the developer cannot be
verified" — which is exactly what a user would see, so it is worth seeing once.

To open it anyway while testing: System Settings -> Privacy & Security ->
Open Anyway. (Control-click -> Open stopped working in macOS 15.) Or, for a
build you made yourself:
    xattr -dr com.apple.quarantine /Applications/SocialMCP.app

Ship signed builds only. See packaging/macos/README.md.
NOTE
fi
