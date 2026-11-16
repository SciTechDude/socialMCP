# Building the macOS app

The `.dmg` is how non-technical users get SocialMCP: one download, drag to
Applications, double-click. No Python, no terminal, no `pip`. Developers use
`pip install` and never need this.

## Build

```bash
./packaging/macos/build.sh            # unsigned, for local testing
./packaging/macos/build.sh --sign     # signed + notarized, for release
```

Output: `dist/SocialMCP-<arch>.dmg` (~21 MB).

The script creates its own `.venv-build` and installs the declared dependencies
there. That is deliberate — building from whatever a developer's shell happens
to contain is how a bundle ends up either missing a dependency or carrying a
dev-only one. It also means `.venv-build` can be deleted at any time.

Build artifacts are gitignored. Installers are published as **GitHub Releases**,
never committed: binaries bloat history permanently, and a release gives you
version tags, changelogs and download counts. The build scripts *are* committed
so anyone can reproduce the installer from source and check it matches — which
matters for a program that reads someone's private contact data.

## One architecture per build

The bundle is built for the host architecture, not `universal2`, because a
universal build requires every bundled binary to be fat and some dependencies
(`_cffi_backend`, reached via the MCP SDK) publish single-arch wheels only.

**An `arm64` build will not run on an Intel Mac, and vice versa.** For a release,
build on both and attach both files, or state clearly which to download.
`uname -m` tells you which machine you are on.

## How the app is put together

One binary, two modes:

| Invocation | Behaviour |
|---|---|
| Double-clicked in Finder | the setup flow: finds installed AI apps, writes their config, opens the drop folder |
| `SocialMCP serve` | the MCP server over stdio — this is what a client runs |
| `SocialMCP doctor`, `ingest`, … | the normal CLI, usable from a terminal with no Python installed |

Keeping both in one executable means the path written into a client's config
(`/Applications/SocialMCP.app/Contents/MacOS/SocialMCP serve`) stays valid with
nothing to install separately. Dialogs come from `osascript`, so no UI toolkit
is bundled.

User data lives in `~/Library/Application Support/SocialMCP`, never inside the
bundle, so replacing or deleting the app never touches someone's CRM.

## Testing on another Mac

This is worth doing properly — the two most serious bugs in this project were
both found by running it somewhere other than the development machine.

1. Copy the `.dmg` across, drag the app to Applications, double-click it.
2. **Unsigned builds will be refused.** That is exactly what a user would see.
   To continue testing, use **System Settings → Privacy & Security → Open
   Anyway**. Apple removed the Control-click → Open shortcut in macOS 15, so
   instructions that rely on it are wrong on current systems. From a terminal,
   `xattr -dr com.apple.quarantine /Applications/SocialMCP.app` also works, but
   do not put that in user-facing documentation — teaching people to strip
   quarantine flags is worse than the problem it solves.
3. Check the setup dialog names the AI apps actually installed on that machine.
4. Drop a LinkedIn export in the folder it opens, then restart the AI app and
   ask it something.
5. Confirm it works on a Mac that has **never had Python installed** — that is
   the whole claim of the bundle.

A quick check without the GUI:

```bash
env -i HOME="$HOME" /Applications/SocialMCP.app/Contents/MacOS/SocialMCP doctor
```

`env -i` strips the environment, so a bundle that only works because of your
shell's `PATH` or `PYTHONPATH` fails here rather than on a user's machine.

## Signing and notarization

Unsigned builds are fine locally and unusable for distribution. You need an
Apple Developer account ($99/year).

One-time setup:

```bash
# After creating a "Developer ID Application" certificate in Xcode or the
# developer portal, store notary credentials in the keychain:
xcrun notarytool store-credentials socialmcp-notary \
  --apple-id you@example.com --team-id TEAMID \
  --password <app-specific-password>     # appleid.apple.com, not your login
```

Then for each release:

```bash
export SOCIALMCP_SIGN_IDENTITY="Developer ID Application: Your Name (TEAMID)"
export SOCIALMCP_NOTARY_PROFILE="socialmcp-notary"
./packaging/macos/build.sh --sign
```

The script signs nested libraries first and the app last (`--deep` is
deprecated and unreliable), notarizes the `.dmg`, then staples the ticket so it
verifies without network access on first open.

`entitlements.plist` grants three things, each required:

- `allow-unsigned-executable-memory` / `allow-jit` — the PyInstaller bootloader
  maps its own code pages, which the hardened runtime otherwise blocks
- `disable-library-validation` — to load `vec0.dylib`, signed by sqlite-vec
  rather than by us
- `network.client` — outbound only, and only to a local embedding model if the
  user runs one. There is no listening socket; the MCP transport is stdio.

## Releasing

```bash
git tag -a v0.1.0 -m "SocialMCP 0.1.0" && git push origin v0.1.0
gh release create v0.1.0 dist/SocialMCP-arm64.dmg dist/SocialMCP-x86_64.dmg \
  --title "SocialMCP 0.1.0" --notes-file CHANGELOG-0.1.0.md
```

Mention in the release notes which file is for Apple Silicon and which for
Intel. Most people do not know which Mac they have.
