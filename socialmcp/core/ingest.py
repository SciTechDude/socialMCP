"""The ingest pipeline: drop-folder .zip -> normalized rows in SQLite.

Adapters produce records; everything stateful happens here, exactly once, for
every source:

    extract (safely) -> dedupe archive by sha256 -> adapter.parse()
      -> in-memory canonicalization (merge the same human across files)
      -> upsert contacts + diff job changes into contact_history
      -> insert interactions (idempotent on dedupe_key)
      -> recompute search_blob / last_interaction_at
      -> temporal event clustering
      -> embed whatever is dirty (optional)

Archives are untrusted input (they are files the user downloaded), so
extraction is hardened against path traversal and decompression bombs.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
import threading
import zipfile
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from ..adapters import ArchiveContext, available_adapters, detect_adapter, get_adapter
from ..config import Config
from .clustering import rebuild_event_clusters
from .db import Database, json_dumps
from .embeddings import Embedder, embedding_text, make_embedder
from .models import (
    STATUS_PRECEDENCE,
    ContactRecord,
    Identity,
    IngestStats,
    InteractionRecord,
)
from .normalize import file_sha256, name_key, profile_handle, slugify, text_hash, utcnow_iso

log = logging.getLogger("socialmcp.ingest")

# Fields whose change between archive drops is interesting enough to keep a
# history row for ("they moved from Stripe to OpenAI in March").
DRIFT_FIELDS = ("company", "position", "headline", "location")

MAX_UNCOMPRESSED_BYTES = 2 * 1024**3   # 2 GiB: generous for a DM history, not a bomb
MAX_COMPRESSION_RATIO = 200
SEARCH_BLOB_LIMIT = 6000

# All writes are serialized: the watchdog thread and MCP tool calls can both
# trigger an ingest, and SQLite would rather we didn't interleave them.
_write_lock = threading.RLock()


class IngestError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# public entry points
# ---------------------------------------------------------------------------


def ingest_path(
    db: Database,
    config: Config,
    path: Path | str,
    *,
    source: str | None = None,
    force: bool = False,
) -> IngestStats:
    """Ingest one .zip (or one already-extracted directory)."""
    path = Path(path).expanduser()
    if not path.exists():
        raise IngestError(f"no such path: {path}")

    # drop-archive/linkedin/foo.zip routes to the LinkedIn adapter without
    # sniffing. An explicit `source` argument always wins.
    source = source or config.source_for(path)

    with _write_lock:
        if path.is_dir():
            return _ingest_directory(db, config, path, archive_file=None, source=source)

        if path.suffix.lower() != ".zip":
            raise IngestError(f"expected a .zip archive or a directory, got {path.name}")

        digest = file_sha256(path)
        existing = db.query_one("SELECT id, file_name, ingested_at FROM archives WHERE sha256 = ?", (digest,))
        if existing and not force:
            log.info("archive %s already ingested at %s", path.name, existing["ingested_at"])
            return IngestStats(
                source=source or "",
                archive=path.name,
                archive_id=existing["id"],
                skipped_duplicate=True,
                warnings=[f"identical archive already ingested on {existing['ingested_at']}"],
            )

        with tempfile.TemporaryDirectory(prefix="socialmcp-") as tmp:
            root = Path(tmp) / "extracted"
            root.mkdir()
            try:
                extract_archive(path, root)
            except zipfile.BadZipFile as exc:
                raise IngestError(f"{path.name} is not a readable .zip: {exc}") from exc
            return _ingest_directory(
                db, config, root, archive_file=path, source=source, sha256=digest
            )


def ingest_drop_dir(db: Database, config: Config, *, source: str | None = None) -> list[IngestStats]:
    """Ingest every .zip sitting in the configured drop folder, oldest first."""
    config.ensure_dirs()
    zips = sorted(config.drop_dir.rglob("*.zip"), key=lambda p: p.stat().st_mtime)
    results: list[IngestStats] = []
    for archive in zips:
        try:
            results.append(ingest_path(db, config, archive, source=source))
        except (IngestError, zipfile.BadZipFile, OSError) as exc:
            # One bad archive must not stop the rest of the drop folder.
            log.error("ingest failed for %s: %s", archive.name, exc)
            results.append(
                IngestStats(archive=archive.name, warnings=[f"{type(exc).__name__}: {exc}"])
            )
    return results


# ---------------------------------------------------------------------------
# archive extraction (hardened)
# ---------------------------------------------------------------------------


def extract_archive(zip_path: Path, dest: Path) -> None:
    """Extract a .zip into `dest`, refusing anything that escapes or bombs.

    `ZipFile.extractall` is not safe for files from the internet: entry names
    may be absolute or contain '..', and a small archive can expand to fill the
    disk. Every member is validated before a byte is written.
    """
    dest = dest.resolve()
    total = 0
    with zipfile.ZipFile(zip_path) as archive:
        for info in archive.infolist():
            name = info.filename
            if name.endswith("/"):
                continue
            if name.startswith(("/", "\\")) or ".." in Path(name).parts:
                raise IngestError(f"unsafe path in archive: {name!r}")

            target = (dest / name).resolve()
            # Compared as paths, not strings. A string prefix check has to pick
            # a separator, and hardcoding "/" rejected every member on Windows,
            # where the resolved path uses backslashes — so no archive could be
            # read at all on that platform.
            if not target.is_relative_to(dest):
                raise IngestError(f"archive member escapes extraction directory: {name!r}")

            total += info.file_size
            if total > MAX_UNCOMPRESSED_BYTES:
                raise IngestError("archive expands beyond the 2 GiB safety limit")
            if info.compress_size and info.file_size / info.compress_size > MAX_COMPRESSION_RATIO:
                raise IngestError(f"suspicious compression ratio for {name!r}")

            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as src, target.open("wb") as out:
                shutil.copyfileobj(src, out)


# ---------------------------------------------------------------------------
# core pipeline
# ---------------------------------------------------------------------------


def _ingest_directory(
    db: Database,
    config: Config,
    root: Path,
    *,
    archive_file: Path | None,
    source: str | None,
    sha256: str | None = None,
) -> IngestStats:
    adapter = None
    routing_warning: str | None = None
    if source:
        try:
            adapter = get_adapter(source)
        except KeyError:
            # The drop-zone layout advertises folders (x/, meta/) whose adapters
            # are still roadmap items. A drop there must degrade to sniffing
            # rather than exploding on a folder name.
            routing_warning = (
                f"no adapter named {source!r} is installed yet; detected the format instead"
            )
            log.info("%s", routing_warning)
    if adapter is None:
        adapter = detect_adapter(root)
    if adapter is None:
        # The list of sources is read from the registry, not hardcoded, so a
        # contributor's new adapter appears in this message for free.
        known = ", ".join(sorted(available_adapters())) or "none installed"
        raise IngestError(
            f"no adapter recognized {archive_file.name if archive_file else root.name}. "
            f"Installed sources: {known}. See CONTRIBUTING.md to add one."
        )

    archive_name = archive_file.name if archive_file else root.name
    ctx = ArchiveContext(root=root, archive_name=archive_name)
    if routing_warning:
        ctx.warn(routing_warning)
    stats = IngestStats(source=adapter.source, archive=archive_name)

    now = utcnow_iso()
    archive_id = _record_archive(
        db,
        source=adapter.source,
        file_name=archive_name,
        file_path=str(archive_file) if archive_file else str(root),
        sha256=sha256 or f"dir:{root.resolve()}",
        size_bytes=archive_file.stat().st_size if archive_file else None,
        ingested_at=now,
    )
    stats.archive_id = archive_id

    contact_records: list[ContactRecord] = []
    interaction_records: list[InteractionRecord] = []
    for record in adapter.parse(ctx):
        if isinstance(record, ContactRecord):
            contact_records.append(record)
        else:
            interaction_records.append(record)

    contacts, aliases = _canonicalize(contact_records, ctx)

    key_to_id: dict[str, int] = {}
    touched: set[int] = set()
    for key, record in contacts.items():
        contact_id, created, changed, history_rows = _upsert_contact(db, record, archive_id, now, ctx)
        key_to_id[key] = contact_id
        touched.add(contact_id)
        if created:
            stats.contacts_new += 1
        elif changed:
            stats.contacts_updated += 1
        else:
            # Re-ingesting an overlapping archive matches everyone without
            # changing anything; reporting that as "updated" is misleading.
            stats.contacts_unchanged += 1
        stats.history_changes += history_rows

    for old_key, new_key in aliases.items():
        if new_key in key_to_id:
            key_to_id[old_key] = key_to_id[new_key]

    invited_in_this_archive: set[int] = set()
    for interaction in interaction_records:
        contact_id = _resolve_interaction_contact(db, interaction, key_to_id, aliases)
        if contact_id is None:
            continue
        if interaction.kind == "invitation":
            # Collected whether or not the row is new, because an unchanged
            # invitation still proves it is *still pending* in this export.
            invited_in_this_archive.add(contact_id)
        if _insert_interaction(db, interaction, contact_id, archive_id, now):
            stats.interactions_new += 1
            touched.add(contact_id)

    stats.invites_lapsed = _sweep_lapsed_invitations(
        db, adapter.source, ctx, invited_in_this_archive, now
    )

    for contact_id in touched:
        _refresh_derived(db, contact_id, now)

    cluster_stats = rebuild_event_clusters(db, config.cluster, source=adapter.source)
    stats.clusters_created = cluster_stats.created
    stats.clusters_updated = cluster_stats.updated

    stats.files_parsed = ctx.files_parsed
    stats.warnings.extend(ctx.warnings)
    stats.pending_invitations = db.scalar("SELECT COUNT(*) FROM contacts WHERE status = 'pending'") or 0

    db.execute(
        "UPDATE archives SET stats_json = ?, status = ? WHERE id = ?",
        (json_dumps(stats.as_dict()), "partial" if ctx.warnings else "ok", archive_id),
    )
    db.commit()

    stats.embeddings_written = refresh_embeddings(db, config)

    if archive_file and config.archive_dir and _is_inside(archive_file, config.drop_dir):
        _move_processed(archive_file, config, stats)

    log.info(
        "ingested %s: +%d contacts, %d updated, %d unchanged, +%d interactions, %d job changes",
        archive_name,
        stats.contacts_new,
        stats.contacts_updated,
        stats.contacts_unchanged,
        stats.interactions_new,
        stats.history_changes,
    )
    return stats


def _record_archive(db: Database, **kwargs: Any) -> int:
    row = db.query_one("SELECT id FROM archives WHERE sha256 = ?", (kwargs["sha256"],))
    if row:
        db.execute(
            "UPDATE archives SET ingested_at = ?, file_path = ? WHERE id = ?",
            (kwargs["ingested_at"], kwargs["file_path"], row["id"]),
        )
        return int(row["id"])
    cursor = db.execute(
        """
        INSERT INTO archives (source, file_name, file_path, sha256, size_bytes, ingested_at)
        VALUES (:source, :file_name, :file_path, :sha256, :size_bytes, :ingested_at)
        """,
        kwargs,
    )
    return int(cursor.lastrowid)


def _is_inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except (ValueError, OSError):
        return False
    return True


def _move_processed(archive_file: Path, config: Config, stats: IngestStats) -> None:
    """Move the consumed .zip out of the drop folder so it is not re-scanned.

    The per-source subfolder is preserved, so ingested/linkedin/export.zip
    mirrors drop-archive/linkedin/export.zip.
    """
    try:
        relative = archive_file.resolve().relative_to(config.drop_dir.resolve())
    except (ValueError, OSError):
        relative = Path(archive_file.name)
    archive_dir = config.archive_dir / relative.parent
    archive_dir.mkdir(parents=True, exist_ok=True)
    target = archive_dir / archive_file.name
    counter = 1
    while target.exists():
        target = archive_dir / f"{archive_file.stem}-{counter}{archive_file.suffix}"
        counter += 1
    try:
        shutil.move(str(archive_file), str(target))
        db_path_note = f"moved to {target}"
    except OSError as exc:
        db_path_note = f"could not move archive out of drop folder: {exc}"
        stats.warnings.append(db_path_note)
    log.info("%s", db_path_note)


# ---------------------------------------------------------------------------
# canonicalization: one human, many rows
# ---------------------------------------------------------------------------


def _canonicalize(
    records: Iterable[ContactRecord], ctx: ArchiveContext
) -> tuple[dict[str, ContactRecord], dict[str, str]]:
    """Merge records that describe the same person inside a single archive.

    The hard case is real: `Connections.csv` has a profile URL, `Invitations.csv`
    (older vintages) does not, so the same person arrives under two different
    keys — 'in/jane-doe' and 'name/jane-doe'. Collapsing them is what preserves
    the clipboard origin_note on the connected contact instead of leaving a
    phantom 'pending' duplicate.

    Name-based collapse only happens when it is unambiguous: if two different
    URL-keyed contacts share a display name, we keep them apart and warn.
    """
    merged: dict[str, ContactRecord] = {}
    for record in records:
        existing = merged.get(record.source_key)
        merged[record.source_key] = _merge_contacts(existing, record) if existing else record

    name_to_url_keys: dict[str, set[str]] = defaultdict(set)
    name_to_name_keys: dict[str, set[str]] = defaultdict(set)
    for key, record in merged.items():
        slug = slugify(record.full_name)
        if not slug:
            continue
        if key.startswith("name/"):
            name_to_name_keys[slug].add(key)
        else:
            name_to_url_keys[slug].add(key)

    aliases: dict[str, str] = {}
    for key in list(merged):
        if not key.startswith("name/"):
            continue
        slug = slugify(merged[key].full_name)
        url_keys = name_to_url_keys.get(slug, set())
        if len(url_keys) == 1:
            target = next(iter(url_keys))
        elif len(url_keys) > 1:
            ctx.warn(
                f"{merged[key].full_name!r} matches {len(url_keys)} profiles; "
                "left unmerged to avoid fusing two people"
            )
            continue
        else:
            # No URL-keyed twin. Collapse a bare 'name/jane-doe' into a
            # company-qualified 'name/jane-doe|acme' when there is exactly one.
            companied = {k for k in name_to_name_keys.get(slug, set()) if "|" in k}
            if key == name_key(merged[key].full_name, None) and len(companied) == 1:
                target = next(iter(companied))
            else:
                continue

        if target == key or target not in merged:
            continue
        merged[target] = _merge_contacts(merged[target], merged.pop(key))
        aliases[key] = target

    return merged, aliases


def _merge_contacts(base: ContactRecord, incoming: ContactRecord) -> ContactRecord:
    """Field-by-field merge with explicit precedence rules.

    Rules that matter:
      status        -> highest precedence wins (connected beats pending)
      origin_note   -> first non-empty wins; it is a historical artifact and a
                       later archive must never overwrite the original context
      invited_at    -> earliest (the first time we reached out)
      connected_on  -> earliest non-null
      profile_url   -> any non-null; URL-keyed records are preferred upstream
    """
    out = ContactRecord(
        source=base.source,
        source_key=base.source_key,
        full_name=_longer(base.full_name, incoming.full_name),
    )
    for field_name in ("first_name", "last_name", "headline", "company", "position", "location", "email"):
        setattr(out, field_name, getattr(base, field_name) or getattr(incoming, field_name))

    out.profile_url = base.profile_url or incoming.profile_url
    out.status = max(
        (base.status, incoming.status), key=lambda s: STATUS_PRECEDENCE.get(s, 0)
    )
    out.origin_note = base.origin_note or incoming.origin_note
    out.origin_direction = base.origin_direction or incoming.origin_direction
    out.invited_at = _earliest(base.invited_at, incoming.invited_at)
    out.connected_on = _earliest(base.connected_on, incoming.connected_on)

    seen: set[tuple[str, str]] = set()
    identities: list[Identity] = []
    for identity in list(base.identities) + list(incoming.identities):
        pair = (identity.kind, identity.value)
        if pair not in seen:
            seen.add(pair)
            identities.append(identity)
    out.identities = identities
    out.raw = {**incoming.raw, **base.raw}
    return out


def _longer(a: str | None, b: str | None) -> str:
    """Prefer the fuller display name ('Jane Q. Doe' over 'Jane')."""
    candidates = [c for c in (a, b) if c]
    return max(candidates, key=len) if candidates else ""


def _earliest(a: str | None, b: str | None) -> str | None:
    candidates = [c for c in (a, b) if c]
    return min(candidates) if candidates else None


# ---------------------------------------------------------------------------
# contacts
# ---------------------------------------------------------------------------


def _upsert_contact(
    db: Database,
    record: ContactRecord,
    archive_id: int,
    now: str,
    ctx: ArchiveContext,
) -> tuple[int, bool, bool, int]:
    """Insert or update one contact.

    Returns (contact_id, created, changed, history_rows), where `changed` is
    False when the record matched an existing row with nothing to write.
    """
    existing = _find_existing(db, record, ctx)

    if existing is None:
        cursor = db.execute(
            """
            INSERT INTO contacts (
                source, source_key, profile_url, full_name, first_name, last_name,
                headline, company, "position", location, email, status,
                origin_note, origin_direction, invited_at, connected_on,
                search_blob, embedding_dirty, first_archive_id, last_archive_id,
                created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?,?)
            """,
            (
                record.source,
                record.source_key,
                record.profile_url,
                record.full_name,
                record.first_name,
                record.last_name,
                record.headline,
                record.company,
                record.position,
                record.location,
                record.email,
                record.status if record.status != "unknown" else "unknown",
                record.origin_note,
                record.origin_direction,
                record.invited_at,
                record.connected_on,
                record.origin_note or "",
                archive_id,
                archive_id,
                now,
                now,
            ),
        )
        contact_id = int(cursor.lastrowid)
        _register_identities(db, contact_id, record)
        return contact_id, True, True, 0

    contact_id = int(existing["id"])
    updates: dict[str, Any] = {}
    history_rows = 0

    # Drift-tracked fields: a changed non-empty value is news, so record it.
    for field_name in DRIFT_FIELDS:
        new_value = getattr(record, field_name)
        if not new_value:
            continue
        old_value = existing[field_name if field_name != "position" else "position"]
        if old_value and old_value != new_value:
            db.execute(
                """
                INSERT INTO contact_history (contact_id, field, old_value, new_value, changed_at, archive_id)
                VALUES (?,?,?,?,?,?)
                """,
                (contact_id, field_name, old_value, new_value, now, archive_id),
            )
            history_rows += 1
            updates[field_name] = new_value
        elif not old_value:
            updates[field_name] = new_value

    if record.email and not existing["email"]:
        updates["email"] = record.email
    if record.profile_url and not existing["profile_url"]:
        updates["profile_url"] = record.profile_url
    if record.full_name and len(record.full_name) > len(existing["full_name"] or ""):
        updates["full_name"] = record.full_name
    for field_name in ("first_name", "last_name"):
        value = getattr(record, field_name)
        if value and not existing[field_name]:
            updates[field_name] = value

    if STATUS_PRECEDENCE.get(record.status, 0) > STATUS_PRECEDENCE.get(existing["status"], 0):
        updates["status"] = record.status
    if record.origin_note and not existing["origin_note"]:
        updates["origin_note"] = record.origin_note
        updates["origin_direction"] = record.origin_direction
    if record.invited_at and (not existing["invited_at"] or record.invited_at < existing["invited_at"]):
        updates["invited_at"] = record.invited_at
    if record.connected_on and (
        not existing["connected_on"] or record.connected_on < existing["connected_on"]
    ):
        updates["connected_on"] = record.connected_on

    # Promote a name-based key to the stable profile-URL key once we learn it.
    handle = profile_handle(record.profile_url)
    if handle and existing["source_key"].startswith("name/"):
        clash = db.query_one(
            "SELECT id FROM contacts WHERE source = ? AND source_key = ? AND id != ?",
            (record.source, handle, contact_id),
        )
        if not clash:
            updates["source_key"] = handle

    changed = bool(updates)
    if changed:
        updates["last_archive_id"] = archive_id
        updates["updated_at"] = now
        assignments = ", ".join(f'"{k}" = :{k}' for k in updates)
        db.execute(
            f"UPDATE contacts SET {assignments} WHERE id = :cid", {**updates, "cid": contact_id}
        )
    else:
        # Still record that this archive saw them, without touching updated_at.
        db.execute("UPDATE contacts SET last_archive_id = ? WHERE id = ?", (archive_id, contact_id))
    _register_identities(db, contact_id, record)
    return contact_id, False, changed, history_rows


def _find_existing(db: Database, record: ContactRecord, ctx: ArchiveContext) -> Any:
    """Resolve a record to an existing row: key -> profile_url -> email -> name."""
    row = db.query_one(
        "SELECT * FROM contacts WHERE source = ? AND source_key = ?",
        (record.source, record.source_key),
    )
    if row:
        return row

    if record.profile_url:
        row = db.query_one(
            "SELECT * FROM contacts WHERE source = ? AND profile_url = ?",
            (record.source, record.profile_url),
        )
        if row:
            return row
        row = _by_identity(db, record.source, "profile_url", record.profile_url)
        if row:
            return row

    if record.email:
        row = _by_identity(db, record.source, "email", record.email.lower())
        if row:
            return row

    slug = slugify(record.full_name)
    if slug:
        candidates = db.query(
            """
            SELECT c.* FROM contacts c
            JOIN contact_identities i ON i.contact_id = c.id
            WHERE i.source = ? AND i.kind = 'name' AND i.value = ?
            """,
            (record.source, slug),
        )
        if len(candidates) == 1:
            candidate = candidates[0]
            # Two different profile URLs means two different people, full stop.
            if not (
                record.profile_url
                and candidate["profile_url"]
                and record.profile_url != candidate["profile_url"]
            ):
                return candidate
        elif len(candidates) > 1:
            ctx.warn(
                f"{record.full_name!r} is ambiguous ({len(candidates)} existing contacts); "
                "created/updated separately rather than guessing"
            )
    return None


def _by_identity(db: Database, source: str, kind: str, value: str) -> Any:
    rows = db.query(
        """
        SELECT c.* FROM contacts c
        JOIN contact_identities i ON i.contact_id = c.id
        WHERE i.source = ? AND i.kind = ? AND i.value = ?
        """,
        (source, kind, value),
    )
    return rows[0] if len(rows) == 1 else None


def _register_identities(db: Database, contact_id: int, record: ContactRecord) -> None:
    rows = [(contact_id, record.source, i.kind, i.value) for i in record.identities if i.value]
    if record.profile_url:
        rows.append((contact_id, record.source, "profile_url", record.profile_url))
    if rows:
        db.executemany(
            """
            INSERT OR IGNORE INTO contact_identities (contact_id, source, kind, value)
            VALUES (?,?,?,?)
            """,
            rows,
        )


# ---------------------------------------------------------------------------
# interactions
# ---------------------------------------------------------------------------


def _resolve_interaction_contact(
    db: Database,
    interaction: InteractionRecord,
    key_to_id: dict[str, int],
    aliases: dict[str, str],
) -> int | None:
    key = aliases.get(interaction.contact_key, interaction.contact_key)
    if key in key_to_id:
        return key_to_id[key]

    row = db.query_one(
        "SELECT id FROM contacts WHERE source = ? AND source_key = ?", (interaction.source, key)
    )
    if row:
        return int(row["id"])

    for identity in interaction.resolve_identities:
        rows = db.query(
            """
            SELECT c.id FROM contacts c
            JOIN contact_identities i ON i.contact_id = c.id
            WHERE i.source = ? AND i.kind = ? AND i.value = ?
            """,
            (interaction.source, identity.kind, identity.value),
        )
        if len(rows) == 1:
            return int(rows[0]["id"])
    # resolve_only records (DMs) intentionally die here rather than inventing
    # a contact for every recruiter who slid into the inbox.
    return None


def _insert_interaction(
    db: Database, interaction: InteractionRecord, contact_id: int, archive_id: int | None, now: str
) -> bool:
    cursor = db.execute(
        """
        INSERT OR IGNORE INTO interactions
            (contact_id, source, kind, direction, occurred_at, subject, body, thread_id,
             dedupe_key, archive_id, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            contact_id,
            interaction.source,
            interaction.kind,
            interaction.direction,
            interaction.occurred_at,
            interaction.subject,
            interaction.body,
            interaction.thread_id,
            interaction.dedupe_key,
            archive_id,
            now,
        ),
    )
    return cursor.rowcount > 0


def _sweep_lapsed_invitations(
    db: Database,
    source: str,
    ctx: ArchiveContext,
    invited_now: set[int],
    now: str,
) -> int:
    """Mark invitations that disappeared from the export without connecting.

    An invitation that was pending in an earlier archive and is absent from
    this one, while the person still is not a connection, has been declined,
    withdrawn, ignored or expired. The export does not distinguish those, so
    neither do we.

    Guarded two ways, because a false positive here would quietly delete
    someone from the follow-up list:
      * only runs when this archive actually contained an Invitations.csv
      * only applies to invitations dated inside the window this archive
        covers. Outside that window the file says nothing, and absence is not
        evidence.
    """
    if not ctx.options.get("invitations_present"):
        return 0
    window_start = ctx.options.get("invitation_window_start")
    if not window_start:
        return 0

    # A re-sent invitation clears any previous lapse.
    if invited_now:
        placeholders = ",".join("?" * len(invited_now))
        db.execute(
            f"UPDATE contacts SET invite_lapsed_at = NULL WHERE invite_lapsed_at IS NOT NULL "
            f"AND id IN ({placeholders})",
            list(invited_now),
        )

    sql = """
        UPDATE contacts SET invite_lapsed_at = ?, updated_at = ?
        WHERE source = ?
          AND status IN ('pending', 'received')
          AND invite_lapsed_at IS NULL
          AND invited_at IS NOT NULL
          AND invited_at >= ?
    """
    params: list[Any] = [now, now, source, window_start]
    if invited_now:
        sql += f" AND id NOT IN ({','.join('?' * len(invited_now))})"
        params += list(invited_now)

    cursor = db.execute(sql, params)
    count = cursor.rowcount or 0
    if count:
        log.info("%d invitation(s) lapsed (gone from the export, never connected)", count)
    return count


def _refresh_derived(db: Database, contact_id: int, now: str) -> None:
    """Recompute last_interaction_at and the keyword search blob for a contact.

    search_blob is denormalized on purpose: FTS5 external-content indexes can
    only read columns of `contacts`, and we want notes and DM text to be
    keyword-searchable alongside the structured fields.
    """
    last_at = db.scalar(
        "SELECT MAX(occurred_at) FROM interactions WHERE contact_id = ? AND occurred_at IS NOT NULL",
        (contact_id,),
    )
    bodies = db.query(
        """
        SELECT kind, body, subject FROM interactions
        WHERE contact_id = ? AND (body IS NOT NULL OR subject IS NOT NULL)
        ORDER BY (kind = 'note') DESC, occurred_at DESC
        LIMIT 60
        """,
        (contact_id,),
    )
    row = db.query_one(
        'SELECT origin_note, full_name, headline, company, "position", location, embedding_hash '
        "FROM contacts WHERE id = ?",
        (contact_id,),
    )
    pieces = [row["origin_note"] or ""] if row else []
    pieces += [" ".join(filter(None, (b["subject"], b["body"]))) for b in bodies]
    blob = "\n".join(p for p in pieces if p).strip()[:SEARCH_BLOB_LIMIT]

    new_hash = text_hash(
        row["full_name"] if row else None,
        row["headline"] if row else None,
        row["company"] if row else None,
        row["position"] if row else None,
        row["location"] if row else None,
        blob,
    )
    dirty = 1 if not row or row["embedding_hash"] != new_hash else 0
    db.execute(
        "UPDATE contacts SET search_blob = ?, last_interaction_at = ?, embedding_dirty = ?, updated_at = ? "
        "WHERE id = ?",
        (blob, last_at, dirty, now, contact_id),
    )


# ---------------------------------------------------------------------------
# notes (used by the log_contact_note MCP tool)
# ---------------------------------------------------------------------------


def add_note(
    db: Database,
    contact_id: int,
    note: str,
    *,
    occurred_at: str | None = None,
    subject: str | None = None,
) -> dict[str, Any]:
    """Append a timestamped local note and refresh the contact's search text.

    `subject` records provenance. It stays empty for notes the user wrote
    themselves and carries a source label for findings gathered elsewhere, so
    the user can always tell their own judgement apart from something an
    assistant looked up. Both are searchable.
    """
    text = (note or "").strip()
    if not text:
        raise ValueError("note is empty")
    now = utcnow_iso()
    stamp = occurred_at or now
    with _write_lock:
        cursor = db.execute(
            """
            INSERT INTO interactions
                (contact_id, source, kind, direction, occurred_at, subject, body,
                 dedupe_key, created_at)
            SELECT ?, source, 'note', 'self', ?, ?, ?, ?, ? FROM contacts WHERE id = ?
            """,
            (
                contact_id,
                stamp,
                subject,
                text,
                f"note:{contact_id}:{now}:{text_hash(text)[:10]}",
                now,
                contact_id,
            ),
        )
        if cursor.rowcount == 0:
            raise ValueError(f"no contact with id {contact_id}")
        _refresh_derived(db, contact_id, now)
        db.commit()
    return {"contact_id": contact_id, "note": text, "logged_at": stamp, "source": subject}


# ---------------------------------------------------------------------------
# embeddings
# ---------------------------------------------------------------------------


def refresh_embeddings(db: Database, config: Config, *, limit: int | None = None) -> int:
    """Embed every contact whose text changed since it was last embedded.

    A no-op when vector search is unavailable or the provider is 'none' — the
    server stays fully functional on keyword search alone.
    """
    if not db.vec_enabled or config.embedding.provider == "none":
        return 0
    embedder: Embedder | None = make_embedder(config.embedding)
    if embedder is None:
        return 0

    rows = db.query(
        'SELECT id, full_name, headline, company, "position", location, origin_note, search_blob, status '
        "FROM contacts WHERE embedding_dirty = 1 ORDER BY id"
        + (f" LIMIT {int(limit)}" if limit else "")
    )
    if not rows:
        return 0

    written = 0
    with _write_lock:
        for batch in _chunks(rows, config.embedding.batch_size):
            texts = [embedding_text(dict(r)) for r in batch]
            try:
                vectors = embedder.embed(texts)
            except Exception as exc:
                log.warning("embedding provider unavailable (%s); leaving %d contacts dirty", exc, len(batch))
                break
            for row, vector in zip(batch, vectors, strict=True):
                db.upsert_embedding(int(row["id"]), vector)
                db.execute(
                    "UPDATE contacts SET embedding_dirty = 0, embedding_hash = ? WHERE id = ?",
                    (
                        text_hash(
                            row["full_name"],
                            row["headline"],
                            row["company"],
                            row["position"],
                            row["location"],
                            row["search_blob"],
                        ),
                        int(row["id"]),
                    ),
                )
                written += 1
        db.commit()
    return written


def _chunks(items: list[Any], size: int) -> Iterable[list[Any]]:
    size = max(1, size)
    for start in range(0, len(items), size):
        yield items[start : start + size]
