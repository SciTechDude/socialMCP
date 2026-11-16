"""Hybrid search: FTS5 keyword + sqlite-vec semantic, fused with RRF.

Why fuse instead of picking one: "GTM background" is a semantic query that
keyword search misses entirely, while "Stripe" is a keyword query where vector
similarity returns everyone vaguely fintech. Reciprocal Rank Fusion needs no
score calibration between the two, which matters because BM25 and cosine
distance are not on comparable scales.

Structured filters (company, title, status, event, date range) are applied as
SQL predicates *before* fusion so they are exact, never approximate.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from ..config import Config
from .db import Database, serialize_f32
from .embeddings import EmbeddingError, embedding_text, make_embedder
from .normalize import slugify
from .vocabulary import breakdown as relationship_breakdown
from .vocabulary import describe_rows

log = logging.getLogger("socialmcp.search")

RRF_K = 60           # standard RRF damping constant
CANDIDATE_DEPTH = 200  # how deep each retriever goes before fusion


@dataclass(slots=True)
class SearchFilters:
    company: str | None = None
    title_contains: str | None = None
    status: str | None = None
    event: str | None = None
    connected_after: str | None = None
    connected_before: str | None = None
    # "Met" window: matches the connection date, or the invitation date for
    # people who never accepted. This is what an event-shaped question wants.
    met_after: str | None = None
    met_before: str | None = None
    has_note: bool | None = None
    source: str | None = None

    def where(self) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if self.company:
            clauses.append("c.company LIKE ?")
            params.append(f"%{self.company}%")
        if self.title_contains:
            clauses.append('(c."position" LIKE ? OR c.headline LIKE ?)')
            params += [f"%{self.title_contains}%"] * 2
        if self.status:
            clauses.append("c.status = ?")
            params.append(self.status)
        if self.source:
            clauses.append("c.source = ?")
            params.append(self.source)
        if self.connected_after:
            clauses.append("c.connected_on >= ?")
            params.append(self.connected_after[:10])
        if self.connected_before:
            clauses.append("c.connected_on <= ?")
            params.append(self.connected_before[:10])
        if self.met_after:
            clauses.append("COALESCE(c.connected_on, substr(c.invited_at, 1, 10)) >= ?")
            params.append(self.met_after[:10])
        if self.met_before:
            clauses.append("COALESCE(c.connected_on, substr(c.invited_at, 1, 10)) <= ?")
            params.append(self.met_before[:10])
        if self.has_note is True:
            clauses.append("(c.origin_note IS NOT NULL OR c.search_blob != '')")
        if self.has_note is False:
            clauses.append("c.origin_note IS NULL AND c.search_blob = ''")
        if self.event:
            clauses.append(
                "EXISTS (SELECT 1 FROM contact_event_clusters cec "
                "JOIN event_clusters ec ON ec.id = cec.cluster_id "
                "WHERE cec.contact_id = c.id AND (ec.slug = ? OR ec.name LIKE ? OR ec.slug LIKE ?))"
            )
            params += [slugify(self.event), f"%{self.event}%", f"%{slugify(self.event)}%"]
        return (" AND ".join(clauses) if clauses else "1=1"), params


@dataclass(slots=True)
class SearchResult:
    contacts: list[dict[str, Any]] = field(default_factory=list)
    mode_used: str = "filter"
    total_matching_filters: int = 0
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode_used": self.mode_used,
            "total_matching_filters": self.total_matching_filters,
            "returned": len(self.contacts),
            "relationships": relationship_breakdown(self.contacts),
            "contacts": self.contacts,
            "notes": self.notes,
        }


# Who approached whom, and what came of it. Derived in SQL so every tool
# reports the same five categories rather than each caller re-inferring them
# from (status, origin_direction) — a distinction a model should never have to
# reconstruct, and would eventually get wrong.
#
# 'unknown_invite' is honest, not a gap: LinkedIn's Invitations.csv only covers
# a recent window, so for older connections the archive simply does not say who
# sent the request.
# Raw, platform-neutral columns only. Naming happens in Python, per source, via
# core/vocabulary.py — so this query already works for a platform that does not
# exist yet, and adding one changes no SQL here.
CONTACT_COLUMNS = """
    c.id, c.source, c.full_name, c.company, c."position", c.headline, c.location,
    c.email, c.profile_url, c.status, c.origin_note, c.origin_direction,
    c.invite_lapsed_at, c.invited_at, c.connected_on, c.last_interaction_at,
    COALESCE(c.connected_on, substr(c.invited_at, 1, 10)) AS met_on
"""


def search_network(
    db: Database,
    config: Config,
    *,
    query: str | None = None,
    filters: SearchFilters | None = None,
    limit: int = 25,
    mode: str = "hybrid",
) -> SearchResult:
    filters = filters or SearchFilters()
    limit = max(1, min(limit, 200))
    where_sql, where_params = filters.where()
    result = SearchResult()

    result.total_matching_filters = (
        db.scalar(f"SELECT COUNT(*) FROM contacts c WHERE {where_sql}", where_params) or 0
    )

    text = (query or "").strip()
    if not text:
        # Pure structured browse: newest connections first.
        rows = db.query(
            f"""SELECT {CONTACT_COLUMNS} FROM contacts c WHERE {where_sql}
                ORDER BY COALESCE(c.connected_on, substr(c.invited_at, 1, 10)) DESC, c.full_name
                LIMIT ?""",
            [*where_params, limit],
        )
        result.contacts = [_decorate(db, dict(r)) for r in rows]
        result.mode_used = "filter"
        return result

    want_keyword = mode in {"hybrid", "keyword"}
    want_vector = mode in {"hybrid", "semantic"} and db.vec_enabled

    keyword_ranking = _keyword_search(db, text, where_sql, where_params) if want_keyword else []
    vector_ranking: list[int] = []
    if want_vector:
        try:
            vector_ranking = _vector_search(db, config, text, where_sql, where_params)
        except EmbeddingError as exc:
            result.notes.append(f"semantic search unavailable ({exc}); used keyword only")
            log.info("semantic search unavailable: %s", exc)
    elif mode in {"hybrid", "semantic"} and not db.vec_enabled:
        result.notes.append(
            "vector search is not enabled (install sqlite-vec and run `socialmcp reindex`); "
            "used keyword search"
        )

    if keyword_ranking and vector_ranking:
        result.mode_used = "hybrid"
    elif vector_ranking:
        result.mode_used = "semantic"
    elif keyword_ranking:
        result.mode_used = "keyword"
    else:
        result.mode_used = "fallback_like"

    ordered = _rrf([keyword_ranking, vector_ranking])

    if not ordered:
        # Last resort: substring match, so a typo'd or stop-word-only query
        # still returns something instead of a bare "no results".
        like = f"%{text}%"
        rows = db.query(
            f"""SELECT {CONTACT_COLUMNS} FROM contacts c
                WHERE {where_sql} AND (
                    c.full_name LIKE ? OR c.company LIKE ? OR c."position" LIKE ?
                    OR c.origin_note LIKE ? OR c.search_blob LIKE ?)
                ORDER BY COALESCE(c.connected_on, c.invited_at) DESC
                LIMIT ?""",
            [*where_params, like, like, like, like, like, limit],
        )
        result.contacts = [_decorate(db, dict(r)) for r in rows]
        return result

    ids = ordered[:limit]
    placeholders = ",".join("?" * len(ids))
    rows = {
        int(r["id"]): dict(r)
        for r in db.query(
            f"SELECT {CONTACT_COLUMNS} FROM contacts c WHERE c.id IN ({placeholders})", ids
        )
    }
    result.contacts = [_decorate(db, rows[i]) for i in ids if i in rows]
    return result


def _keyword_search(db: Database, text: str, where_sql: str, where_params: list[Any]) -> list[int]:
    match = _to_fts_query(text)
    if not match:
        return []
    try:
        rows = db.query(
            f"""
            SELECT c.id
            FROM contacts_fts f
            JOIN contacts c ON c.id = f.rowid
            WHERE contacts_fts MATCH ? AND {where_sql}
            ORDER BY bm25(contacts_fts, 8.0, 4.0, 4.0, 2.0, 6.0, 1.0)
            LIMIT ?
            """,
            [match, *where_params, CANDIDATE_DEPTH],
        )
    except Exception as exc:
        log.info("FTS query %r rejected (%s); falling back", match, exc)
        return []
    return [int(r["id"]) for r in rows]


def _vector_search(
    db: Database, config: Config, text: str, where_sql: str, where_params: list[Any]
) -> list[int]:
    embedder = make_embedder(config.embedding)
    if embedder is None:
        return []
    vector = embedder.embed([embedding_text({"full_name": text, "search_blob": text})])[0]
    rows = db.query(
        f"""
        SELECT c.id, v.distance
        FROM vec_contacts v
        JOIN contacts c ON c.id = v.contact_id
        WHERE v.embedding MATCH ? AND k = ? AND {where_sql}
        ORDER BY v.distance
        """,
        [serialize_f32(vector), CANDIDATE_DEPTH, *where_params],
    )
    return [int(r["id"]) for r in rows]


def _rrf(rankings: list[list[int]]) -> list[int]:
    """Reciprocal Rank Fusion: score = sum(1 / (k + rank)) across retrievers."""
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, contact_id in enumerate(ranking, start=1):
            scores[contact_id] = scores.get(contact_id, 0.0) + 1.0 / (RRF_K + rank)
    return [cid for cid, _ in sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))]


_FTS_TOKEN = re.compile(r"[0-9A-Za-z_]+")


def _to_fts_query(text: str) -> str:
    """Turn free text into a safe FTS5 MATCH expression.

    Every token is quoted (so '/', '-', 'AND' and other FTS operators in user
    text cannot produce a syntax error) and prefix-matched, then OR-ed for
    recall — bm25 does the ranking.
    """
    tokens = [t for t in _FTS_TOKEN.findall(text) if len(t) > 1]
    if not tokens:
        return ""
    return " OR ".join(f'"{t}"*' for t in tokens[:16])


def _decorate(db: Database, contact: dict[str, Any]) -> dict[str, Any]:
    """Attach the context a human actually wants to see with a search hit."""
    # Platform-specific naming is applied here, once, from the row's source.
    describe_rows([contact])
    contact_id = int(contact["id"])
    contact["events"] = [
        row["name"]
        for row in db.query(
            """
            SELECT ec.name FROM event_clusters ec
            JOIN contact_event_clusters cec ON cec.cluster_id = ec.id
            WHERE cec.contact_id = ? ORDER BY ec.start_date
            """,
            (contact_id,),
        )
    ]
    contact["notes"] = [
        {"logged_at": row["occurred_at"], "note": row["body"]}
        for row in db.query(
            "SELECT occurred_at, body FROM interactions WHERE contact_id = ? AND kind = 'note' "
            "ORDER BY occurred_at DESC LIMIT 5",
            (contact_id,),
        )
    ]
    contact["job_changes"] = [
        {"field": r["field"], "from": r["old_value"], "to": r["new_value"], "observed": r["changed_at"]}
        for r in db.query(
            "SELECT field, old_value, new_value, changed_at FROM contact_history "
            "WHERE contact_id = ? ORDER BY changed_at DESC LIMIT 5",
            (contact_id,),
        )
    ]
    return contact


def get_contact_detail(db: Database, contact_id: int, *, interaction_limit: int = 25) -> dict[str, Any] | None:
    row = db.query_one(f"SELECT {CONTACT_COLUMNS} FROM contacts c WHERE c.id = ?", (contact_id,))
    if row is None:
        return None
    contact = _decorate(db, dict(row))
    contact["interactions"] = [
        dict(r)
        for r in db.query(
            """
            SELECT kind, direction, occurred_at, subject, body, thread_id
            FROM interactions WHERE contact_id = ?
            ORDER BY occurred_at DESC NULLS LAST LIMIT ?
            """,
            (contact_id, interaction_limit),
        )
    ]
    return contact


def resolve_contact(db: Database, ref: str | int) -> dict[str, Any] | None:
    """Find one contact by id, profile URL, or name — for note-logging by name."""
    if isinstance(ref, int) or (isinstance(ref, str) and str(ref).isdigit()):
        row = db.query_one("SELECT * FROM contacts WHERE id = ?", (int(ref),))
        return dict(row) if row else None

    text = str(ref).strip()
    rows = db.query(
        """
        SELECT * FROM contacts
        WHERE profile_url = ? OR source_key = ? OR lower(full_name) = lower(?)
        ORDER BY (status = 'connected') DESC, updated_at DESC
        """,
        (text, text, text),
    )
    if not rows:
        rows = db.query(
            "SELECT * FROM contacts WHERE full_name LIKE ? ORDER BY (status='connected') DESC LIMIT 5",
            (f"%{text}%",),
        )
    if len(rows) == 1:
        return dict(rows[0])
    if len(rows) > 1:
        raise AmbiguousContactError(
            [
                {
                    "id": int(r["id"]),
                    "full_name": r["full_name"],
                    "company": r["company"],
                    "position": r["position"],
                }
                for r in rows[:10]
            ]
        )
    return None


class AmbiguousContactError(ValueError):
    """Raised when a name matches several contacts; carries the candidates."""

    def __init__(self, candidates: list[dict[str, Any]]):
        self.candidates = candidates
        names = ", ".join(f"{c['full_name']} (id {c['id']})" for c in candidates)
        super().__init__(f"several contacts match: {names}")
