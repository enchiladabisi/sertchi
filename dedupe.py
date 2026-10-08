"""URL identity — cross-provider dedupe lives here (SKETCH.md §2.7 decision 2:
``dedupe.py`` owns ``clean_url``/``canonicalize``/``registrable_domain``/
``dedupe_key``, every adapter imports from it; there is no separate
``urls.py``).

``_TRACKING_PARAMS`` and ``canonicalize`` were forward-ported in Phase 1a
(``fable-prompt-build-v1.md`` §5 Phase 1a) — verbatim from
``tributary-cpp-v2/ddg.py`` lines 57-59 and 102-119, re-verified line by line
against that file in Phase 2 before anything else was built on top of them
(that file is read-only, per I10: never edited, never imported). Phase 2
(this pass) adds ``clean_url`` (ported from ``ddg.py:64``),
``registrable_domain`` (ported from ``ddg.py:83``, including
``_TWO_CHAR_SPECIAL``), ``dedupe_key`` (a Sertchi-only decision layered on
top of ``canonicalize``), and ``merge`` (the cross-provider merge Phase 7's
``multi_search()`` calls).
"""

from __future__ import annotations

import re
import urllib.parse
from typing import Sequence

from .types import Provider, ProviderResponse, ResultItem

_TRACKING_PARAMS = re.compile(
    r"^(?:utm_|fbclid$|gclid$|msclkid$|mc_[ce]id$|igshid$|ref$|ref_src$|"
    r"source$|spm$|_hsenc$|_hsmi$|yclid$|vero_id$|rut$)", re.IGNORECASE)

_TWO_CHAR_SPECIAL = {"co", "com", "org", "net", "ac", "gov", "edu"}


def clean_url(href: str) -> str:
    """Unwrap DDG's `/l/?uddg=<real-url>` redirect wrapper (both DDG surfaces
    use it). Ported verbatim from `tributary-cpp-v2/ddg.py:64`."""
    if not href:
        return ""
    if href.startswith("//"):
        href = "https:" + href
    try:
        parsed = urllib.parse.urlparse(href)
    except ValueError:
        return ""
    if parsed.path.startswith("/l/"):
        qs = urllib.parse.parse_qs(parsed.query)
        real = qs.get("uddg", [""])[0]
        if real:
            return real
    return href


def registrable_domain(url: str) -> str:
    """Last two labels of the netloc, last three when the TLD is 2-char and
    the second-level label is a common one (co.uk, com.au). Stdlib only --
    no `tldextract` (I1). Ported verbatim from
    `tributary-cpp-v2/ddg.py:83`, including `_TWO_CHAR_SPECIAL`."""
    try:
        netloc = urllib.parse.urlparse(url).netloc.lower()
    except ValueError:
        return ""
    netloc = netloc.split("@")[-1].split(":")[0]
    labels = [l for l in netloc.split(".") if l]
    if len(labels) < 2:
        return netloc
    tld = labels[-1]
    if len(tld) == 2 and len(labels) >= 3 and labels[-2] in _TWO_CHAR_SPECIAL:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def canonicalize(url: str) -> str:
    """Normalise scheme/host case, drop the fragment, and remove tracking
    parameters where safe.  Never touches a parameter it does not recognise --
    a stripped `?id=` can change what page is returned."""
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return url
    query = [(k, v) for k, v in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
             if not _TRACKING_PARAMS.match(k)]
    netloc = parsed.netloc.lower()
    if netloc.endswith(":80") and parsed.scheme == "http":
        netloc = netloc[:-3]
    if netloc.endswith(":443") and parsed.scheme == "https":
        netloc = netloc[:-4]
    path = parsed.path or "/"
    return urllib.parse.urlunsplit((parsed.scheme.lower(), netloc, path,
                                    urllib.parse.urlencode(query), ""))


def dedupe_key(url: str) -> str:
    """Cross-provider identity key (decision, `fable-prompt-build-v1.md` §5
    Phase 2): `canonicalize(url)` with the scheme forced to `https`, a
    leading `www.` stripped from the host, and a trailing slash stripped from
    the path, so `http://www.x.com/a`, `https://x.com/a`, and
    `https://x.com/a/` all collapse to the same key. The trailing-slash
    normalization (added 2026-08-20 after a live multi_search showed two
    providers' `.../asyncio-timeout` and `.../asyncio-timeout/` both surviving
    the merge) guards the bare root: `https://x.com/` keeps its `/` rather
    than collapsing to an empty path. Query strings are still left alone --
    a stripped `?id=` can change the page (`ddg.py:105`'s warning)."""
    canonical = canonicalize(url)
    parsed = urllib.parse.urlsplit(canonical)
    netloc = parsed.netloc
    if netloc.startswith("www."):
        netloc = netloc[4:]
    path = parsed.path.rstrip("/") or "/"
    return urllib.parse.urlunsplit(("https", netloc, path, parsed.query, ""))


def merge(responses: Sequence[ProviderResponse], *,
          order: Sequence[Provider]) -> list[ResultItem]:
    """Cross-provider merge for `multi_search()` (SKETCH.md §6, Phase 7).

    **Decision (merge order):** round-robins by `provider_rank` across
    providers in declaration order -- keenable#1, exa#1, tavily#1,
    firecrawl#1, serper#1, ddg#1, keenable#2, ... -- because breadth is multi-search's stated purpose. On a
    `dedupe_key` collision, the row from the higher-ranked provider (the one
    earlier in `order`) is kept and the other dropped.

    **Decision:** no per-domain cap in v1 -- `registrable_domain` is
    populated on every row so a *caller* can apply its own domain policy;
    Sertchi does not rank or filter results itself.
    """
    by_provider: dict[str, list[ResultItem]] = {}
    for response in responses:
        by_provider.setdefault(response.provider, []).extend(response.results)
    for rows in by_provider.values():
        rows.sort(key=lambda row: row.provider_rank)

    provider_order = [provider.value for provider in order]
    seen_keys: set[str] = set()
    merged: list[ResultItem] = []
    max_rank = max((len(rows) for rows in by_provider.values()), default=0)
    for rank_index in range(max_rank):
        for provider_name in provider_order:
            rows = by_provider.get(provider_name)
            if rows is None or rank_index >= len(rows):
                continue
            row = rows[rank_index]
            key = dedupe_key(row.url)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            merged.append(row)
    return merged
