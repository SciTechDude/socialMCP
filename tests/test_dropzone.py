"""Drop-zone layout and per-source routing (LinkedIn is the only source today)."""

from __future__ import annotations

from pathlib import Path

from socialmcp.config import DROP_SUBDIRS, Config
from socialmcp.core.harness import Harness
from socialmcp.core.ingest import ingest_path

from .conftest import make_zip


def test_ensure_dirs_scaffolds_the_drop_zone(config: Config):
    config.ensure_dirs()
    assert set(DROP_SUBDIRS) == {"linkedin"}
    for folder in DROP_SUBDIRS:
        assert (config.drop_dir / folder).is_dir()
        assert (config.drop_dir / folder / "README.md").is_file()
    assert "LinkedIn" in (config.drop_dir / "linkedin" / "README.md").read_text()


def test_source_is_derived_from_subfolder(config: Config):
    config.ensure_dirs()
    assert config.source_for(config.drop_dir / "linkedin" / "export.zip") == "linkedin"
    assert config.source_for(config.drop_dir / "loose.zip") is None        # top level: sniff it
    assert config.source_for(config.drop_dir / "other" / "x.zip") is None  # unknown folder: sniff it
    assert config.source_for(Path("/tmp/elsewhere.zip")) is None


def test_zip_in_linkedin_folder_is_routed_and_filed(db, config: Config, linkedin_dir: Path):
    config.ensure_dirs()
    dropped = make_zip(linkedin_dir, config.drop_dir / "linkedin" / "export.zip")

    stats = ingest_path(db, config, dropped)

    assert stats.source == "linkedin"
    assert stats.contacts_new == 12
    # Processed archives keep their subfolder, so provenance survives filing.
    assert not dropped.exists()
    assert (config.archive_dir / "linkedin" / "export.zip").is_file()


def test_unknown_source_falls_back_to_detection(db, config: Config, linkedin_dir: Path):
    """An explicit source we don't ship must degrade to sniffing, with a warning."""
    config.ensure_dirs()
    dropped = make_zip(linkedin_dir, config.drop_dir / "export.zip")

    stats = ingest_path(db, config, dropped, source="x_twitter")

    assert stats.source == "linkedin"
    assert any("x_twitter" in w for w in stats.warnings)
    assert stats.contacts_new == 12


def test_harness_scans_subfolders(db, config: Config, linkedin_dir: Path):
    config.ensure_dirs()
    make_zip(linkedin_dir, config.drop_dir / "linkedin" / "nested.zip")

    results = Harness(db, config).scan_now(reason="test")

    assert [r.contacts_new for r in results] == [12]
    assert db.scalar("SELECT COUNT(*) FROM contacts") == 12
