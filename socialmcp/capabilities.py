"""What SocialMCP can and cannot do, in plain language.

This exists because the first thing a non-technical user asks an assistant is
"what can you do with this?" — and the assistant should answer from the server
rather than guess from tool names. It is also the honest place to record the
limits, which matter more than the features: a user who believes this can read
live LinkedIn will be disappointed, and a user who does not know the invitation
history is a rolling window will misread an empty result.

Deliberately generic. Capabilities are described as shapes of question, never
as canned prompts naming a particular event or person, so the wording stays
true for anyone's network.

One source of truth: the `list_capabilities` tool, the
`socialmcp://capabilities` resource and CAPABILITIES.md all read from here, and
a test fails if the document drifts from this list.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True, frozen=True)
class Capability:
    key: str
    title: str
    # What the user gets, in their words, not the implementation's.
    description: str
    # Shapes of question, with placeholders rather than real names.
    ask_like: tuple[str, ...]
    tools: tuple[str, ...]
    # Anything that would otherwise surprise someone.
    caveat: str = ""


CAPABILITIES: tuple[Capability, ...] = (
    Capability(
        key="find_people",
        title="Find people by what they do, not just by name",
        description=(
            "Search your network by meaning as well as keywords. A description of a "
            "role or background finds people whose titles use different words, and "
            "filters narrow by company, title, date met or event."
        ),
        ask_like=(
            "who in my network works in <field or role>",
            "find people at <company>",
            "who could help me with <problem>",
        ),
        tools=("search_network", "get_network_digest"),
        caveat=(
            "Meaning-based search needs a local embedding model; without one, search "
            "falls back to matching words literally."
        ),
    ),
    Capability(
        key="recall_how_you_met",
        title="Recall how and when you met someone",
        description=(
            "The note you sent with a connection request is kept and attached to that "
            "person, along with the date. That is usually where you were and what you "
            "talked about — the context that disappears from memory within weeks."
        ),
        ask_like=(
            "how do I know <person>",
            "remind me where I met <person>",
            "show everything you have on <person>",
        ),
        tools=("get_contact", "search_network"),
        caveat=(
            "Only invitations you sent carry a note. When the other person invited "
            "you, there is usually no note to keep."
        ),
    ),
    Capability(
        key="events",
        title="Group connections into the events they came from",
        description=(
            "Bursts of connections over a few days are detected automatically and "
            "grouped, because that pattern is what a conference looks like in data. "
            "You can give a group its real name and then ask about it by that name."
        ),
        ask_like=(
            "what events are in my network",
            "name that group <event name>",
            "who did I meet at <event name>",
        ),
        tools=("list_event_clusters", "get_event_cluster", "rename_event_cluster"),
        caveat=(
            "The assistant works out an event's dates itself; SocialMCP only "
            "understands dates. Groups start with placeholder names until you name them."
        ),
    ),
    Capability(
        key="who_approached_whom",
        title="See who reached out to whom",
        description=(
            "Every connection records which side sent the invitation and whether it "
            "was accepted, so you can tell the people who sought you out from the "
            "people you chased."
        ),
        ask_like=(
            "who reached out to me at <event or period>",
            "how many of my invitations were accepted",
        ),
        tools=("get_event_cluster", "search_network", "get_network_digest"),
        caveat=(
            "The export's invitation history covers roughly the last six months, so "
            "for older connections it cannot say who invited whom."
        ),
    ),
    Capability(
        key="outstanding_invitations",
        title="Know which invitations are waiting on you, and which on them",
        description=(
            "Outstanding invitations are separated by who owes the next action: ones "
            "you sent that have not been accepted, and ones you received that you "
            "have not answered. Only the second kind can be cleared by you."
        ),
        ask_like=(
            "which invitations are waiting on me",
            "which of my invitations were never accepted",
            "show invitations older than <number> days",
        ),
        tools=("list_pending_invitations",),
        caveat=(
            "An invitation that vanishes from a later export without connecting is "
            "marked lapsed — declined, withdrawn or expired; the export does not say "
            "which."
        ),
    ),
    Capability(
        key="follow_ups",
        title="Find connections you have never actually spoken to",
        description=(
            "Being connected and having had a conversation are different things. "
            "People you connected with but never messaged are listed separately from "
            "people whose invitation is still outstanding."
        ),
        ask_like=(
            "who have I never followed up with",
            "who did I meet recently and never message",
        ),
        tools=("get_event_cluster", "get_network_briefing", "get_network_digest"),
        caveat="Only messages present in the export count as contact.",
    ),
    Capability(
        key="notes",
        title="Keep private notes on people",
        description=(
            "Add your own timestamped notes to anyone — what they are working on, "
            "what you promised, why you are not pursuing them. Stored only on this "
            "computer, and searchable alongside everything else."
        ),
        ask_like=(
            "make a note on <person>: <what you want to remember>",
            "what notes do I have on <person>",
        ),
        tools=("log_contact_note", "get_contact"),
        caveat="Notes never leave your machine and are not sent to any social network.",
    ),
    Capability(
        key="enrichment",
        title="Attach public information your assistant found",
        description=(
            "When your assistant looks something up about a person from public "
            "sources, the finding can be filed against that contact with its source, "
            "so it is kept and searchable. Your own notes stay distinguishable from "
            "anything that was looked up."
        ),
        ask_like=(
            "look up what <company> does and save it to <person>",
            "save this finding to <person> with the source",
        ),
        tools=("enrich_contact",),
        caveat=(
            "SocialMCP never fetches anything itself and never reads any social "
            "network. The assistant does the lookup, one person at a time, and only "
            "when asked."
        ),
    ),
    Capability(
        key="job_changes",
        title="See who changed jobs since your last export",
        description=(
            "When a newer export shows a different company or title for someone, the "
            "change is recorded with the date it was noticed, so a fresh export tells "
            "you who moved."
        ),
        ask_like=(
            "who changed jobs recently",
            "has anything changed in my network",
        ),
        tools=("get_network_briefing", "get_contact"),
        caveat="Needs at least two exports taken far enough apart to differ.",
    ),
    Capability(
        key="ingest",
        title="Load a new data export",
        description=(
            "Drop a new export into the watched folder and it is read automatically: "
            "new people added, existing people updated, nothing duplicated. You can "
            "also trigger it on demand."
        ),
        ask_like=(
            "load my new export",
            "what changed since last time",
        ),
        tools=("trigger_archive_ingest", "get_network_briefing", "network_stats"),
        caveat=(
            "Use the archive file exactly as downloaded, without unzipping it. "
            "Re-loading the same export changes nothing."
        ),
    ),
)


LIMITS: tuple[str, ...] = (
    "Reads only the data export you download from the social network yourself. It "
    "never logs in, never uses your password or cookies, and never reads any website.",
    "Cannot see anything the export leaves out. Your connections' full profiles are "
    "not in it — only their name, company, title and the date you connected.",
    "Cannot tell 2nd from 3rd degree. Anyone absent from your connections list is "
    "simply not a connection; the export does not say how far away they are.",
    "Cannot send messages, accept invitations or change anything on the social "
    "network. It is read-only by design.",
    "Invitation history covers roughly the last six months, so older connections "
    "cannot be attributed to whoever sent the invitation.",
    "Knows nothing about events by name on its own. Your assistant supplies the "
    "dates; SocialMCP matches them against when you met people.",
    "Everything is stored in one file on this computer. Nothing is uploaded, and "
    "deleting that file deletes all of it.",
)


def as_dict(include_limits: bool = True) -> dict[str, Any]:
    out: dict[str, Any] = {
        "what_this_is": (
            "A private CRM built from your own social data export. Your assistant "
            "queries it on your behalf; the data stays on this computer."
        ),
        "capabilities": [
            {
                "key": c.key,
                "title": c.title,
                "description": c.description,
                "ask_like": list(c.ask_like),
                "tools": list(c.tools),
                **({"caveat": c.caveat} if c.caveat else {}),
            }
            for c in CAPABILITIES
        ],
    }
    if include_limits:
        out["limits"] = list(LIMITS)
    return out


def as_markdown() -> str:
    """The same content as a document, so README/CAPABILITIES.md cannot drift."""
    lines = [
        "# What SocialMCP can do",
        "",
        "A private CRM built from your own social data export. Your AI assistant",
        "queries it on your behalf, and the data never leaves your computer.",
        "",
        "Ask in your own words — the shapes below are just the kinds of question it",
        "can answer. Replace anything in angle brackets with your own details.",
        "",
    ]
    for capability in CAPABILITIES:
        lines += [f"## {capability.title}", "", capability.description, ""]
        lines.append("Ask like:")
        lines += [f"- *{phrasing}*" for phrasing in capability.ask_like]
        lines.append("")
        if capability.caveat:
            lines += [f"**Note:** {capability.caveat}", ""]
    lines += ["# What it cannot do", ""]
    lines += [f"- {limit}" for limit in LIMITS]
    lines.append("")
    return "\n".join(lines)


def capability_keys() -> set[str]:
    return {c.key for c in CAPABILITIES}


__all__ = ["CAPABILITIES", "LIMITS", "Capability", "as_dict", "as_markdown", "capability_keys"]
