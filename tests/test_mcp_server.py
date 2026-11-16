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

    # Two distinct follow-up problems, reported separately. Marcus is a
    # first-degree connection who has never been messaged; Rafael and Yuki are
    # not connected at all. Flattening these into one list is the bug this
    # structure exists to prevent.
    follow_up = detail["follow_up_needed"]
    assert "Marcus Webb" in follow_up["connected_but_never_messaged"]
    assert "Marcus Webb" not in follow_up["invite_waiting_on_them"]
    assert "Marcus Webb" not in follow_up["invite_waiting_on_you"]
    # And the outstanding invitations are split by who must act: Rafael has not
    # answered the user, while Yuki is waiting on the user to accept.
    assert follow_up["invite_waiting_on_them"] == ["Rafael Santos"]
    assert follow_up["invite_waiting_on_you"] == ["Yuki Tanaka"]
    assert all(
        name in detail["first_degree_connections"]
        for name in follow_up["connected_but_never_messaged"]
    )


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
    assert by_name["Rafael Santos"]["how"] == "i_invited_not_yet_accepted"
    assert by_name["Yuki Tanaka"]["how"] == "they_invited_i_have_not_accepted_yet"
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
    assert "Rafael Santos" in groups["i_invited_not_yet_accepted"]
    assert "Yuki Tanaka" in groups["they_invited_i_have_not_accepted_yet"]
    assert "Priya Raghavan" in groups["i_invited_they_accepted"]

    counts = detail["relationships"]["counts"]
    assert sum(counts.values()) == detail["returned"]
    # The legend only explains categories that are actually present.
    assert set(detail["relationships"]["legend"]) == set(counts)
    assert "unknown" not in counts or counts["unknown"] == 0


def test_degree_distinguishes_connected_from_merely_met(server_state):
    """Connection degree and follow-up state are different facts.

    Conflating them is how a reader concludes they are not connected to someone
    they are in fact connected to but have simply never messaged.
    """
    digest = {p["name"]: p for p in srv.get_network_digest()["people"]}

    # In Connections.csv -> 1st, whether or not they have ever been spoken to.
    assert digest["Priya Raghavan"]["degree"] == "1st"
    assert digest["Marcus Webb"]["degree"] == "1st"
    assert digest["Marcus Webb"]["followed_up"] is False      # 1st but never contacted

    # Absent from Connections.csv -> 2nd or further; the export cannot say which.
    assert digest["Rafael Santos"]["degree"] == "2nd+"
    assert digest["Yuki Tanaka"]["degree"] == "2nd+"

    cluster = srv.get_event_cluster(srv.list_event_clusters()["clusters"][0]["slug"])
    assert "Priya Raghavan" in cluster["first_degree_connections"]
    assert set(cluster["not_connected_yet"]) == {"Rafael Santos", "Yuki Tanaka"}
    assert len(cluster["first_degree_connections"]) + len(cluster["not_connected_yet"]) == 9


def test_pending_invitations_say_who_must_act(server_state):
    """"Pending" alone reads as if the user is the blocker in both directions.

    An invitation the user sent cannot be progressed from this side; one the
    user received can be cleared today. The tool groups them accordingly
    instead of returning a single list.
    """
    both = srv.list_pending_invitations()

    assert both["waiting_on_them"]["count"] == 1
    assert both["waiting_on_them"]["invitations"][0]["full_name"] == "Rafael Santos"
    assert "not accepted" in both["waiting_on_them"]["meaning"]

    assert both["waiting_on_you"]["count"] == 1
    assert both["waiting_on_you"]["invitations"][0]["full_name"] == "Yuki Tanaka"
    assert "accepting or declining" in both["waiting_on_you"]["action"]

    # Each invitation also carries the answer on itself, not only in its group.
    assert both["waiting_on_them"]["invitations"][0]["waiting_on"] == "them"
    assert both["waiting_on_you"]["invitations"][0]["waiting_on"] == "you"

    # Either direction can be requested alone.
    assert set(srv.list_pending_invitations(direction="sent")) == {"waiting_on_them"}
    assert set(srv.list_pending_invitations(direction="received")) == {"waiting_on_you"}


def test_completed_and_lapsed_ties_have_nobody_waiting(server_state):
    """`waiting_on` is absent when there is no outstanding request at all."""
    digest = {p["name"]: p for p in srv.get_network_digest()["people"]}
    assert "waiting_on" not in digest["Priya Raghavan"]      # already connected
    assert digest["Rafael Santos"]["waiting_on"] == "them"
    assert digest["Yuki Tanaka"]["waiting_on"] == "you"


def test_capabilities_tool_is_generic_and_honest(server_state):
    """The helper a user reaches for first, so it must not mislead.

    Capabilities are described as shapes of question, never as canned prompts
    naming a real event or person — wording that would read as someone else's
    network. The limits are part of the payload because a user who believes
    this reads live data will misinterpret an empty result.
    """
    payload = srv.list_capabilities()

    assert len(payload["capabilities"]) >= 8
    assert payload["limits"], "limits must ship with the capabilities"

    for capability in payload["capabilities"]:
        assert capability["title"] and capability["description"]
        assert capability["ask_like"], f"{capability['key']} has no example phrasing"
        assert capability["tools"], f"{capability['key']} names no tool"

    # Every named tool really exists, so the helper cannot promise a missing one.
    advertised = {t for c in payload["capabilities"] for t in c["tools"]}
    registered = {t.name for t in resolve(srv.mcp.list_tools())}
    assert advertised <= registered, advertised - registered

    # No specific event, person or company names from any one network.
    blob = json.dumps(payload).lower()
    for leaked in ("seattle", "linkedin.com", "priya", "marcus webb", "surf"):
        assert leaked not in blob, f"capability text mentions {leaked!r}"

    # The scraping boundary is stated, not implied.
    assert any("never" in limit.lower() and "log" in limit.lower() for limit in payload["limits"])

    assert "limits" not in srv.list_capabilities(include_limits=False)


def test_enrich_contact_files_a_sourced_finding(server_state):
    """Public lookups are stored with provenance, separate from the user's own notes."""
    looked_up = srv.enrich_contact(
        contact="Marcus Webb",
        finding="Cascade Robotics builds warehouse automation; raised a Series A.",
        source_url="https://example.com/news/cascade-robotics",
    )
    assert looked_up["full_name"] == "Marcus Webb"
    assert looked_up["recorded_as"] == "looked up · example.com"
    assert looked_up["unsourced"] is False

    srv.log_contact_note("Marcus Webb", "My read: worth a warm intro in Q1.")

    notes = srv.get_contact("Marcus Webb")["notes"]
    by_text = {n["note"].split("\n")[0]: n for n in notes}

    own = by_text["My read: worth a warm intro in Q1."]
    assert own["source"] is None, "the user's own note must not look looked-up"
    found = by_text["Cascade Robotics builds warehouse automation; raised a Series A."]
    assert found["source"] == "looked up · example.com"
    assert "https://example.com/news/cascade-robotics" in found["note"]

    # Both are searchable.
    hit = srv.search_network(query="warehouse automation", mode="keyword", limit=3)
    assert "Marcus Webb" in [c["full_name"] for c in hit["contacts"]]


def test_enrich_contact_flags_an_unsourced_finding(server_state):
    result = srv.enrich_contact(contact="Marcus Webb", finding="Heard they are hiring.")
    assert result["unsourced"] is True
    assert result["recorded_as"] == "looked up"
    assert srv.enrich_contact(contact="Marcus Webb", finding="   ")["error"]
    assert srv.enrich_contact(contact="Nobody At All", finding="x")["error"] == "not_found"
