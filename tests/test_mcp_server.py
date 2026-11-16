"""MCP layer smoke tests: tools are registered and return JSON-able payloads."""

from __future__ import annotations

import asyncio
import inspect
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
    "get_network_digest",
}


@pytest.fixture
def server_state(db, config, linkedin_dir: Path):
    """Wire the module-level state the way `lifespan` does, without stdio."""
    ingest_path(db, config, linkedin_dir)
    srv._state = srv.ServerState(config=config, db=db, harness=Harness(db, config))
    yield srv._state
    srv._state = None


def resolve(value):
    """mcp 1.x returns a coroutine from list_tools/list_resources; 2.x is sync."""
    return asyncio.run(value) if inspect.isawaitable(value) else value


def test_all_tools_are_registered():
    tools = resolve(srv.mcp.list_tools())
    assert {t.name for t in tools} >= EXPECTED_TOOLS
    # Every tool needs a description: it is the model's only usage documentation.
    assert all(t.description for t in tools)


def test_resources_are_registered():
    uris = {str(r.uri) for r in resolve(srv.mcp.list_resources())}
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
    assert detail["cluster"]["contact_count"] == 9
    assert len(detail["contacts"]) == 9
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


def test_digest_returns_a_compact_slate_for_the_model_to_rank(server_state):
    """The alternative to similarity ranking: hand the model the whole field.

    Cheap enough to be practical (~20 tokens per person) and complete, so the
    model's own judgement decides who matters instead of a cosine score.
    """
    digest = srv.get_network_digest()

    assert digest["total_matching"] == 12
    assert digest["returned"] == 12
    assert digest["truncated"] is False

    by_name = {p["name"]: p for p in digest["people"]}
    priya = by_name["Priya Raghavan"]
    assert priya["role"] == "Head of GTM @ Northwind AI"
    assert "Seattle AI Week" in priya["how_we_met"]
    assert priya["met"] == "2025-10-13"
    # Every person carries an explicit category, so the model never has to
    # infer who approached whom from two separate fields.
    assert by_name["Rafael Santos"]["how"] == "i_invited_awaiting_them"
    assert by_name["Yuki Tanaka"]["how"] == "they_invited_awaiting_me"
    assert by_name["Priya Raghavan"]["how"] == "i_invited_they_accepted"
    assert digest["relationships"]["counts"]["i_invited_they_accepted"] >= 1
    assert "legend" in digest["relationships"]

    # Filters compose, and notes can be dropped to save tokens.
    scoped = srv.get_network_digest(status="connected", include_notes=False, limit=3)
    assert scoped["returned"] == 3
    assert scoped["truncated"] is True
    assert all("how_we_met" not in p for p in scoped["people"])


def test_event_cluster_groups_people_by_who_approached_whom(server_state):
    """The tool categorizes; the model should not have to reconstruct it."""
    slug = srv.list_event_clusters()["clusters"][0]["slug"]
    detail = srv.get_event_cluster(slug)

    groups = detail["names_by_relationship"]
    assert "Rafael Santos" in groups["i_invited_awaiting_them"]
    assert "Yuki Tanaka" in groups["they_invited_awaiting_me"]
    assert "Priya Raghavan" in groups["i_invited_they_accepted"]

    counts = detail["relationships"]["counts"]
    assert sum(counts.values()) == detail["returned"]
    # The legend only explains categories that are actually present.
    assert set(detail["relationships"]["legend"]) == set(counts)
    assert "unknown" not in counts or counts["unknown"] == 0
