"""Per-platform terminology, kept out of the storage and query layers.

The schema is deliberately source-agnostic: a tie between two people is stored
as (status, origin_direction, invite_lapsed_at) and nothing else. What that tie
is *called* is platform-specific, and platforms disagree deeply:

    LinkedIn   symmetric. An invitation is sent, accepted or ignored, and the
               result is a "connection" with a degree (1st / 2nd+).
    X          asymmetric. Following someone is not a request and needs no
               approval; "mutual" is simply both edges existing. No degrees.
    Facebook   symmetric like LinkedIn, but the words are "friend request" and
               "friends", and there is no notion of degree shown to users.
    Instagram  asymmetric like X, except a private account turns a follow into
               a request that must be approved.

So the pipeline computes a platform-neutral `EdgeState`, and each adapter
supplies a `Vocabulary` that names those states in its own terms. Adding X
means adding one Vocabulary and one parser — no schema change, no SQL change,
and no edits to any MCP tool.

Anything that would read "1st degree" or "invitation" belongs here, never in
core/search.py or mcp_server/server.py.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class EdgeState(StrEnum):
    """The platform-neutral shape of a tie between the user and a contact."""

    MUTUAL = "mutual"                    # both sides are linked
    MUTUAL_ORIGIN_UNKNOWN = "mutual_origin_unknown"   # linked, but who asked is unrecorded
    OUTBOUND_PENDING = "outbound_pending"             # user reached out, no answer yet
    INBOUND_PENDING = "inbound_pending"               # they reached out, user hasn't answered
    OUTBOUND_LAPSED = "outbound_lapsed"               # user's request vanished without linking
    INBOUND_LAPSED = "inbound_lapsed"                 # their request vanished without linking
    OUTBOUND_WITHDRAWN = "outbound_withdrawn"         # user took their request back
    UNKNOWN = "unknown"


def edge_state(
    status: str | None, origin_direction: str | None, lapsed_at: str | None = None
) -> EdgeState:
    """Derive the neutral state from what the database stores.

    Pure and platform-independent: every adapter writes the same three fields,
    so this works for a source that does not exist yet.
    """
    if status == "connected":
        if origin_direction == "outbound":
            return EdgeState.MUTUAL
        if origin_direction == "inbound":
            return EdgeState.MUTUAL
        return EdgeState.MUTUAL_ORIGIN_UNKNOWN
    if status == "withdrawn":
        return EdgeState.OUTBOUND_WITHDRAWN
    if status == "pending":
        return EdgeState.OUTBOUND_LAPSED if lapsed_at else EdgeState.OUTBOUND_PENDING
    if status == "received":
        return EdgeState.INBOUND_LAPSED if lapsed_at else EdgeState.INBOUND_PENDING
    return EdgeState.UNKNOWN


def is_linked(state: EdgeState) -> bool:
    """Is the tie actually established, whatever the platform calls that?"""
    return state in {EdgeState.MUTUAL, EdgeState.MUTUAL_ORIGIN_UNKNOWN}


@dataclass(slots=True, frozen=True)
class Vocabulary:
    """How one platform names its ties. Supplied by the adapter.

    `labels` maps an EdgeState to the token a tool returns; `meanings` explains
    each token to the model in that platform's own words. `tier_labels` covers
    platforms that show a distance (LinkedIn's 1st / 2nd+); platforms without
    the concept leave it None and the field is simply omitted from output.
    """

    source: str
    # What one of these people is called, singular and plural.
    contact_noun: str = "contact"
    contact_noun_plural: str = "contacts"
    # What the act of linking is called.
    request_noun: str = "request"
    link_verb: str = "linked"
    labels: dict[EdgeState, str] = field(default_factory=dict)
    meanings: dict[str, str] = field(default_factory=dict)
    # (linked_label, not_linked_label) or None when the platform has no notion.
    tier_labels: tuple[str, str] | None = None
    tier_meanings: dict[str, str] = field(default_factory=dict)

    def label(self, state: EdgeState) -> str:
        return self.labels.get(state, str(state))

    def tier(self, state: EdgeState) -> str | None:
        if self.tier_labels is None:
            return None
        linked, not_linked = self.tier_labels
        return linked if is_linked(state) else not_linked

    # Set by a source whose naming needs more than a state->label map, e.g.
    # LinkedIn splitting MUTUAL by who sent the invitation.
    labeller: Callable[[EdgeState, str | None], str] | None = None

    def describe(self, row: Any, lapsed_at: str | None = None) -> dict[str, Any]:
        """Produce the display fields for one contact row."""
        direction = row.get("origin_direction") if isinstance(row, dict) else row["origin_direction"]
        state = edge_state(row["status"], direction, lapsed_at)
        label = self.labeller(state, direction) if self.labeller else self.label(state)
        out: dict[str, Any] = {"how": label, "edge_state": str(state)}
        tier = self.tier(state)
        if tier is not None:
            out["degree"] = tier
        return out


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------

GENERIC = Vocabulary(
    source="*",
    labels={
        EdgeState.MUTUAL: "linked_both_ways",
        EdgeState.MUTUAL_ORIGIN_UNKNOWN: "linked_origin_unknown",
        EdgeState.OUTBOUND_PENDING: "i_reached_out_no_answer_yet",
        EdgeState.INBOUND_PENDING: "they_reached_out_i_have_not_answered",
        EdgeState.OUTBOUND_LAPSED: "my_request_lapsed",
        EdgeState.INBOUND_LAPSED: "their_request_lapsed",
        EdgeState.OUTBOUND_WITHDRAWN: "i_withdrew_my_request",
        EdgeState.UNKNOWN: "unknown",
    },
    meanings={
        "linked_both_ways": "the link exists in both directions",
        "linked_origin_unknown": "linked, but the export does not record who asked first",
        "i_reached_out_no_answer_yet": "you reached out and have had no answer",
        "they_reached_out_i_have_not_answered": "they reached out and you have not answered",
        "my_request_lapsed": "your request disappeared from a later export without linking",
        "their_request_lapsed": "their request disappeared from a later export without linking",
        "i_withdrew_my_request": "you withdrew your request",
        "unknown": "no request or link recorded",
    },
)

_REGISTRY: dict[str, Vocabulary] = {}


def register_vocabulary(vocabulary: Vocabulary) -> Vocabulary:
    _REGISTRY[vocabulary.source] = vocabulary
    return vocabulary


def vocabulary_for(source: str | None) -> Vocabulary:
    """The platform's vocabulary, or a neutral one for an unregistered source."""
    if not source:
        return GENERIC
    if source not in _REGISTRY:
        # Importing the adapter registers its vocabulary as a side effect.
        from ..adapters import load_builtin_adapters

        load_builtin_adapters()
    return _REGISTRY.get(source, GENERIC)


def describe_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Annotate rows in place with per-source labels. Rows must carry `source`.

    Mixed-source result sets are supported, which is the point: a search across
    LinkedIn and X should label each tie in its own platform's words rather
    than forcing one platform's metaphor onto the other.
    """
    for row in rows:
        vocabulary = vocabulary_for(row.get("source"))
        row.update(vocabulary.describe(row, row.get("invite_lapsed_at")))
    return rows


def breakdown(rows: list[dict[str, Any]], key: str = "how") -> dict[str, Any]:
    """Counts per label, with a legend covering only the labels present."""
    counts: dict[str, int] = {}
    sources: set[str] = set()
    for row in rows:
        label = row.get(key)
        if label:
            counts[label] = counts.get(label, 0) + 1
        if row.get("source"):
            sources.add(row["source"])

    legend: dict[str, str] = {}
    for source in sources or {"*"}:
        legend.update(vocabulary_for(source).meanings)
    ordered = dict(sorted(counts.items(), key=lambda kv: -kv[1]))
    return {
        "counts": ordered,
        "legend": {k: legend[k] for k in ordered if k in legend},
    }
