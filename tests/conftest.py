from __future__ import annotations

import shutil
import zipfile
from pathlib import Path

import pytest

from socialmcp.config import ClusterConfig, Config, EmbeddingConfig
from socialmcp.core.db import Database

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(
        home=tmp_path,
        db_path=tmp_path / "test.db",
        drop_dir=tmp_path / "drop",
        archive_dir=tmp_path / "ingested",
        auto_ingest=False,
        # Tests must never touch the network, so embeddings are off.
        embedding=EmbeddingConfig(provider="none"),
        cluster=ClusterConfig(max_gap_days=2, max_span_days=5, min_contacts=5),
    )


@pytest.fixture
def db(config: Config) -> Database:
    config.ensure_dirs()
    database = Database(config.db_path, embedding_dim=config.embedding.dim)
    yield database
    database.close()


@pytest.fixture
def linkedin_dir() -> Path:
    return FIXTURES / "linkedin"


@pytest.fixture
def linkedin_month2_dir() -> Path:
    return FIXTURES / "linkedin_month2"


def make_zip(source_dir: Path, dest_zip: Path, *, inner_prefix: str = "") -> Path:
    """Zip a fixture folder the way LinkedIn does (flat files at the root)."""
    dest_zip.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest_zip, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(source_dir.iterdir()):
            if path.is_file():
                archive.write(path, arcname=f"{inner_prefix}{path.name}")
    return dest_zip


@pytest.fixture
def linkedin_zip(linkedin_dir: Path, tmp_path: Path) -> Path:
    return make_zip(linkedin_dir, tmp_path / "Basic_LinkedInDataExport_10-18-2025.zip")


@pytest.fixture
def copy_fixture(tmp_path: Path):
    def _copy(src: Path, name: str) -> Path:
        target = tmp_path / name
        shutil.copytree(src, target)
        return target

    return _copy
