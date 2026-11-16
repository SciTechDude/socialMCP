"""The MCP server: SocialMCP's entire user interface.

There is no SPA, no REST API and no listening socket — the server speaks MCP
over stdio to whatever MCP client launched it, and that client's chat window
*is* the database view.

Tool design rules followed here:
  * Every tool returns plain JSON-able dicts, never prose. The model narrates.
  * Errors come back as `{"error": ..., "hint": ...}` instead of exceptions, so
    a wrong argument produces a self-correcting turn rather than a dead tool.
  * Anything ambiguous (two people named Chris) returns the candidate list and
    asks the model to disambiguate instead of guessing.
  * Nothing writes outside the local SQLite file; nothing ever fetches a social
    network over the wire.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# The MCP Python SDK renamed FastMCP to MCPServer in 2.0. Everything this
# server uses — the constructor's name/instructions/lifespan, the .tool() and
# .resource() decorators, and .run()'s stdio default — is identical across the
# two, so supporting both is a two-line import rather than forcing every user
# onto one SDK major. Caught by installing from a clean venv, where
# `mcp>=1.2.0` resolves to 2.x and v1 code fails at import.
try:  # mcp >= 2.0
    from mcp.server.mcpserver import MCPServer as _MCPServerImpl
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _MCPServerImpl

from ..adapters import available_adapters
from ..config import Config, load_config
from ..core import clustering
from ..core.db import Database, open_database
from ..core.harness import Harness
from ..core.ingest import IngestError, add_note, ingest_path, refresh_embeddings
from ..core.search import (
    RELATIONSHIP_SQL,
    AmbiguousContactError,
    SearchFilters,
    get_contact_detail,
    relationship_breakdown,
    resolve_contact,
)
from ..core.search import (
    search_network as run_search,
)

log = logging.getLogger("socialmcp.server")

INSTRUCTIONS = """\
SocialMCP is a local-first personal CRM built from the user's own social data
exports (LinkedIn today). All data is in a local SQLite file; nothing is scraped.

How to work with it:
* `search_network` is the main entry point. It does hybrid semantic + keyword
  search with structured filters, so prefer one call with filters over several.
* `origin_note` on a contact is the message the user sent with the connection
  invite — usually the venue and date they met ("Great meeting you at Seattle AI
  Week"). Treat it as ground truth about how they met.
* Event clusters are auto-detected bursts of connections. New ones have
  placeholder names like "12-15 Oct 2025 burst (17 connections)" — if the user
  tells you what that event was, call `rename_event_cluster`.
* Contacts with status 'pending' are invitations the user sent that were never
  accepted. They are a follow-up list, not connections.
* Call `get_network_briefing` at the start of a session to learn what the
  background harness ingested since last time.
"""


@dataclass
class ServerState:
    config: Config
    db: Database
    harness: Harness


_state: ServerState | None = None


def state() -> ServerState:
    if _state is None:  # pragma: no cover - guarded by lifespan
        raise RuntimeError("server state not initialized")
    return _state


@asynccontextmanager
async def lifespan(_server: _MCPServerImpl) -> AsyncIterator[ServerState]:
    """Open the DB and start the background harness for the session's lifetime."""
    global _state
    config = load_config()
    config.ensure_dirs()
    db = open_database(config)
    harness = Harness(db, config)
    _state = ServerState(config=config, db=db, harness=harness)
    try:
        harness.start()
    except OSError as exc:
        log.warning("harness could not start (%s); tools still work on demand", exc)
    log.info("socialmcp ready: db=%s drop=%s", config.db_path, config.drop_dir)
    try:
        yield _state
    finally:
        harness.stop()
        db.close()
        _state = None


mcp = _MCPServerImpl("socialmcp", instructions=INSTRUCTIONS, lifespan=lifespan)


# ---------------------------------------------------------------------------
# tools: querying
# ---------------------------------------------------------------------------


@mcp.tool()
def search_network(
    query: str = "",
    company: str | None = None,
    title_contains: str | None = None,
    status: str | None = None,
    event: str | None = None,
    connected_after: str | None = None,
    connected_before: str | None = None,
    met_after: str | None = None,
    met_before: str | None = None,
    has_note: bool | None = None,
    mode: str = "hybrid",
    limit: int = 25,
) -> dict[str, Any]:
    """Search the user's network by meaning and/or exact filters.

    Args:
        query: Free text. Matched semantically (embeddings) and by keyword
            against names, companies, titles, the connection-invite note, and
            logged notes. Leave empty to browse purely by filters.
        company: Substring match on current company.
        title_contains: Substring match on job title or headline.
        status: 'connected' | 'pending' | 'received' | 'withdrawn'.
        event: Event cluster name or slug, e.g. 'seattle-ai-week'.
        connected_after: 'YYYY-MM-DD' inclusive lower bound on the date the
            connection was accepted.
        connected_before: 'YYYY-MM-DD' inclusive upper bound on the same.
        met_after: 'YYYY-MM-DD' lower bound on when they were MET — the
            connection date, or the invitation date for people who never
            accepted. Prefer this over connected_after for event-shaped
            questions ("who did I meet at the conference in late October"),
            because it includes the invitations that went unanswered, who are
            usually the people most worth following up with.
        met_before: 'YYYY-MM-DD' upper bound on the same.
        has_note: True to return only contacts that have an origin note or logged notes.
        mode: 'hybrid' (default), 'keyword', or 'semantic'.
        limit: Max contacts to return (1-200).
    """
    st = state()
    filters = SearchFilters(
        company=company,
        title_contains=title_contains,
        status=status,
        event=event,
        connected_after=connected_after,
        connected_before=connected_before,
        met_after=met_after,
        met_before=met_before,
        has_note=has_note,
    )
    result = run_search(st.db, st.config, query=query, filters=filters, limit=limit, mode=mode)
    return result.as_dict()


@mcp.tool()
def get_contact(contact: str, interaction_limit: int = 25) -> dict[str, Any]:
    """Get one contact's full local record: notes, invite message, DMs, job changes.

    Args:
        contact: Contact id, profile URL, or full name.
        interaction_limit: How many interactions to include, newest first.
    """
    st = state()
    try:
        row = resolve_contact(st.db, contact)
    except AmbiguousContactError as exc:
        return {
            "error": "ambiguous_contact",
            "candidates": exc.candidates,
            "hint": "Call get_contact again with the numeric id of the right person.",
        }
    if row is None:
        return {"error": "not_found", "contact": contact}
    return get_contact_detail(st.db, int(row["id"]), interaction_limit=interaction_limit) or {
        "error": "not_found",
        "contact": contact,
    }


@mcp.tool()
def get_event_cluster(event: str, limit: int = 100) -> dict[str, Any]:
    """Pull up everyone met during one event window, with their invite notes.

    Includes people whose invitation was never accepted — they were met at the
    event too, and are listed in `invitation_never_accepted`.

    Args:
        event: Cluster name, slug, numeric id, or any date inside the window
            ('seattle-ai-week', '2025-10-14', or 12).
        limit: Max contacts to return.
    """
    st = state()
    cluster = clustering.resolve_cluster(st.db, event)
    if cluster is None:
        known = [
            dict(r)
            for r in st.db.query(
                "SELECT id, slug, name, start_date, end_date, contact_count "
                "FROM event_clusters ORDER BY end_date DESC LIMIT 20"
            )
        ]
        return {"error": "no_matching_cluster", "event": event, "known_clusters": known}

    rows = st.db.query(
        f"""
        SELECT c.id, c.full_name, c.company, c."position", c.profile_url, c.status,
               c.origin_note, c.origin_direction, c.connected_on, c.invited_at,
               c.last_interaction_at,
               COALESCE(c.connected_on, substr(c.invited_at, 1, 10)) AS met_on,
               ({RELATIONSHIP_SQL}) AS how,
               EXISTS (SELECT 1 FROM interactions i
                       WHERE i.contact_id = c.id AND i.kind IN ('message','note')) AS followed_up
        FROM contacts c
        JOIN contact_event_clusters cec ON cec.contact_id = c.id
        WHERE cec.cluster_id = ?
        ORDER BY met_on, c.full_name
        LIMIT ?
        """,
        (cluster["id"], max(1, min(limit, 500))),
    )
    contacts = [dict(r) for r in rows]
    never_accepted = [c["full_name"] for c in contacts if c["status"] in {"pending", "received"}]
    by_category: dict[str, list[str]] = {}
    for contact in contacts:
        by_category.setdefault(contact["how"], []).append(contact["full_name"])
    return {
        "cluster": {
            "id": int(cluster["id"]),
            "slug": cluster["slug"],
            "name": cluster["name"],
            "start_date": cluster["start_date"],
            "end_date": cluster["end_date"],
            "contact_count": int(cluster["contact_count"]),
            "name_is_placeholder": not bool(cluster["renamed_by_user"]),
        },
        "returned": len(contacts),
        "contacts": contacts,
        "not_followed_up": [c["full_name"] for c in contacts if not c["followed_up"]],
        "invitation_never_accepted": never_accepted,
        "relationships": relationship_breakdown(contacts),
        "names_by_relationship": by_category,
    }


@mcp.tool()
def list_event_clusters(limit: int = 25) -> dict[str, Any]:
    """List detected connection bursts (candidate events), newest first."""
    st = state()
    rows = st.db.query(
        """
        SELECT id, slug, name, start_date, end_date, contact_count, renamed_by_user, notes
        FROM event_clusters ORDER BY end_date DESC LIMIT ?
        """,
        (max(1, min(limit, 200)),),
    )
    return {
        "clusters": [
            dict(r) | {"name_is_placeholder": not bool(r["renamed_by_user"])} for r in rows
        ]
    }


@mcp.tool()
def list_pending_invitations(min_days_waiting: int = 0, limit: int = 50) -> dict[str, Any]:
    """List invitations the user sent that were never accepted — the follow-up list.

    Args:
        min_days_waiting: Only include invites older than this many days.
        limit: Max rows.
    """
    st = state()
    rows = st.db.query(
        "SELECT * FROM v_pending_invitations WHERE COALESCE(days_waiting, 0) >= ? LIMIT ?",
        (max(0, min_days_waiting), max(1, min(limit, 500))),
    )
    return {"count": len(rows), "pending": [dict(r) for r in rows]}


@mcp.tool()
def get_network_digest(
    company: str | None = None,
    event: str | None = None,
    status: str | None = None,
    met_after: str | None = None,
    met_before: str | None = None,
    include_notes: bool = True,
    limit: int = 400,
) -> dict[str, Any]:
    """Get a compact one-line-per-person slate to reason over yourself.

    Use this instead of `search_network` when ranking needs YOUR judgement
    rather than a similarity score: open-ended questions ("who should I
    reconnect with before the holidays"), multi-criteria trade-offs, or
    grouping and summarizing a whole segment. `search_network` returns its best
    guesses; this returns the field so you can decide.

    Roughly 20 tokens per person, so a few hundred people cost a few thousand
    tokens. Narrow with filters when you can — and prefer `search_network` for
    a specific lookup, since it is ~20x cheaper.

    Args:
        company: Substring match on company.
        event: Event cluster name or slug.
        status: 'connected' | 'pending' | 'received'.
        met_after: 'YYYY-MM-DD' lower bound on when they were met.
        met_before: 'YYYY-MM-DD' upper bound on when they were met.
        include_notes: Include the connection-invite note (how you met). Costs
            tokens but is usually the most useful column.
        limit: Max people (1-2000).
    """
    st = state()
    filters = SearchFilters(
        company=company, event=event, status=status, met_after=met_after, met_before=met_before
    )
    where_sql, params = filters.where()
    limit = max(1, min(limit, 2000))

    total = st.db.scalar(f"SELECT COUNT(*) FROM contacts c WHERE {where_sql}", params) or 0
    rows = st.db.query(
        f"""
        SELECT c.id, c.full_name, c.company, c."position", c.status, c.origin_note,
               COALESCE(c.connected_on, substr(c.invited_at, 1, 10)) AS met_on,
               ({RELATIONSHIP_SQL}) AS how,
               (SELECT ec.name FROM event_clusters ec
                JOIN contact_event_clusters cec ON cec.cluster_id = ec.id
                WHERE cec.contact_id = c.id ORDER BY ec.start_date LIMIT 1) AS event,
               EXISTS (SELECT 1 FROM interactions i
                       WHERE i.contact_id = c.id AND i.kind IN ('message','note')) AS followed_up
        FROM contacts c WHERE {where_sql}
        ORDER BY met_on DESC, c.full_name
        LIMIT ?
        """,
        [*params, limit],
    )

    people = []
    for row in rows:
        entry: dict[str, Any] = {
            "id": int(row["id"]),
            "name": row["full_name"],
            "role": " @ ".join(p for p in (row["position"], row["company"]) if p) or None,
            "met": row["met_on"],
            "how": row["how"],
            "followed_up": bool(row["followed_up"]),
        }
        if row["event"]:
            entry["event"] = row["event"]
        if include_notes and row["origin_note"]:
            entry["how_we_met"] = row["origin_note"][:200]
        people.append({k: v for k, v in entry.items() if v is not None})

    return {
        "total_matching": total,
        "returned": len(people),
        "truncated": total > len(people),
        "relationships": relationship_breakdown(people),
        "people": people,
    }


@mcp.tool()
def network_stats() -> dict[str, Any]:
    """Counts and health of the local CRM: contacts, sources, clusters, index state."""
    st = state()
    return st.db.stats() | {
        "adapters_available": sorted(available_adapters()),
        "drop_folder": str(st.config.drop_dir),
        "embedding_provider": st.config.embedding.provider,
    }


# ---------------------------------------------------------------------------
# tools: writing
# ---------------------------------------------------------------------------


@mcp.tool()
def log_contact_note(contact: str, note: str) -> dict[str, Any]:
    """Append a timestamped private note to a contact, stored locally.

    Args:
        contact: Contact id, profile URL, or full name.
        note: The note text, e.g. 'Intro'd me to their CTO; wants a demo in Q1.'
    """
    st = state()
    try:
        row = resolve_contact(st.db, contact)
    except AmbiguousContactError as exc:
        return {
            "error": "ambiguous_contact",
            "candidates": exc.candidates,
            "hint": "Call log_contact_note again with the numeric id.",
        }
    if row is None:
        return {"error": "not_found", "contact": contact}
    try:
        result = add_note(st.db, int(row["id"]), note)
    except ValueError as exc:
        return {"error": str(exc)}
    return result | {"full_name": row["full_name"]}


@mcp.tool()
def rename_event_cluster(event: str, new_name: str) -> dict[str, Any]:
    """Give an auto-detected burst its real event name, e.g. 'Seattle AI Week 2025'.

    Args:
        event: Current cluster name, slug, id, or a date inside its window.
        new_name: The human name to pin. Clustering will never overwrite it.
    """
    st = state()
    try:
        return clustering.rename_cluster(st.db, event, new_name)
    except ValueError as exc:
        return {"error": str(exc)}


@mcp.tool()
def trigger_archive_ingest(path: str | None = None, source: str | None = None) -> dict[str, Any]:
    """Ingest a social data export: unzip, parse, dedupe, upsert, re-cluster.

    Args:
        path: Path to a .zip (or extracted folder). Omit to process everything
            currently sitting in the configured drop folder.
        source: Force an adapter, e.g. 'linkedin'. Omit to auto-detect.
    """
    st = state()
    if path:
        try:
            stats = ingest_path(st.db, st.config, Path(path).expanduser(), source=source)
        except (IngestError, OSError, KeyError) as exc:
            return {"error": f"{type(exc).__name__}: {exc}", "path": path}
        return stats.as_dict()

    results = st.harness.scan_now(reason="trigger_archive_ingest")
    if not results:
        return {
            "ingested": [],
            "message": f"No .zip archives found under {st.config.drop_dir}",
            "hint": (
                f"Drop a LinkedIn export .zip in {st.config.drop_dir / 'linkedin'}/ "
                "(or any subfolder of the drop zone), or pass an explicit path."
            ),
        }
    return {"ingested": [r.as_dict() for r in results]}


@mcp.tool()
def reindex_embeddings(limit: int | None = None) -> dict[str, Any]:
    """Re-embed contacts whose text changed (needed after switching models).

    Args:
        limit: Cap how many contacts to embed in this call.
    """
    st = state()
    written = refresh_embeddings(st.db, st.config, limit=limit)
    return {
        "embedded": written,
        "vector_search_enabled": st.db.vec_enabled,
        "pending": st.db.scalar("SELECT COUNT(*) FROM contacts WHERE embedding_dirty = 1"),
        "provider": st.config.embedding.provider,
    }


# ---------------------------------------------------------------------------
# tools + resources: harness state / context injection
# ---------------------------------------------------------------------------


@mcp.tool()
def get_network_briefing(acknowledge: bool = True) -> dict[str, Any]:
    """What the background harness did since last time, plus current follow-ups.

    Call this at the start of a session to preload network state.

    Args:
        acknowledge: Mark the reported harness events as seen (default True).
    """
    st = state()
    return st.harness.briefing(acknowledge=acknowledge)


@mcp.resource("socialmcp://briefing", mime_type="application/json")
def briefing_resource() -> dict[str, Any]:
    """Current network state: harness activity, pending follow-ups, recent events."""
    # Read-only: a resource read never acknowledges events.
    return state().harness.briefing(acknowledge=False)


@mcp.resource("socialmcp://stats", mime_type="application/json")
def stats_resource() -> dict[str, Any]:
    """Local CRM counts and index health."""
    return state().db.stats()


@mcp.resource("socialmcp://clusters", mime_type="application/json")
def clusters_resource() -> dict[str, Any]:
    """All detected event clusters."""
    st = state()
    return {
        "clusters": [
            dict(r)
            for r in st.db.query(
                "SELECT id, slug, name, start_date, end_date, contact_count, renamed_by_user "
                "FROM event_clusters ORDER BY end_date DESC"
            )
        ]
    }


@mcp.resource("socialmcp://adapters", mime_type="application/json")
def adapters_resource() -> dict[str, Any]:
    """Which export formats this install can ingest."""
    return {
        "adapters": [
            {
                "source": adapter.source,
                "display_name": adapter.display_name,
                "signature_files": list(adapter.signature_files),
            }
            for adapter in available_adapters().values()
        ]
    }


def main() -> None:
    """stdio entry point used by MCP clients (`socialmcp-server`)."""
    logging.basicConfig(
        level=os.environ.get("SOCIALMCP_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        # stdout is the MCP transport — logs must go to stderr or they corrupt it.
        stream=__import__("sys").stderr,
    )
    mcp.run()


if __name__ == "__main__":  # pragma: no cover
    main()
