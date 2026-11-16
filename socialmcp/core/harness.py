"""The local agent harness: the body around the model's brain.

Without this, SocialMCP is purely reactive — nothing happens until you type a
prompt. The harness makes it a daemon-ish local service that does deterministic
work on its own and leaves a trail the AI can read:

  1. Ingestion watchdog   watch the drop folder; the moment a .zip lands (and
                          finishes copying), parse -> upsert -> cluster.
  2. Proactive clustering  every ingest re-derives event clusters, so bursts are
                          grouped before you ever ask about them.
  3. Activity log          each autonomous action writes a `harness_events` row,
                          so a new chat session can be told what it missed.
  4. Context injection     `briefing()` is a compact snapshot (pending invites,
                          recent events, stale follow-ups) exposed as an MCP
                          resource so the client can preload network state.

Deliberately framework-free: a thread, a debounce, and SQL. `watchdog` is used
when installed; otherwise a polling loop does the same job a bit less promptly.
No sockets are opened — the harness is in-process and local-only.
"""

from __future__ import annotations

import logging
import threading
import time
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ..config import Config
from .db import Database, json_dumps
from .ingest import IngestError, ingest_path
from .models import IngestStats
from .normalize import utcnow_iso
from .vocabulary import vocabulary_for

log = logging.getLogger("socialmcp.harness")

# A 400 MB LinkedIn export takes a moment to land on disk. Ingesting a
# half-written .zip is the single most likely way to corrupt a first run, so we
# wait for the size to hold steady before touching it.
STABLE_CHECKS = 3
STABLE_INTERVAL_SECONDS = 0.5
SETTLED_AGE_SECONDS = 5.0
POLL_INTERVAL_SECONDS = 5.0
STALE_FOLLOWUP_DAYS = 14


@dataclass(slots=True)
class HarnessStatus:
    running: bool
    watching: Path | None
    backend: str                 # 'watchdog' | 'poll' | 'off'
    ingests_this_session: int
    last_event: str | None


class Harness:
    """Owns the background file watcher and the autonomous ingest loop."""

    def __init__(self, db: Database, config: Config):
        self.db = db
        self.config = config
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._observer: Any = None
        self._backend = "off"
        self._ingests = 0
        self._lock = threading.RLock()

    # -- lifecycle -----------------------------------------------------------

    def start(self, *, catch_up: bool = True) -> HarnessStatus:
        """Begin watching. Safe to call twice; the second call is a no-op."""
        self.config.ensure_dirs()
        if catch_up:
            # Anything dropped while the harness was down still gets processed.
            self.scan_now(reason="startup catch-up")

        if not self.config.auto_ingest:
            self._backend = "off"
            return self.status()
        if self._thread or self._observer:
            return self.status()

        if self._start_watchdog():
            self._backend = "watchdog"
        else:
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._poll_loop, name="socialmcp-harness", daemon=True
            )
            self._thread.start()
            self._backend = "poll"

        self._event(
            "watch",
            f"harness watching {self.config.drop_dir} ({self._backend} backend)",
            level="info",
        )
        log.info("harness watching %s via %s", self.config.drop_dir, self._backend)
        return self.status()

    def stop(self) -> None:
        self._stop.set()
        if self._observer is not None:
            try:
                self._observer.stop()
                self._observer.join(timeout=5)
            finally:
                self._observer = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        self._backend = "off"

    def status(self) -> HarnessStatus:
        last = self.db.query_one(
            "SELECT summary FROM harness_events ORDER BY created_at DESC, id DESC LIMIT 1"
        )
        return HarnessStatus(
            running=self._backend != "off",
            watching=self.config.drop_dir if self._backend != "off" else None,
            backend=self._backend,
            ingests_this_session=self._ingests,
            last_event=last["summary"] if last else None,
        )

    def __enter__(self) -> Harness:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # -- watching ------------------------------------------------------------

    def _start_watchdog(self) -> bool:
        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer
        except ImportError:
            log.info("watchdog not installed; using a %ss polling loop", POLL_INTERVAL_SECONDS)
            return False

        harness = self

        class _DropHandler(FileSystemEventHandler):
            def on_created(self, event: Any) -> None:
                self._maybe(event)

            def on_moved(self, event: Any) -> None:
                self._maybe(event, attr="dest_path")

            def on_modified(self, event: Any) -> None:
                self._maybe(event)

            def _maybe(self, event: Any, attr: str = "src_path") -> None:
                if getattr(event, "is_directory", False):
                    return
                path = Path(getattr(event, attr, ""))
                if path.suffix.lower() == ".zip":
                    harness._handle_drop(path)

        try:
            observer = Observer()
            observer.schedule(_DropHandler(), str(self.config.drop_dir), recursive=True)
            observer.daemon = True
            observer.start()
        except OSError as exc:  # inotify limits, permissions, etc.
            log.warning("watchdog could not watch %s (%s); polling instead", self.config.drop_dir, exc)
            return False
        self._observer = observer
        return True

    def _poll_loop(self) -> None:
        seen: dict[Path, float] = {}
        while not self._stop.is_set():
            try:
                for path in sorted(self.config.drop_dir.rglob("*.zip")):
                    mtime = path.stat().st_mtime
                    if seen.get(path) == mtime:
                        continue
                    seen[path] = mtime
                    self._handle_drop(path)
            except OSError as exc:
                log.warning("harness poll error: %s", exc)
            self._stop.wait(POLL_INTERVAL_SECONDS)

    def _handle_drop(self, path: Path) -> None:
        if not _wait_until_stable(path):
            return
        with self._lock:
            try:
                stats = ingest_path(self.db, self.config, path)
            except (IngestError, OSError, zipfile.BadZipFile) as exc:
                self._event("error", f"ingest failed for {path.name}: {exc}", level="error")
                log.error("harness ingest failed for %s: %s", path.name, exc)
                return
            self._record_ingest(stats)

    # -- autonomous actions --------------------------------------------------

    def scan_now(self, *, reason: str = "manual") -> list[IngestStats]:
        """Process every .zip currently in the drop folder, synchronously."""
        self.config.ensure_dirs()
        results: list[IngestStats] = []
        with self._lock:
            for archive in sorted(self.config.drop_dir.rglob("*.zip"), key=lambda p: p.stat().st_mtime):
                if not _wait_until_stable(archive):
                    continue
                try:
                    stats = ingest_path(self.db, self.config, archive)
                except (IngestError, OSError, zipfile.BadZipFile) as exc:
                    self._event("error", f"ingest failed for {archive.name}: {exc}", level="error")
                    results.append(IngestStats(archive=archive.name, warnings=[str(exc)]))
                    continue
                self._record_ingest(stats, reason=reason)
                results.append(stats)
        return results

    def _record_ingest(self, stats: IngestStats, *, reason: str = "watch") -> None:
        if stats.skipped_duplicate:
            log.info("harness: %s already ingested, skipped", stats.archive)
            return
        self._ingests += 1
        parts = [f"{stats.contacts_new} new contacts"]
        if stats.contacts_updated:
            parts.append(f"{stats.contacts_updated} updated")
        if stats.contacts_unchanged:
            parts.append(f"{stats.contacts_unchanged} unchanged")
        if stats.interactions_new:
            parts.append(f"{stats.interactions_new} interactions")
        if stats.history_changes:
            parts.append(f"{stats.history_changes} job changes")
        # The platform supplies the noun: LinkedIn says "invitations", an
        # asymmetric source would say "follow requests".
        noun = vocabulary_for(stats.source).request_noun
        parts.append(f"{stats.pending_invitations} {noun}s still pending")
        summary = f"Processed {stats.archive}: " + ", ".join(parts)
        if stats.clusters_created:
            summary += f", {stats.clusters_created} new event cluster(s)"
        self._event(
            "ingest",
            summary,
            level="warning" if stats.warnings else "info",
            detail=stats.as_dict(),
            archive_id=stats.archive_id,
        )
        log.info("harness: %s (%s)", summary, reason)

    def _event(
        self,
        kind: str,
        summary: str,
        *,
        level: str = "info",
        detail: dict[str, Any] | None = None,
        archive_id: int | None = None,
    ) -> None:
        self.db.execute(
            """
            INSERT INTO harness_events (kind, level, summary, detail_json, archive_id, created_at)
            VALUES (?,?,?,?,?,?)
            """,
            (kind, level, summary, json_dumps(detail) if detail else None, archive_id, utcnow_iso()),
        )
        self.db.commit()

    # -- context injection ---------------------------------------------------

    def briefing(self, *, acknowledge: bool = False, limit: int = 5) -> dict[str, Any]:
        """A compact snapshot of network state, for preloading into a session.

        Kept small on purpose: this is meant to be injected into context, so it
        reports counts and a handful of names rather than dumping the CRM.
        """
        db = self.db
        unacked = db.query(
            "SELECT id, kind, level, summary, created_at FROM harness_events "
            "WHERE acknowledged = 0 ORDER BY created_at DESC, id DESC LIMIT 20"
        )
        recent_events = db.query(
            """
            SELECT ec.id, ec.slug, ec.name, ec.start_date, ec.end_date, ec.contact_count,
                   ec.renamed_by_user
            FROM event_clusters ec ORDER BY ec.end_date DESC LIMIT ?
            """,
            (limit,),
        )
        pending = db.query(
            "SELECT id, full_name, company, origin_note, invited_at, days_waiting "
            "FROM v_pending_invitations LIMIT ?",
            (limit,),
        )
        # The window is anchored to the newest connection in the database, not
        # to wall-clock time: importing a three-month-old export should still
        # produce a useful follow-up list instead of an empty one.
        stale = db.query(
            """
            SELECT c.id, c.full_name, c.company, c.origin_note, c.connected_on
            FROM contacts c
            WHERE c.status = 'connected'
              AND c.connected_on >= date(
                    COALESCE((SELECT MAX(connected_on) FROM contacts), 'now'), ?)
              AND NOT EXISTS (
                  SELECT 1 FROM interactions i
                  WHERE i.contact_id = c.id AND i.kind IN ('message', 'note')
              )
            ORDER BY c.connected_on DESC LIMIT ?
            """,
            (f"-{STALE_FOLLOWUP_DAYS * 3} days", limit),
        )
        job_changes = db.query(
            """
            SELECT c.full_name, h.field, h.old_value, h.new_value, h.changed_at
            FROM contact_history h JOIN contacts c ON c.id = h.contact_id
            ORDER BY h.changed_at DESC, h.id DESC LIMIT ?
            """,
            (limit,),
        )

        out: dict[str, Any] = {
            "generated_at": utcnow_iso(),
            "harness": asdict(self.status()) | {"watching": str(self.config.drop_dir)},
            "stats": db.stats(),
            "while_you_were_away": [dict(r) for r in unacked],
            "recent_event_clusters": [dict(r) for r in recent_events],
            "pending_invitations": [dict(r) for r in pending],
            "recent_contacts_never_followed_up": [dict(r) for r in stale],
            "recent_job_changes": [dict(r) for r in job_changes],
            "drop_folder": str(self.config.drop_dir),
            "hint": (
                "Unnamed clusters look like '12-15 Oct 2025 burst (17 connections)'. "
                "Ask the user what the event was and call rename_event_cluster."
            ),
        }

        if acknowledge and unacked:
            db.executemany(
                "UPDATE harness_events SET acknowledged = 1 WHERE id = ?",
                [(row["id"],) for row in unacked],
            )
            db.commit()
        return out


def _wait_until_stable(path: Path) -> bool:
    """Block until the file's size stops changing, so we never read a partial zip."""
    try:
        # A file that has not been touched for a while is already fully written
        # (this is the common case at startup catch-up), so skip the debounce.
        if time.time() - path.stat().st_mtime > SETTLED_AGE_SECONDS:
            return True
    except FileNotFoundError:
        return False

    last = -1
    stable = 0
    for _ in range(120):  # ~2 minutes of patience for a big export
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            return False
        if size == last and size > 0:
            stable += 1
            if stable >= STABLE_CHECKS:
                return True
        else:
            stable = 0
            last = size
        time.sleep(STABLE_INTERVAL_SECONDS)
    log.warning("%s never stopped changing; skipping for now", path.name)
    return False
