"""Guards that personal data and credentials cannot reach the remote.

This project's entire premise is that your network stays on your machine, so
these tests treat "could this be committed?" as a correctness property rather
than a matter of discipline. They run against the real repository and the real
.gitignore, so they fail if someone loosens an ignore rule.
"""

from __future__ import annotations

import re
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

TEXT_SUFFIXES = {
    ".py", ".md", ".toml", ".sql", ".csv", ".json", ".txt", ".yml", ".yaml",
    ".cfg", ".ini", ".sh", ".js", ".ts", ".gitignore", "",
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
