"""The standardized record shapes every adapter emits.

An adapter never touches SQL. It yields `ContactRecord` / `InteractionRecord`
objects and the ingest pipeline (core/ingest.py) owns upserts, history diffing,
identity resolution and clustering. That boundary is what makes a new adapter a
single-file contribution.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Status = Literal["connected", "pending", "received", "withdrawn", "unknown"]
Direction = Literal["outbound", "inbound", "self"]
InteractionKind = Literal["invitation", "connection", "message", "note"]

# Higher wins when two records for the same person disagree. A person seen in
# Connections.csv is connected even if an older Invitations.csv row said pending.
STATUS_PRECEDENCE: dict[str, int] = {
    "unknown": 0,
    "withdrawn": 1,
    "received": 2,
    "pending": 3,
    "connected": 4,
}


@dataclass(slots=True)
class Identity:
    """One known way of referring to a person within a source."""

    kind: Literal["profile_url", "email", "name", "handle", "thread"]
    value: str


@dataclass(slots=True)
class ContactRecord:
    source: str
    source_key: str
    full_name: str
    first_name: str | None = None
    last_name: str | None = None
    headline: str | None = None
    company: str | None = None
    position: str | None = None
    location: str | None = None
    email: str | None = None
    profile_url: str | None = None
    status: Status = "unknown"
    origin_note: str | None = None
    origin_direction: Direction | None = None
    invited_at: str | None = None          # ISO-8601 UTC
    connected_on: str | None = None        # YYYY-MM-DD
    identities: list[Identity] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class InteractionRecord:
    source: str
    contact_key: str                       # must match a ContactRecord.source_key
    kind: InteractionKind
    occurred_at: str | None = None         # ISO-8601 UTC
    direction: Direction | None = None
    subject: str | None = None
    body: str | None = None
    thread_id: str | None = None
    dedupe_key: str | None = None
    # Set when the interaction's person is only identifiable by URL/name, so the
    # pipeline can attach it to an existing contact instead of creating one.
    resolve_only: bool = False
    resolve_identities: list[Identity] = field(default_factory=list)


Record = ContactRecord | InteractionRecord


@dataclass(slots=True)
class IngestStats:
    """Returned to the MCP client so the chat UI can report what landed."""

    source: str = ""
    archive: str = ""
    archive_id: int | None = None
    skipped_duplicate: bool = False
    files_parsed: list[str] = field(default_factory=list)
    contacts_new: int = 0
    contacts_updated: int = 0      # matched an existing row AND changed a field
    contacts_unchanged: int = 0    # matched an existing row, nothing to change
    interactions_new: int = 0
    history_changes: int = 0
    clusters_created: int = 0
    clusters_updated: int = 0
    pending_invitations: int = 0
    embeddings_written: int = 0
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "archive": self.archive,
            "archive_id": self.archive_id,
            "skipped_duplicate": self.skipped_duplicate,
            "files_parsed": self.files_parsed,
            "contacts_new": self.contacts_new,
            "contacts_updated": self.contacts_updated,
            "contacts_unchanged": self.contacts_unchanged,
            "interactions_new": self.interactions_new,
            "history_changes": self.history_changes,
            "clusters_created": self.clusters_created,
            "clusters_updated": self.clusters_updated,
            "pending_invitations": self.pending_invitations,
            "embeddings_written": self.embeddings_written,
            "warnings": self.warnings,
        }
