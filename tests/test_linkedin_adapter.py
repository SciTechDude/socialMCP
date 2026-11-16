"""Adapter-level tests: the CSV quirks, in isolation from the database."""

from __future__ import annotations

from pathlib import Path

import pytest

from socialmcp.adapters.base import ArchiveContext, HeaderNotFoundError, read_csv_rows
from socialmcp.adapters.linkedin.adapter import LinkedInAdapter
from socialmcp.adapters.linkedin.urls import normalize as normalize_profile_url
from socialmcp.core.models import ContactRecord, InteractionRecord
from socialmcp.core.normalize import (
    name_key,
    parse_date,
    parse_timestamp,
)


def parse_all(root: Path):
    ctx = ArchiveContext(root=root, archive_name=root.name)
    records = list(LinkedInAdapter().parse(ctx))
    contacts = [r for r in records if isinstance(r, ContactRecord)]
    interactions = [r for r in records if isinstance(r, InteractionRecord)]
    return ctx, contacts, interactions


def test_skips_multiline_preamble_and_bom(linkedin_dir: Path):
    rows = list(read_csv_rows(linkedin_dir / "Connections.csv", ("first_name", "last_name")))
    assert len(rows) == 10
    # BOM must not end up glued to the first header name.
    assert rows[0]["first_name"] == "Priya"
    assert rows[0]["connected_on"] == "13 Oct 2025"


def test_header_detection_fails_loudly_on_garbage(tmp_path: Path):
    bad = tmp_path / "Connections.csv"
    bad.write_text("just,some,random\n1,2,3\n")
    with pytest.raises(HeaderNotFoundError):
        list(read_csv_rows(bad, ("first_name", "last_name")))


def test_detects_archive_owner(linkedin_dir: Path):
    ctx = ArchiveContext(root=linkedin_dir, archive_name="t")
    assert LinkedInAdapter()._detect_owner(ctx) == "Avery Chen"


def test_origin_note_extracted_from_invitations(linkedin_dir: Path):
    _, contacts, _ = parse_all(linkedin_dir)
    notes = {c.full_name: c.origin_note for c in contacts if c.origin_note}
    assert "Seattle AI Week" in notes["Priya Raghavan"]
    assert notes["Marcus Webb"] == "Great meeting you at Seattle AI Week today!"


def test_invitation_direction_and_status(linkedin_dir: Path):
    _, contacts, _ = parse_all(linkedin_dir)
    by_name = {c.full_name: c for c in contacts}
    # Outbound invite, never in Connections.csv -> provisional 'pending'.
    assert by_name["Rafael Santos"].status == "pending"
    assert by_name["Rafael Santos"].origin_direction == "outbound"
    # Inbound invite -> 'received', and the owner is never a contact.
    assert by_name["Yuki Tanaka"].status == "received"
    assert by_name["Yuki Tanaka"].origin_direction == "inbound"
    assert "Avery Chen" not in by_name


def test_connection_without_url_uses_name_key(linkedin_dir: Path):
    _, contacts, _ = parse_all(linkedin_dir)
    ingrid = next(c for c in contacts if c.full_name == "Ingrid Novak" and c.company)
    assert ingrid.source_key == name_key("Ingrid Novak", "Sound Transit Labs")
    assert ingrid.profile_url is None


def test_bad_date_row_is_warned_not_fatal(linkedin_dir: Path):
    ctx, contacts, _ = parse_all(linkedin_dir)
    oscar = next(c for c in contacts if c.full_name == "Oscar Mendes")
    assert oscar.connected_on is None
    assert any("unparseable date" in w for w in ctx.warnings)
    # The remaining 9 connections still parsed.
    assert sum(1 for c in contacts if c.status == "connected") == 10


def test_messages_are_resolve_only(linkedin_dir: Path):
    _, _, interactions = parse_all(linkedin_dir)
    messages = [i for i in interactions if i.kind == "message"]
    assert messages, "expected DM rows"
    assert all(m.resolve_only for m in messages)
    outbound = [m for m in messages if m.direction == "outbound"]
    inbound = [m for m in messages if m.direction == "inbound"]
    assert outbound and inbound


def test_interaction_dedupe_keys_are_unique(linkedin_dir: Path):
    _, _, interactions = parse_all(linkedin_dir)
    keys = [i.dedupe_key for i in interactions]
    assert len(keys) == len(set(keys))


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://www.linkedin.com/in/Jane-Doe/", "https://www.linkedin.com/in/jane-doe"),
        ("linkedin.com/in/jane-doe?trk=contact-info", "https://www.linkedin.com/in/jane-doe"),
        ("https://www.linkedin.com/pub/jane-doe/1/2/3", "https://www.linkedin.com/in/jane-doe"),
        ("", None),
        ("not a url", None),
    ],
)
def test_profile_url_normalization(raw, expected):
    assert normalize_profile_url(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("13 Oct 2025", "2025-10-13"),
        ("2025-10-13", "2025-10-13"),
        ("Oct 13, 2025", "2025-10-13"),
        ("garbage", None),
    ],
)
def test_date_parsing(raw, expected):
    assert parse_date(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("10/13/25, 6:42 PM", "2025-10-13T18:42:00Z"),
        ("2025-10-20 17:04:11 UTC", "2025-10-20T17:04:11Z"),
        ("1760000000", "2025-10-09T08:53:20Z"),  # epoch seconds
        ("13 Oct 2025", "2025-10-13T00:00:00Z"),
    ],
)
def test_timestamp_parsing(raw, expected):
    assert parse_timestamp(raw) == expected
