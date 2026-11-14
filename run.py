#!/usr/bin/env python3
"""SocialMCP entry point.

    python run.py              # run the MCP server over stdio (what Claude/Cursor launch)
    python run.py watch        # run just the harness in the foreground (no AI client)
    python run.py ingest       # ingest everything in drop-archive/ and exit
    python run.py stats

Any other argument is passed straight through to the CLI (`socialmcp --help`).
This file exists so the project runs from a bare checkout with no install step.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from socialmcp.cli import main

if __name__ == "__main__":
    # No arguments means "be an MCP server", because that is how MCP clients
    # launch this file: `python /path/to/run.py` with stdio attached.
    raise SystemExit(main(sys.argv[1:] or ["serve"]))
