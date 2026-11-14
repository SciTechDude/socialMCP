"""Deterministic temporal event clustering — no LLM involved.

The insight this encodes: at a conference you add 15 people in three days, and
on a normal week you add one. A burst of connection dates *is* an event, and
detecting it mechanically gives the AI client a queryable handle ("who did I
meet at Seattle AI Week?") without asking a model to guess.

Algorithm (greedy single pass over sorted connection dates):
    * walk dates ascending; extend the current burst while
        - the gap to the previous date <= max_gap_days, and
        - the span from the burst's first date <= max_span_days
    * emit a cluster when it holds >= min_contacts people

Re-running is idempotent: clusters are matched to existing rows by overlapping
date range, auto-generated names are refreshed, and a name the user set by hand
is never overwritten.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta

from ..config import ClusterConfig
from .db import Database
from .normalize import slugify, to_date, utcnow_iso

log = logging.getLogger("socialmcp.clustering")


@dataclass(slots=True)
class ClusterStats:
    created: int = 0
    updated: int = 0
    removed: int = 0


@dataclass(slots=True)
class Burst:
    start: date
    end: date
    contact_ids: list[int]

    @property
    def size(self) -> int:
        return len(self.contact_ids)

    def auto_name(self) -> str:
        """A neutral, informative placeholder the user can rename in chat."""
        if self.start == self.end:
            return f"{self.start.strftime('%d %b %Y')} burst ({self.size} connections)"
        if (self.start.year, self.start.month) == (self.end.year, self.end.month):
            span = f"{self.start.day}-{self.end.day} {self.start.strftime('%b %Y')}"
        else:
            span = f"{self.start.strftime('%d %b')} - {self.end.strftime('%d %b %Y')}"
        return f"{span} burst ({self.size} connections)"


def find_bursts(
    rows: list[tuple[int, str]], config: ClusterConfig
) -> list[Burst]:
    """rows: (contact_id, 'YYYY-MM-DD') pairs, any order."""
    dated = sorted(
        ((cid, d) for cid, raw in rows if (d := to_date(raw)) is not None),
        key=lambda pair: pair[1],
    )
    if not dated:
        return []

    gap = timedelta(days=max(0, config.max_gap_days))
    span = timedelta(days=max(0, config.max_span_days))

    bursts: list[Burst] = []
    current: Burst | None = None
    previous: date | None = None

    for contact_id, day in dated:
        if current is None:
            current = Burst(start=day, end=day, contact_ids=[contact_id])
        elif (day - previous) <= gap and (day - current.start) <= span:
            current.contact_ids.append(contact_id)
            current.end = day
        else:
            bursts.append(current)
            current = Burst(start=day, end=day, contact_ids=[contact_id])
        previous = day

    if current is not None:
        bursts.append(current)

    return [b for b in bursts if b.size >= config.min_contacts]


def rebuild_event_clusters(
    db: Database, config: ClusterConfig, *, source: str | None = None
) -> ClusterStats:
    """Recompute clusters from scratch and reconcile them with stored rows."""
    params: list[object] = []
    sql = "SELECT id, connected_on FROM contacts WHERE connected_on IS NOT NULL"
    if source:
        sql += " AND source = ?"
        params.append(source)

    rows = [(int(r["id"]), r["connected_on"]) for r in db.query(sql, params)]
    bursts = find_bursts(rows, config)
    stats = ClusterStats()
    now = utcnow_iso()

    for burst in bursts:
        start, end = burst.start.isoformat(), burst.end.isoformat()
        existing = db.query_one(
            """
            SELECT * FROM event_clusters
            WHERE (source IS ? OR source IS NULL)
              AND start_date <= ? AND end_date >= ?
            ORDER BY ABS(julianday(start_date) - julianday(?)) ASC
            LIMIT 1
            """,
            (source, end, start, start),
        )

        if existing is None:
            slug = _unique_slug(db, burst.auto_name())
            cursor = db.execute(
                """
                INSERT INTO event_clusters
                    (slug, name, start_date, end_date, source, contact_count,
                     auto_generated, renamed_by_user, created_at, updated_at)
                VALUES (?,?,?,?,?,?,1,0,?,?)
                """,
                (slug, burst.auto_name(), start, end, source, burst.size, now, now),
            )
            cluster_id = int(cursor.lastrowid)
            stats.created += 1
        else:
            cluster_id = int(existing["id"])
            name = existing["name"] if existing["renamed_by_user"] else burst.auto_name()
            db.execute(
                """
                UPDATE event_clusters
                SET name = ?, start_date = ?, end_date = ?, contact_count = ?, updated_at = ?
                WHERE id = ?
                """,
                (name, start, end, burst.size, now, cluster_id),
            )
            stats.updated += 1

        db.executemany(
            "INSERT OR IGNORE INTO contact_event_clusters (contact_id, cluster_id) VALUES (?,?)",
            [(cid, cluster_id) for cid in burst.contact_ids],
        )
        # Membership can shrink if a contact's connected_on was corrected by a
        # later archive, so prune anyone no longer inside the window.
        placeholders = ",".join("?" * len(burst.contact_ids))
        db.execute(
            f"DELETE FROM contact_event_clusters WHERE cluster_id = ? "
            f"AND contact_id NOT IN ({placeholders})",
            [cluster_id, *burst.contact_ids],
        )

    # Auto-generated clusters that no longer correspond to any burst (e.g. the
    # threshold was raised in config) are dropped; user-named ones are kept.
    live_ranges = {(b.start.isoformat(), b.end.isoformat()) for b in bursts}
    for row in db.query(
        "SELECT id, start_date, end_date FROM event_clusters WHERE auto_generated = 1 AND renamed_by_user = 0"
    ):
        if (row["start_date"], row["end_date"]) not in live_ranges:
            db.execute("DELETE FROM event_clusters WHERE id = ?", (row["id"],))
            stats.removed += 1

    db.commit()
    log.info(
        "clusters: %d created, %d updated, %d removed", stats.created, stats.updated, stats.removed
    )
    return stats


def _unique_slug(db: Database, name: str) -> str:
    base = slugify(name) or "event"
    slug, counter = base, 2
    while db.query_one("SELECT 1 FROM event_clusters WHERE slug = ?", (slug,)):
        slug = f"{base}-{counter}"
        counter += 1
    return slug


def rename_cluster(db: Database, cluster_ref: str | int, new_name: str) -> dict[str, object]:
    """Rename a cluster and pin the name so clustering never clobbers it."""
    row = resolve_cluster(db, cluster_ref)
    if row is None:
        raise ValueError(f"no event cluster matching {cluster_ref!r}")
    name = new_name.strip()
    if not name:
        raise ValueError("new_name is empty")
    slug = _unique_slug(db, name)
    db.execute(
        "UPDATE event_clusters SET name = ?, slug = ?, renamed_by_user = 1, updated_at = ? WHERE id = ?",
        (name, slug, utcnow_iso(), row["id"]),
    )
    db.commit()
    return {"id": int(row["id"]), "slug": slug, "name": name, "previous_name": row["name"]}


def resolve_cluster(db: Database, ref: str | int | None) -> object | None:
    """Look up a cluster by id, slug, name substring, or a date inside its range."""
    if ref is None:
        return None
    if isinstance(ref, int) or (isinstance(ref, str) and ref.isdigit()):
        return db.query_one("SELECT * FROM event_clusters WHERE id = ?", (int(ref),))

    text = str(ref).strip()
    row = db.query_one("SELECT * FROM event_clusters WHERE slug = ?", (slugify(text),))
    if row:
        return row

    day = to_date(text)
    if day:
        row = db.query_one(
            "SELECT * FROM event_clusters WHERE start_date <= ? AND end_date >= ? "
            "ORDER BY contact_count DESC LIMIT 1",
            (day.isoformat(), day.isoformat()),
        )
        if row:
            return row

    return db.query_one(
        "SELECT * FROM event_clusters WHERE name LIKE ? OR slug LIKE ? "
        "ORDER BY contact_count DESC LIMIT 1",
        (f"%{text}%", f"%{slugify(text)}%"),
    )
