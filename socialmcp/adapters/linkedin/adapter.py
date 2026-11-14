"""LinkedIn Data Archive adapter (v1 MVP).

Input: the .zip from Settings & Privacy -> Data Privacy -> Get a copy of your data.
No scraping, no cookies, no live API — this reads only files the user already owns.

Files consumed (all optional; whatever is present gets used):

  Connections.csv   First Name, Last Name, URL, Email Address, Company, Position, Connected On
                    Preceded by a human-readable "Notes:" preamble that must be skipped.
  Invitations.csv   From, To, Sent At, Message, Direction [, inviterProfileUrl, inviteeProfileUrl]
                    The `Message` column is the whole point: it holds the clipboard
                    text ("Great meeting you at Seattle AI Week today!") that dates
                    and places the introduction.
  messages.csv      CONVERSATION ID, CONVERSATION TITLE, FROM, SENDER PROFILE URL, TO,
                    RECIPIENT PROFILE URLS, DATE, SUBJECT, CONTENT, FOLDER
  Profile.csv       Used only to learn the archive owner's name so message and
                    invitation direction can be assigned.

Quirks handled here, in the order they bite you:
  * multi-line preamble before the real header row (length varies by vintage)
  * UTF-8 BOM on the first header cell
  * header spelling drift (`Direction` absent in older exports; camelCase URL columns)
  * invitation rows that carry no profile URL at all -> name-based identity
  * opaque /in/ACoAAB… member URNs in messages.csv vs vanity slugs in Connections.csv
  * "Connected On" is a date with no time and no timezone; invitations have both
"""

from __future__ import annotations

import hashlib
import logging
from collections import Counter
from collections.abc import Iterator
from pathlib import Path

from ...core.models import (
    ContactRecord,
    Identity,
    InteractionRecord,
    Record,
)
from ...core.normalize import (
    clean,
    full_name,
    name_key,
    normalize_profile_url,
    parse_date,
    parse_timestamp,
    profile_handle,
    slugify,
    split_name,
)
from ..base import (
    ArchiveContext,
    BaseAdapter,
    HeaderNotFoundError,
    find_file,
    find_files,
    read_csv_rows,
    register,
)

log = logging.getLogger("socialmcp.adapters.linkedin")

SOURCE = "linkedin"

CONNECTIONS_HEADERS = ("first_name", "last_name")
INVITATIONS_HEADERS = ("from", "to", "sent_at")
MESSAGES_HEADERS = ("conversation_id", "from", "date")
PROFILE_HEADERS = ("first_name", "last_name")

# LinkedIn's placeholder for members who hide their name from exports.
REDACTED_NAMES = {"linkedin member", "linkedin user"}


@register
class LinkedInAdapter(BaseAdapter):
    source = SOURCE
    display_name = "LinkedIn Data Archive"
    signature_files = ("Connections.csv", "Invitations.csv", "Profile.csv", "messages.csv")

    def parse(self, ctx: ArchiveContext) -> Iterator[Record]:
        owner = self._detect_owner(ctx)
        ctx.options["owner_name"] = owner
        owner_slug = slugify(owner or "")

        yield from self._parse_connections(ctx)
        yield from self._parse_invitations(ctx, owner_slug)
        yield from self._parse_messages(ctx, owner_slug)

    # -- owner ---------------------------------------------------------------

    def _detect_owner(self, ctx: ArchiveContext) -> str | None:
        """Who owns this archive? Needed to orient invitations and DMs.

        Profile.csv is authoritative. If it is missing (users can request a
        partial export), fall back to the most frequent sender in messages.csv,
        which is overwhelmingly the archive owner across their whole DM history.
        """
        profile = find_file(ctx.root, "Profile.csv")
        if profile:
            try:
                for row in read_csv_rows(profile, PROFILE_HEADERS):
                    name = full_name(row.get("first_name"), row.get("last_name"))
                    if name:
                        ctx.mark_parsed(profile)
                        return name
            except (HeaderNotFoundError, OSError) as exc:
                ctx.warn(f"Profile.csv unreadable ({exc}); inferring owner from messages")

        counter: Counter[str] = Counter()
        for path in find_files(ctx.root, "messages*.csv"):
            try:
                for row in read_csv_rows(path, MESSAGES_HEADERS):
                    sender = clean(row.get("from"))
                    if sender:
                        counter[sender] += 1
            except (HeaderNotFoundError, OSError):
                continue
        if counter:
            owner, _ = counter.most_common(1)[0]
            ctx.warn(f"owner inferred from messages.csv as {owner!r} (no usable Profile.csv)")
            return owner
        ctx.warn("could not determine archive owner; invitation/message direction may be wrong")
        return None

    # -- Connections.csv -----------------------------------------------------

    def _parse_connections(self, ctx: ArchiveContext) -> Iterator[Record]:
        path = find_file(ctx.root, "Connections.csv")
        if not path:
            return
        try:
            rows = list(read_csv_rows(path, CONNECTIONS_HEADERS))
        except (HeaderNotFoundError, OSError) as exc:
            ctx.warn(f"Connections.csv skipped: {exc}")
            return
        ctx.mark_parsed(path)

        for index, row in enumerate(rows):
            name = full_name(row.get("first_name"), row.get("last_name"))
            if not name:
                continue
            url = normalize_profile_url(row.get("url"))
            company = clean(row.get("company"))
            key = _contact_key(url, name, company)
            if not key:
                ctx.warn(f"Connections.csv row {index + 1}: no usable identity, skipped")
                continue

            connected_on = parse_date(row.get("connected_on"))
            if row.get("connected_on") and not connected_on:
                ctx.warn(f"Connections.csv row {index + 1}: unparseable date {row['connected_on']!r}")

            first, last = clean(row.get("first_name")), clean(row.get("last_name"))
            yield ContactRecord(
                source=SOURCE,
                source_key=key,
                full_name=name,
                first_name=first,
                last_name=last,
                company=company,
                position=clean(row.get("position")),
                email=_clean_email(row.get("email_address")),
                profile_url=url,
                status="connected",
                connected_on=connected_on,
                identities=_identities(url, name, _clean_email(row.get("email_address"))),
                raw={"file": "Connections.csv"},
            )
            if connected_on:
                yield InteractionRecord(
                    source=SOURCE,
                    contact_key=key,
                    kind="connection",
                    occurred_at=f"{connected_on}T00:00:00Z",
                    direction="self",
                    body=None,
                    dedupe_key=f"connection:{key}:{connected_on}",
                )

    # -- Invitations.csv -----------------------------------------------------

    def _parse_invitations(self, ctx: ArchiveContext, owner_slug: str) -> Iterator[Record]:
        path = find_file(ctx.root, "Invitations.csv")
        if not path:
            return
        try:
            rows = list(read_csv_rows(path, INVITATIONS_HEADERS))
        except (HeaderNotFoundError, OSError) as exc:
            ctx.warn(f"Invitations.csv skipped: {exc}")
            return
        ctx.mark_parsed(path)

        for index, row in enumerate(rows):
            sender = clean(row.get("from"))
            recipient = clean(row.get("to"))
            direction = _invite_direction(row.get("direction"), sender, owner_slug)

            # The other party is whichever end of the invite is not the owner.
            if direction == "outbound":
                other_name, other_url = recipient, row.get("invitee_profile_url")
            else:
                other_name, other_url = sender, row.get("inviter_profile_url")

            if not other_name or slugify(other_name) in REDACTED_NAMES:
                ctx.warn(f"Invitations.csv row {index + 1}: counterparty name missing, skipped")
                continue

            url = normalize_profile_url(other_url)
            # Invitations carry no company, so the name key is company-less and
            # merges with the Connections row via the shared name identity.
            key = _contact_key(url, other_name, None)
            if not key:
                continue

            sent_at = parse_timestamp(row.get("sent_at"))
            if row.get("sent_at") and not sent_at:
                ctx.warn(f"Invitations.csv row {index + 1}: unparseable timestamp {row['sent_at']!r}")
            message = clean(row.get("message"))
            first, last = split_name(other_name)

            # status here is provisional: if Connections.csv also lists this
            # person, the pipeline's status precedence promotes them to
            # 'connected'. Anything left at 'pending' is a real follow-up item.
            yield ContactRecord(
                source=SOURCE,
                source_key=key,
                full_name=other_name,
                first_name=first,
                last_name=last,
                profile_url=url,
                status="pending" if direction == "outbound" else "received",
                origin_note=message,
                origin_direction=direction,
                invited_at=sent_at,
                identities=_identities(url, other_name, None),
                raw={"file": "Invitations.csv", "direction": direction},
            )
            yield InteractionRecord(
                source=SOURCE,
                contact_key=key,
                kind="invitation",
                direction=direction,
                occurred_at=sent_at,
                body=message,
                dedupe_key=f"invitation:{direction}:{key}:{sent_at or index}",
            )

    # -- messages.csv --------------------------------------------------------

    def _parse_messages(self, ctx: ArchiveContext, owner_slug: str) -> Iterator[Record]:
        """Attach DM context to contacts we already know.

        Deliberately `resolve_only`: a DM never creates a contact. Group threads,
        recruiter blasts and InMail would otherwise fill the CRM with people the
        user never actually connected with.
        """
        for path in find_files(ctx.root, "messages*.csv"):
            try:
                rows = list(read_csv_rows(path, MESSAGES_HEADERS))
            except (HeaderNotFoundError, OSError) as exc:
                ctx.warn(f"{path.name} skipped: {exc}")
                continue
            ctx.mark_parsed(path)

            for index, row in enumerate(rows):
                content = clean(row.get("content"))
                subject = clean(row.get("subject"))
                if not content and not subject:
                    continue

                sender = clean(row.get("from"))
                sender_url = normalize_profile_url(row.get("sender_profile_url"))
                is_outbound = bool(owner_slug) and slugify(sender or "") == owner_slug
                direction = "outbound" if is_outbound else "inbound"

                if is_outbound:
                    counterparties = _split_multi(row.get("to"), row.get("recipient_profile_urls"))
                else:
                    counterparties = [(sender, sender_url)]

                # Group threads: >1 counterparty. Still useful, so the message is
                # logged against each known participant.
                occurred_at = parse_timestamp(row.get("date"))
                thread_id = clean(row.get("conversation_id"))
                digest = hashlib.sha256((content or subject or "").encode("utf-8")).hexdigest()[:12]

                for name, url in counterparties:
                    if not name and not url:
                        continue
                    key = _contact_key(url, name, None)
                    if not key:
                        continue
                    yield InteractionRecord(
                        source=SOURCE,
                        contact_key=key,
                        kind="message",
                        direction=direction,
                        occurred_at=occurred_at,
                        subject=subject,
                        body=content,
                        thread_id=thread_id,
                        dedupe_key=f"message:{thread_id or path.name}:{occurred_at or index}:{digest}:{key}",
                        resolve_only=True,
                        resolve_identities=_identities(url, name, None),
                    )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _contact_key(url: str | None, name: str | None, company: str | None) -> str | None:
    """Stable per-source identity key: prefer the profile URL, else the name."""
    handle = profile_handle(url)
    if handle:
        return handle
    return name_key(name, company)


def _identities(url: str | None, name: str | None, email: str | None) -> list[Identity]:
    out: list[Identity] = []
    if url:
        out.append(Identity("profile_url", url))
    if email:
        out.append(Identity("email", email.lower()))
    slug = slugify(name or "")
    if slug and slug not in REDACTED_NAMES:
        out.append(Identity("name", slug))
    return out


def _invite_direction(raw: str | None, sender: str | None, owner_slug: str) -> str:
    """Modern exports have a Direction column; older ones do not."""
    value = (clean(raw) or "").lower()
    if value.startswith("out") or value == "sent":
        return "outbound"
    if value.startswith("in") or value == "received":
        return "inbound"
    if owner_slug and slugify(sender or "") == owner_slug:
        return "outbound"
    # Default to outbound: the pending-follow-up list is the feature users want,
    # and a stray inbound misfiled as outbound is visible in the note text.
    return "outbound"


def _split_multi(names: str | None, urls: str | None) -> list[tuple[str | None, str | None]]:
    """messages.csv packs group-thread recipients into one cell.

    Names are semicolon-or-comma separated; URLs are whitespace/comma separated
    and are not guaranteed to be the same length as the name list, so they are
    zipped positionally only when the counts agree.
    """
    name_list = [clean(n) for n in _split_cell(names)]
    name_list = [n for n in name_list if n]
    # URL cells are sometimes space-separated instead of comma-separated; URLs
    # never contain spaces, so splitting on whitespace too is safe here.
    url_list = [normalize_profile_url(u) for u in _split_cell(urls, split_whitespace=True)]
    url_list = [u for u in url_list if u]

    if name_list and url_list and len(name_list) == len(url_list):
        return list(zip(name_list, url_list, strict=True))
    if name_list:
        return [(n, None) for n in name_list]
    return [(None, u) for u in url_list]


def _split_cell(value: str | None, *, split_whitespace: bool = False) -> list[str]:
    if not value:
        return []
    text = str(value)
    for separator in (";", "|", "\n"):
        text = text.replace(separator, ",")
    if split_whitespace:
        text = ",".join(text.split())
    return [part.strip() for part in text.split(",") if part.strip()]


def _clean_email(value: str | None) -> str | None:
    email = clean(value)
    if not email or "@" not in email:
        return None
    return email.lower()


def extract_archive_owner(root: Path) -> str | None:
    """Public helper used by tests and tooling."""
    ctx = ArchiveContext(root=root, archive_name=root.name)
    return LinkedInAdapter()._detect_owner(ctx)
