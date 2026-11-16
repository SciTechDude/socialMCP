"""Normalization helpers shared by every adapter.

Everything here is pure and deterministic — no LLM, no network. Adapters are
expected to push all of their messy real-world strings through these functions
so that identity keys and timestamps are comparable across sources and across
monthly archive drops.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

ISO_FMT = "%Y-%m-%dT%H:%M:%SZ"

# LinkedIn/Meta/X exports use a surprising number of date shapes. Order matters:
# the first format that parses wins, so put the unambiguous ones first.
_DATETIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S UTC",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S.%fZ",
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%dT%H:%M:%S",
    "%m/%d/%y, %I:%M %p",
    "%m/%d/%Y, %I:%M %p",
    "%m/%d/%y %I:%M %p",
    "%m/%d/%Y %I:%M %p",
    "%d %b %Y %H:%M",
)

_DATE_FORMATS = (
    "%Y-%m-%d",
    "%d %b %Y",   # LinkedIn Connections.csv: "06 Oct 2025"
    "%d %B %Y",
    "%b %d, %Y",
    "%B %d, %Y",
    "%m/%d/%Y",
    "%m/%d/%y",
    "%d/%m/%Y",
)


def utcnow_iso() -> str:
    """Current UTC time as the canonical ISO-8601 string used everywhere in the DB."""
    return datetime.now(UTC).strftime(ISO_FMT)


def parse_timestamp(value: str | None) -> str | None:
    """Parse an export timestamp into canonical ISO-8601 UTC, or None.

    Accepts epoch seconds/milliseconds (Meta exports), ISO-8601 with or without
    an offset, and the US-locale shapes LinkedIn emits for invitations.
    """
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None

    # Epoch (Facebook friends.json, X archives).
    if re.fullmatch(r"\d{10}", raw):
        return datetime.fromtimestamp(int(raw), tz=UTC).strftime(ISO_FMT)
    if re.fullmatch(r"\d{13}", raw):
        return datetime.fromtimestamp(int(raw) / 1000, tz=UTC).strftime(ISO_FMT)

    # LinkedIn writes U+202F (narrow no-break space) before AM/PM in some locales.
    candidate = raw.replace("\u202f", " ")

    # ISO-8601 with offset, e.g. 2025-10-06T14:23:11-07:00
    try:
        dt = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
    except ValueError:
        dt = None
    if dt is not None:
        return _to_utc(dt).strftime(ISO_FMT)

    for fmt in _DATETIME_FORMATS:
        try:
            # Naive by design: exports carry no offset, so _to_utc assumes UTC.
            return _to_utc(datetime.strptime(candidate, fmt)).strftime(ISO_FMT)  # noqa: DTZ007
        except ValueError:
            continue

    # Date-only input: anchor at midnight UTC so range filters still work.
    day = parse_date(candidate)
    if day:
        return f"{day}T00:00:00Z"
    return None


def parse_date(value: str | None) -> str | None:
    """Parse an export date into canonical 'YYYY-MM-DD', or None."""
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date().isoformat()  # noqa: DTZ007 - date only
        except ValueError:
            continue
    # Last resort: a full timestamp was handed to us.
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return None


def _to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def date_only(iso_timestamp: str | None) -> str | None:
    """'2025-10-06T14:23:11Z' -> '2025-10-06'."""
    if not iso_timestamp:
        return None
    return iso_timestamp[:10] or None


def to_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def slugify(value: str, *, max_len: int = 80) -> str:
    """ASCII, lowercase, hyphen-separated slug. Used for names and cluster slugs."""
    text = unicodedata.normalize("NFKD", value or "")
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text[:max_len]


def normalize_profile_url(
    url: str | None, canonicalize: Callable[[str, str], str | None] | None = None
) -> str | None:
    """Canonicalize a profile URL: strip scheme noise, casing and tracking params.

    Host-agnostic on purpose. Platform-specific path rules (which path prefixes
    identify a person, which are aliases of each other) belong to that
    platform's adapter, which passes them in as `canonicalize(host, path)`.
    Without one, a URL is reduced to a lowercase host plus path.

    Returns None for blanks and for values that are not recognizable URLs, so
    callers can fall back to name-based identity.
    """
    if not url:
        return None
    raw = str(url).strip()
    if not raw or raw.lower() in {"-", "n/a", "none"}:
        return None
    if "://" not in raw:
        raw = "https://" + raw.lstrip("/")

    parsed = urlparse(raw)
    host = parsed.netloc.lower().split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    if host.startswith(("m.", "mobile.", "touch.")):
        host = host.split(".", 1)[1]
    path = unquote(parsed.path).rstrip("/")

    if canonicalize is not None:
        return canonicalize(host, path)

    if not path or path == "/":
        return None
    return f"https://{host}{path.lower()}"


def profile_handle(normalized_url: str | None) -> str | None:
    """'https://www.linkedin.com/in/jane-doe' -> 'in/jane-doe' (the source_key form)."""
    if not normalized_url:
        return None
    path = urlparse(normalized_url).path.strip("/")
    return path or None


def clean(value: str | None) -> str | None:
    """Collapse whitespace; map empty/placeholder strings to None."""
    if value is None:
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    if not text or text.lower() in {"n/a", "na", "-", "none", "null"}:
        return None
    return text


def full_name(first: str | None, last: str | None, fallback: str | None = None) -> str | None:
    parts = [clean(first), clean(last)]
    name = " ".join(p for p in parts if p)
    return name or clean(fallback)


def split_name(name: str | None) -> tuple[str | None, str | None]:
    """Best-effort first/last split for sources that only give a display name."""
    text = clean(name)
    if not text:
        return None, None
    bits = text.split(" ")
    if len(bits) == 1:
        return bits[0], None
    return bits[0], " ".join(bits[1:])


def name_key(name: str | None, company: str | None = None) -> str | None:
    """Fallback identity key for rows with no profile URL.

    Company is folded in because 'John Smith' alone collides constantly; without
    a company we accept the collision risk rather than inventing an identity.
    """
    slug = slugify(name or "")
    if not slug:
        return None
    company_slug = slugify(company or "")
    return f"name/{slug}|{company_slug}" if company_slug else f"name/{slug}"


def text_hash(*parts: str | None) -> str:
    """Stable hash of the text that feeds an embedding, for staleness checks."""
    joined = "\u001f".join(p or "" for p in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def file_sha256(path: str | Path, *, chunk_size: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()
