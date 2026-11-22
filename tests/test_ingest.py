"""End-to-end pipeline tests: zip in, queryable CRM out."""

from __future__ import annotations

import shutil
import zipfile
from pathlib import Path

import pytest

from socialmcp.config import Config
from socialmcp.core.db import Database
from socialmcp.core.ingest import IngestError, add_note, extract_archive, ingest_path
from socialmcp.core.search import SearchFilters, search_network

from .conftest import make_zip


def test_full_ingest_from_zip(db: Database, config: Config, linkedin_zip: Path):
    stats = ingest_path(db, config, linkedin_zip)
    assert stats.source == "linkedin"
    # 10 connections + Rafael (outbound invite, never accepted) + Yuki (inbound invite).
    # The archive owner (Avery Chen) is never stored as a contact.
    assert stats.contacts_new == 12
    assert "Connections.csv" in stats.files_parsed
    assert "Invitations.csv" in stats.files_parsed
    assert db.scalar("SELECT COUNT(*) FROM contacts") == 12


def test_invite_note_merges_onto_url_keyed_connection(db: Database, config: Config, linkedin_dir: Path):
    """The core quirk: Invitations.csv has no URL, Connections.csv does.

    They must collapse into ONE contact that is 'connected' AND carries the
    clipboard origin note — not a connected row plus a phantom pending row.
    """
    ingest_path(db, config, linkedin_dir)
    rows = db.query("SELECT * FROM contacts WHERE full_name = 'Priya Raghavan'")
    assert len(rows) == 1
    priya = rows[0]
    assert priya["status"] == "connected"
    assert priya["source_key"] == "in/priya-raghavan-ai"
    assert "Seattle AI Week" in priya["origin_note"]
    assert priya["origin_direction"] == "outbound"
    assert priya["invited_at"] == "2025-10-13T18:42:00Z"
    assert priya["connected_on"] == "2025-10-13"


def test_pending_invitations_are_tracked(db: Database, config: Config, linkedin_dir: Path):
    ingest_path(db, config, linkedin_dir)
    pending = db.query("SELECT * FROM v_pending_invitations")
    assert [p["full_name"] for p in pending] == ["Rafael Santos"]
    assert "eval tooling" in pending[0]["origin_note"]


def test_messages_attach_to_contacts_but_never_create_them(
    db: Database, config: Config, linkedin_dir: Path
):
    ingest_path(db, config, linkedin_dir)
    priya_id = db.scalar("SELECT id FROM contacts WHERE full_name = 'Priya Raghavan'")
    messages = db.query(
        "SELECT * FROM interactions WHERE contact_id = ? AND kind = 'message' ORDER BY occurred_at",
        (priya_id,),
    )
    # Matched by display name even though the DM used an opaque ACoAAB… URL.
    assert len(messages) == 2
    assert messages[0]["direction"] == "outbound"
    assert db.scalar("SELECT last_interaction_at FROM contacts WHERE id = ?", (priya_id,)).startswith(
        "2025-10-21"
    )
    assert db.scalar("SELECT COUNT(*) FROM contacts WHERE full_name = 'Avery Chen'") == 0


def test_event_cluster_detected_from_burst(db: Database, config: Config, linkedin_dir: Path):
    ingest_path(db, config, linkedin_dir)
    clusters = db.query("SELECT * FROM event_clusters")
    assert len(clusters) == 1
    cluster = clusters[0]
    assert cluster["start_date"] == "2025-10-13"
    # Runs to the 16th and holds 9 people because membership is by the date you
    # MET someone: Rafael's invite (15th) was never accepted, and Yuki invited
    # you back on the 16th. Both were at the event; only acceptance lagged.
    assert cluster["end_date"] == "2025-10-16"
    assert cluster["contact_count"] == 9
    assert cluster["auto_generated"] == 1
    # Scattered connections (March, June) are not events.
    members = db.query(
        "SELECT c.full_name FROM contacts c JOIN contact_event_clusters cec ON cec.contact_id = c.id"
    )
    names = {m["full_name"] for m in members}
    assert "Luis Ferreira" not in names and "Aisha Bello" not in names


def test_reingesting_identical_archive_is_a_noop(db: Database, config: Config, linkedin_zip: Path):
    first = ingest_path(db, config, linkedin_zip)
    assert not first.skipped_duplicate
    # ingest_path moved the zip out of the drop dir only if it lived there; here
    # it is in tmp_path, so the same file can be offered again.
    second = ingest_path(db, config, linkedin_zip)
    assert second.skipped_duplicate
    assert db.scalar("SELECT COUNT(*) FROM contacts") == 12


def test_second_monthly_drop_records_job_change(
    db: Database, config: Config, linkedin_dir: Path, linkedin_month2_dir: Path
):
    ingest_path(db, config, linkedin_dir)
    stats = ingest_path(db, config, linkedin_month2_dir)

    assert stats.history_changes == 2  # Priya: company + position
    history = db.query(
        """
        SELECT h.field, h.old_value, h.new_value FROM contact_history h
        JOIN contacts c ON c.id = h.contact_id
        WHERE c.full_name = 'Priya Raghavan' ORDER BY h.field
        """
    )
    assert [(h["field"], h["old_value"], h["new_value"]) for h in history] == [
        ("company", "Northwind AI", "Aurora Labs"),
        ("position", "Head of GTM", "VP Go-To-Market"),
    ]
    assert db.scalar("SELECT company FROM contacts WHERE full_name = 'Priya Raghavan'") == "Aurora Labs"
    # Priya is still one row, and the original invite note is untouched.
    assert db.scalar("SELECT COUNT(*) FROM contacts WHERE full_name = 'Priya Raghavan'") == 1
    assert "Seattle AI Week" in db.scalar(
        "SELECT origin_note FROM contacts WHERE full_name = 'Priya Raghavan'"
    )


def test_pending_invite_promotes_to_connected_on_later_drop(
    db: Database, config: Config, linkedin_dir: Path, linkedin_month2_dir: Path
):
    ingest_path(db, config, linkedin_dir)
    assert db.scalar("SELECT status FROM contacts WHERE full_name = 'Rafael Santos'") == "pending"
    ingest_path(db, config, linkedin_month2_dir)
    rafael = db.query_one("SELECT * FROM contacts WHERE full_name = 'Rafael Santos'")
    assert rafael["status"] == "connected"
    assert rafael["connected_on"] == "2025-10-18"
    assert "eval tooling" in rafael["origin_note"]      # origin note survives
    assert rafael["source_key"] == "in/rafaelsantos"    # key promoted to the URL form


def test_notes_are_searchable(db: Database, config: Config, linkedin_dir: Path):
    ingest_path(db, config, linkedin_dir)
    contact_id = db.scalar("SELECT id FROM contacts WHERE full_name = 'Dana Olufemi'")
    add_note(db, contact_id, "Wants a pilot in Q1; budget owner is her VP.")

    result = search_network(db, config, query="pilot budget", mode="keyword", limit=5)
    assert [c["full_name"] for c in result.contacts] == ["Dana Olufemi"]
    assert result.contacts[0]["notes"][0]["note"].startswith("Wants a pilot")


def test_search_filters_and_event_scope(db: Database, config: Config, linkedin_dir: Path):
    ingest_path(db, config, linkedin_dir)
    slug = db.scalar("SELECT slug FROM event_clusters LIMIT 1")

    scoped = search_network(db, config, filters=SearchFilters(event=slug), limit=50)
    assert len(scoped.contacts) == 9

    sales = search_network(db, config, query="enterprise AI sales", mode="keyword", limit=5)
    assert "Dana Olufemi" in [c["full_name"] for c in sales.contacts]

    by_company = search_network(db, config, filters=SearchFilters(company="Cascade"), limit=5)
    assert [c["full_name"] for c in by_company.contacts] == ["Marcus Webb"]


def test_fts_query_with_punctuation_does_not_explode(db: Database, config: Config, linkedin_dir: Path):
    ingest_path(db, config, linkedin_dir)
    # Bare FTS operators in user text would be a syntax error if not quoted.
    for query in ["AI AND OR", "go-to-market", 'he said "hi"', "C++ / SQL*"]:
        result = search_network(db, config, query=query, limit=5)
        assert isinstance(result.contacts, list)


def test_zip_slip_is_rejected(tmp_path: Path):
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as archive:
        archive.writestr("../../escaped.txt", "nope")
    with pytest.raises(IngestError, match="unsafe path"):
        extract_archive(evil, tmp_path / "out")


def test_unknown_archive_is_rejected(db: Database, config: Config, tmp_path: Path):
    junk = tmp_path / "junk"
    junk.mkdir()
    (junk / "random.txt").write_text("hello")
    with pytest.raises(IngestError, match="no adapter recognized"):
        ingest_path(db, config, junk)


def test_drop_folder_archive_is_moved_after_ingest(
    db: Database, config: Config, linkedin_dir: Path
):
    config.ensure_dirs()
    dropped = make_zip(linkedin_dir, config.drop_dir / "export.zip")
    ingest_path(db, config, dropped)
    assert not dropped.exists()
    assert (config.archive_dir / "export.zip").exists()


def test_identical_redrop_reports_unchanged_not_updated(
    db: Database, config: Config, linkedin_dir: Path, tmp_path: Path
):
    """A re-packaged export with the same content must look like a no-op.

    LinkedIn's "Complete" archive contains the same Connections/Invitations/
    messages files as the basic one plus extra files we don't parse, so it has a
    different SHA-256 and is genuinely re-ingested. Everyone must match in
    place: no duplicates, no phantom interactions, no false job changes, and the
    report must say "unchanged" rather than claiming hundreds of updates.
    """
    first = ingest_path(db, config, linkedin_dir)
    assert first.contacts_new == 12

    # Same data, different archive identity (extra unparsed file).
    repackaged = tmp_path / "complete"
    shutil.copytree(linkedin_dir, repackaged)
    (repackaged / "SearchQueries.csv").write_text("Time,Search Query\n2025-10-13,ai week\n")

    second = ingest_path(db, config, repackaged)

    assert second.skipped_duplicate is False      # it really was processed
    assert second.contacts_new == 0
    assert second.contacts_updated == 0           # nothing actually changed
    assert second.contacts_unchanged == 12
    assert second.interactions_new == 0
    assert second.history_changes == 0
    assert db.scalar("SELECT COUNT(*) FROM contacts") == 12
    assert db.scalar("SELECT COUNT(*) FROM archives") == 2


def test_met_window_includes_invitations_that_were_never_accepted(
    db: Database, config: Config, linkedin_dir: Path
):
    """"Who did I meet at the conference" must not silently drop non-acceptances.

    A date filter on connected_on cannot see someone you invited at an event who
    never accepted — and that person is the whole point of a follow-up list. The
    met_* window matches the connection date, or the invitation date when there
    is no connection.
    """
    ingest_path(db, config, linkedin_dir)

    connected_only = search_network(
        db,
        config,
        filters=SearchFilters(connected_after="2025-10-13", connected_before="2025-10-16"),
        limit=50,
    )
    met = search_network(
        db, config, filters=SearchFilters(met_after="2025-10-13", met_before="2025-10-16"), limit=50
    )

    connected_names = {c["full_name"] for c in connected_only.contacts}
    met_names = {c["full_name"] for c in met.contacts}

    assert "Rafael Santos" not in connected_names    # invited, never accepted
    assert "Rafael Santos" in met_names
    assert met_names > connected_names
    assert met.contacts, "met window must return the event's people"


def test_event_cluster_contains_the_unaccepted_invite(
    db: Database, config: Config, linkedin_dir: Path
):
    ingest_path(db, config, linkedin_dir)
    members = db.query(
        """
        SELECT c.full_name, c.status FROM contacts c
        JOIN contact_event_clusters cec ON cec.contact_id = c.id
        """
    )
    by_name = {m["full_name"]: m["status"] for m in members}
    assert by_name.get("Rafael Santos") == "pending"
    assert by_name.get("Yuki Tanaka") == "received"
    # People connected months apart are still not part of any event.
    assert "Luis Ferreira" not in by_name


def _write_invitations(path: Path, rows: list[str]) -> None:
    header = "From,To,Sent At,Message,Direction\n"
    path.write_text(header + "".join(rows), encoding="utf-8")


def test_invitation_that_vanishes_without_connecting_is_marked_lapsed(
    db: Database, config: Config, linkedin_dir: Path, tmp_path: Path
):
    """Declined/withdrawn invitations are inferred, because LinkedIn omits them.

    A declined invitation does not appear in any export — the row simply
    disappears from Invitations.csv. So the signal is: it was pending in an
    earlier archive, it is gone from this archive's invitations list, and the
    person still is not in Connections.csv.
    """
    ingest_path(db, config, linkedin_dir)
    assert db.scalar("SELECT status FROM contacts WHERE full_name='Rafael Santos'") == "pending"
    assert "Rafael Santos" in [r["full_name"] for r in db.query("SELECT * FROM v_pending_invitations")]

    # Next month's archive: Rafael's invitation is gone, Yuki's is still there,
    # and neither appears in Connections.csv.
    later = tmp_path / "month2"
    shutil.copytree(linkedin_dir, later)
    _write_invitations(
        later / "Invitations.csv",
        [
            'Avery Chen,Priya Raghavan,"10/13/25, 6:42 PM","Great meeting you at Seattle AI Week today!",OUTGOING\n',
            'Yuki Tanaka,Avery Chen,"10/16/25, 8:15 AM","Enjoyed your question at the keynote.",INCOMING\n',
        ],
    )

    stats = ingest_path(db, config, later)

    assert stats.invites_lapsed == 1
    rafael = db.query_one("SELECT * FROM contacts WHERE full_name='Rafael Santos'")
    assert rafael["invite_lapsed_at"] is not None
    assert rafael["status"] == "pending"          # the raw fact is preserved
    assert rafael["origin_note"]                  # and so is how they were met
    # He drops off the follow-up list, because there is nothing left to wait for.
    assert "Rafael Santos" not in [
        r["full_name"] for r in db.query("SELECT * FROM v_pending_invitations")
    ]
    # Yuki's invitation is still live and must not be touched.
    assert db.scalar("SELECT invite_lapsed_at FROM contacts WHERE full_name='Yuki Tanaka'") is None


def test_invitations_outside_the_export_window_are_never_marked_lapsed(
    db: Database, config: Config, linkedin_dir: Path, tmp_path: Path
):
    """Absence only means something inside the window the export covers.

    LinkedIn's Invitations.csv is a rolling window (~6 months in observed
    archives). An older invitation naturally ages out of the file, and reading
    that as a rejection would silently delete real follow-ups.
    """
    ingest_path(db, config, linkedin_dir)

    later = tmp_path / "month2"
    shutil.copytree(linkedin_dir, later)
    # This archive's invitations all post-date the fixture's, so every earlier
    # invitation is outside its window.
    _write_invitations(
        later / "Invitations.csv",
        ['Avery Chen,Nora Blake,"03/02/26, 9:00 AM","Good to meet you.",OUTGOING\n'],
    )

    stats = ingest_path(db, config, later)

    assert stats.invites_lapsed == 0
    assert db.scalar("SELECT invite_lapsed_at FROM contacts WHERE full_name='Rafael Santos'") is None


def test_a_resent_invitation_clears_a_previous_lapse(
    db: Database, config: Config, linkedin_dir: Path, tmp_path: Path
):
    ingest_path(db, config, linkedin_dir)
    later = tmp_path / "month2"
    shutil.copytree(linkedin_dir, later)
    _write_invitations(
        later / "Invitations.csv",
        ['Avery Chen,Priya Raghavan,"10/13/25, 6:42 PM","note",OUTGOING\n'],
    )
    ingest_path(db, config, later)
    assert db.scalar("SELECT invite_lapsed_at FROM contacts WHERE full_name='Rafael Santos'")

    # Third archive: the invitation is back, so the lapse was wrong (or they
    # were re-invited). Either way the flag must clear.
    third = tmp_path / "month3"
    shutil.copytree(linkedin_dir, third)
    ingest_path(db, config, third)
    assert db.scalar("SELECT invite_lapsed_at FROM contacts WHERE full_name='Rafael Santos'") is None


def test_extraction_accepts_normal_members_on_any_platform(tmp_path: Path):
    """Regression: the escape check must not depend on the path separator.

    The original check compared `str(target).startswith(str(dest) + "/")`,
    which on Windows never matches because resolved paths use backslashes. The
    result was that every member of every archive was rejected as a traversal
    attempt, so ingestion was completely broken on that platform while passing
    every test on macOS and Linux.
    """
    archive = tmp_path / "ok.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("Connections.csv", "First Name,Last Name\nA,B\n")
        z.writestr("nested/dir/messages.csv", "CONVERSATION ID\n1\n")

    out = tmp_path / "out"
    extract_archive(archive, out)

    assert (out / "Connections.csv").is_file()
    assert (out / "nested" / "dir" / "messages.csv").is_file()


def test_extraction_still_rejects_real_escapes(tmp_path: Path):
    """The portable check must not have loosened the protection it replaced."""
    for bad_name in ("../escaped.txt", "a/../../escaped.txt", "/abs/escaped.txt"):
        archive = tmp_path / "evil.zip"
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr(bad_name, "nope")
        with pytest.raises(IngestError):
            extract_archive(archive, tmp_path / f"out-{abs(hash(bad_name))}")


def test_a_recognised_duplicate_is_filed_away_not_left_in_the_drop_folder(
    db: Database, config: Config, linkedin_zip: Path
):
    """The drop folder means "waiting to be read". A duplicate is not waiting.

    Re-offering an export that is already in the database used to leave it
    sitting in the drop folder for good, because the move happened after the
    duplicate check returned. The folder then filled up with exports that
    looked unprocessed — and its own README promises the opposite: "once read,
    the file moves to ingested/ so it is not read twice".
    """
    import shutil

    dropped = config.drop_dir / "linkedin" / linkedin_zip.name
    dropped.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(linkedin_zip, dropped)

    first = ingest_path(db, config, dropped)
    assert not first.skipped_duplicate
    assert not dropped.exists(), "a processed export should leave the drop folder"
    filed = config.archive_dir / "linkedin" / linkedin_zip.name
    assert filed.exists(), "and should arrive in the ingested folder"

    # Offer the very same bytes again, as someone re-picking yesterday's file.
    again = config.drop_dir / "linkedin" / linkedin_zip.name
    shutil.copy2(linkedin_zip, again)
    second = ingest_path(db, config, again)

    assert second.skipped_duplicate, "identical content must still be recognised"
    assert not again.exists(), (
        "a duplicate must not be left behind in the drop folder either"
    )
    # Filed under a distinct name, so neither copy is destroyed by the other.
    filed_copies = sorted((config.archive_dir / "linkedin").glob("*.zip"))
    assert len(filed_copies) == 2, f"expected both copies kept, got {filed_copies}"
    assert db.scalar("SELECT COUNT(*) FROM contacts") == 12, "and nothing re-imported"


def test_a_duplicate_outside_the_drop_folder_is_left_where_it_is(
    db: Database, config: Config, linkedin_zip: Path
):
    """Filing away is for our own folder. A user's own file is never moved."""
    ingest_path(db, config, linkedin_zip)
    assert linkedin_zip.exists()
    second = ingest_path(db, config, linkedin_zip)
    assert second.skipped_duplicate
    assert linkedin_zip.exists(), "a file we were merely pointed at stays put"
