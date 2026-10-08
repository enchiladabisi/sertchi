"""Firecrawl adapter -- REST, key-gated, #4 in the provider order (NEW-PROVIDERS.md §5.3).

**Verified against live Firecrawl docs 2026-08-20** (fable-prompt-build-v1.md
§5 Phase 4's mandatory build-time check), fetched from
https://docs.firecrawl.dev/api-reference/endpoint/search (fetched twice: once
for the general endpoint/response shape, once specifically for the full
request-body field list) and corroborated for the base URL via a curl example
surfaced by web search (both agree on `https://api.firecrawl.dev/v2/search`):

- **Endpoint.** `POST https://api.firecrawl.dev/v2/search`. Auth is
  `Authorization: Bearer <key>` (documented `bearerAuth` scheme -- same form
  Tavily's docs specify, see tavily.py's docstring point (a)).
- **`sources` request shape.** An array of `{"type": "web"|"news"|"images"}`
  objects, defaulting to `[{"type": "web"}]` when omitted. This adapter only
  ever sends ONE entry per call -- see `_SOURCE_FOR_TYPE` and the I7/§6
  comment below; images is out of scope (§3 -- deferred by result-shape, not
  availability).
- **`limit` semantics -- CONFIRMED per-source, not total.** The docs state it
  explicitly: "Maximum number of results to return (per source type when
  using multiple sources)". SKETCH.md §2's claim was right. Sertchi only ever
  requests one source per call, so in practice `limit` always means "this
  one source's result count" here regardless.
- **Credits-remaining figure -- CONFIRMED ABSENT.** The response body carries
  `creditsUsed` (an integer -- what THIS call cost, mirroring Tavily's opt-in
  `usage.credits`), not an account-wide remaining-balance figure, and no
  response header exposes one either. `provider_metadata["reported_remaining"]`
  is therefore always `None` here -- a documented absence like Tavily's, not
  an "undocumented as of 2026-08-20" unknown. The raw `creditsUsed` Firecrawl
  reports is still surfaced under `provider_metadata["credits_used_reported"]`
  for a future Phase 6 reconciliation pass to compare against this adapter's
  own `estimate_cost()` formula -- `ProviderResponse.usage` itself always uses
  the formula below, per the build plan, never the provider-reported figure.
- **Response shape.** `{"success": bool, "data": {"web": [...], "news": [...],
  "images": [...]}, "warning": str|None, "id": str, "creditsUsed": int}` --
  results grouped by source key, not one flat list. Row fields: `title`,
  `url`, `description`; `markdown` (nullable, populated only when
  `scrapeOptions.formats` is requested -- Sertchi never requests scrape
  formats, I7, so `markdown` is expected empty/absent in practice and this
  adapter falls back to `description` for `snippet`). News rows additionally
  document `date`, `imageUrl`, `snippet`, `position`. Firecrawl's docs do not
  state `date`'s exact string format (unlike Tavily's confirmed RFC-822) --
  this adapter keeps it as the raw string Firecrawl returns rather than
  guessing a parse, and only trusts it as `REPORTED` when present and
  non-empty (a missing date on one row of a genuinely native news endpoint
  reads as `NONE` -- an absence -- not `INFERRED`, which this codebase
  reserves for a provider emulating recency the way DDG does; see
  ``providers/serper.py`` for the one case where the plan explicitly pins
  `INFERRED` instead).

I7 -- this adapter calls the **search endpoint only**. Never `crawl`,
`scrape`, `extract`, or `map`, even though Firecrawl is primarily an
extraction product and those endpoints are one line away in the same client.
"""

from __future__ import annotations

import math
import urllib.parse
from typing import Any

import httpx

from .. import keys
from ..dedupe import canonicalize, registrable_domain
from ..types import (
    CreditCost,
    DateConfidence,
    InvalidRequest,
    Provider,
    ProviderAuthError,
    ProviderResponse,
    ResultItem,
    SearchType,
)
from .base import (
    REGISTRY,
    ProviderBlocked,
    ProviderMalformedResponse,
    ProviderRateLimited,
    ProviderTimeout,
    ProviderUnavailable,
)

ENDPOINT = "https://api.firecrawl.dev/v2/search"
REQUEST_TIMEOUT_S = 10
DEFAULT_LIMIT = 10
SNIPPET_MAX_CHARS = 400  # matches tributary-cpp-v2/config.py's snippet_char_cap
                          # -- Sertchi is not a content store (SKETCH.md §9).

# Status codes this provider uses to say "your key is wrong or missing".
# Raises types.ProviderAuthError, which ESCAPES past search() as a
# configuration error instead of falling through (user decision,
# 2026-08-20). Firecrawl documents 401 for an invalid bearer token; its 403
# reads as an exhausted plan and stays operational. Contrast serper.py.
_AUTH_STATUSES = frozenset({401})

# I7/§6 -- one request per Sertchi call, always. Firecrawl's free tier caps at
# 2 concurrent requests; a future call that needed web+news together would
# have to batch them as two entries in ONE `sources` list rather than firing
# two requests. Sertchi's `search()` takes exactly one SearchType per call, so
# this mapping structurally cannot produce more than one source entry -- there
# is no code path below that issues a second httpx call for one search() call.
_SOURCE_FOR_TYPE: dict[SearchType, str] = {
    SearchType.WEB: "web",
    SearchType.NEWS: "news",
}


class FirecrawlProvider:
    """Key-gated Firecrawl adapter. `search` endpoint only (I7) -- never
    `crawl`/`scrape`/`extract`/`map`.

    `api_key` is an optional constructor override (tests/callers that already
    hold a key in hand pass it explicitly, e.g.
    `FirecrawlProvider(api_key='x')` -- this keeps working unchanged). When
    omitted (`None`, the default), `search()` reads the key lazily from
    `keys.py` at CALL time -- never at construction/import time, so the
    module-level `REGISTRY` registration below never touches `keys.json`
    just by being imported (Phase 5).
    """

    name = "firecrawl"
    supports = frozenset({SearchType.WEB, SearchType.NEWS})
    requires_key = True

    def __init__(self, api_key: str | None = None) -> None:
        # I6: held only in this private attribute; resolved (constructor
        # value, else a lazy keys.get() read) at call time inside search(),
        # never logged/echoed/printed, never placed in a URL or an exception
        # message.
        self._api_key = api_key

    def estimate_cost(self, search_type: SearchType, **opts: Any) -> CreditCost:
        """Pure, network-free (SKETCH.md §11). `2 * ceil(results_requested /
        10)`, confirmed against Firecrawl's own docs' "2 credits per 10
        results" framing, 2026-08-20."""
        n = int(opts.get("limit", DEFAULT_LIMIT))
        credits = 2 * math.ceil(n / 10)
        return CreditCost(credits=credits, unit="api_credit", basis=f"ceil({n}/10)*2")

    async def search(
        self,
        query: str,
        search_type: SearchType | str,
        *,
        client: httpx.AsyncClient | None = None,
        **opts: Any,
    ) -> ProviderResponse:
        try:
            resolved_type = SearchType(search_type)
        except ValueError as exc:
            raise InvalidRequest(f"invalid search_type: {search_type!r}") from exc
        if resolved_type not in self.supports:
            raise InvalidRequest(f"{self.name} does not support {resolved_type.value}")

        limit = int(opts.get("limit", DEFAULT_LIMIT))
        source_key = _SOURCE_FOR_TYPE[resolved_type]
        body: dict[str, Any] = {
            "query": query,
            "limit": limit,
            # I7/§6: exactly one entry, always -- see _SOURCE_FOR_TYPE above.
            "sources": [{"type": source_key}],
        }

        # Lazy, call-time key resolution (I6): an explicit constructor value
        # wins; otherwise read keys.json now, not at import time.
        api_key = self._api_key or keys.get(self.name)

        owns_client = client is None
        http_client = client or httpx.AsyncClient()
        try:
            try:
                # I7/§6: this is the ONLY httpx call this method makes.
                resp = await http_client.post(
                    ENDPOINT,
                    json=body,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    timeout=REQUEST_TIMEOUT_S,
                )
            except httpx.TimeoutException as exc:
                # I6: type(exc).__name__ only -- never the exception text.
                raise ProviderTimeout(type(exc).__name__) from None
            except httpx.HTTPError as exc:
                raise ProviderUnavailable(type(exc).__name__) from None

            # Same split tavily.py documents: 401 means the key is wrong or
            # missing -- a CONFIGURATION error that escapes past search()
            # (user decision, 2026-08-20) -- while 403 stays operational and
            # falls through, since for Firecrawl it reads as an exhausted
            # plan rather than a bad token.
            if resp.status_code in _AUTH_STATUSES:
                raise ProviderAuthError(self.name, resp.status_code)
            if resp.status_code == 403:
                raise ProviderBlocked(f"http {resp.status_code}")
            if resp.status_code == 429:
                raise ProviderRateLimited(f"http {resp.status_code}")
            if resp.status_code >= 500:
                raise ProviderUnavailable(f"http {resp.status_code}")
            if resp.status_code >= 400:
                # Any other 4xx is not in I3's closed set -- propagates
                # uncaught rather than being treated as fall-through-worthy.
                resp.raise_for_status()

            try:
                payload = resp.json()
            except Exception as exc:
                raise ProviderMalformedResponse(type(exc).__name__) from None

            data = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(data, dict):
                raise ProviderMalformedResponse("missing or non-dict 'data' field")
            raw_rows = data.get(source_key, [])
            if not isinstance(raw_rows, list):
                raise ProviderMalformedResponse(f"non-list '{source_key}' field in 'data'")
        finally:
            if owns_client:
                await http_client.aclose()

        # Filter first, rank after -- keeps provider_rank 1-based and dense
        # over what's actually kept (tavily.py's / ddg.py's shared pattern).
        kept: list[dict[str, Any]] = []
        for row in raw_rows:
            if not isinstance(row, dict):
                continue
            title = " ".join(str(row.get("title") or "").split())
            raw_url = str(row.get("url") or "")
            if not title or not raw_url:
                continue
            kept.append(row)

        results: list[ResultItem] = []
        for rank, row in enumerate(kept, start=1):
            title = " ".join(str(row.get("title") or "").split())
            url = canonicalize(str(row.get("url") or ""))
            # Sertchi never requests scrape formats (I7), so `markdown` is
            # expected empty/absent in practice; `description` (web/news) and
            # `snippet` (news) are the documented fallbacks.
            text = row.get("markdown") or row.get("description") or row.get("snippet") or ""
            snippet = " ".join(str(text).split())[:SNIPPET_MAX_CHARS]

            published_date: str | None = None
            date_confidence = DateConfidence.NONE
            if resolved_type is SearchType.NEWS:
                raw_date = row.get("date")
                if isinstance(raw_date, str) and raw_date.strip():
                    published_date = raw_date.strip()
                    date_confidence = DateConfidence.REPORTED

            results.append(ResultItem(
                title=title,
                url=url,
                snippet=snippet,
                domain=urllib.parse.urlsplit(url).netloc.lower(),
                registrable_domain=registrable_domain(url),
                published_date=published_date,
                date_confidence=date_confidence,
                provider=self.name,
                provider_rank=rank,
                provider_score=None,
                thumbnail_url=None,
            ))

        return ProviderResponse(
            provider=self.name,
            results=results,
            provider_metadata={
                "reported_remaining": None,  # confirmed absent -- see docstring
                "credits_used_reported": payload.get("creditsUsed"),
            },
            usage=self.estimate_cost(resolved_type, limit=limit),
        )


# No key passed here -- search() resolves it lazily via
# keys.get("firecrawl") at call time (Phase 5). Constructing with an
# explicit api_key (tests, or a caller that already holds one) still
# overrides this per-instance.
REGISTRY[Provider.FIRECRAWL] = FirecrawlProvider()
