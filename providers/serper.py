"""Serper adapter -- REST, key-gated, #5 in the provider order (NEW-PROVIDERS.md §5.3).

**Verified against Serper docs and independent pricing breakdowns, 2026-08-20**
(fable-prompt-build-v1.md §5 Phase 4's mandatory build-time check). Serper's
own https://serper.dev/docs returned an HTTP 404 when fetched directly for
this build -- its documentation appears to live behind an authenticated
dashboard session rather than a public docs page. Unlike Tavily/Firecrawl
(both fetched from the provider's own primary docs page), the findings below
are corroborated across THREE independent third-party technical sources
instead, cross-checked against each other:
https://www.coldiq.com/blog/serper-pricing,
https://apiserpent.com/blog/serper-pricing-credits-explained, and a worked
code example at
https://mintlify.wiki/azlan18/Hackathon-Resources/examples/serper-search.
This is a materially weaker verification tier than the other two keyed
adapters and is flagged plainly here rather than presented as equally
verified (priority #4).

- **Endpoints (per-vertical, corroborated across all three sources above).**
  `POST https://google.serper.dev/search` (web) and
  `POST https://google.serper.dev/news` (news). Auth: `X-API-KEY: <key>`
  header -- NOT a Bearer token; a distinct convention from Tavily/Firecrawl,
  confirmed consistently across every source fetched.
- **Request params.** `{"q": <query>, "num": <count>}`; the `tbs` time-filter
  parameter (`qdr:h`/`qdr:d`/`qdr:w`/`qdr:m`/`qdr:y`, standard Google SERP
  time-box syntax) is accepted on both verticals per the third-party sources,
  but this adapter does NOT send it by default -- only when the caller passes
  `opts["tbs"]` explicitly -- since no source fetched pins an authoritative
  default window the way Tavily's `days` default is documented.
- **(i) Do news/images share the standard endpoint's 1|2-credit rule? --
  CONFIRMED YES, 2026-08-20.** coldiq.com states it explicitly: "Serper's
  endpoints share the same credit rule, which keeps the math simple. A call
  to search, images, news, maps, places, videos, shopping, scholar, patents,
  or autocomplete each costs 1 credit for up to 10 results, and 2 credits if
  you ask for more." apiserpent.com corroborates the base <=10/11-100 rule
  without contradicting the cross-endpoint claim. This closes SKETCH.md §7's
  open question -- one uniform rule, no per-vertical exception needed in
  `estimate_cost()` below.
- **(ii) Does the free 2,500-credit signup grant expire? --
  UNDOCUMENTED AS OF 2026-08-20.** Neither coldiq.com nor apiserpent.com
  states an expiry for the free grant; both explicitly scope their "valid 6
  months" language to PAID credit purchases only ("every paid credit is
  valid for 6 months from purchase" -- coldiq.com), leaving the free tier's
  lifetime unstated rather than confirmed-unlimited. Per the build plan: this
  is recorded as genuinely unknown, not assumed either way. `usage.py`
  (Phase 6) should not assume the free grant never expires.
- **Response shape.** Web: `{"searchParameters": {...}, "organic": [{"title",
  "link", "snippet", "position", ...}], ...}`. News: `{"news": [{"title",
  "link", "source", "date" (present on at least some rows, absent on others
  per the worked example), ...}]}`. Serper's rows use `link`, not `url` --
  the field name differs from every other adapter in this tree and is easy to
  typo; this adapter reads `link` explicitly (see
  `tests/test_provider_serper.py::SerperFieldNameTest`). **`snippet` on news
  rows is not confirmed present in any source fetched for this build** -- the
  worked example only showed `title`/`source`/`link`/`date`. Modeled
  defensively per the plan's instruction: read `row.get("snippet")` and fall
  back to an empty string when absent, rather than assuming it exists.
- **Remaining-credits figure.** Not mentioned as present in any source
  fetched. `provider_metadata["reported_remaining"]` is set to `None`
  defensively, matching the other two keyed adapters -- but this is a weaker
  claim than Tavily's/Firecrawl's *documented* absences above; it was simply
  never seen in any source consulted here, not confirmed absent from
  Serper's own schema.

I7 -- this adapter calls Serper's search/news endpoints only; there is no
crawl/extract/scrape-equivalent product on Serper to accidentally reach for.
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
    ProviderMalformedResponse,
    ProviderRateLimited,
    ProviderTimeout,
    ProviderUnavailable,
)

WEB_ENDPOINT = "https://google.serper.dev/search"
NEWS_ENDPOINT = "https://google.serper.dev/news"
REQUEST_TIMEOUT_S = 10
DEFAULT_NUM = 10

# Status codes this provider uses to say "your key is wrong or missing".
# Raises types.ProviderAuthError, which ESCAPES past search() as a
# configuration error instead of falling through (user decision,
# 2026-08-20).
#
# Serper is the ONE adapter that needs 403 in here as well as 401 --
# tavily.py and firecrawl.py both use 401 alone. Multiple reports (notably
# crewAI issue #633, "Unauthorized Error (403) when Using SERPER_API_KEY")
# show Serper answering a missing/incorrect key with 403 rather than 401.
# Serper's own docs page 404s (see the module docstring), so this rests on
# secondary sources -- and 403 is overloaded here, also covering account
# restrictions and quota problems. See the comment at the raise site for
# why that trade-off is accepted rather than hedged.
_AUTH_STATUSES = frozenset({401, 403})

_ENDPOINT_FOR_TYPE: dict[SearchType, str] = {
    SearchType.WEB: WEB_ENDPOINT,
    SearchType.NEWS: NEWS_ENDPOINT,
}
_ROWS_KEY_FOR_TYPE: dict[SearchType, str] = {
    SearchType.WEB: "organic",
    SearchType.NEWS: "news",
}


class SerperProvider:
    """Key-gated Serper adapter -- separate per-vertical endpoints (web/news),
    both hit through `X-API-KEY` auth (never Tavily/Firecrawl's Bearer form).

    `api_key` is an optional constructor override (tests/callers that already
    hold a key in hand pass it explicitly, e.g. `SerperProvider(api_key='x')`
    -- this keeps working unchanged). When omitted (`None`, the default),
    `search()` reads the key lazily from `keys.py` at CALL time -- never at
    construction/import time, so the module-level `REGISTRY` registration
    below never touches `keys.json` just by being imported (Phase 5).
    """

    name = "serper"
    supports = frozenset({SearchType.WEB, SearchType.NEWS})
    requires_key = True

    def __init__(self, api_key: str | None = None) -> None:
        # I6: held only in this private attribute; resolved (constructor
        # value, else a lazy keys.get() read) at call time inside search(),
        # never logged/echoed/printed, never placed in a URL or an exception
        # message.
        self._api_key = api_key

    def estimate_cost(self, search_type: SearchType, **opts: Any) -> CreditCost:
        """Pure, network-free (SKETCH.md §11). 1 credit for <=10 results, 2
        for 11-100 -- confirmed uniform across web/news/images (docstring
        point (i)). Serper's homepage "1 credit = 1 search" framing is only
        the <=10 default; do NOT simplify this to a flat 1
        (fable-prompt-build-v1.md §5 Phase 4)."""
        num = int(opts.get("num", DEFAULT_NUM))
        if num <= 10:
            return CreditCost(credits=1, unit="api_credit", basis="<=10 results")
        return CreditCost(credits=2, unit="api_credit", basis="11-100 results")

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

        num = int(opts.get("num", DEFAULT_NUM))
        body: dict[str, Any] = {"q": query, "num": num}
        if opts.get("tbs"):
            body["tbs"] = opts["tbs"]

        endpoint = _ENDPOINT_FOR_TYPE[resolved_type]
        rows_key = _ROWS_KEY_FOR_TYPE[resolved_type]

        # Lazy, call-time key resolution (I6): an explicit constructor value
        # wins; otherwise read keys.json now, not at import time.
        api_key = self._api_key or keys.get(self.name)

        owns_client = client is None
        http_client = client or httpx.AsyncClient()
        try:
            try:
                resp = await http_client.post(
                    endpoint,
                    json=body,
                    headers={
                        "X-API-KEY": api_key,
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
            # search() (user decision, 2026-08-20).
            #
            # Serper is the one adapter where 403 -- not just 401 -- means
            # "bad key", which is why _AUTH_STATUSES differs here from
            # tavily.py/firecrawl.py. Evidence: crewAI issue #633,
            # "Unauthorized Error (403) when Using SERPER_API_KEY", plus
            # multiple reports of 403 on a missing/incorrect key. Serper's
            # own docs page 404s (see this module's docstring), so this
            # rests on secondary sources.
            #
            # Known trade-off, accepted deliberately: Serper overloads 403
            # to also cover account-level restrictions and quota problems,
            # so an exhausted account can surface as "check your key". That
            # errs toward telling the user something is wrong rather than
            # silently falling through to a weaker provider, which is the
            # behaviour this classification exists to prevent.
            if resp.status_code in _AUTH_STATUSES:
                raise ProviderAuthError(self.name, resp.status_code)
            if resp.status_code == 429:
                raise ProviderRateLimited(f"http {resp.status_code}")
            if resp.status_code >= 500:
                raise ProviderUnavailable(f"http {resp.status_code}")
            if resp.status_code >= 400:
                resp.raise_for_status()

            try:
                payload = resp.json()
            except Exception as exc:
                raise ProviderMalformedResponse(type(exc).__name__) from None

            raw_rows = payload.get(rows_key) if isinstance(payload, dict) else None
            if not isinstance(raw_rows, list):
                raise ProviderMalformedResponse(f"missing or non-list '{rows_key}' field")
        finally:
            if owns_client:
                await http_client.aclose()

        kept: list[dict[str, Any]] = []
        for row in raw_rows:
            if not isinstance(row, dict):
                continue
            title = " ".join(str(row.get("title") or "").split())
            raw_url = str(row.get("link") or "")
            if not title or not raw_url:
                continue
            kept.append(row)

        results: list[ResultItem] = []
        for rank, row in enumerate(kept, start=1):
            title = " ".join(str(row.get("title") or "").split())
            url = canonicalize(str(row.get("link") or ""))
            snippet = " ".join(str(row.get("snippet") or "").split())

            published_date: str | None = None
            date_confidence = DateConfidence.NONE
            if resolved_type is SearchType.NEWS:
                raw_date = row.get("date")
                if isinstance(raw_date, str) and raw_date.strip():
                    published_date = raw_date.strip()
                    date_confidence = DateConfidence.REPORTED
                else:
                    date_confidence = DateConfidence.INFERRED

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
            provider_metadata={"reported_remaining": None},  # see docstring
            usage=self.estimate_cost(resolved_type, num=num),
        )


# No key passed here -- search() resolves it lazily via keys.get("serper")
# at call time (Phase 5). Constructing with an explicit api_key (tests, or a
# caller that already holds one) still overrides this per-instance.
REGISTRY[Provider.SERPER] = SerperProvider()
