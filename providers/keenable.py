"""Keenable adapter -- REST, key-gated, #1 in the target six-provider order
(etc/planning/fable-prompt-keenable-exa-reorder.md Phase 2; the plan's own
evidence lives in etc/planning/NEW-PROVIDERS.md §2 and §5).

**Verified against Keenable's primary docs, fetched 2026-09-13** (this is the
same evidence tier as Tavily/Firecrawl -- a primary-docs read, not a
third-party corroboration like serper.py's). What follows is what actually
shipped in this adapter, cross-checked against the plan's decision table:

- **Endpoint.** `POST https://api.keenable.ai/v1/search`. Auth is
  `X-API-Key: <key>` header -- Serper's shape, not Tavily/Firecrawl's Bearer
  form.
- **`requires_key = True` -- deliberately, option (a) of the plan's open
  question 1.** Keenable also documents a generous keyless tier
  (`/v1/search/public`, 1,000 requests/hr per IP) that this adapter
  deliberately does NOT use in v1 -- the user's call was to build only the
  keyed path.
- **Cost is flat, not tiered.** `CreditCost(credits=1, unit="api_credit",
  basis="1 per search")` regardless of `max_results` -- unlike Firecrawl's
  `ceil(n/10)*2` or Serper's <=10/11-100 split, Keenable's docs price one
  search call at one credit no matter how many results are requested.
- **Live-cost read -- deliberately NOT built.** Keenable's docs describe a
  per-call cost figure returned in `_meta["keenable/usage"]`, but that shape
  is documented for the **MCP transport only**; no equivalent field or
  response header is documented for the plain REST endpoint this adapter
  calls. Rather than guess a field name, `provider_metadata["credits_used_reported"]`
  is populated with a defensive `.get()` (any of a small set of plausible
  keys) that is `None` in practice today -- this is an honest gap, not a
  wired-in reconciliation feature (see etc/planning/NEW-PROVIDERS.md §5.1
  item 5, which supersedes §2.3 item 2's original recommendation to build
  the live-cost read).
- **`402` means "out of credits" and is OPERATIONAL, not a bad key.** It maps
  to `ProviderBlocked` and falls through to the next provider in the chain.
  `401` (missing/invalid key) and `403` (key disabled/revoked) both mean
  "fix your key" and raise `types.ProviderAuthError`, which ESCAPES past
  `search()` as a configuration error (the same 2026-08-20 user decision
  serper.py/firecrawl.py/tavily.py already implement). See `_AUTH_STATUSES`
  below for why Keenable's set is `{401, 403}`, matching serper.py's shape
  rather than firecrawl.py's `{401}`-only shape -- deliberately, per the
  plan; do not "harmonise" the two new adapters' auth sets with each other
  or with the existing four.
- **NEWS has no native endpoint.** Keenable models recency as a date filter
  on the same `/v1/search` call: `published_after="7d"` (the documented
  relative-delta syntax). This is a real, provider-modeled filter -- not
  DDG's query-string emulation -- so a genuinely absent `published_at` on a
  row reads as `DateConfidence.NONE`, never `INFERRED` (the firecrawl.py
  precedent: an absence on a provider with a real date field is an absence,
  not an inference).
- **Snippet.** `snippet` if present, else `description`, else `""` -- both
  fields are documented as "if available", so this is modeled defensively
  rather than assuming either is always populated.

I7 -- this adapter calls `POST /v1/search` and nothing else, ever. Keenable
also documents a `/v1/fetch` endpoint ("any URL as clean markdown") that
converts Keenable into a scrape/extraction tool -- exactly the kind of
one-line-away temptation firecrawl.py's crawl/scrape/extract endpoints are.
It is never called here.
"""

from __future__ import annotations

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

ENDPOINT = "https://api.keenable.ai/v1/search"
REQUEST_TIMEOUT_S = 10
DEFAULT_MAX_RESULTS = 10
SNIPPET_MAX_CHARS = 400  # matches every other adapter's cap -- Sertchi is
                          # not a content store (SKETCH.md §9).

# Status codes this provider uses to say "your key is wrong or missing".
# Raises types.ProviderAuthError, which ESCAPES past search() as a
# configuration error instead of falling through (user decision,
# 2026-08-20, reused here).
#
# Keenable is the SECOND adapter (after serper.py) that needs 403 as well
# as 401 -- deliberately, and NOT because it was copied without thought:
# Keenable's docs describe 401 as "missing/invalid key" and 403 as "key
# disabled/revoked". Both mean "fix your key," unlike firecrawl.py's 403
# (an exhausted plan, which stays operational there). Do not harmonise this
# with firecrawl.py's {401}-only set or with exa.py's -- each is evidenced
# independently.
_AUTH_STATUSES = frozenset({401, 403})


class KeenableProvider:
    """Key-gated Keenable adapter. `/v1/search` only (I7) -- never
    `/v1/fetch`.

    `api_key` is an optional constructor override (tests/callers that
    already hold a key in hand pass it explicitly, e.g.
    `KeenableProvider(api_key='x')` -- this keeps working unchanged). When
    omitted (`None`, the default), `search()` reads the key lazily from
    `keys.py` at CALL time -- never at construction/import time, so the
    module-level `REGISTRY` registration below never touches `keys.json`
    just by being imported.
    """

    name = "keenable"
    supports = frozenset({SearchType.WEB, SearchType.NEWS})
    requires_key = True

    def __init__(self, api_key: str | None = None) -> None:
        # I6: held only in this private attribute; resolved (constructor
        # value, else a lazy keys.get() read) at call time inside search(),
        # never logged/echoed/printed, never placed in a URL or an exception
        # message.
        self._api_key = api_key

    def estimate_cost(self, search_type: SearchType, **opts: Any) -> CreditCost:
        """Pure, network-free (SKETCH.md §11). Flat 1 credit per search,
        independent of `max_results` -- Keenable's docs price the call, not
        the result count, unlike Firecrawl's/Serper's tiered formulas."""
        return CreditCost(credits=1, unit="api_credit", basis="1 per search")

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

        max_results = int(opts.get("max_results", DEFAULT_MAX_RESULTS))
        body: dict[str, Any] = {
            "query": query,
            "max_results": max_results,
            "snippet_max_length": SNIPPET_MAX_CHARS,
        }
        if resolved_type is SearchType.NEWS:
            # I8: Keenable has no native news endpoint. NEWS is modeled as a
            # date filter on the same call -- the documented relative-delta
            # syntax.
            body["published_after"] = "7d"

        # Lazy, call-time key resolution (I6): an explicit constructor value
        # wins; otherwise read keys.json now, not at import time.
        api_key = self._api_key or keys.get(self.name)

        owns_client = client is None
        http_client = client or httpx.AsyncClient()
        try:
            try:
                # I7: this is the ONLY httpx call this method makes.
                resp = await http_client.post(
                    ENDPOINT,
                    json=body,
                    headers={
                        "X-API-Key": api_key,
                        "Content-Type": "application/json",
                    },
                    timeout=REQUEST_TIMEOUT_S,
                )
            except httpx.TimeoutException as exc:
                # I6: type(exc).__name__ only -- never the exception text.
                raise ProviderTimeout(type(exc).__name__) from None
            except httpx.HTTPError as exc:
                raise ProviderUnavailable(type(exc).__name__) from None

            # A rejected key is a CONFIGURATION error and escapes past
            # search() (user decision, 2026-08-20, reused here). 402 means
            # "out of credits" -- operational, falls through to the next
            # provider in the chain.
            if resp.status_code in _AUTH_STATUSES:
                raise ProviderAuthError(self.name, resp.status_code)
            if resp.status_code == 402:
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

            if not isinstance(payload, dict):
                raise ProviderMalformedResponse("non-dict response body")
            raw_rows = payload.get("results", [])
            if not isinstance(raw_rows, list):
                raise ProviderMalformedResponse("non-list 'results' field")
        finally:
            if owns_client:
                await http_client.aclose()

        # Filter first, rank after -- keeps provider_rank 1-based and dense
        # over what's actually kept (firecrawl.py's / serper.py's shared
        # pattern).
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
            text = row.get("snippet") or row.get("description") or ""
            snippet = " ".join(str(text).split())[:SNIPPET_MAX_CHARS]

            raw_date = row.get("published_at")
            published_date: str | None = None
            date_confidence = DateConfidence.NONE
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

        # Defensive-only: no per-call cost field is documented for the REST
        # transport (that figure is MCP-only, see module docstring). This is
        # not wired into any billing decision (I4) -- usage always comes
        # from estimate_cost() above.
        credits_used_reported = None
        if isinstance(payload, dict):
            meta = payload.get("_meta")
            if isinstance(meta, dict):
                credits_used_reported = meta.get("keenable/usage")

        return ProviderResponse(
            provider=self.name,
            results=results,
            provider_metadata={
                "reported_remaining": None,  # not documented for REST (see docstring)
                "credits_used_reported": credits_used_reported,
            },
            usage=self.estimate_cost(resolved_type, max_results=max_results),
        )


# No key passed here -- search() resolves it lazily via
# keys.get("keenable") at call time. Constructing with an explicit api_key
# (tests, or a caller that already holds one) still overrides this
# per-instance.
REGISTRY[Provider.KEENABLE] = KeenableProvider()
