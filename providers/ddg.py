"""DDG adapter — keyless, dual-surface (Lite → HTML) web/news search.

Adapted from `tributary-cpp-v2/ddg.py` (the hardened reference; read-only
per I10 -- never edited, never imported). This module ports the result
parsers, anomaly detection, and dual-surface retry essentially as-is, and
deliberately drops everything `fable-prompt-build-v1.md` §5 Phase 2 /
SKETCH.md §9 name as non-goals: `is_safe_url`/`fetch_page` (no page
fetching), the readability extractor (`main_paragraphs`,
`_ReadabilityParser`, `choose_passage`), the embedding rerank (`_rerank`),
and `classify_source`. Nothing here imports `config`, `trace`, `schemas`, or
`llama_client`.

**I8 — the invariant most at risk in this module.** DDG has no real `news`
endpoint; `search_type=NEWS` is emulated by biasing the query text. Every
row returned for a NEWS call carries `date_confidence=INFERRED` and
`published_date=None` -- it must never be presented as a real dated result.

**I3 — this adapter RAISES on operational trouble; it does not swallow it.**
This is the one deliberate behavioral difference from `ddg.py`'s own
`search_one`, which fails open *at that layer* and returns an empty
`QueryOutcome`. Sertchi's provider/orchestrator split (I2/I3) moves that
decision up to `__init__.py` (Phase 7): this adapter's job is only to
classify what happened -- timeout / rate-limit / unavailable / block /
malformed -- into the closed `ProviderOperationalError` set defined in
`providers/base.py`, and raise it. The orchestrator decides whether to fall
through to another provider.
"""

from __future__ import annotations

import asyncio
import random
import urllib.parse
from html.parser import HTMLParser
from typing import Any, Sequence

import httpx

from ..dedupe import canonicalize, clean_url, registrable_domain
from ..types import (
    CreditCost,
    DateConfidence,
    InvalidRequest,
    Provider,
    ProviderResponse,
    ResultItem,
    SearchType,
)
from .base import (
    REGISTRY,
    ProviderBlocked,
    ProviderMalformedResponse,
    ProviderOperationalError,
    ProviderRateLimited,
    ProviderTimeout,
    ProviderUnavailable,
)

# --------------------------------------------------------------------------
# Config constants -- copied from tributary-cpp-v2/config.py's SEARCH dict
# (fable-prompt-build-v1.md §2.3), UA product token changed to sertchi/1.0.
# --------------------------------------------------------------------------

DDG_ENDPOINTS = (
    "https://lite.duckduckgo.com/lite/",
    "https://html.duckduckgo.com/html/",
)
USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) sertchi/1.0 "
              "(shared multi-provider search utility; one query per call)")
ACCEPT_LANGUAGE = "en-US,en;q=0.9"
REQUEST_TIMEOUT_S = 10
BACKOFF_MS = (900, 1800)
MAX_RESULTS_PER_QUERY = 5

# --------------------------------------------------------------------------
# Result parsers -- ported essentially as-is from ddg.py:176-307. clean_url
# is NOT redefined here; it lives in dedupe.py (§2.7 decision 2) and is
# imported above.
# --------------------------------------------------------------------------


class _LiteResultParser(HTMLParser):
    """DDG Lite surface (`lite.duckduckgo.com/lite/`).

    Anatomy: a results table where each result is
    `<a class="result-link" href="/l/?uddg=...">Title</a>` followed by
    `<td class="result-snippet">…</td>`.  Sponsored rows carry a
    `result-sponsored` class and are skipped.
    """

    def __init__(self) -> None:
        super().__init__()
        self.results: list[dict[str, str]] = []
        self._in_title = False
        self._in_snippet = False
        self._skip = False

    @staticmethod
    def _classes(attrs: Sequence[tuple[str, str | None]]) -> list[str]:
        return (dict(attrs).get("class") or "").split()

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        classes = self._classes(attrs)
        if any("sponsored" in c for c in classes):
            self._skip = True
            return
        if tag == "a" and ("result-link" in classes or "result__a" in classes):
            href = clean_url(dict(attrs).get("href") or "")
            if href:
                self.results.append({"title": "", "url": href, "snippet": ""})
                self._in_title = True
        elif tag == "td" and "result-snippet" in classes and self.results:
            self._in_snippet = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._in_title:
            self._in_title = False
        if tag == "td" and self._in_snippet:
            self._in_snippet = False
        if tag == "tr":
            self._skip = False

    def handle_data(self, data: str) -> None:
        if self._skip or not self.results:
            return
        if self._in_title:
            self.results[-1]["title"] += data
        elif self._in_snippet:
            self.results[-1]["snippet"] += data


class _HtmlResultParser(HTMLParser):
    """DDG HTML surface (`html.duckduckgo.com/html/`).  Each result is a
    `div.result` holding `a.result__a` and a `result__snippet`; ad rows
    carry `result--ad`."""

    def __init__(self) -> None:
        super().__init__()
        self.results: list[dict[str, str]] = []
        self._in_title = False
        self._in_snippet = False
        self._skip_current = False

    @staticmethod
    def _classes(attrs: Sequence[tuple[str, str | None]]) -> list[str]:
        return (dict(attrs).get("class") or "").split()

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        classes = self._classes(attrs)
        if tag == "div" and "result" in classes:
            self._skip_current = "result--ad" in classes
        elif tag == "a" and "result__a" in classes and not self._skip_current:
            href = clean_url(dict(attrs).get("href") or "")
            self.results.append({"title": "", "url": href, "snippet": ""})
            self._in_title = True
        elif (tag in ("a", "td", "div") and "result__snippet" in classes
              and not self._skip_current and self.results):
            self._in_snippet = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._in_title:
            self._in_title = False
        if tag in ("a", "td", "div") and self._in_snippet:
            self._in_snippet = False

    def handle_data(self, data: str) -> None:
        if not self.results:
            return
        if self._in_title:
            self.results[-1]["title"] += data
        elif self._in_snippet:
            self.results[-1]["snippet"] += data


# DDG's block/challenge page returns HTTP 200 with no results; parsing it
# yields garbage, so it is detected by signature and reported as a clean
# block (ProviderBlocked).
_ANOMALY_MARKERS = (
    "anomaly.js", "anomaly_modal", "unfortunately, bots",
    "if this error persists", "detected unusual traffic",
    "please try again later", "challenge-platform", "captcha",
)


def looks_like_anomaly(html: str) -> bool:
    lowered = html[:20000].lower()
    return any(marker in lowered for marker in _ANOMALY_MARKERS)


def parse_results(html: str, *, surface: str,
                   limit: int) -> tuple[list[dict[str, str]], bool]:
    """Parse one surface's HTML into (results, anomaly).  Rows without both a
    title and a real destination URL are dropped."""
    if looks_like_anomaly(html):
        return [], True
    parser: HTMLParser = _LiteResultParser() if surface == "lite" else _HtmlResultParser()
    try:
        parser.feed(html)
    except Exception:
        pass
    raw = getattr(parser, "results", [])
    results: list[dict[str, str]] = []
    for row in raw:
        title = " ".join(row["title"].split())
        url = row["url"].strip()
        if not title or not url or not url.startswith(("http://", "https://")):
            continue
        if "duckduckgo.com/y.js" in url or "/y.js?" in url:
            continue                        # ad redirector
        results.append({"title": title, "url": url,
                        "snippet": " ".join(row["snippet"].split())})
        if len(results) >= limit:
            break
    return results, False


# --------------------------------------------------------------------------
# Network call -- adapted from ddg.py's search_one. Unlike search_one, THIS
# function raises a classified ProviderOperationalError on exhaustion
# instead of returning an empty outcome (I3 -- see module docstring).
# --------------------------------------------------------------------------


async def _sleep_jitter(bounds: tuple[int, int]) -> None:
    low, high = bounds
    await asyncio.sleep(random.uniform(low, high) / 1000.0)


async def _search_one(client: httpx.AsyncClient, query: str, *,
                       limit: int) -> tuple[list[dict[str, str]], str]:
    """Lite surface first, HTML second, with one bounded jittered retry.
    Returns (rows, surface) on success -- `rows` may be empty, which is NOT
    an error: an operational empty is represented by returning zero rows,
    not by raising (the orchestrator, Phase 7, is what turns zero rows into
    an AttemptOutcome.EMPTY). Raises the appropriate
    `ProviderOperationalError` once every surface x retry combination has
    failed.
    """
    headers = {
        "User-Agent": USER_AGENT,
        "Accept-Language": ACCEPT_LANGUAGE,
        "Content-Type": "application/x-www-form-urlencoded",
    }
    last_error: ProviderOperationalError | None = None
    for attempt in (0, 1):
        retry_worthy = False
        for index, endpoint in enumerate(DDG_ENDPOINTS):
            surface = "lite" if index == 0 else "html"
            try:
                resp = await client.post(
                    endpoint, data={"q": query, "kl": "us-en"}, headers=headers,
                    timeout=REQUEST_TIMEOUT_S, follow_redirects=True)
            except httpx.TimeoutException as exc:
                last_error = ProviderTimeout(type(exc).__name__)
                retry_worthy = True
                continue
            except httpx.HTTPError as exc:
                last_error = ProviderUnavailable(type(exc).__name__)
                continue
            # DDG-specific rate-limit/block status codes (ddg.py:596).
            if resp.status_code in (202, 403, 429, 503):
                last_error = ProviderRateLimited(f"http {resp.status_code}")
                retry_worthy = True
                continue
            if resp.status_code >= 500:
                last_error = ProviderUnavailable(f"http {resp.status_code}")
                continue
            # Any other non-2xx (e.g. 400/404) is not in I3's closed set --
            # it propagates uncaught rather than being treated as a
            # fall-through-worthy operational failure.
            resp.raise_for_status()
            content_type = resp.headers.get("content-type", "")
            if content_type and "html" not in content_type.lower():
                last_error = ProviderMalformedResponse(
                    f"unexpected content-type {content_type!r}")
                continue
            try:
                rows, anomaly = parse_results(resp.text, surface=surface, limit=limit)
            except Exception as exc:
                last_error = ProviderMalformedResponse(type(exc).__name__)
                continue
            if anomaly:
                last_error = ProviderBlocked("anomaly page")
                retry_worthy = True
                continue
            return rows, surface
        if attempt == 0 and retry_worthy:
            await _sleep_jitter(BACKOFF_MS)
    raise last_error or ProviderUnavailable("ddg: exhausted lite+html surfaces")


# --------------------------------------------------------------------------
# Adapter
# --------------------------------------------------------------------------


class DDGProvider:
    """Keyless DDG adapter. `news` is an emulation (I8) -- see module
    docstring."""

    name = "ddg"
    supports = frozenset({SearchType.WEB, SearchType.NEWS})
    requires_key = False

    def estimate_cost(self, search_type: SearchType, **opts: Any) -> CreditCost:
        """Pure, network-free -- DDG is keyless (SKETCH.md §11)."""
        return CreditCost(credits=0, unit="none", basis="keyless")

    async def search(self, query: str, search_type: SearchType | str, *,
                      client: httpx.AsyncClient | None = None,
                      **opts: Any) -> ProviderResponse:
        try:
            resolved_type = SearchType(search_type)
        except ValueError as exc:
            raise InvalidRequest(f"invalid search_type: {search_type!r}") from exc
        if resolved_type not in self.supports:
            raise InvalidRequest(f"{self.name} does not support {resolved_type.value}")

        # I8: news is a recency-biased query, never a real dated endpoint.
        effective_query = f"{query} news" if resolved_type is SearchType.NEWS else query
        limit = int(opts.get("limit", MAX_RESULTS_PER_QUERY))

        owns_client = client is None
        http_client = client or httpx.AsyncClient()
        try:
            rows, surface = await _search_one(http_client, effective_query, limit=limit)
        finally:
            if owns_client:
                await http_client.aclose()

        date_confidence = (DateConfidence.INFERRED if resolved_type is SearchType.NEWS
                            else DateConfidence.NONE)
        results: list[ResultItem] = []
        for rank, row in enumerate(rows, start=1):
            url = canonicalize(row["url"])
            results.append(ResultItem(
                title=row["title"],
                url=url,
                snippet=row["snippet"],
                domain=urllib.parse.urlsplit(url).netloc.lower(),
                registrable_domain=registrable_domain(url),
                # I8: never a real date for NEWS, and DDG never has one for
                # WEB either -- both cases stay None.
                published_date=None,
                date_confidence=date_confidence,
                provider=self.name,
                provider_rank=rank,
                provider_score=None,
                thumbnail_url=None,
            ))
        return ProviderResponse(
            provider=self.name,
            results=results,
            provider_metadata={"surface": surface},
            usage=CreditCost(credits=0, unit="none", basis="keyless"),
        )


REGISTRY[Provider.DDG] = DDGProvider()
