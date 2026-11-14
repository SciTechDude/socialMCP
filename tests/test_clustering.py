"""Clustering is pure and deterministic, so it gets pure unit tests."""

from __future__ import annotations

from socialmcp.config import ClusterConfig
from socialmcp.core.clustering import find_bursts, rebuild_event_clusters, rename_cluster
from socialmcp.core.ingest import ingest_path

CONFIG = ClusterConfig(max_gap_days=2, max_span_days=5, min_contacts=5)


def rows(*pairs):
    return [(i, date) for i, date in enumerate(pairs, start=1)]


def test_single_burst_detected():
    bursts = find_bursts(
        rows("2025-10-13", "2025-10-13", "2025-10-14", "2025-10-14", "2025-10-15"), CONFIG
    )
    assert len(bursts) == 1
    assert (bursts[0].start.isoformat(), bursts[0].end.isoformat()) == ("2025-10-13", "2025-10-15")
    assert bursts[0].size == 5


def test_below_threshold_is_not_an_event():
    assert find_bursts(rows("2025-10-13", "2025-10-14", "2025-10-14"), CONFIG) == []


def test_gap_larger_than_max_splits_bursts():
    dates = ["2025-10-13"] * 5 + ["2025-10-25"] * 5
    bursts = find_bursts(rows(*dates), CONFIG)
    assert [b.size for b in bursts] == [5, 5]
    assert bursts[1].start.isoformat() == "2025-10-25"


def test_span_cap_closes_a_long_slow_drip():
    # One connection a day for 10 days: each day is within max_gap, but the
    # burst must still close at max_span so a slow month isn't one "event".
    dates = [f"2025-10-{day:02d}" for day in range(1, 11)]
    bursts = find_bursts(rows(*dates), CONFIG)
    assert all((b.end - b.start).days <= CONFIG.max_span_days for b in bursts)
    # Oct 1-6 is a 6-contact burst; the Oct 7-10 tail is below min_contacts.
    assert [b.size for b in bursts] == [6]


def test_unparseable_dates_are_ignored():
    bursts = find_bursts(rows("2025-10-13", "nonsense", "2025-10-13", "", "2025-10-14"), CONFIG)
    assert bursts == [] or bursts[0].size == 3


def test_auto_name_is_a_readable_placeholder():
    burst = find_bursts(rows(*(["2025-10-13"] * 3 + ["2025-10-15"] * 2)), CONFIG)[0]
    assert burst.auto_name() == "13-15 Oct 2025 burst (5 connections)"


def test_rename_is_sticky_across_reclustering(db, config, linkedin_dir):
    ingest_path(db, config, linkedin_dir)
    cluster_id = db.scalar("SELECT id FROM event_clusters LIMIT 1")

    renamed = rename_cluster(db, cluster_id, "Seattle AI Week 2025")
    assert renamed["slug"] == "seattle-ai-week-2025"

    rebuild_event_clusters(db, config.cluster, source="linkedin")
    row = db.query_one("SELECT * FROM event_clusters WHERE id = ?", (cluster_id,))
    assert row["name"] == "Seattle AI Week 2025"
    assert row["renamed_by_user"] == 1


def test_reclustering_is_idempotent(db, config, linkedin_dir):
    ingest_path(db, config, linkedin_dir)
    before = db.query("SELECT id, start_date, end_date, contact_count FROM event_clusters")
    rebuild_event_clusters(db, config.cluster, source="linkedin")
    rebuild_event_clusters(db, config.cluster, source="linkedin")
    after = db.query("SELECT id, start_date, end_date, contact_count FROM event_clusters")
    assert [dict(r) for r in before] == [dict(r) for r in after]
