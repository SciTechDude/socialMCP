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


def test_embeddings_are_backfilled_when_a_provider_turns_up_later(
    db, config, linkedin_dir: Path, monkeypatch
):
    """An import with no embedding provider must not stay unindexed for good.

    This is the real-world sequence: someone imports their export, no Ollama
    is running, and the contacts are left marked dirty. Nothing used to come
    back for them, so meaning-based search stayed off until the user asked
    for a reindex — which they never would, because a search that silently
    matches words literally does not look broken.
    """
    from socialmcp.core import ingest as ingest_module

    config.ensure_dirs()
    make_zip(linkedin_dir, config.drop_dir / "LinkedInDataExport.zip")

    harness = Harness(db, config)
    harness.scan_now(reason="test")

    pending = db.scalar("SELECT COUNT(*) FROM contacts WHERE embedding_dirty = 1")
    assert pending == 12, "nothing should be indexed yet: no provider was running"

    # The provider appears. One pass of the loop body is what a tick does.
    calls: list[int] = []

    class Fake:
        def embed(self, texts):
            calls.append(len(texts))
            return [[0.05] * config.embedding.dim for _ in texts]

    monkeypatch.setattr(ingest_module, "make_embedder", lambda _cfg: Fake())
    monkeypatch.setattr(db, "vec_enabled", True, raising=False)
    monkeypatch.setattr(config.embedding, "provider", "ollama")

    written = ingest_module.refresh_embeddings(db, config)

    assert calls, "the provider should have been used"
    assert written == 12
    assert db.scalar("SELECT COUNT(*) FROM contacts WHERE embedding_dirty = 1") == 0


def test_the_backfill_thread_runs_under_every_watch_backend(db, config):
    """It must not live inside the poll loop, which watchdog replaces.

    Under the watchdog backend `_poll_loop` never executes, and watchdog is
    the backend on a normal Mac — so a backfill hung off that loop would be
    missing on precisely the machines that need it.
    """
    import inspect

    from socialmcp.core import harness as harness_module

    poll_source = inspect.getsource(harness_module.Harness._poll_loop)
    assert "refresh_embeddings" not in poll_source, (
        "the backfill must not depend on the poll loop running"
    )

    start_source = inspect.getsource(harness_module.Harness.start)
    assert "_start_backfill" in start_source, "start() should launch the backfill"

    # And it is skipped entirely when there is nothing it could ever do.
    config.embedding.provider = "none"
    harness = Harness(db, config)
    harness._start_backfill()
    assert harness._backfill_thread is None, (
        "no thread should start when embeddings are switched off"
    )
    harness.stop()


def test_the_backfill_backs_off_when_no_provider_answers(db, config, monkeypatch):
    """On a Mac with no Ollama — most of them — this must cost nothing.

    Probing a closed port every half minute for the life of the server is the
    failure mode to avoid, so the interval doubles up to a ceiling.
    """
    from socialmcp.core import harness as harness_module

    assert harness_module.BACKFILL_INTERVAL_SECONDS >= 30, "do not poll aggressively"
    assert harness_module.BACKFILL_MAX_INTERVAL_SECONDS >= 600, (
        "the ceiling should be minutes, not seconds"
    )

    import inspect

    source = inspect.getsource(harness_module.Harness._backfill_loop)
    assert "min(delay * 2" in source, "the interval should back off, not stay fixed"
    assert "BACKFILL_MAX_INTERVAL_SECONDS" in source
    # A batch, not the whole table: this shares the database with live queries.
    assert "limit=BACKFILL_BATCH" in source
    assert harness_module.BACKFILL_BATCH <= 256


def test_the_backfill_thread_actually_indexes_without_being_asked(
    db, config, linkedin_dir: Path, monkeypatch
):
    """Drives the real thread, not just the function it calls.

    The function was already being called at import time and still left
    everything dirty, so the part worth testing is the thing that is new: a
    loop that comes back on its own.
    """
    from socialmcp.core import harness as harness_module
    from socialmcp.core import ingest as ingest_module

    config.ensure_dirs()
    make_zip(linkedin_dir, config.drop_dir / "LinkedInDataExport.zip")
    harness = Harness(db, config)
    harness.scan_now(reason="test")
    assert db.scalar("SELECT COUNT(*) FROM contacts WHERE embedding_dirty = 1") == 12

    class Fake:
        def embed(self, texts):
            return [[0.05] * config.embedding.dim for _ in texts]

    monkeypatch.setattr(ingest_module, "make_embedder", lambda _cfg: Fake())
    monkeypatch.setattr(db, "vec_enabled", True, raising=False)
    monkeypatch.setattr(config.embedding, "provider", "ollama")
    monkeypatch.setattr(harness_module, "BACKFILL_INTERVAL_SECONDS", 0.05)
    monkeypatch.setattr(harness_module, "BACKFILL_MAX_INTERVAL_SECONDS", 0.05)
    monkeypatch.setattr(harness_module, "BACKFILL_BATCH", 5)   # forces several passes

    harness._start_backfill()
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if not db.scalar("SELECT COUNT(*) FROM contacts WHERE embedding_dirty = 1"):
                break
            time.sleep(0.05)
    finally:
        harness.stop()

    assert db.scalar("SELECT COUNT(*) FROM contacts WHERE embedding_dirty = 1") == 0, (
        "the background thread should have indexed everything with no prompting"
    )
    # And it said so, once, where the briefing can report it.
    summaries = [
        row["summary"]
        for row in db.query("SELECT summary FROM harness_events WHERE kind = 'embed'")
    ]
    assert any("indexing" in s for s in summaries), summaries
    assert any("ready" in s for s in summaries), summaries
