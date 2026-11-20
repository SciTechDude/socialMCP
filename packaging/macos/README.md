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
Apple Developer account ($99/year), which covers unlimited apps and
notarizations — there is no per-app or per-build fee.

One-time setup, in order. Each step has a way to go wrong that produces a
confusing error later, so they are spelled out:

1. **Create a `Developer ID Application` certificate.** Not "Developer ID
   Installer" (that signs `.pkg` files only), and not any "Apple
   Development"/"Distribution" type (those are for the App Store). Without
   full Xcode there is no "Manage Certificates" button, so generate a CSR
   yourself and upload it to the developer portal:

   ```bash
   openssl req -new -newkey rsa:2048 -nodes \
     -keyout developerID-app.key -out developerID-app.csr \
     -subj "/emailAddress=you@example.com/CN=Your Name/C=US"
   ```

   Apple issues **one certificate per CSR** — a second certificate needs a
   second CSR and its own key. Keep each key beside its certificate; a
   certificate without its private key cannot sign anything.

2. **Import the certificate and key** into the login keychain. `security`
   cannot read a PKCS#8 PEM key, so bundle them as a PKCS#12 first, and note
   that OpenSSL 3 defaults to a cipher Keychain rejects — `-legacy` is
   required:

   ```bash
   openssl pkcs12 -export -legacy -inkey developerID-app.key \
     -in developerID_application.cer -out bundle.p12 -passout pass:"$PASS"
   security import bundle.p12 -k ~/Library/Keychains/login.keychain-db \
     -P "$PASS" -T /usr/bin/codesign
   rm bundle.p12
   ```

3. **Import Apple's G2 intermediate CA** if `security find-identity -v -p
   codesigning` reports `0 valid identities found` while
   `security find-identity -p codesigning` lists yours. That combination means
   the certificate is present but macOS cannot build its chain, which happens
   on a Mac with Command Line Tools and no Xcode. The CA URL is inside your own
   certificate, so read it rather than guessing:

   ```bash
   openssl x509 -inform DER -in developerID_application.cer -noout -text |
     grep -A2 "Authority Information Access"     # → http://certs.apple.com/devidg2.der
   curl -fsSLO http://certs.apple.com/devidg2.der
   security import devidg2.der -k ~/Library/Keychains/login.keychain-db
   ```

4. **Store notary credentials.** The password must be an *app-specific*
   password from appleid.apple.com, never the Apple ID password — the real one
   returns `HTTP 401 Invalid credentials`. Omitting `--password` makes it
   prompt, which keeps the secret out of shell history. Note that changing your
   Apple ID password revokes every app-specific password you have generated.

   ```bash
   xcrun notarytool store-credentials socialmcp-notary \
     --apple-id you@example.com --team-id TEAMID
   ```

Keep the signing values in a file **outside this repository** so no `git add`
can reach them, and source it per release:

```bash
# ~/.socialmcp-signing/signing.env  (chmod 600, dir chmod 700)
export SOCIALMCP_SIGN_IDENTITY="Developer ID Application: Your Name (TEAMID)"
export SOCIALMCP_NOTARY_PROFILE="socialmcp-notary"
```

```bash
source ~/.socialmcp-signing/signing.env
./packaging/macos/build.sh --sign
```

`SOCIALMCP_SIGN_IDENTITY` must match `security find-identity -v -p codesigning`
exactly, byte for byte. `tests/test_privacy.py` enforces that none of these
values, nor an app-specific password, can be committed — including a check
against git history, because deleting a leaked credential in a later commit
does not unpublish it.

The first `codesign` run prompts for your **login keychain password** (your
macOS account password) to authorize access to the key. Choose **Always
Allow**, or every nested library on every build asks again.

Certificates are issued for about a year. Expiry does not break builds you
have already shipped: `--timestamp` proves the signature predated expiry and
notarization tickets do not lapse. **Never revoke a certificate you released
with** — revocation *is* retroactive and breaks every copy already downloaded.

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
gh release create v0.1.1 dist/SocialMCP-0.1.1-arm64.dmg \
  --title "SocialMCP 0.1.1" --notes-file CHANGELOG-0.1.1.md --latest
```

Three rules, each learned the hard way:

**Never replace an asset on a published release.** Download counts live on the
asset and reset to zero, and the checksum in the notes stops matching what
earlier downloaders have — which looks like tampering for a program that reads
private contact data. Ship a new version instead. The versioned filename makes
this mostly self-enforcing: a new release's asset cannot collide with an old
one.

**Never delete an old release.** Mark a superseded one as a pre-release if you
like, but keep it. It costs nothing, and it is the only way to bisect a
regression — the one case where "please upgrade" is exactly the wrong advice.

**Bump the version before building.** `pyproject.toml` is the single source;
the spec and the dmg filename both read it, and `tests/test_packaging.py`
fails if `__version__` drifts from it.

Apple Silicon only. Intel Macs are pointed at the `pipx`/`uv tool` install,
which works there and needs no bundled interpreter. GitHub-hosted macOS
runners are not usable for this repository — jobs are never allocated a runner
and are cancelled after queueing for 24 hours — so builds and signing happen
on a maintainer's Mac, which is where the certificate lives anyway.
