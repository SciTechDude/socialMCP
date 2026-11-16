"""LinkedIn's terminology, owned by the LinkedIn adapter.

Kept beside the parser rather than in core/ so a platform's words travel with
its parser. X calls these follows, Facebook calls them friends, and neither has
LinkedIn's visible degree — none of which core/ should know about.

LinkedIn's model: a symmetric invitation that is sent, accepted, declined or
ignored, producing a "connection" with a degree shown as 1st / 2nd+.
"""

from __future__ import annotations

from ...core.vocabulary import EdgeState, Vocabulary, register_vocabulary

SOURCE = "linkedin"

LABELS: dict[EdgeState, str] = {
    EdgeState.MUTUAL: "i_invited_they_accepted",
    EdgeState.MUTUAL_ORIGIN_UNKNOWN: "connected_unknown_invite",
    EdgeState.OUTBOUND_PENDING: "i_invited_not_yet_accepted",
    EdgeState.INBOUND_PENDING: "they_invited_i_have_not_accepted_yet",
    EdgeState.OUTBOUND_LAPSED: "i_invited_it_lapsed",
    EdgeState.INBOUND_LAPSED: "they_invited_it_lapsed",
    EdgeState.OUTBOUND_WITHDRAWN: "i_invited_then_withdrew",
    EdgeState.UNKNOWN: "unknown",
}

MEANINGS: dict[str, str] = {
    "i_invited_they_accepted": "you sent the invitation and they accepted",
    "they_invited_i_accepted": "they sent the invitation and you accepted",
    "i_invited_not_yet_accepted": "your invitation is still unanswered",
    "they_invited_i_have_not_accepted_yet": (
        "their invitation is still sitting unanswered by you"
    ),
    "i_invited_it_lapsed": (
        "your invitation disappeared from a later export without connecting - "
        "declined, withdrawn or expired; the export does not say which"
    ),
    "they_invited_it_lapsed": (
        "their invitation disappeared from a later export without connecting - "
        "you declined or ignored it, or it expired"
    ),
    "i_invited_then_withdrew": "you withdrew the invitation",
    "connected_unknown_invite": (
        "connected, but the export's invitation history (a rolling ~6-month window) "
        "does not reach back far enough to say who invited whom"
    ),
    "unknown": "no invitation or connection record",
}


def label_for(state: EdgeState, origin_direction: str | None) -> str:
    """LinkedIn separates the two mutual cases by who sent the invitation.

    A plain state->label map cannot express this, because both cases are the
    same neutral EdgeState.MUTUAL; only LinkedIn cares which side asked.
    """
    if state is EdgeState.MUTUAL and origin_direction == "inbound":
        return "they_invited_i_accepted"
    return LABELS.get(state, str(state))


VOCABULARY = register_vocabulary(
    Vocabulary(
        source=SOURCE,
        contact_noun="connection",
        contact_noun_plural="connections",
        request_noun="invitation",
        link_verb="connected",
        labels=LABELS,
        meanings=MEANINGS,
        labeller=label_for,
        # LinkedIn shows a degree; the export only ever proves 1st, because
        # Connections.csv *is* the first-degree list. Absence means 2nd or
        # further, and the archive cannot narrow it beyond that.
        tier_labels=("1st", "2nd+"),
        tier_meanings={
            "1st": "a first-degree connection - present in Connections.csv",
            "2nd+": (
                "absent from Connections.csv, so 2nd degree or further; the export does "
                "not say which, and it reflects the state on the export date"
            ),
        },
    )
)
