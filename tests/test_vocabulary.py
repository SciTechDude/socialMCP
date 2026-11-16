"""Platform isolation: a new source must not require touching an existing one.

The point of these tests is the *negative* claim. A contributor adding X or
Facebook should write one package under adapters/<source>/ and change nothing
else — no schema migration, no SQL edit, no change to any MCP tool, and
certainly no edit to the LinkedIn adapter. Each test below fails if that
boundary erodes.

The fake platform used here is deliberately asymmetric (follows, not mutual
invitations), because that is the shape LinkedIn's vocabulary cannot express
and therefore the strongest test of the abstraction.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from socialmcp.core import search
from socialmcp.core.vocabulary import (
    GENERIC,
    EdgeState,
    Vocabulary,
    breakdown,
    describe_rows,
    edge_state,
    is_linked,
    register_vocabulary,
    vocabulary_for,
)

# ---------------------------------------------------------------------------
# a fake asymmetric platform, registered the way a real adapter would
# ---------------------------------------------------------------------------

FOLLOW_LABELS = {
    EdgeState.MUTUAL: "we_follow_each_other",
    EdgeState.MUTUAL_ORIGIN_UNKNOWN: "we_follow_each_other",
    EdgeState.OUTBOUND_PENDING: "i_follow_them",
    EdgeState.INBOUND_PENDING: "they_follow_me",
    EdgeState.OUTBOUND_LAPSED: "i_unfollowed_them",
    EdgeState.INBOUND_LAPSED: "they_unfollowed_me",
    EdgeState.OUTBOUND_WITHDRAWN: "i_unfollowed_them",
    EdgeState.UNKNOWN: "unknown",
}


@pytest.fixture
def fake_platform() -> Vocabulary:
    return register_vocabulary(
        Vocabulary(
            source="fakegram",
            contact_noun="follower",
            contact_noun_plural="followers",
            request_noun="follow",
            link_verb="followed",
            labels=FOLLOW_LABELS,
            meanings={
                "we_follow_each_other": "you follow each other",
                "i_follow_them": "you follow them, they do not follow you back",
                "they_follow_me": "they follow you, you do not follow back",
            },
            # Asymmetric platforms have no degree, so the field is simply absent.
            tier_labels=None,
        )
    )


# ---------------------------------------------------------------------------
# the neutral layer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "direction", "lapsed", "expected"),
    [
        ("connected", "outbound", None, EdgeState.MUTUAL),
        ("connected", "inbound", None, EdgeState.MUTUAL),
        ("connected", None, None, EdgeState.MUTUAL_ORIGIN_UNKNOWN),
        ("pending", "outbound", None, EdgeState.OUTBOUND_PENDING),
        ("pending", "outbound", "2026-01-01T00:00:00Z", EdgeState.OUTBOUND_LAPSED),
        ("received", "inbound", None, EdgeState.INBOUND_PENDING),
        ("received", "inbound", "2026-01-01T00:00:00Z", EdgeState.INBOUND_LAPSED),
        ("withdrawn", "outbound", None, EdgeState.OUTBOUND_WITHDRAWN),
        ("unknown", None, None, EdgeState.UNKNOWN),
    ],
)
def test_edge_state_is_platform_independent(status, direction, lapsed, expected):
    """The same three stored fields map to the same neutral state everywhere."""
    assert edge_state(status, direction, lapsed) is expected


def test_is_linked_covers_both_mutual_shapes():
    assert is_linked(EdgeState.MUTUAL)
    assert is_linked(EdgeState.MUTUAL_ORIGIN_UNKNOWN)
    assert not is_linked(EdgeState.OUTBOUND_PENDING)
    assert not is_linked(EdgeState.INBOUND_LAPSED)


# ---------------------------------------------------------------------------
# the isolation claim
# ---------------------------------------------------------------------------


def test_a_new_platform_names_ties_in_its_own_words(fake_platform: Vocabulary):
    rows = [
        {"source": "fakegram", "status": "connected", "origin_direction": "outbound"},
        {"source": "fakegram", "status": "pending", "origin_direction": "outbound"},
        {"source": "fakegram", "status": "received", "origin_direction": "inbound"},
    ]
    describe_rows(rows)

    assert [r["how"] for r in rows] == [
        "we_follow_each_other",
        "i_follow_them",
        "they_follow_me",
    ]
    # No degree concept, so no degree field is invented.
    assert all("degree" not in r for r in rows)


def test_linkedin_is_unaffected_by_the_new_platform(fake_platform: Vocabulary):
    """Registering a platform must not change how another one reads."""
    rows = [
        {"source": "linkedin", "status": "connected", "origin_direction": "outbound"},
        {"source": "linkedin", "status": "connected", "origin_direction": "inbound"},
        {"source": "linkedin", "status": "pending", "origin_direction": "outbound"},
    ]
    describe_rows(rows)

    assert [r["how"] for r in rows] == [
        "i_invited_they_accepted",
        "they_invited_i_accepted",
        "i_invited_not_yet_accepted",
    ]
    assert [r["degree"] for r in rows] == ["1st", "1st", "2nd+"]


def test_one_result_set_can_mix_platforms(fake_platform: Vocabulary):
    """A search spanning sources labels each tie in its own platform's words.

    This is why naming happens in Python rather than in SQL: a single CASE
    expression would have to pick one platform's metaphor for every row.
    """
    rows = [
        {"source": "linkedin", "status": "pending", "origin_direction": "outbound"},
        {"source": "fakegram", "status": "pending", "origin_direction": "outbound"},
    ]
    describe_rows(rows)

    assert rows[0]["how"] == "i_invited_not_yet_accepted"
    assert rows[1]["how"] == "i_follow_them"
    # Same neutral state underneath, different words on top.
    assert rows[0]["edge_state"] == rows[1]["edge_state"] == str(EdgeState.OUTBOUND_PENDING)

    summary = breakdown(rows)
    assert summary["counts"] == {"i_invited_not_yet_accepted": 1, "i_follow_them": 1}
    # The legend explains both platforms' labels in one place.
    assert "i_follow_them" in summary["legend"]
    assert "i_invited_not_yet_accepted" in summary["legend"]


def test_an_unregistered_source_still_works():
    """A half-finished adapter degrades to neutral wording, never crashes."""
    rows = [{"source": "brand_new_thing", "status": "connected", "origin_direction": None}]
    describe_rows(rows)
    assert rows[0]["how"] == "linked_origin_unknown"
    assert vocabulary_for("brand_new_thing") is GENERIC
    assert vocabulary_for(None) is GENERIC


# ---------------------------------------------------------------------------
# structural guards
# ---------------------------------------------------------------------------


def test_core_contains_no_platform_terminology():
    """Structural guard: platform words must live in adapters/<source>/.

    This is the rule that lets someone add a platform without editing shared
    code, so it is asserted rather than documented — documentation does not
    fail a build.

    Only string *values* are inspected, via the AST. Comments and docstrings in
    core/ are free to discuss platforms (they have to, to explain the
    abstraction); what must never appear is a platform's word in data that a
    user or model will read. Schema identifiers such as `invited_at` are the
    deliberately shared storage contract and are not string values.
    """
    import ast

    banned = (
        "1st",
        "2nd+",
        "linkedin",
        "follower",
        "friend",
        "i_invited",
        "they_invited",
        "connected_unknown",
    )
    core = Path(search.__file__).parent
    offenders: list[str] = []

    for path in sorted(core.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = {
            node.body[0].value
            for node in ast.walk(tree)
            if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if node in docstrings:
                continue
            lowered = node.value.lower()
            for word in banned:
                if word in lowered:
                    offenders.append(f"{path.name}:{node.lineno} has {word!r} in a string value")

    assert not offenders, "platform terminology leaked into core/: " + "; ".join(offenders[:8])


def test_linkedin_package_owns_both_parser_and_words():
    import socialmcp.adapters.linkedin as package

    directory = Path(package.__file__).parent
    names = {p.name for p in directory.glob("*.py")}
    assert {"adapter.py", "vocabulary.py"} <= names
    assert package.VOCABULARY.source == "linkedin"
    assert package.VOCABULARY.contact_noun == "connection"
    assert package.VOCABULARY.tier_labels == ("1st", "2nd+")
