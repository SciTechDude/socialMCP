"""MCP layer smoke tests: tools are registered and return JSON-able payloads."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from socialmcp.core.harness import Harness
from socialmcp.core.ingest import ingest_path
from socialmcp.mcp_server import server as srv

EXPECTED_TOOLS = {
    "search_network",
    "get_contact",
    "get_event_cluster",
    "list_event_clusters",
    "list_pending_invitations",
    "network_stats",
    "log_contact_note",
    "rename_event_cluster",
    "trigger_archive_ingest",
    "reindex_embeddings",
    "get_network_briefing",
}


@pytest.fixture
def server_state(db, config, linkedin_dir: Path):
    """Wire the module-level state the way `lifespan` does, without stdio."""
    ingest_path(db, config, linkedin_dir)
    srv._state = srv.ServerState(config=config, db=db, harness=Harness(db, config))
    yield srv._state
    srv._state = None


def test_all_tools_are_registered():
    tools = asyncio.run(srv.mcp.list_tools())
    assert {t.name for t in tools} >= EXPECTED_TOOLS
    # Every tool needs a description: it is the model's only usage documentation.
    assert all(t.description for t in tools)


def test_resources_are_registered():
    uris = {str(r.uri) for r in asyncio.run(srv.mcp.list_resources())}
    assert {"socialmcp://briefing", "socialmcp://stats", "socialmcp://clusters"} <= uris


def test_search_tool_returns_serializable_results(server_state):
    result = srv.search_network(query="Seattle AI Week", mode="keyword", limit=5)
    json.dumps(result)  # must survive MCP serialization
    assert result["returned"] > 0
    assert {"full_name", "origin_note", "events"} <= set(result["contacts"][0])


def test_event_cluster_tool_lists_people_and_followups(server_state):
    clusters = srv.list_event_clusters()["clusters"]
    assert clusters and clusters[0]["name_is_placeholder"] is True

    detail = srv.get_event_cluster(clusters[0]["slug"])
    assert detail["cluster"]["contact_count"] == 7
    assert len(detail["contacts"]) == 7
    assert "Marcus Webb" in detail["not_followed_up"]


def test_note_tool_round_trips_by_name(server_state):
    written = srv.log_contact_note("Marcus Webb", "Wants early access to the eval harness.")
    assert written["full_name"] == "Marcus Webb"

    contact = srv.get_contact("Marcus Webb")
    assert contact["notes"][0]["note"].startswith("Wants early access")


def test_tools_return_errors_instead_of_raising(server_state):
    assert srv.get_contact("Nobody At All")["error"] == "not_found"
    assert srv.log_contact_note("Nobody At All", "hi")["error"] == "not_found"
    assert "error" in srv.rename_event_cluster("no-such-cluster", "X")
    missing = srv.get_event_cluster("not-an-event")
    assert missing["error"] == "no_matching_cluster"
    assert missing["known_clusters"], "should offer the clusters that do exist"


def test_rename_tool_pins_the_event_name(server_state):
    slug = srv.list_event_clusters()["clusters"][0]["slug"]
    renamed = srv.rename_event_cluster(slug, "Seattle AI Week 2025")
    assert renamed["name"] == "Seattle AI Week 2025"
    assert srv.get_event_cluster("seattle-ai-week-2025")["cluster"]["name_is_placeholder"] is False


def test_trigger_ingest_reports_an_empty_drop_zone(server_state):
    result = srv.trigger_archive_ingest()
    assert result["ingested"] == []
    assert "linkedin" in result["hint"]


def test_briefing_tool_is_serializable(server_state):
    briefing = srv.get_network_briefing(acknowledge=False)
    json.dumps(briefing, default=str)
    assert briefing["stats"]["contacts"] == 12
    assert briefing["pending_invitations"][0]["full_name"] == "Rafael Santos"
