# Contributing to SocialMCP

The highest-value contribution is **a new adapter**. The data model, ingest
pipeline, clustering, search and MCP tools are all source-agnostic, so adding
Instagram, Facebook, X, Mastodon, Bluesky or a plain vCard importer is one
module and a test — no schema migration, no changes to the server.

## Ground rules

1. **Never scrape.** No headless browsers, no session cookies (`li_at` and
   friends), no live API calls against a social network, no "it's just one
   request". Only files the user downloaded from their own data export. A PR
   that violates this will be closed regardless of quality.
2. **Local by default.** New network calls need a strong reason and must be
   off unless the user opts in.
3. **One bad row never kills a file.** Exports are messy. Warn and continue.
4. **Tests use fixtures, never real data.** Anonymize everything.

## Setup

```bash
git clone https://github.com/SciTechDude/socialMCP.git
cd socialMCP
python3 -m venv .venv            # Windows: py -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest
ruff check .
```

Work inside the venv. Nothing in this project should ever be installed into a
system interpreter — not in development, and not for users: the published
command-line install uses `uv tool`/`pipx` (each app gets its own private
environment) and the macOS app carries its own interpreter inside the bundle.

## Where a new platform's code goes

Everything about one platform lives in one directory, and nothing about it
lives anywhere else:

```
socialmcp/adapters/<source>/
├── __init__.py       registers the adapter and its vocabulary
├── adapter.py        parses that platform's export files
├── vocabulary.py     what that platform calls a tie
└── urls.py           that platform's profile-URL rules (if it has any)
```

Shared code is platform-neutral and must stay that way:

| Layer | Knows about platforms? |
|---|---|
| `core/schema.sql` | No. A tie is `(status, origin_direction, invite_lapsed_at)`. |
| `core/search.py` | No. Queries select raw columns; naming happens in Python. |
| `core/vocabulary.py` | Only the abstraction — `EdgeState` and the `Vocabulary` contract. |
| `mcp_server/server.py` | No. Tools call `describe_rows()` and get per-source labels. |
| `adapters/<source>/` | **Yes. All of it.** |

`tests/test_vocabulary.py::test_core_contains_no_platform_terminology` enforces
this by walking the AST of every module in `core/` and failing if a platform's
word appears in a string value. Comments and docstrings may discuss platforms;
data a user or model reads may not.

### Naming ties on a platform that is not LinkedIn

LinkedIn is symmetric: an invitation is sent, accepted or ignored, and the
result has a degree (1st / 2nd+). X and Instagram are asymmetric — following
someone is not a request and needs no approval. Facebook is symmetric but calls
it friendship and shows no degree.

The pipeline resolves all of these to a neutral `EdgeState`
(`MUTUAL`, `OUTBOUND_PENDING`, `INBOUND_PENDING`, `OUTBOUND_LAPSED`, …), and
your `Vocabulary` names those states in your platform's words:

```python
VOCABULARY = register_vocabulary(
    Vocabulary(
        source="x_twitter",
        contact_noun="follower",
        request_noun="follow",
        labels={
            EdgeState.MUTUAL: "we_follow_each_other",
            EdgeState.OUTBOUND_PENDING: "i_follow_them",
            EdgeState.INBOUND_PENDING: "they_follow_me",
        },
        meanings={"i_follow_them": "you follow them, they do not follow back"},
        tier_labels=None,   # X has no degree; the field is then omitted entirely
    )
)
```

Set `labeller=` when a single state maps to more than one word on your platform
— LinkedIn uses it to split `MUTUAL` by who sent the invitation.

A result set may mix sources, and each row is labelled in its own platform's
words. That is why naming is done in Python rather than in SQL: one `CASE`
expression would have to impose one platform's metaphor on every row.

## Writing an adapter

### 1. What your adapter must do

Implement two methods. That's the whole contract:

```python
from socialmcp.adapters.base import ArchiveContext, BaseAdapter, register
from socialmcp.core.models import ContactRecord, Identity, InteractionRecord


@register
class InstagramAdapter(BaseAdapter):
    source = "instagram"
    display_name = "Instagram Data Export"
    signature_files = ("followers_1.json", "following.json")

    def parse(self, ctx: ArchiveContext):
        path = find_file(ctx.root, "followers_1.json")
        if not path:
            return
        ctx.mark_parsed(path)
        for entry in load(path):
            yield ContactRecord(
                source=self.source,
                source_key=f"handle/{entry['username']}",
                full_name=entry.get("full_name") or entry["username"],
                profile_url=normalize_profile_url(entry.get("href")),
                status="connected",
                connected_on=parse_date(entry.get("timestamp")),
                identities=[Identity("handle", entry["username"])],
            )
```

`detect()` is inherited and returns True when any `signature_files` entry is
present; override it if your format needs smarter sniffing.

### 2. What the pipeline does for you

Do **not** write SQL, open the database, compute embeddings, or dedupe across
archives. `core/ingest.py` already handles:

- safe zip extraction (path traversal, compression bombs)
- archive-level dedupe by SHA-256
- merging records that describe the same person, within and across archives
- identity resolution (profile URL → email → unambiguous name)
- job-change diffing into `contact_history`
- `search_blob` / `last_interaction_at` maintenance
- event clustering and embedding refresh

Your job is to turn messy files into clean `ContactRecord` /
`InteractionRecord` objects. If you find yourself wanting to touch the DB, the
pipeline is probably missing a hook — open an issue.

### 3. Rules for the records you emit

- **`source_key` must be stable across exports.** Prefer a profile URL handle
  (`in/jane-doe`, `handle/janedoe`). Fall back to `name_key()` only when the
  export genuinely has no stable identifier.
- **Normalize through `core/normalize.py`.** `normalize_profile_url`,
  `parse_date`, `parse_timestamp`, `clean`, `slugify`, `name_key`. Don't
  hand-roll date parsing; add a format to the existing lists instead.
- **Register every identity you see** (`Identity("profile_url", …)`,
  `("email", …)`, `("name", slug)`). This is how a DM that only knows an opaque
  URN still finds the right person.
- **`dedupe_key` must be deterministic** for the same underlying event, so a
  re-dropped overlapping archive inserts nothing new.
- **Use `resolve_only=True`** for interactions that must never create a contact
  (DMs, group threads, recruiter blasts).
- **Set `status` honestly.** `connected`, `pending` (we reached out, no answer),
  `received` (they reached out), `withdrawn`, `unknown`. Precedence resolves
  conflicts; `connected` always wins.

### 4. Register it

Add your module to `BUILTIN_MODULES` in `socialmcp/adapters/__init__.py`, then
add a drop-zone folder in `DROP_SUBDIRS` in `socialmcp/config.py` so users get
`drop-archive/<yoursource>/` scaffolded automatically.

### 5. Test it

Add an anonymized fixture under `tests/fixtures/<source>/` and a test module
that covers, at minimum:

- the format's structural quirk (preambles, nesting, encoding)
- identity merging across the export's own files
- one malformed row that must warn rather than raise
- re-ingesting the same archive twice changing nothing

`tests/test_linkedin_adapter.py` and `tests/test_ingest.py` are the templates.

## Pull requests

- One adapter (or one concern) per PR.
- `pytest` and `ruff check .` must pass.
- Say in the PR body which export vintage you tested against and roughly how
  large it was. Format drift is this project's main maintenance burden, so that
  note is genuinely useful later.
- Explain anything non-obvious with a comment that says *why*, not *what*.

## Reporting a format change

If an export shape changes, open an issue with: the file name, the old and new
headers or JSON keys, the approximate date you requested the export, and a
small anonymized sample. That is enough to fix it without anyone touching your
real data.

## Simulating a new user before you open a PR

The failures that reach users are environmental, and an editable install in your
own shell hides all of them. Two bugs shipped past a green local suite this way:
the MCP SDK's 2.0 rename (`mcp>=1.2` resolves to 2.x, where `FastMCP` became
`MCPServer`, so the server failed at import for anyone installing fresh), and a
drop zone that resolved to the developer's source tree instead of the user's
configured home.

CI runs this on every push (`.github/workflows/ci.yml`, job `clean-install`).
To run it yourself:

```bash
python -m build --wheel                    # pip install build
python -m venv /tmp/v && /tmp/v/bin/pip install dist/*.whl
export SOCIALMCP_HOME=/tmp/socialmcp-test SOCIALMCP_EMBED_PROVIDER=none
/tmp/v/bin/socialmcp stats                 # scaffolds the home + drop zone
# zip tests/fixtures/linkedin into $SOCIALMCP_HOME/drop-archive/linkedin/
/tmp/v/bin/socialmcp ingest
/tmp/v/bin/socialmcp search "Seattle AI Week"
```

A wheel install — not `pip install -e .` — is the point. It is the only way to
catch `schema.sql` missing from the package, console scripts that don't
generate, and config that silently resolves relative to a checkout.

**Docker is not the right tool for this.** It hides the host-environment
problems that actually break users, and containerizing a stdio server that
reads a host folder and writes a host database adds volume mounts, UID
mismatches and `host.docker.internal` indirection that no real user
encounters — a container would report Ollama as unreachable when it is fine.
Use Docker only to check a clean Linux userspace; use a wheel in a fresh venv
for everything else.
