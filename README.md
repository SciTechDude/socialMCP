# SocialMCP

**A local-first personal CRM for your professional network, delivered entirely as an MCP server.**

> **Early release.** This is new software with one maintainer and very few
> users so far. The downloads are marked as pre-releases while it is being
> hardened: expect rough edges, and please
> [report anything that breaks](#tell-me-what-broke) — your data is never at
> risk (SocialMCP only reads the export you give it), but the experience
> around it is still being smoothed out.

No scraping. No cookies. No browser automation. No web UI. You drop the data
export that LinkedIn already owes you into a folder, and **any MCP client**
becomes the interface to a searchable CRM that remembers how you met everyone.

```
You:        Who did I meet at Seattle AI Week, and who haven't I followed up with?

Assistant:  7 people, 13-15 Oct. Four are still cold:
            • Marcus Webb — Staff ML Engineer, Cascade Robotics
              "Great meeting you at Seattle AI Week today!"
            • Hana Suzuki — Director of Product, Elliott Bay Health
            ...
            Also: Priya Raghavan moved from Northwind AI to Aurora Labs as VP GTM
            since your last export.
```

*(Illustrative — the names above come from the synthetic fixtures in
`tests/fixtures/`, not from anyone's real network.)*

> **Not a developer?** Follow [QUICKSTART.md](QUICKSTART.md) instead — five
> steps, no config files to edit. `socialmcp setup` connects your AI app for you.

## Get it

**Mac, no setup** — [**Download SocialMCP**](https://github.com/SciTechDude/socialMCP/releases)
→ drag it to Applications → then **open it from Applications**. Those last two
are separate steps, and the second is the one that does the work: dragging only
copies the app, and nothing sets itself up until you open it. It then finds your
AI app, connects itself, and asks you to pick your LinkedIn export.

No Python and no terminal. When you pick your export, macOS may ask whether
SocialMCP can open your Downloads, Desktop or Documents folder — that is macOS
asking on behalf of the file-chooser, and **you can say no to all of them** and
drag the `.zip` into the app's own folder instead. Either way it reads only
that one file.

**One honest caveat about where your data goes.** SocialMCP uploads nothing.
But when you ask your assistant a question, the contacts it reads to answer you
become part of that conversation — so whoever runs the assistant (Anthropic,
OpenAI, Google) sees them, exactly as they see everything else you type. That
is true of any cloud assistant and is outside SocialMCP's control. A model
running on your own machine is the only way to avoid it.

> **Pick the file that matches your Mac.** There are two, and the wrong one
> will not open at all:
>
> | Your Mac | Download |
> |---|---|
> | Apple Silicon — M1, M2, M3, M4 | the file ending `-arm64.dmg` |
> | Intel | No app bundle — use the [command-line install](#get-it) below, which works on Intel today |
>
> Not sure?  menu → **About This Mac**. "Apple M*" means Apple Silicon;
> "Intel Core" means Intel. Or run `uname -m` — `arm64` or `x86_64`.
>
> If you pick wrong, macOS reports that the app is damaged or cannot be opened.
> Nothing is broken: delete it and download the other file.

The download is **signed with an Apple Developer ID and notarized by Apple**.

**The first time you open it, macOS will ask "are you sure?"** — that is normal
for anything downloaded from the web, and it is what success looks like. The
message should read:

> "SocialMCP" is an app downloaded from the Internet. Are you sure you want to
> open it? **Apple checked it for malicious software and none was detected.**

Click **Open**. It happens once, and that second sentence is the notarization.

What you should *never* have to do is bypass anything. If macOS says the
developer "cannot be verified", offers only **Move to Trash**, or you are told
to visit Privacy & Security or strip a quarantine flag, the file did not come
from [the releases page](https://github.com/SciTechDude/socialMCP/releases). To
check a download yourself before opening it:

```bash
spctl --assess --type open --context context:primary-signature -v ~/Downloads/SocialMCP-*.dmg
# accepted
# source=Notarized Developer ID
```

That matters more here than for most apps: this one reads your entire
professional network, so being able to confirm who built the binary — and that
Apple scanned it — is part of the point.

Then: [**5-minute quick start**](QUICKSTART.md) · [What it can do](#what-you-can-do-with-it)

**Windows, Linux, or any Mac** — from the command line, installed into its own
isolated environment and never into your system Python. There is no Windows or
Linux installer; this is the supported route there, and
[QUICKSTART.md](QUICKSTART.md) has step-by-step instructions for each:

```bash
uv tool install git+https://github.com/SciTechDude/socialMCP.git
# no Git installed (common on Windows)? use the zip instead:
# pipx install https://github.com/SciTechDude/socialMCP/archive/refs/heads/main.zip

socialmcp setup        # connects the MCP clients you have installed
socialmcp drop         # opens the folder for your export
socialmcp ingest
```

`uv tool` and `pipx` each create a private virtualenv for the app and put its
commands on your PATH. **Avoid a bare `pip install`** — it would add
dependencies to whichever interpreter happens to be active, which on many
systems is the one the OS relies on.

Requires Python 3.11+. Linux and Windows are supported this way; the Mac app is
the same software with its own interpreter inside.

## Tell me what broke

This is new, and **SocialMCP collects nothing** — no telemetry, no analytics,
no crash reports. That is the point, and it also means I have no idea whether
it worked for you. A download count is the only thing I can see, and it cannot
tell the difference between "works beautifully" and "crashed on launch".

**Please try the [newest release](https://github.com/SciTechDude/socialMCP/releases)
first** — it is a one-person project and only the current version is
supported. Older releases stay downloadable so a regression can be traced, but
most problems are already fixed in the newest build. (That link goes to the
full list rather than `/releases/latest`, because GitHub excludes pre-releases
from "latest" and the newest build is often a pre-release.)

Then, if anything failed, confused you, or parsed your export wrong:
[open an issue](https://github.com/SciTechDude/socialMCP/issues), or ask in
[Discussions](https://github.com/SciTechDude/socialMCP/discussions) if it is
more of a question than a bug — even a one-liner is useful. Especially worth
reporting:

- it would not install, or the app would not open
- your export parsed but something came out wrong — a missing person, a wrong
  date, an event that should have been one cluster and came out as three
- your AI client could not see it after `socialmcp setup`
- an export vintage this does not understand (LinkedIn changes the CSVs
  periodically, and that is exactly the kind of thing I cannot discover alone)

**Never paste your export, your database, or a screenshot with real names.**
`socialmcp doctor` output is safe to share — it reports counts and statuses,
never contact data. Describe the shape of the problem and I can usually
reproduce it from the synthetic fixtures.

Not a GitHub user? Message me on
[LinkedIn](https://www.linkedin.com/in/anil-mandra-640aa5/) instead — a
one-line DM saying what happened is worth more to me than silence. Also the
place to ask for a Windows or Linux installer: both platforms work from the
command line today, and if enough people want a double-click installer I will
know it is worth building.

## Why this exists

Networking at a fast-paced event creates a memory gap: three days later you have
forty new connections and no idea which conversation went with which name.
LinkedIn used to let you tag and annotate connections privately — that is now
behind Sales Navigator.

The workaround most people already use by hand:

1. A QR code on your lock screen so people can scan instead of typing.
2. A clipboard template in the connection invite — *"Great meeting you at
   Seattle AI Week today!"*

That second habit is the key. Your official LinkedIn data export contains
`Invitations.csv`, and **the `Message` column preserves exactly what you pasted,
with a timestamp**. That is a dated, venue-stamped note about how you met every
single person — already written, just never surfaced. SocialMCP extracts it,
clusters the connections into the events they came from, and makes the whole
thing queryable in natural language.

## What you can do with it

<!-- capabilities:start - generated by socialmcp/capabilities.py, do not edit -->

| What you can do | Ask it something like |
|---|---|
| **Find people by what they do, not just by name** | `who in my network works in <field or role>` |
| **Recall how and when you met someone** | `how do I know <person>` |
| **Group connections into the events they came from** | `what events are in my network` |
| **See who reached out to whom** | `who reached out to me at <event or period>` |
| **Know which invitations are waiting on you, and which on them** | `which invitations are waiting on me` |
| **Find connections you have never actually spoken to** | `who have I never followed up with` |
| **Keep private notes on people** | `make a note on <person>: <what you want to remember>` |
| **Attach public information your assistant found** | `look up what <company> does and save it to <person>` |
| **See who changed jobs since your last export** | `who changed jobs recently` |
| **Load a new data export** | `load my new export` |

Each of these has caveats worth knowing — see [CAPABILITIES.md](CAPABILITIES.md) for the full description of every one, or ask your assistant *"what can you do with my network?"* and it will answer from the server.

### What it cannot do

- Reads only the data export you download from the social network yourself. It never logs in, never uses your password or cookies, and never reads any website.
- Cannot see anything the export leaves out. Your connections' full profiles are not in it — only their name, company, title and the date you connected.
- Cannot tell 2nd from 3rd degree. Anyone absent from your connections list is simply not a connection; the export does not say how far away they are.
- Cannot send messages, accept invitations or change anything on the social network. It is read-only by design.
- Invitation history covers roughly the last six months, so older connections cannot be attributed to whoever sent the invitation.
- Knows nothing about events by name on its own. Your assistant supplies the dates; SocialMCP matches them against when you met people.
- Cannot control what your AI assistant does with an answer. SocialMCP uploads nothing, but the results it hands back become part of your conversation — so whoever runs that assistant (Anthropic, OpenAI, Google) sees whatever it read to answer you, exactly as they see the rest of the chat. A model running on your own machine is the only way to avoid that.
- Everything is stored in one file on this computer. SocialMCP uploads nothing and deleting that file deletes all of it.

<!-- capabilities:end -->

## Principles

| | |
|---|---|
| **MCP-first, headless** | No SPA, no REST API, no listening socket. The chat window is the UI. |
| **Zero scraping, ToS-compliant** | Only user-owned GDPR/CCPA export files. Never a session cookie, headless browser, or live API call against a social network. |
| **Local-first privacy** | Contacts and DM history live in one SQLite file on your machine. Embeddings default to local Ollama. Cloud LLM keys are strictly opt-in. |
| **Contributor-extensible** | New sources are one module implementing `BaseAdapter`. No schema changes. |

## Install from source

```bash
git clone https://github.com/SciTechDude/socialMCP.git
cd socialMCP
python3 -m venv .venv            # keeps everything out of your system Python
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest
```

The venv is required, not a nicety: installing the dependencies into a system
or `mise`-managed interpreter is how you end up debugging an unrelated package
conflict later. `.venv/` is gitignored.

Optional, for meaning-based search (recommended, and entirely local):

```bash
ollama pull nomic-embed-text    # install Ollama from ollama.com first
socialmcp reindex
```

Without it, search falls back to SQLite FTS5 keyword matching and everything
else works unchanged.

## Get your data

LinkedIn → **Settings & Privacy → Data Privacy → Get a copy of your data** →
request the **larger archive** (it is the one with `Invitations.csv` and
`messages.csv`). It arrives by email in minutes to 24 hours.

Drop the `.zip` — unopened, as downloaded — into:

```
drop-archive/linkedin/
```

That is the whole ingestion UX. The background harness notices the file, waits
for the copy to finish, parses it, and files it away under `ingested/linkedin/`.

## Connect it to any MCP client

Easiest path — this detects the MCP clients you have installed, writes their
config (backing up anything it touches), and tells you what to restart:

```bash
socialmcp setup
```

To do it by hand: SocialMCP is a standard **stdio MCP server** with no
client-specific code, so anything that speaks MCP can drive it. The whole
integration is one command:

```
python /absolute/path/to/socialMCP/run.py
```

Most clients configure that as JSON in the now-conventional `mcpServers` shape:

```json
{
  "mcpServers": {
    "socialmcp": {
      "command": "python",
      "args": ["/absolute/path/to/socialMCP/run.py"]
    }
  }
}
```

Where that JSON goes, by client:

| Client | Location |
|---|---|
| Claude Desktop | `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS), `%APPDATA%\Claude\claude_desktop_config.json` (Windows) |
| Claude Code | `claude mcp add socialmcp -- python /abs/path/run.py` |
| Cursor | `.cursor/mcp.json` in the project, or `~/.cursor/mcp.json` globally |
| VS Code (Copilot) | `.vscode/mcp.json`, under a `servers` key instead of `mcpServers` |
| Zed | `context_servers` in `settings.json` |
| Continue | `mcpServers` in `~/.continue/config.yaml` |
| Windsurf, Cline, LibreChat, Goose, … | their own MCP config file, same command |
| Your own code | `mcp` SDK (Python/TS), Pydantic-AI, LangChain MCP adapters, … |

> **Tested with:** the Mac app has been tested end-to-end with **Claude Code**
> and **Claude Desktop** only. The other clients are wired up the same way —
> same command, same JSON — and `socialmcp setup` detects them and writes their
> config, but nobody has yet confirmed the whole flow on them. If you try one,
> [say how it went](https://github.com/SciTechDude/socialMCP/discussions); that
> is the single most useful thing you can report.
>
> **Not detected automatically:** OpenAI Codex CLI (`~/.codex/config.toml`),
> Continue (`~/.continue/config.yaml`) and Goose
> (`~/.config/goose/config.yaml`) keep their settings in TOML or YAML rather
> than JSON. SocialMCP will not edit those formats, because doing it without
> mangling someone's comments and layout is a different job. Add the command
> below by hand and they work fine.

Check your client's docs for the exact key name — the command and args never
change. Restart the client afterwards, then ask it to call
`get_network_briefing`.

If a client won't start the server, run `python run.py stats` first: that
exercises the same database and config without the protocol in the way.

### Verifying it from your own script

No AI client needed to confirm the server works — this is the reference client
from the `mcp` SDK:

```python
import asyncio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def main():
    params = StdioServerParameters(command="python", args=["/abs/path/to/socialMCP/run.py"])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            print([t.name for t in (await session.list_tools()).tools])
            print((await session.call_tool("network_stats", {})).content[0].text)

asyncio.run(main())
```

`tests/test_mcp_protocol.py` is this same flow as a test, so client-neutral
compatibility is enforced on every CI run rather than assumed.

## Also usable without an AI client

```bash
python run.py ingest                                   # process the drop zone now
python run.py search "enterprise AI sales" --limit 5
python run.py search "" --event seattle-ai-week-2025
python run.py clusters
python run.py briefing
python run.py watch                                    # harness only, foreground
python run.py                                          # MCP server over stdio
```

## How it works

```
drop-archive/linkedin/export.zip
        │
        ▼
  core/harness.py ──── watchdog + debounce (never reads a half-copied zip)
        │
        ▼
  core/ingest.py ───── safe extract → sha256 dedupe → adapter → merge → upsert
        │                                                  │
        │                                     adapters/linkedin/adapter.py
        │                                     (CSV quirks live here, only here)
        ▼
  SQLite: contacts, interactions, contact_history, event_clusters,
          contact_identities, harness_events, contacts_fts, vec_contacts
        │
        ├── core/clustering.py  deterministic event detection
        ├── core/search.py      FTS5 + sqlite-vec fused with RRF
        └── mcp_server/server.py  the only thing the model talks to
```

### Repository layout

```
socialMCP/
├── run.py                        # entry point: MCP server, or CLI subcommands
├── drop-archive/linkedin/        # ← drop LinkedIn .zip exports here
├── ingested/                     # processed archives, structure mirrored
├── socialmcp/
│   ├── config.py                 # TOML + env config, drop-zone routing
│   ├── cli.py
│   ├── core/
│   │   ├── schema.sql            # the full data model, commented
│   │   ├── db.py                 # connection, migrations, sqlite-vec loading
│   │   ├── models.py             # the records every adapter emits
│   │   ├── normalize.py          # URL/date/name canonicalization
│   │   ├── ingest.py             # extract → merge → upsert → history
│   │   ├── clustering.py         # temporal event detection
│   │   ├── search.py             # hybrid search + RRF fusion
│   │   ├── embeddings.py         # Ollama / OpenAI / none
│   │   └── harness.py            # watcher, activity log, briefing
│   ├── adapters/
│   │   ├── base.py               # BaseAdapter + CSV header-detection helpers
│   │   └── linkedin/adapter.py   # v1 adapter
│   └── mcp_server/server.py      # FastMCP tools and resources
└── tests/                        # 63 tests, anonymized fixtures, no network
```

### The LinkedIn quirks it handles

These are the things that break naive parsers, all covered by tests:

- **Multi-line preamble.** `Connections.csv` opens with a human-readable
  "Notes:" disclaimer. The line count has changed between export vintages, so
  the parser scans for the first row containing the real headers instead of
  skipping a hard-coded three lines.
- **UTF-8 BOM** glued to the first header cell.
- **Header drift.** `Direction` is absent in older `Invitations.csv`; profile-URL
  columns appear in camelCase when they appear at all. Headers are normalized
  (`SENDER PROFILE URL` → `sender_profile_url`) rather than matched literally.
- **Invitations have no profile URL.** The same person therefore arrives as
  `in/jane-doe` from Connections and `name/jane-doe` from Invitations. These are
  merged — otherwise your clipboard note ends up orphaned on a phantom "pending"
  duplicate. The merge refuses to fire when a name is ambiguous.
- **Opaque member URNs.** `messages.csv` uses `/in/ACoAAB…` URLs that never match
  the vanity slugs in `Connections.csv`, so DMs are matched through an alias
  table (URL → email → unambiguous name).
- **Dates everywhere.** `"13 Oct 2025"`, `"10/13/25, 6:42 PM"`,
  `"2025-10-20 17:04:11 UTC"`, bare epochs. One bad date warns and keeps going.
- **Monthly re-drops.** Archives are deduped by SHA-256; overlapping rows are
  idempotent; a changed company or title is written to `contact_history` so you
  can see who moved.
- **Untrusted zips.** Extraction rejects path traversal (`../`) and absurd
  compression ratios. Exports are downloaded files, not trusted input.

### Event clustering

A greedy pass over connection dates: extend a burst while each gap is ≤
`max_gap_days` (2) and the total span stays ≤ `max_span_days` (5); emit it if it
holds ≥ `min_contacts` (5) people. Fifteen connections over a conference weekend
becomes one cluster; one connection a week never does. Tunable in
`socialmcp.toml`. Clusters you rename are never renamed back.

### Search

Keyword (FTS5, BM25-weighted toward names and invite notes) and semantic
(sqlite-vec cosine) rankings are fused with Reciprocal Rank Fusion, so
`"GTM background"` works without keyword overlap and `"Stripe"` still ranks the
literal match first. Structured filters are plain SQL predicates applied before
fusion — exact, never approximate.

## Why there are two Mac downloads

The app carries its own Python interpreter, which is how it runs on a Mac with
no Python installed. That interpreter is a compiled binary, so it is built for
one processor family at a time.

A single universal build is not possible here: macOS universal binaries require
*every* bundled library to contain both architectures, and some of this
project's dependencies publish single-architecture wheels only. Rather than
drop those dependencies or ship something that silently fails on half of all
Macs, there are two files.

Installing from the command line sidesteps this entirely — `uv tool` and `pipx`
build against whatever Python you already have, on any architecture, including
Linux and Windows.

## Removing it

```bash
socialmcp uninstall              # disconnect from AI apps; data kept
socialmcp uninstall --dry-run    # show exactly what would change
socialmcp uninstall --purge-data # also delete the database and folders
```

Mac app users can click **Disconnect** on the app's second launch instead.

### Exactly what each method touches

| What you do | AI app settings | Your database and folders | The app itself |
|---|---|---|---|
| Drag the app to the Trash | untouched — the entry stays and will point at a missing binary | **kept** | removed |
| `socialmcp uninstall`, or **Disconnect** in the app | the `socialmcp` entry is removed; the file is backed up first | **kept** | kept |
| `socialmcp uninstall --purge-data`, or **Disconnect → Delete my data** | same as above | **deleted** — database, drop folder, processed archives | kept |
| `socialmcp uninstall --dry-run` | nothing — prints what it would do | nothing | nothing |

**What "deleted" means, precisely.** Only these, and only when they sit inside
SocialMCP's own folder:

- `socialmcp.db` — contacts, your private notes, the event names you chose,
  and which exports have already been read
- `drop-archive/` — exports waiting to be read
- `ingested/` — exports already read

**What is never deleted.** Anything outside that folder. The drop folder is
configurable, so somebody could point it at `~/Downloads`; deletion is confined
to paths inside SocialMCP's own directory, symlinks are refused rather than
followed, and standard folders — Downloads, Documents, Desktop, Library, your
home directory — are rejected outright. Anything skipped is named in the
output, so the tool never claims to have done more than it did. See
`Config.deletable_data_paths` in
[socialmcp/config.py](socialmcp/config.py) and the tests in
[tests/test_privacy.py](tests/test_privacy.py) if you would rather read it than
take our word for it.

**Why dragging the app to the Trash leaves your data.** Deleting someone's
notes as a side effect of removing an application is not a decision this
software should make for them. The cost is a folder left in Application
Support, so it contains a plain-text file explaining what it is, what is lost
if you delete it, and how to remove both at once.

Uninstall is otherwise the mirror of setup and holds to the same rule: it
deletes only the `socialmcp` entry it created, leaves other MCP servers and
unrelated settings alone, backs up every file it edits, and refuses to rewrite
a config file it cannot parse. Notes and event names exist nowhere else, and no
fresh export restores them.

## Privacy

Everything is a local file. The server speaks stdio to its parent process and
opens no network socket. The only outbound requests SocialMCP can make are to
your configured embedding provider — `127.0.0.1:11434` by default — and nothing
at all if you set `provider = "none"`. `.gitignore` blocks exports, the
database, and local config so you cannot accidentally commit your network.

## Tool reference

The capability table above is what these add up to; this is the
machine-level view, for anyone writing against the server directly.

| Tool | Purpose |
|---|---|
| `search_network` | Hybrid semantic + keyword search with structured filters (company, title, status, event, date range). |
| `get_event_cluster` | Everyone connected during one event window, with their invite notes and who is still un-followed-up. |
| `get_contact` | One person's full record: invite note, DMs, your notes, job changes. |
| `log_contact_note` | Append a timestamped private note. |
| `list_pending_invitations` | Invites you sent that were never accepted — your follow-up list. |
| `rename_event_cluster` | Turn `"13-15 Oct 2025 burst (7 connections)"` into `"Seattle AI Week 2025"`. |
| `trigger_archive_ingest` | Ingest on demand instead of waiting for the watcher. |
| `enrich_contact` | File something the assistant looked up from a public source, with provenance. |
| `list_capabilities` | What it can and cannot do, in plain language — see [CAPABILITIES.md](CAPABILITIES.md). |
| `list_event_clusters`, `network_stats`, `reindex_embeddings`, `get_network_briefing` | Housekeeping and session priming. |

Resources: `socialmcp://briefing`, `socialmcp://stats`, `socialmcp://clusters`,
`socialmcp://capabilities`, `socialmcp://adapters`.

Not sure what to ask? `socialmcp can-do`, or just ask your assistant *"what can
you do with my network?"* — it calls `list_capabilities` and answers from the
server rather than guessing.

## Development

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest              # no network access required
ruff check .
```

Fixtures in `tests/fixtures/linkedin/` are a hand-written, fully synthetic
export that reproduces every quirk above. Use them instead of your real data.

## Contributing

Adapters for other platforms are the easiest place to start: one module under
`socialmcp/adapters/<source>/`, no schema change, nothing shared to edit. See
[CONTRIBUTING.md](CONTRIBUTING.md).

## License

Apache License 2.0 — see [LICENSE](LICENSE).

Apache-2.0 rather than MIT for the explicit patent grant and because its
contribution clause means contributors don't need to sign a separate CLA.
