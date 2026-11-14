"""The adapter contract. A new data source is one module that subclasses this.

An adapter's job is narrow on purpose:
    1. say whether an extracted export directory looks like its source (`detect`)
    2. stream `ContactRecord` / `InteractionRecord` objects out of it (`parse`)

It never opens the database, never embeds text, never clusters. That is the
pipeline's job, which is why adding Instagram or X support is ~200 lines and no
schema change. See CONTRIBUTING.md.
"""

from __future__ import annotations

import csv
import io
import logging
from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from ..core.models import Record

log = logging.getLogger("socialmcp.adapters")

# Exports are user data, not trusted input: cap row length so a malformed or
# hostile CSV cannot balloon memory through one enormous quoted field.
csv.field_size_limit(10 * 1024 * 1024)


@dataclass(slots=True)
class ArchiveContext:
    """Everything an adapter is told about the archive being parsed."""

    root: Path                      # directory the archive was extracted into
    archive_name: str               # original .zip file name
    warnings: list[str] = field(default_factory=list)
    files_parsed: list[str] = field(default_factory=list)
    options: dict[str, Any] = field(default_factory=dict)

    def warn(self, message: str) -> None:
        log.warning("%s: %s", self.archive_name, message)
        self.warnings.append(message)

    def mark_parsed(self, path: Path) -> None:
        try:
            self.files_parsed.append(str(path.relative_to(self.root)))
        except ValueError:
            self.files_parsed.append(path.name)


class BaseAdapter(ABC):
    source: ClassVar[str]
    display_name: ClassVar[str]
    # Any one of these present in the export is enough to claim it.
    signature_files: ClassVar[tuple[str, ...]] = ()

    def detect(self, root: Path) -> bool:
        return any(find_file(root, name) for name in self.signature_files)

    @abstractmethod
    def parse(self, ctx: ArchiveContext) -> Iterator[Record]:
        """Yield normalized records. Must not raise on a single bad row."""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{type(self).__name__} source={self.source!r}>"


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, type[BaseAdapter]] = {}


def register(cls: type[BaseAdapter]) -> type[BaseAdapter]:
    """Class decorator that makes an adapter discoverable by the pipeline."""
    if not getattr(cls, "source", None):
        raise ValueError(f"{cls.__name__} must define a `source` class attribute")
    _REGISTRY[cls.source] = cls
    return cls


def available_adapters() -> dict[str, BaseAdapter]:
    from . import load_builtin_adapters

    load_builtin_adapters()
    return {source: cls() for source, cls in sorted(_REGISTRY.items())}


def get_adapter(source: str) -> BaseAdapter:
    adapters = available_adapters()
    try:
        return adapters[source]
    except KeyError:
        raise KeyError(f"unknown source {source!r}; known: {sorted(adapters)}") from None


def detect_adapter(root: Path) -> BaseAdapter | None:
    """Pick the adapter that recognizes this extracted export, if any."""
    for adapter in available_adapters().values():
        try:
            if adapter.detect(root):
                return adapter
        except OSError as exc:  # unreadable archive tree
            log.warning("adapter %s failed detection: %s", adapter.source, exc)
    return None


# ---------------------------------------------------------------------------
# file + CSV helpers shared by adapters
# ---------------------------------------------------------------------------


def find_file(root: Path, name: str) -> Path | None:
    """Case-insensitive recursive lookup. Exports vary in casing and nesting.

    Returns the shallowest match so a top-level `Connections.csv` beats a copy
    buried in a nested folder.
    """
    target = name.lower()
    matches = [p for p in root.rglob("*") if p.is_file() and p.name.lower() == target]
    if not matches:
        return None
    return min(matches, key=lambda p: (len(p.relative_to(root).parts), str(p)))


def find_files(root: Path, pattern: str) -> list[Path]:
    """Case-insensitive glob by suffix pattern, e.g. 'messages*.csv'."""
    import fnmatch

    lowered = pattern.lower()
    return sorted(
        p for p in root.rglob("*") if p.is_file() and fnmatch.fnmatch(p.name.lower(), lowered)
    )


def normalize_header(value: str) -> str:
    """'Sent At' / 'SENDER PROFILE URL' / 'inviterProfileUrl' -> 'sent_at' …

    Column names drift between export vintages; normalizing lets one parser
    handle several header spellings without a lookup table per release.
    """
    import re

    text = (value or "").replace("﻿", "").strip()
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", text)      # camelCase -> camel_Case
    text = re.sub(r"[^0-9A-Za-z]+", "_", text)
    return text.strip("_").lower()


def read_csv_rows(
    path: Path,
    required_headers: tuple[str, ...],
    *,
    max_preamble_rows: int = 25,
) -> Iterator[dict[str, str]]:
    """Yield dict rows from a CSV that may carry a human-readable preamble.

    LinkedIn's `Connections.csv` begins with a multi-line "Notes:" disclaimer
    before the real header row, and the number of lines has changed between
    export vintages. So rather than skipping a hard-coded 3 lines, we scan with
    the csv reader (which correctly handles quoted newlines in the preamble)
    until we find the first row that contains every required header, then treat
    that row as the header.

    `required_headers` are given in normalized form, e.g. ('first_name', 'last_name').
    """
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(text, newline=""))

    header: list[str] | None = None
    for index, row in enumerate(reader):
        if index > max_preamble_rows:
            break
        normalized = [normalize_header(cell) for cell in row]
        if all(req in normalized for req in required_headers):
            header = normalized
            break

    if header is None:
        raise HeaderNotFoundError(
            f"{path.name}: no header row containing {required_headers} in the first "
            f"{max_preamble_rows} rows"
        )

    width = len(header)
    for row in reader:
        if not any(cell.strip() for cell in row):
            continue
        # Pad/truncate: trailing-comma rows and extra columns both occur in the wild.
        if len(row) < width:
            row = row + [""] * (width - len(row))
        elif len(row) > width:
            row = row[:width]
        yield dict(zip(header, row, strict=True))


class HeaderNotFoundError(ValueError):
    """Raised when a CSV has no recognizable header row."""
