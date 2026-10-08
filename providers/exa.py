"""Exa adapter -- REST, key-gated, #2 in the target six-provider order
(etc/planning/fable-prompt-keenable-exa-reorder.md Phase 3; the plan's own
evidence lives in etc/planning/NEW-PROVIDERS.md §1 and §5).

**Verified against Exa's primary docs, fetched 2026-09-13** (the same
evidence tier as Tavily/Firecrawl -- a primary-docs read, not a third-party
corroboration like serper.py's). What follows is what actually shipped in
this adapter, cross-checked against the plan's decision table:

- **Endpoint.** `POST https://api.exa.ai/search`. Auth is
  `Authorization: Bearer <key>` -- Tavily's/Firecrawl's shape, not
  Keenable's/Serper's header form.
- **Contents are NOT free with a plain search.** A bare `/search` call
  returns no snippet-equivalent at all -- `text`/`highlights`/`summary` each
  default to `false` and each is separately billed. This adapter requests
  **exactly one** content type, `contents: {"highlights": true}`, on every
  call, and never `text` or `.summary` (I7 -- see the warning below). The
  exact request shape (`{"highlights": True}` as a bare boolean flag rather
  than a nested options object) is taken from Exa's own guide example and
  has **not been exercised live** as of this writing -- an honest gap, not
  a verified fact.
- **Cost is in micro-dollars, not a provider-native integer credit.** Unlike
  Keenable's/Tavily's/Firecrawl's/Serper's flat or tiered integer credits,
  Exa bills in fractional dollars per component (search tier + each content
  type + a per-extra-result charge). `unit="usd_micro"` (1 credit =
  $0.000001) lets `CreditCost.credits` stay an exact integer instead of a
  float, so the ledger never accumulates floating-point drift across many
  calls.
- **Cost formula.** Base by `type`: `7_000` for anything that is not
  explicitly a "deep" tier (`instant`/`fast`/`auto`, and -- deliberately --
  any *unrecognized* type string, the same defensive "anything not the
  named special case bills at the standard rate" structure `tavily.py` uses
  for its non-`"advanced"` depths); `deep-lite`/`deep` -> `12_000`;
  `deep-reasoning` -> `15_000`. Plus a flat `1_000` for the one content type
  requested (`highlights`). Plus `1_000 * max(0, numResults - 10)` for
  results beyond the first ten. `basis` is a legible string, e.g.
  `"7000+content1000+extra0"`, so a human auditing the ledger can read the
  formula's inputs straight off the record.
- **`costDollars` is recorded, never billed from.** The response carries
  `payload["costDollars"]`, Exa's own account of what the call cost --
  surfaced under `provider_metadata["cost_dollars_reported"]` for a future
  reconciliation pass (the firecrawl.py precedent: `creditsUsed` is kept
  the same way). `ProviderResponse.usage` always comes from
  `estimate_cost()`'s formula above, never from this field (I4).
- **Remaining-credits figure.** Not documented on `/search`; the same
  documented-absence posture as Tavily/Firecrawl.
  `provider_metadata["reported_remaining"]` is always `None`.
- **`402` means "out of credits" and is OPERATIONAL, not a bad key.** It
  maps to `ProviderBlocked` and falls through to the next provider in the
  chain -- the same rule Keenable's adapter implements.
- **`401` is the ONLY documented auth-rejection code -- there is no 403 at
  all for `/search`.** Exa's documented status codes are
  400/401/402/429/500/503. `_AUTH_STATUSES = frozenset({401})` -- do NOT
  copy serper.py's/keenable.py's `{401, 403}` shape here; that divergence
  is deliberate and evidenced, not an oversight. Because 403 is not a
  documented code at all, this adapter still classifies a live 403 as
  **operational** (`ProviderBlocked`, the same treatment firecrawl.py gives
  its own non-auth block code) rather than letting it fall into the
  catch-all `raise_for_status()` path -- a provider that unexpectedly
  starts sending 403 should not dead-end the whole fallback chain over a
  code its own docs never named.
- **NEWS has no separate endpoint.** Exa models it as `category: "news"` on
  the same `/search` call, with no date range sent by default.
- **`publishedDate` is passed through unparsed.** It already arrives as an
  ISO-8601 string; this adapter does not reparse or reformat it (I1 -- no
  date library). Present and non-empty -> `REPORTED`; absent (on either
  `web` or `news` calls, treated identically) -> `None` + `NONE`. Exa has a
  genuine native `publishedDate` field, so an absence is an absence, the
  firecrawl.py precedent, not DDG's emulated-recency `INFERRED`.
- **`score`.** Recorded as `provider_score`, exactly as tavily.py treats its
  own `score` -- never filtered, floored, or reordered on.

I7 -- this adapter calls `POST /search` and nothing else, ever. Exa also
documents an `/answer` endpoint (a hosted RAG answer over search results)
and `contents.text`/`contents.summary` options on `/search` itself, each
separately billed and each pulling Sertchi toward being a content store
rather than a normalized search result list (SKETCH.md §9). None of those
are called here.
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

ENDPOINT = "https://api.exa.ai/search"
REQUEST_TIMEOUT_S = 10
DEFAULT_NUM_RESULTS = 10
DEFAULT_TYPE = "auto"
SNIPPET_MAX_CHARS = 400  # matches every other adapter's cap -- Sertchi is
                          # not a content store (SKETCH.md §9).

# Micro-dollar cost components (I4 -- this arithmetic lives here, never in
# usage.py). Base by `type`; anything not an explicitly-named "deep" tier
# (including an unrecognized string) bills at the standard base -- the same
# defensive structure tavily.py uses for its non-"advanced" depths.
_BASE_STANDARD = 7_000
_BASE_DEEP = 12_000  # "deep-lite" and "deep"
_BASE_DEEP_REASONING = 15_000
_CONTENT_COST = 1_000  # one content type (highlights) requested, always
_EXTRA_RESULT_COST = 1_000  # per result beyond the first 10

# Status codes this provider uses to say "your key is wrong or missing".
# Raises types.ProviderAuthError, which ESCAPES past search() as a
# configuration error instead of falling through (user decision,
# 2026-08-20, reused here).
#
# Exa is the FIRST adapter in this tree whose _AUTH_STATUSES is a bare
# {401} despite living right next to keenable.py's {401, 403} -- deliberate
# and evidenced: Exa's documented status codes for /search are
# 400/401/402/429/500/503. There is no 403 at all. Do not "harmonise" this
# with keenable.py's or serper.py's set.
_AUTH_STATUSES = frozenset({401})


def _cost_base(exa_type: str) -> int:
    if exa_type == "deep-reasoning":
        return _BASE_DEEP_REASONING
    if exa_type in ("deep-lite", "deep"):
        return _BASE_DEEP
    # Standard tiers (instant/fast/auto) AND any unrecognized type string --
    # pinned deliberately so nobody "tightens" this into a raise.
    return _BASE_STANDARD


class ExaProvider:
    """Key-gated Exa adapter. `/search` only (I7) -- never `/answer`, and
    never any content type but `highlights`.

    `api_key` is an optional constructor override (tests/callers that
    already hold a key in hand pass it explicitly, e.g.
    `ExaProvider(api_key='x')` -- this keeps working unchanged). When
    omitted (`None`, the default), `search()` reads the key lazily from
    `keys.py` at CALL time -- never at construction/import time, so the
    module-level `REGISTRY` registration below never touches `keys.json`
    just by being imported.
    """

    name = "exa"
    supports = frozenset({SearchType.WEB, SearchType.NEWS})
    requires_key = True

    def __init__(self, api_key: str | None = None) -> None:
        # I6: held only in this private attribute; resolved (constructor
        # value, else a lazy keys.get() read) at call time inside search(),
        # never logged/echoed/printed, never placed in a URL or an exception
        # message.
        self._api_key = api_key

    def estimate_cost(self, search_type: SearchType, **opts: Any) -> CreditCost:
        """Pure, network-free (SKETCH.md §11). Micro-dollars: base(type) +
        1,000 (one content type, always highlights) + 1,000 per result
        beyond the first 10."""
        exa_type = str(opts.get("type", DEFAULT_TYPE))
        num_results = int(opts.get("numResults", DEFAULT_NUM_RESULTS))
        base = _cost_base(exa_type)
        extra = max(0, num_results - 10) * _EXTRA_RESULT_COST
        credits = base + _CONTENT_COST + extra
        basis = f"{base}+content{_CONTENT_COST}+extra{extra}"
        return CreditCost(credits=credits, unit="usd_micro", basis=basis)

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

        exa_type = str(opts.get("type", DEFAULT_TYPE))
        num_results = int(opts.get("numResults", DEFAULT_NUM_RESULTS))
        body: dict[str, Any] = {
            "query": query,
            "type": exa_type,
            "numResults": num_results,
            # I7: exactly one content type, always -- never text/summary.
            "contents": {"highlights": True},
        }
        if resolved_type is SearchType.NEWS:
            # I8: no separate news endpoint -- category on the same call.
            body["category"] = "news"

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

            # A rejected key is a CONFIGURATION error and escapes past
            # search() (user decision, 2026-08-20, reused here). 402 means
            # "out of credits" -- operational, falls through. 403 is not a
            # documented Exa code at all, but is treated as operational
            # rather than the catch-all raise_for_status() path, so an
            # unexpected 403 does not dead-end the whole fallback chain.
            if resp.status_code in _AUTH_STATUSES:
                raise ProviderAuthError(self.name, resp.status_code)
            if resp.status_code == 402:
                raise ProviderBlocked(f"http {resp.status_code}")
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

            if not isinstance(payload, dict):
                raise ProviderMalformedResponse("non-dict response body")
            raw_rows = payload.get("results", [])
            if not isinstance(raw_rows, list):
                raise ProviderMalformedResponse("non-list 'results' field")
        finally:
            if owns_client:
                await http_client.aclose()

        # Filter first, rank after -- keeps provider_rank 1-based and dense
        # over what's actually kept (firecrawl.py's / keenable.py's shared
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

            raw_highlights = row.get("highlights")
            if not isinstance(raw_highlights, list):
                raw_highlights = []
            snippet = " ".join(
                " ".join(str(h).split()) for h in raw_highlights
            )
            snippet = " ".join(snippet.split())[:SNIPPET_MAX_CHARS]

            raw_date = row.get("publishedDate")
            published_date: str | None = None
            date_confidence = DateConfidence.NONE
            if isinstance(raw_date, str) and raw_date.strip():
                # Passed through unparsed (I1) -- Exa already returns ISO-8601.
                published_date = raw_date
                date_confidence = DateConfidence.REPORTED

            raw_score = row.get("score")
            provider_score = float(raw_score) if isinstance(raw_score, (int, float)) else None

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
                provider_score=provider_score,
                thumbnail_url=None,
            ))

        return ProviderResponse(
            provider=self.name,
            results=results,
            provider_metadata={
                "reported_remaining": None,  # not documented for /search
                "cost_dollars_reported": payload.get("costDollars"),
            },
            # I4: always the formula above, never payload["costDollars"].
            usage=self.estimate_cost(resolved_type, type=exa_type, numResults=num_results),
        )


# No key passed here -- search() resolves it lazily via keys.get("exa") at
# call time. Constructing with an explicit api_key (tests, or a caller that
# already holds one) still overrides this per-instance.
REGISTRY[Provider.EXA] = ExaProvider()
