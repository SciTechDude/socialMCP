"""End-to-end MCP protocol test: spawn the real server and talk to it over stdio.

`test_mcp_server.py` calls the tool functions directly, which does not prove the
server actually works for a client: schemas must be generated, the lifespan must
open the database, results must survive JSON serialization, and logging must not
contaminate stdout (stdout IS the transport — a stray print corrupts the stream
and the client sees a parse error instead of a result).

This test launches `python -m socialmcp.cli serve` as a subprocess exactly the
way Claude Desktop or Cursor would, performs the initialize handshake, and
exercises read and write tools plus a resource read.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from socialmcp.config import Config, EmbeddingConfig
from socialmcp.core.db import Database
from socialmcp.core.ingest import ingest_path

mcp_client = pytest.importorskip("mcp.client.stdio", reason="mcp client SDK not available")
from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
TIMEOUT = 60


def seed_home(home: Path, fixture: Path) -> Config:
    """Build a throwaway SocialMCP home with the fixture export already ingested."""
    config = Config(
        home=home,
        drop_dir=home / "drop-archive",
        archive_dir=home / "ingested",
        auto_ingest=False,
        embedding=EmbeddingConfig(provider="none"),
    )
    config.ensure_dirs()
    db = Database(config.db_path, embedding_dim=config.embedding.dim)
    try:
        ingest_path(db, config, fixture)
    finally:
        db.close()
    return config


def server_params(home: Path) -> StdioServerParameters:
    env = {
        **os.environ,
        "SOCIALMCP_HOME": str(home),
        "SOCIALMCP_DROP_DIR": str(home / "drop-archive"),
        "SOCIALMCP_ARCHIVE_DIR": str(home / "ingested"),
        "SOCIALMCP_AUTO_INGEST": "0",
        "SOCIALMCP_EMBED_PROVIDER": "none",
        "SOCIALMCP_LOG_LEVEL": "WARNING",
        "PYTHONPATH": str(REPO),
        # Never let a developer's real socialmcp.toml influence the test.
        "SOCIALMCP_CONFIG": str(home / "absent.toml"),
    }
    return StdioServerParameters(
        command=sys.executable, args=["-m", "socialmcp.cli", "serve"], env=env, cwd=str(REPO)
    )


def field(obj: Any, *names: str) -> Any:
    """Read the first attribute that exists.

    The SDK's client models switched from camelCase (1.x) to snake_case (2.x),
    so a test pinned to one spelling silently stops covering the other SDK.
    """
    for name in names:
        value = getattr(obj, name, None)
        if value is not None:
            return value
    return None


def payload(result: Any) -> dict[str, Any]:
    """Pull the JSON body out of an MCP tool result."""
    structured = field(result, "structuredContent", "structured_content")
    if structured:
        # FastMCP wraps non-model returns in {"result": ...}
        return structured.get("result", structured)
    assert result.content, "tool returned no content"
    return json.loads(result.content[0].text)


async def run_session(home: Path):
    async with stdio_client(server_params(home)) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            out: dict[str, Any] = {"server_name": field(init, "serverInfo", "server_info").name}
            out["instructions_present"] = bool(init.instructions)

            tools = await session.list_tools()
            out["tools"] = sorted(t.name for t in tools.tools)
            out["schemas_ok"] = all(
                (field(t, "inputSchema", "input_schema") or {}).get("type") == "object"
                for t in tools.tools
            )

            resources = await session.list_resources()
            out["resources"] = sorted(str(r.uri) for r in resources.resources)

            out["stats"] = payload(await session.call_tool("network_stats", {}))
            out["clusters"] = payload(await session.call_tool("list_event_clusters", {}))
            out["search"] = payload(
                await session.call_tool(
                    "search_network", {"query": "Seattle AI Week", "mode": "keyword", "limit": 5}
                )
            )
            out["pending"] = payload(await session.call_tool("list_pending_invitations", {}))

            # A write tool, then read it back through a different tool.
            out["note"] = payload(
                await session.call_tool(
                    "log_contact_note",
                    {"contact": "Marcus Webb", "note": "Protocol test note."},
                )
            )
            out["contact"] = payload(
                await session.call_tool("get_contact", {"contact": "Marcus Webb"})
            )

            # Renaming a cluster, the way a user would after being asked.
            slug = out["clusters"]["clusters"][0]["slug"]
            out["renamed"] = payload(
                await session.call_tool(
                    "rename_event_cluster", {"event": slug, "new_name": "Seattle AI Week 2025"}
                )
            )
            out["cluster_detail"] = payload(
                await session.call_tool("get_event_cluster", {"event": "seattle-ai-week-2025"})
            )

            resource = await session.read_resource("socialmcp://stats")
            out["resource_stats"] = json.loads(resource.contents[0].text)
            return out


@pytest.fixture(scope="module")
def session_output(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    home = tmp_path_factory.mktemp("mcp-home")
    seed_home(home, REPO / "tests" / "fixtures" / "linkedin")
    return asyncio.run(asyncio.wait_for(run_session(home), timeout=TIMEOUT))


def test_handshake_and_identity(session_output: dict[str, Any]):
    assert session_output["server_name"] == "socialmcp"
    assert session_output["instructions_present"], "clients rely on server instructions"


def test_tools_and_schemas_are_advertised(session_output: dict[str, Any]):
    assert "search_network" in session_output["tools"]
    assert "log_contact_note" in session_output["tools"]
    assert "trigger_archive_ingest" in session_output["tools"]
    assert len(session_output["tools"]) >= 11
    assert session_output["schemas_ok"], "every tool needs a JSON object input schema"


def test_resources_are_readable(session_output: dict[str, Any]):
    assert "socialmcp://briefing" in session_output["resources"]
    assert session_output["resource_stats"]["contacts"] == 12


def test_read_tools_return_real_data(session_output: dict[str, Any]):
    assert session_output["stats"]["contacts"] == 12
    assert session_output["stats"]["adapters_available"] == ["linkedin"]
    assert session_output["search"]["returned"] > 0
    assert session_output["pending"]["pending"][0]["full_name"] == "Rafael Santos"


def test_write_tools_persist_through_the_protocol(session_output: dict[str, Any]):
    assert session_output["note"]["full_name"] == "Marcus Webb"
    notes = session_output["contact"]["notes"]
    assert notes and notes[0]["note"] == "Protocol test note."


def test_cluster_rename_round_trips(session_output: dict[str, Any]):
    assert session_output["renamed"]["name"] == "Seattle AI Week 2025"
    detail = session_output["cluster_detail"]
    assert detail["cluster"]["name_is_placeholder"] is False
    assert detail["cluster"]["contact_count"] == 7
    assert len(detail["contacts"]) == 7
