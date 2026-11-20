"""Configuration: TOML file + environment overrides, no network calls.

Resolution order (last wins):
    1. built-in defaults
    2. ./socialmcp.toml, or $SOCIALMCP_CONFIG
    3. SOCIALMCP_* environment variables

MCP clients generally launch the server with a bare environment, so
every setting must be reachable through an env var as well as the TOML file.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

# Pure constant: the environment is read in load_config() only, so that
# "did the user choose a home?" stays answerable here.
DEFAULT_HOME = Path.home() / ".socialmcp"

# The drop zone is split by source so an archive's provenance is unambiguous
# before a single byte is parsed: a .zip in drop-archive/linkedin/ is routed
# straight to the LinkedIn adapter instead of being sniffed.
#
# Folder -> (adapter source, description). LinkedIn is the only supported
# source today; a future adapter adds one entry here and gets its own folder.
# Archives dropped at the top level of the drop zone are auto-detected.
DROP_SUBDIRS: dict[str, tuple[str, str]] = {
    "linkedin": ("linkedin", "LinkedIn Data Archive (Settings & Privacy -> Get a copy of your data)"),
}

# How each platform spells its own name, for text a user reads. Folder names are
# lowercase, and `"linkedin".capitalize()` gives "Linkedin", which looks like a
# typo in the one place a new user is already unsure what they are looking at.
PLATFORM_NAMES: dict[str, str] = {"linkedin": "LinkedIn"}


def _default_drop_dir(home: Path) -> Path:
    """Prefer a repo-local ./drop-archive when running from a source checkout.

    MCP clients launch the server with an arbitrary working directory, so this
    is resolved from the package location, never from os.getcwd().
    """
    repo_root = Path(__file__).resolve().parent.parent
    candidate = repo_root / "drop-archive"
    if candidate.is_dir() or (repo_root / "pyproject.toml").is_file():
        return candidate
    return home / "drop-archive"


@dataclass(slots=True)
class EmbeddingConfig:
    # 'none' keeps SocialMCP fully offline and keyword-only; it is a first-class
    # mode, not a degraded one — hybrid search falls back to FTS5 + filters.
    provider: str = "ollama"              # 'ollama' | 'openai' | 'none'
    model: str = "nomic-embed-text"
    dim: int = 768
    ollama_url: str = "http://127.0.0.1:11434"
    api_key_env: str = "OPENAI_API_KEY"   # only read when provider == 'openai'
    batch_size: int = 16
    timeout_seconds: float = 30.0


@dataclass(slots=True)
class ClusterConfig:
    # "bursts of connections within sliding 3-to-5-day windows"
    max_gap_days: int = 2        # a gap larger than this ends the burst
    max_span_days: int = 5       # a burst never spans more than this
    min_contacts: int = 5        # below this it is not an event, just a Tuesday


@dataclass(slots=True)
class Config:
    home: Path = field(default_factory=lambda: DEFAULT_HOME)
    db_path: Path | None = None
    drop_dir: Path | None = None
    archive_dir: Path | None = None      # processed .zips are moved here
    auto_ingest: bool = True             # watch drop_dir while the server runs
    create_source_dirs: bool = True      # scaffold drop-archive/{linkedin,x,meta,misc}
    embedding: EmbeddingConfig = field(default_factory=EmbeddingConfig)
    cluster: ClusterConfig = field(default_factory=ClusterConfig)
    max_results: int = 25

    def __post_init__(self) -> None:
        self.home = Path(self.home).expanduser()
        self.db_path = Path(self.db_path).expanduser() if self.db_path else self.home / "socialmcp.db"
        if self.drop_dir:
            self.drop_dir = Path(self.drop_dir).expanduser()
        elif self.home != DEFAULT_HOME:
            # An explicitly chosen home owns everything under it. Without this,
            # setting SOCIALMCP_HOME would move the database but leave the drop
            # zone pointing at whatever source checkout the package was
            # installed from — which is where an editable install lives.
            self.drop_dir = self.home / "drop-archive"
        else:
            self.drop_dir = _default_drop_dir(self.home)
        self.archive_dir = (
            Path(self.archive_dir).expanduser()
            if self.archive_dir
            else self.drop_dir.parent / "ingested"
        )

    def ensure_dirs(self) -> None:
        """Create the home, database, drop zone and per-source subfolders."""
        for path in (self.home, self.drop_dir, self.archive_dir, self.db_path.parent):
            path.mkdir(parents=True, exist_ok=True)
        if not self.create_source_dirs:
            return
        for folder, (source, _description) in DROP_SUBDIRS.items():
            target = self.drop_dir / folder
            target.mkdir(parents=True, exist_ok=True)
            readme = target / "README.md"
            if not readme.exists():
                # Written for whoever opens this folder, which is usually
                # someone mid-install wondering what to do. The previous
                # version mentioned adapters and gitignore, which answers a
                # question nobody standing here is asking.
                # `description` carries the long "where to find it" phrasing,
                # which belongs in the body, not a heading or a trailing
                # restatement. The folder name is the clearer title.
                platform = PLATFORM_NAMES.get(folder, folder.capitalize()) if source else "data"
                readme.write_text(
                    f"# Put your {platform} export here\n\n"
                    "**Drag the `.zip` into this folder — do not double-click it.**\n"
                    "Double-clicking unzips it, and the `.zip` is what SocialMCP\n"
                    "wants, exactly as you downloaded it.\n\n"
                    "Easier: open the SocialMCP app and click "
                    '"Choose my export…" — it will find the file for you.\n\n'
                    "Don't have an export yet? LinkedIn → Settings & Privacy →\n"
                    "Data Privacy → Get a copy of your data. Choose the **larger**\n"
                    "archive (the one with your messages and invitations). It\n"
                    "arrives by email, usually within a day.\n\n"
                    f"Once read, the file moves to `{self.archive_dir.name}/{folder}/`"
                    " so it is not read twice. You can drop a newer export in\n"
                    "here any time; re-reading the same one changes nothing.\n\n"
                    "Nothing in this folder is ever uploaded anywhere.\n"
                )

    def source_for(self, archive_path: Path) -> str | None:
        """Which adapter a dropped file is routed to, from its subfolder.

        Returns None when the folder carries no routing hint, in which case the
        pipeline sniffs the extracted contents instead.
        """
        try:
            relative = Path(archive_path).resolve().relative_to(self.drop_dir.resolve())
        except ValueError:
            return None
        if len(relative.parts) < 2:
            return None
        source, _ = DROP_SUBDIRS.get(relative.parts[0].lower(), ("", ""))
        return source or None


def _coerce(target: Any, data: dict[str, Any]) -> Any:
    """Apply a dict of overrides to a dataclass, ignoring unknown keys."""
    known = set(target.__slots__)
    clean = {k: v for k, v in data.items() if k in known and not isinstance(v, dict)}
    return replace(target, **clean) if clean else target


def load_config(path: str | Path | None = None) -> Config:
    config_path = Path(path) if path else Path(os.environ.get("SOCIALMCP_CONFIG", "socialmcp.toml"))
    data: dict[str, Any] = {}
    if config_path.expanduser().is_file():
        with config_path.expanduser().open("rb") as handle:
            data = tomllib.load(handle)

    # Environment overrides are folded in BEFORE the Config is constructed, so
    # path derivation (`archive_dir` defaults to a sibling of `drop_dir`) runs
    # exactly once against the final values. Mutating a Config afterwards and
    # re-deriving would leave paths computed from the pre-override defaults.
    env = os.environ
    raw: dict[str, Any] = {k: v for k, v in data.items() if not isinstance(v, dict)}
    for key, var in (
        ("home", "SOCIALMCP_HOME"),
        ("db_path", "SOCIALMCP_DB"),
        ("drop_dir", "SOCIALMCP_DROP_DIR"),
        ("archive_dir", "SOCIALMCP_ARCHIVE_DIR"),
    ):
        if env.get(var):
            raw[key] = Path(env[var])
    if env.get("SOCIALMCP_AUTO_INGEST"):
        raw["auto_ingest"] = env["SOCIALMCP_AUTO_INGEST"].strip().lower() in {"1", "true", "yes", "on"}

    embedding_data = dict(data.get("embedding") or {})
    if env.get("SOCIALMCP_EMBED_PROVIDER"):
        embedding_data["provider"] = env["SOCIALMCP_EMBED_PROVIDER"].strip().lower()
    if env.get("SOCIALMCP_EMBED_MODEL"):
        embedding_data["model"] = env["SOCIALMCP_EMBED_MODEL"]
    if env.get("SOCIALMCP_EMBED_DIM"):
        embedding_data["dim"] = int(env["SOCIALMCP_EMBED_DIM"])
    if env.get("SOCIALMCP_OLLAMA_URL"):
        embedding_data["ollama_url"] = env["SOCIALMCP_OLLAMA_URL"].rstrip("/")

    # Constructed fresh (not `replace()`d) because an existing instance would
    # already carry derived paths, which would suppress re-derivation.
    config = Config(**{k: v for k, v in raw.items() if k in Config.__slots__})
    if embedding_data:
        config.embedding = _coerce(config.embedding, embedding_data)
    if isinstance(data.get("cluster"), dict):
        config.cluster = _coerce(config.cluster, data["cluster"])
    return config
