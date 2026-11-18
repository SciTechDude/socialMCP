"""Config resolution — mostly a regression suite for path derivation.

The bug these guard against: `archive_dir` defaults to a sibling of `drop_dir`,
so if overrides are applied after the defaults are derived, a custom drop zone
silently files processed archives next to the *default* one. That lost a real
export during manual testing, hence the explicit coverage.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from socialmcp.config import Config, load_config


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    for var in (
        "SOCIALMCP_HOME",
        "SOCIALMCP_DB",
        "SOCIALMCP_DROP_DIR",
        "SOCIALMCP_ARCHIVE_DIR",
        "SOCIALMCP_AUTO_INGEST",
        "SOCIALMCP_EMBED_PROVIDER",
        "SOCIALMCP_EMBED_MODEL",
        "SOCIALMCP_EMBED_DIM",
        "SOCIALMCP_OLLAMA_URL",
        "SOCIALMCP_CONFIG",
    ):
        monkeypatch.delenv(var, raising=False)
    # Never let a developer's real socialmcp.toml leak into these tests.
    monkeypatch.chdir(tmp_path)


def test_env_drop_dir_also_moves_the_archive_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("SOCIALMCP_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SOCIALMCP_DROP_DIR", str(tmp_path / "zone"))

    config = load_config()

    assert config.drop_dir == tmp_path / "zone"
    assert config.archive_dir == tmp_path / "ingested"   # sibling of the drop zone
    assert config.db_path == tmp_path / "home" / "socialmcp.db"


def test_explicit_archive_dir_wins(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("SOCIALMCP_DROP_DIR", str(tmp_path / "zone"))
    monkeypatch.setenv("SOCIALMCP_ARCHIVE_DIR", str(tmp_path / "done"))

    assert load_config().archive_dir == tmp_path / "done"


def test_toml_values_and_nested_sections(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    toml = tmp_path / "socialmcp.toml"
    # Single-quoted TOML literal strings take the path verbatim. In a
    # double-quoted basic string a Windows path turns "\Users" into an invalid
    # escape sequence and the file fails to parse.
    toml.write_text(
        f"""
        home = '{tmp_path / 'h'}'
        drop_dir = '{tmp_path / 'd'}'
        auto_ingest = false
        max_results = 7

        [embedding]
        provider = "none"
        dim = 384

        [cluster]
        min_contacts = 12
        """
    )
    config = load_config(toml)

    assert config.auto_ingest is False
    assert config.max_results == 7
    assert config.embedding.provider == "none"
    assert config.embedding.dim == 384
    assert config.cluster.min_contacts == 12
    assert config.drop_dir == tmp_path / "d"
    assert config.archive_dir == tmp_path / "ingested"


def test_env_overrides_toml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    toml = tmp_path / "socialmcp.toml"
    toml.write_text('[embedding]\nprovider = "ollama"\n')
    monkeypatch.setenv("SOCIALMCP_EMBED_PROVIDER", "none")

    assert load_config(toml).embedding.provider == "none"


def test_defaults_are_self_consistent(tmp_path: Path):
    config = Config(home=tmp_path)
    assert config.db_path.parent == tmp_path
    assert config.archive_dir == config.drop_dir.parent / "ingested"


def test_explicit_home_owns_the_drop_zone(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Setting only SOCIALMCP_HOME must relocate the drop zone too.

    Regression: the drop zone defaults to a repo-local ./drop-archive when
    running from a source checkout, resolved from the package's own location.
    Under an editable install that location is the developer's source tree, so
    a user who set SOCIALMCP_HOME got their database in one place and their
    drop zone in another — found by installing into a clean venv.
    """
    monkeypatch.setenv("SOCIALMCP_HOME", str(tmp_path / "myhome"))

    config = load_config()

    assert config.db_path == tmp_path / "myhome" / "socialmcp.db"
    assert config.drop_dir == tmp_path / "myhome" / "drop-archive"
    assert config.archive_dir == tmp_path / "myhome" / "ingested"


def test_source_checkout_still_gets_a_repo_local_drop_zone():
    """With no configuration at all, a checkout keeps ./drop-archive."""
    config = Config()
    assert config.drop_dir.name == "drop-archive"
    # Either the repo-local one or the one under the default home; never a
    # path unrelated to both.
    assert config.drop_dir.parent in {Path(__file__).resolve().parent.parent, config.home}
