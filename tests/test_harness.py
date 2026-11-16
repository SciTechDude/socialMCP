"""Harness tests: autonomous ingest, the activity log, and the briefing snapshot."""

from __future__ import annotations

import time
from pathlib import Path

from socialmcp.core.harness import Harness
from socialmcp.core.ingest import add_note

from .conftest import make_zip


def test_scan_now_picks_up_a_dropped_zip(db, config, linkedin_dir: Path):
    config.ensure_dirs()
    make_zip(linkedin_dir, config.drop_dir / "LinkedInDataExport.zip")

    harness = Harness(db, config)
    results = harness.scan_now(reason="test")

    assert len(results) == 1
    assert results[0].contacts_new == 12
    assert db.scalar("SELECT COUNT(*) FROM harness_events WHERE kind = 'ingest'") == 1
    summary = db.scalar("SELECT summary FROM harness_events WHERE kind = 'ingest'")
    assert "12 new contacts" in summary
    assert "new event cluster" in summary


def test_harness_logs_errors_without_raising(db, config, tmp_path: Path):
    config.ensure_dirs()
    (config.drop_dir / "broken.zip").write_bytes(b"not a zip at all")

    harness = Harness(db, config)
    results = harness.scan_now(reason="test")

    assert results and results[0].warnings
    assert db.scalar("SELECT COUNT(*) FROM harness_events WHERE level = 'error'") == 1


def test_briefing_reports_unacknowledged_work(db, config, linkedin_dir: Path):
    config.ensure_dirs()
    make_zip(linkedin_dir, config.drop_dir / "export.zip")
    harness = Harness(db, config)
    harness.scan_now(reason="test")

    briefing = harness.briefing(acknowledge=False)
    assert briefing["while_you_were_away"]
    assert briefing["stats"]["contacts"] == 12
    assert briefing["pending_invitations"][0]["full_name"] == "Rafael Santos"
    assert briefing["recent_event_clusters"][0]["contact_count"] == 9
    # Nobody has been messaged or noted except via fixtures, so follow-ups show up.
    assert briefing["recent_contacts_never_followed_up"]

    # Reading twice without acknowledging keeps the backlog visible.
    assert harness.briefing(acknowledge=True)["while_you_were_away"]
    assert harness.briefing(acknowledge=False)["while_you_were_away"] == []


def test_briefing_excludes_followed_up_contacts(db, config, linkedin_dir: Path):
    config.ensure_dirs()
    make_zip(linkedin_dir, config.drop_dir / "export.zip")
    harness = Harness(db, config)
    harness.scan_now(reason="test")

    before = {c["full_name"] for c in harness.briefing()["recent_contacts_never_followed_up"]}
    target = db.scalar("SELECT id FROM contacts WHERE full_name = 'Ben Kowalski'")
    add_note(db, target, "Sent him the deck.")
    after = {c["full_name"] for c in harness.briefing()["recent_contacts_never_followed_up"]}

    assert "Ben Kowalski" in before
    assert "Ben Kowalski" not in after


def test_watcher_ingests_on_drop(db, config, linkedin_dir: Path, tmp_path: Path):
    """The watchdog/polling loop must notice a file that appears after startup."""
    config.auto_ingest = True
    config.ensure_dirs()
    harness = Harness(db, config)
    harness.start()
    try:
        assert harness.status().backend in {"watchdog", "poll"}
        make_zip(linkedin_dir, tmp_path / "staged.zip").replace(config.drop_dir / "late.zip")

        deadline = time.time() + 30
        while time.time() < deadline:
            if db.scalar("SELECT COUNT(*) FROM contacts") >= 12:
                break
            time.sleep(0.5)
    finally:
        harness.stop()

    assert db.scalar("SELECT COUNT(*) FROM contacts") == 12
    assert not (config.drop_dir / "late.zip").exists()
