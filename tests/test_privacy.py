"""Guards that personal data and credentials cannot reach the remote.

This project's entire premise is that your network stays on your machine, so
these tests treat "could this be committed?" as a correctness property rather
than a matter of discipline. They run against the real repository and the real
.gitignore, so they fail if someone loosens an ignore rule.

The last section covers Apple code-signing material, which is a distinct
hazard: signing a release build pulls a certificate, a private key, an Apple
ID and an app-specific password onto the machine *while* someone is iterating
on the build script — which is exactly when `git add -A` gets typed. Those
tests work in two layers, because either alone is insufficient: pattern rules
that run everywhere including CI, and a search for the real values read from
the local signing env file. The real values are never written into this file —
hardcoding the Team ID to test for it would commit the string the test exists
to keep out.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

# Representative paths that must never be committable. These files are not
# created; `git check-ignore` matches on path patterns alone.
MUST_BE_IGNORED = [
    # Personal data exports
    "drop-archive/linkedin/Basic_LinkedInDataExport_11-14-2026.zip",
    "drop-archive/linkedin/Basic_LinkedInDataExport_11-14-2026.zip.zip",
    "drop-archive/linkedin/Connections.csv",
    "drop-archive/linkedin/messages.csv",
    "drop-archive/anything.zip",
    "ingested/linkedin/old-export.zip",
    "Connections.csv",
    "Invitations.csv",
    "messages.csv",
    "Profile.csv",
    # The local CRM itself
    "socialmcp.db",
    "socialmcp.db-wal",
    "socialmcp.db-shm",
    "data/network.sqlite3",
    # Local config, which may hold absolute paths or key env names
    "socialmcp.toml",
    # Credentials
    ".env",
    ".env.local",
    "github-token.txt",
    "gh-token",
    "my.token",
    "id_rsa.key",
    "server.pem",
    "secrets.json",
    ".netrc",
    # Apple code-signing material, under every name the portal and Apple's
    # tooling actually produce. Paths rather than extensions, because the
    # mistake is always a specific file in a specific place.
    "developerID.csr",
    "developerID.cer",
    "developerID.key",
    "CertificateSigningRequest.certSigningRequest",
    "signing.env",
    ".env.signing",
    "DeveloperIDApplication.p12",
    "packaging/macos/certificate.p12",
    "packaging/macos/signing.env",
    "embedded.provisionprofile",
    "profile.mobileprovision",
    "apple-notary-password.txt",
]

# Files that MUST stay committable, so the ignore rules are not over-broad.
MUST_NOT_BE_IGNORED = [
    "README.md",
    "CONTRIBUTING.md",
    "pyproject.toml",
    "run.py",
    "socialmcp.example.toml",
    "socialmcp/core/schema.sql",
    "socialmcp/adapters/linkedin/adapter.py",
    "tests/fixtures/linkedin/Connections.csv",
    "tests/fixtures/linkedin/Invitations.csv",
    "tests/fixtures/linkedin/messages.csv",
    "drop-archive/linkedin/.gitkeep",
]

# Credential shapes that must never appear in a tracked file.
SECRET_PATTERNS = {
    "GitHub personal access token": re.compile(r"\bghp_[A-Za-z0-9]{20,}"),
    "GitHub fine-grained token": re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    "GitHub OAuth token": re.compile(r"\bgho_[A-Za-z0-9]{20,}"),
    "Anthropic API key": re.compile(r"\bsk-ant-[A-Za-z0-9\-_]{20,}"),
    "OpenAI API key": re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9]{32,}"),
    "AWS access key id": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "private key block": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "Slack token": re.compile(r"\bxox[abpsr]-[A-Za-z0-9-]{10,}"),
}

# Apple distribution credentials. Separate from SECRET_PATTERNS because the
# signing tests below check these against build.sh and the CI workflows too,
# not only against tracked files. Placeholder forms used in documentation are
# allowed through by design — the docs have to show the shape of the command.
APPLE_SECRET_PATTERNS = {
    # Apple app-specific passwords are four lowercase groups of four letters.
    "Apple app-specific password": re.compile(r"\b[a-z]{4}(?:-[a-z]{4}){3}\b"),
    # A concrete signing identity: real Team IDs are ten characters.
    "real signing identity": re.compile(
        r"Developer ID (?:Application|Installer):[^\n(]*"
        r"\((?!TEAMID|YOUR_TEAM_ID|XXXXXXXXXX)[A-Z0-9]{10}\)"
    ),
    # notarytool / altool invocations carrying a literal rather than a
    # variable reference or a documented `<placeholder>`.
    "literal --team-id": re.compile(
        r"--team-id[= ]+(?!TEAMID|\$|[\"']\$|<)[A-Z0-9]{10}\b"
    ),
    "literal --password": re.compile(
        r"--password[= ]+(?![\"']?\$|<|@keychain:|@env:)[^\s\\]{6,}"
    ),
    "literal --apple-id": re.compile(
        r"--apple-id[= ]+(?!\$|[\"']\$|<)[\w.+-]+@(?!example\.(?:com|org|net))[\w.-]+"
    ),
}

TEXT_SUFFIXES = {
    ".py", ".md", ".toml", ".sql", ".csv", ".json", ".txt", ".yml", ".yaml",
    ".cfg", ".ini", ".sh", ".js", ".ts", ".plist", ".spec", ".gitignore", "",
}


def git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=REPO, capture_output=True, text=True, check=False
    )
    return result.stdout


def is_repo() -> bool:
    return (REPO / ".git").exists() and git("rev-parse", "--is-inside-work-tree").strip() == "true"


requires_git = pytest.mark.skipif(not is_repo(), reason="not a git repository")


@requires_git
@pytest.mark.parametrize("path", MUST_BE_IGNORED)
def test_sensitive_paths_are_gitignored(path: str):
    result = subprocess.run(
        ["git", "check-ignore", "-q", "--no-index", path],
        cwd=REPO,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"{path!r} is NOT ignored by .gitignore — personal data or a credential "
        "could be committed. Add a rule before changing this test."
    )


@requires_git
@pytest.mark.parametrize("path", MUST_NOT_BE_IGNORED)
def test_project_files_are_not_over_ignored(path: str):
    result = subprocess.run(
        ["git", "check-ignore", "-q", "--no-index", path],
        cwd=REPO,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0, f"{path!r} is ignored but must be part of the repo"


@requires_git
def test_no_tracked_file_looks_like_personal_data():
    tracked = [p for p in git("ls-files").splitlines() if p]
    if not tracked:
        pytest.skip("nothing committed yet")

    offenders = []
    for path in tracked:
        lowered = path.lower()
        if lowered.endswith((".zip", ".db", ".sqlite", ".sqlite3", ".db-wal", ".db-shm")):
            offenders.append(path)
        # Real export CSVs are only ever allowed as anonymized fixtures.
        if lowered.endswith(".csv") and not lowered.startswith("tests/fixtures/"):
            offenders.append(path)
    assert not offenders, f"tracked files look like personal data: {offenders}"


@requires_git
def test_no_tracked_file_contains_a_credential():
    tracked = [p for p in git("ls-files").splitlines() if p]
    findings: list[str] = []
    for path in tracked:
        full = REPO / path
        if full.suffix.lower() not in TEXT_SUFFIXES or not full.is_file():
            continue
        if full.stat().st_size > 2 * 1024 * 1024:
            continue
        content = full.read_text(encoding="utf-8", errors="ignore")
        for label, pattern in SECRET_PATTERNS.items():
            match = pattern.search(content)
            if match:
                # Report the location, never the secret itself.
                line = content[: match.start()].count("\n") + 1
                findings.append(f"{path}:{line} ({label})")
    assert not findings, f"credential-shaped strings in tracked files: {findings}"


# Only these email domains may appear anywhere in the repo. RFC 2606 reserves
# example.* precisely so fixtures and docs cannot reach a real mailbox.
ALLOWED_EMAIL_DOMAINS = {"example.com", "example.org", "example.net"}

# Domains that would mean a real person's address slipped in.
REAL_MAIL_PROVIDERS = re.compile(
    r"@(?:gmail|googlemail|yahoo|ymail|hotmail|live|outlook|msn|icloud|aol|"
    r"proton|protonmail|gmx|zoho|fastmail|yandex|qq)\.[a-z.]{2,}",
    re.IGNORECASE,
)

# Phone-number shapes, loose enough to catch a pasted contact detail.
PHONE_PATTERN = re.compile(
    r"(?<![\w.])(?:\+\d{1,3}[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?![\w.])"
)

EMAIL_PATTERN = re.compile(r"[\w.+-]+@([\w-]+\.[\w.-]+)")


def tracked_text_files() -> list[Path]:
    """Every committed text file — exactly the set that reaches GitHub."""
    paths = []
    for relative in git("ls-files").splitlines():
        if not relative:
            continue
        full = REPO / relative
        if (
            full.is_file()
            and full.suffix.lower() in TEXT_SUFFIXES
            and full.stat().st_size < 2_000_000
        ):
            paths.append(full)
    return paths


@requires_git
def test_no_real_email_addresses_anywhere_in_the_repo():
    """Covers fixtures AND test code: tests are published like any other file.

    A fixture quietly built from a real export would leak just as badly as
    committing the export itself, so this scans everything that is tracked.
    """
    files = [p for p in tracked_text_files() if p.name != "test_privacy.py"]
    if not files:
        pytest.skip("nothing committed yet")

    bad: list[str] = []
    for path in files:
        text = path.read_text(encoding="utf-8", errors="ignore")
        relative = path.relative_to(REPO)
        for domain in EMAIL_PATTERN.findall(text):
            if domain.lower() not in ALLOWED_EMAIL_DOMAINS:
                bad.append(f"{relative}: @{domain}")
        if REAL_MAIL_PROVIDERS.search(text):
            bad.append(f"{relative}: consumer mail provider address")
    assert not bad, (
        "non-anonymized email addresses in committed files "
        f"(use @example.com in fixtures and tests): {bad}"
    )


@requires_git
def test_no_phone_numbers_in_committed_files():
    files = [p for p in tracked_text_files() if p.name != "test_privacy.py"]
    if not files:
        pytest.skip("nothing committed yet")
    bad = [
        str(path.relative_to(REPO))
        for path in files
        if PHONE_PATTERN.search(path.read_text(encoding="utf-8", errors="ignore"))
    ]
    assert not bad, f"phone-number-shaped strings in committed files: {bad}"


def test_committed_fixtures_are_small_and_hand_written():
    """A dumped real export would be far larger than a hand-made fixture.

    A smell test, not a proof: it catches "I just copied my own
    Connections.csv into tests/", which is the realistic failure mode.
    """
    for path in (REPO / "tests" / "fixtures").rglob("*.csv"):
        rows = path.read_text(encoding="utf-8", errors="ignore").count("\n")
        assert rows <= 60, (
            f"{path.relative_to(REPO)} has {rows} rows — fixtures must be small, "
            "hand-written and anonymized, never a real export"
        )


def test_no_real_export_sits_in_the_repo_working_tree_unignored():
    """A dropped export must be ignored even though it lives inside the repo."""
    drop = REPO / "drop-archive"
    if not drop.is_dir():
        pytest.skip("no drop zone in this checkout")
    for archive in drop.rglob("*.zip*"):
        relative = archive.relative_to(REPO)
        result = subprocess.run(
            ["git", "check-ignore", "-q", str(relative)],
            cwd=REPO,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, f"{relative} is in the repo and NOT ignored"


@requires_git
def test_no_virtualenv_or_build_artifact_is_tracked():
    """Nothing from a venv or build output may be committed.

    This is a regression test. A per-architecture rename turned the build
    environment into `.venv-build-<arch>`, which the existing `.venv-build/`
    rule did not match, and a routine `git add -A` committed 3,317 files and
    75 MB of vendored third-party code. The privacy suite noticed only because
    that code happens to contain credential-shaped test material.
    """
    tracked = git("ls-files").splitlines()
    if not tracked:
        pytest.skip("nothing committed yet")

    offenders = [
        path
        for path in tracked
        if path.startswith((".venv", "venv", "dist/", "build/"))
        or "/site-packages/" in path
        or path.endswith((".dmg", ".pkg", ".whl"))
    ]
    assert not offenders, f"virtualenv or build artifacts are tracked: {offenders[:10]}"


@pytest.mark.parametrize(
    "path",
    [
        ".venv",
        ".venv/lib/python3.12/site-packages/anything.py",
        ".venv-build",
        ".venv-build-arm64",
        ".venv-build-x86_64/lib/site-packages/x.py",
        "venv",
        "venv-3.12",
        "dist/SocialMCP-arm64.dmg",
        "build/arm64/anything",
    ],
)
@requires_git
def test_every_venv_shape_is_ignored(path: str):
    """Covers the names a venv actually gets, not just the one in use today.

    The rule intentionally has no trailing slash: git cannot tell that a path
    which does not exist yet is a directory, so a directory-only pattern fails
    to match a venv before it is created — exactly when the mistake is made.
    """
    import subprocess

    result = subprocess.run(
        ["git", "check-ignore", "-q", "--no-index", path],
        cwd=REPO,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, f"{path!r} would be committable"


# ---------------------------------------------------------------------------
# Apple code-signing credentials
# ---------------------------------------------------------------------------

# The signing material lives outside the repository on purpose: a path that is
# not in the working tree cannot be reached by a careless `git add`, whatever
# .gitignore says. Overridable so another machine can keep it elsewhere.
SIGNING_DIR = Path(
    os.environ.get("SOCIALMCP_SIGNING_DIR", Path.home() / ".socialmcp-signing")
)
SIGNING_ENV = SIGNING_DIR / "signing.env"

# Variables whose values are account identifiers or credentials. The notary
# profile name and the word "Developer ID Application" are deliberately not
# here: they are not secrets, and treating them as such would fail on the
# project's own documentation.
SECRET_ENV_VARS = ("SOCIALMCP_TEAM_ID", "SOCIALMCP_APPLE_ID")

ENV_ASSIGNMENT = re.compile(
    r"^\s*(?:export\s+)?([A-Z_][A-Z0-9_]*)\s*=\s*[\"']?([^\"'\n#]*)", re.MULTILINE
)


def local_secret_values() -> dict[str, str]:
    """Real signing values from this machine, if it has any.

    Absent on CI and in a fresh clone, so the tests using this skip rather
    than fail; the pattern layer is what runs everywhere.
    """
    values: dict[str, str] = {}
    if SIGNING_ENV.is_file():
        text = SIGNING_ENV.read_text(encoding="utf-8", errors="ignore")
        for name, value in ENV_ASSIGNMENT.findall(text):
            value = value.strip()
            if (
                name in SECRET_ENV_VARS
                and len(value) >= 6
                and "CHANGEME" not in value.upper()
                and "example.com" not in value.lower()
            ):
                values[name] = value
    # An exported value wins: a shell that has sourced the file is the state
    # the signed build actually runs in.
    for name in SECRET_ENV_VARS:
        from_env = os.environ.get(name, "").strip()
        if len(from_env) >= 6 and "CHANGEME" not in from_env.upper():
            values[name] = from_env
    return values


@requires_git
def test_no_apple_signing_artifact_is_tracked():
    """Ignore rules are irrelevant for a file already added with `git add -f`."""
    tracked = git("ls-files").splitlines()
    if not tracked:
        pytest.skip("nothing committed yet")
    suffixes = (
        ".cer", ".csr", ".certsigningrequest", ".p12", ".pfx", ".key", ".pem",
        ".p8", ".provisionprofile", ".mobileprovision", ".keychain",
    )
    offenders = [p for p in tracked if p.lower().endswith(suffixes)]
    assert not offenders, f"signing material is tracked: {offenders}"


@requires_git
def test_no_tracked_file_contains_apple_signing_credentials():
    files = [p for p in tracked_text_files() if p.name != Path(__file__).name]
    if not files:
        pytest.skip("nothing committed yet")

    findings: list[str] = []
    for path in files:
        content = path.read_text(encoding="utf-8", errors="ignore")
        for label, pattern in APPLE_SECRET_PATTERNS.items():
            match = pattern.search(content)
            if match:
                # Location and kind only — never the matched text.
                line = content[: match.start()].count("\n") + 1
                findings.append(f"{path.relative_to(REPO)}:{line} ({label})")
    assert not findings, f"Apple signing credentials in tracked files: {findings}"


@requires_git
def test_real_signing_values_are_absent_from_tracked_files():
    """The specific failure: a Team ID or Apple ID pasted in "just to test"."""
    secrets = local_secret_values()
    if not secrets:
        pytest.skip("no local signing env file; the pattern tests cover this")

    findings: list[str] = []
    for path in tracked_text_files():
        content = path.read_text(encoding="utf-8", errors="ignore")
        for name, value in secrets.items():
            index = content.find(value)
            if index != -1:
                findings.append(
                    f"{path.relative_to(REPO)}:{content[:index].count(chr(10)) + 1} ({name})"
                )
    assert not findings, (
        "the real value of a signing variable appears in a committed file — "
        f"keep it in {SIGNING_ENV.name} and reference the env var: {findings}"
    )


@requires_git
def test_real_signing_values_were_never_committed_in_any_revision():
    """Deleting a leaked value in a later commit does not unpublish it.

    Once pushed, every revision stays retrievable from GitHub, so the only
    safe property is that the string never entered history at all. `git log -S`
    (the pickaxe) searches content across all revisions rather than just the
    current tree.
    """
    secrets = local_secret_values()
    if not secrets:
        pytest.skip("no local signing env file; the pattern tests cover this")
    if not git("rev-parse", "--quiet", "--verify", "HEAD").strip():
        pytest.skip("no commits yet")

    findings: list[str] = []
    for name, value in secrets.items():
        hits = subprocess.run(
            ["git", "log", "--all", "--oneline", "-S", value, "--"],
            cwd=REPO,
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        ).stdout.strip()
        if hits:
            findings.append(
                f"{name} in commits: {[line.split()[0] for line in hits.splitlines()]}"
            )
    assert not findings, (
        "a signing credential is in git history and must be treated as "
        f"compromised — rotate it, do not merely delete it: {findings}"
    )


def test_build_script_notarizes_through_the_keychain_not_a_password():
    """`notarytool --password` on a command line is the mistake prevented here.

    It lands in shell history, in CI logs and in the process table. The
    keychain profile exists so the password is typed once, by a human, into
    `store-credentials`.
    """
    script = (REPO / "packaging" / "macos" / "build.sh").read_text(encoding="utf-8")
    submit_lines = [
        ln for ln in script.splitlines() if "notarytool" in ln and "submit" in ln
    ]
    assert submit_lines, "build.sh no longer submits for notarization"
    for line in submit_lines:
        assert "--keychain-profile" in line, (
            "notarytool submit must authenticate with --keychain-profile: " + line
        )
        assert "--password" not in line, (
            "a password on the notarytool command line leaks into shell history, "
            "CI logs and the process table: " + line
        )


def test_build_script_takes_signing_config_from_the_environment():
    """No identity, Team ID or Apple ID may be baked into the build script."""
    script = (REPO / "packaging" / "macos" / "build.sh").read_text(encoding="utf-8")
    assert "SOCIALMCP_SIGN_IDENTITY" in script
    assert "SOCIALMCP_NOTARY_PROFILE" in script
    for label, pattern in APPLE_SECRET_PATTERNS.items():
        assert not pattern.search(script), f"build.sh contains {label}"


@pytest.mark.parametrize("workflow", ["ci.yml", "release.yml"])
def test_workflows_never_inline_apple_credentials(workflow: str):
    """A workflow file on a public repo is published the instant it is pushed.

    If CI ever signs, it must read from `${{ secrets.* }}` — including the
    certificate, which reaches CI as a base64 .p12 and must never be a literal.
    """
    path = REPO / ".github" / "workflows" / workflow
    if not path.is_file():
        pytest.skip(f"{workflow} not present")
    content = path.read_text(encoding="utf-8")
    for label, pattern in APPLE_SECRET_PATTERNS.items():
        assert not pattern.search(content), f"{workflow} contains {label}"
    for prologue in ("MIIK", "MIIJ"):  # base64 PKCS#12 / DER
        assert prologue not in content, (
            f"{workflow} appears to embed a certificate blob; use a secret"
        )


def test_signing_directory_is_outside_the_repository():
    """Nothing .gitignore can do is as strong as the file not being here."""
    resolved = SIGNING_DIR.resolve()
    assert resolved != REPO and REPO not in resolved.parents, (
        f"signing material at {SIGNING_DIR} is inside the working tree; move it "
        "out so no git command can reach it"
    )


@pytest.mark.skipif(not SIGNING_ENV.is_file(), reason="no local signing env file")
def test_local_signing_material_is_not_world_readable():
    """On a shared or backed-up machine, mode matters as much as location."""
    offenders = []
    for path in [SIGNING_DIR, *SIGNING_DIR.iterdir()]:
        mode = path.stat().st_mode
        # The CSR and the certificate are public; keys and env are not.
        if path.is_file() and path.suffix in {".csr", ".cer"}:
            continue
        if mode & (stat.S_IRGRP | stat.S_IROTH):
            offenders.append(f"{path.name} is mode {stat.S_IMODE(mode):o}")
    assert not offenders, (
        f"signing material is readable beyond your account: {offenders} — "
        "chmod 700 the directory and 600 the files"
    )
