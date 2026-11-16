"""LinkedIn profile URL rules, owned by the LinkedIn adapter.

Which path prefixes identify a person, and which are aliases of one another, is
platform knowledge. Keeping it here means a new platform's URL shapes never
touch shared code, and a change to LinkedIn's URL scheme is a one-file fix.

Handled here:
  /in/<vanity>      the modern canonical form
  /pub/<name>/1/2/3 the legacy form, aliases to /in/
  /sales/lead/...   Sales Navigator deep links
  /company, /school organization pages, kept distinct from people
  /in/ACoAAB...     opaque member URNs, common in messages.csv
"""

from __future__ import annotations

import re

from ...core.normalize import normalize_profile_url

# 'pub' and 'sales/lead' are alternate spellings of a person's profile.
PERSON_PREFIXES = {"in", "pub", "sales/lead"}
ORG_PREFIXES = {"company", "school", "showcase"}

_PATH = re.compile(r"/(in|pub|company|school|showcase|sales/lead)/([^/]+)")


def canonicalize(host: str, path: str) -> str | None:
    """Reduce any LinkedIn URL shape to https://www.linkedin.com/<kind>/<handle>."""
    if "linkedin." not in host:
        return None
    match = _PATH.search(path)
    if not match:
        return None
    kind, handle = match.group(1), match.group(2).lower()
    if kind in PERSON_PREFIXES:
        kind = "in"
    return f"https://www.linkedin.com/{kind}/{handle}"


def normalize(url: str | None) -> str | None:
    """Normalize a LinkedIn URL, or return None if it is not one."""
    return normalize_profile_url(url, canonicalize)
