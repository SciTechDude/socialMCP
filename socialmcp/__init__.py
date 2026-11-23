"""SocialMCP — a local-first, ToS-compliant personal CRM exposed as an MCP server."""

from __future__ import annotations

# Kept as a literal rather than read from package metadata, because the macOS
# bundle has no installed distribution to read it from. tests/test_packaging.py
# asserts it matches pyproject.toml, so the two cannot drift silently.
__version__ = "0.1.2a6"
__all__ = ["__version__"]
