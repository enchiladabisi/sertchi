"""Brave Search adapter -- REST, key-gated, #3 in the seven-provider order
(Keenable, Exa, Brave, Tavily, Firecrawl, Serper, DDG).

**Evidence tier: Brave's primary docs, read 2026-10-08, plus one
user-supplied fact** (the account has a $5/month free credit), then a LIVE
smoke the same day (web + news + a bad-key probe + one orchestrated call):
shapes held, the ledger charged exactly 5,000, and a bogus token returned
`422 {"error": {"code": "SUBSCRIPTION_TOKEN_INVALID", ...}}` -- the 422 rule
below is CONFIRMED, not a guess. A missing header is a plain
`422 code VALIDATION` (never reached: gate 2 drops a provider with no key).
Still UNVERIFIED: the credits-spent status and news pricing. Brave's
dashboard docs fetched for this build covered the endpoints, auth header,
parameters and the web/news row fields; they did NOT document the error
body, which status code means "credits spent", or the per-request price of
the news endpoint. Those three are marked UNVERIFIED below and are pinned by
tests as *behaviour we chose*, not as facts Brave told us. A live smoke
(etc/planning/) is the way to close them. Brave was dropped from Sertchi on
2026-08-20 because its free path seemed gated behind public attribution
(SKETCH.md §2); the user's account has the credit, so it is back.

- **Endpoints.** `GET https://api.search.brave.com/res/v1/web/search` and
  `GET https://api.search.brave.com/res/v1/news/search`. Auth is the
  `X-Subscription-Token: <key>` header (a third spelling -- not Bearer, not
  `X-API-Key`). Query goes in the `q` URL parameter; the key never does (I6).
- **Row shapes differ per vertical.** Web rows live in `payload["web"]
  ["results"]` (and the whole `web` object is absent when nothing matched --
  that reads as an empty result, not malformed). News rows live in the
  top-level `payload["results"]`. Both carry `title`, `url`, `description`,
  `age` (a *relative* string like "2 days ago") and `page_age` (an ISO
  datetime). Only `page_age` is a date, so only it becomes `published_date`
  (`REPORTED`); `age` is deliberately ignored. A missing `page_age` is an
  absence on a provider with a real date field -> `NONE`, never `INFERRED`
  (the firecrawl.py / keenable.py precedent, I8).
- **`description` is HTML-flavoured.** Brave wraps matched terms in
  `<strong>` and escapes entities, so the snippet (and title) are stripped of
  tags and unescaped here -- a caller should not receive markup.
- **Cost unit `usd_micro`, same as Exa, for the same reason.** The free
  allowance is a dollar balance ($5/month), and Brave re-priced the Search
  plan once already (from $30 to $5 per 1,000 in May 2026). 1 credit =
  $0.000001, so one search is `5_000` and the integer ledger never learns
  dollars exist (I4). Flat per request: `count` does not change the price.
  **UNVERIFIED:** that news is billed at the same $5/1,000 as web; the pricing
  pages seen describe one "Search" plan, not per-vertical rates.
- **Status codes.** `401` and `403` are not special-cased as a pair: `401` is
  a rejected key -> `ProviderAuthError`. **`422` is Brave's validation
  status AND (UNVERIFIED, from memory of the error schema) the status it uses
  for `SUBSCRIPTION_TOKEN_INVALID`** (live-confirmed), so a 422 is classified by the `code`
  field of its error body: a code naming the token -> `ProviderAuthError`,
  anything else -> a plain `raise_for_status()` (our own bad request, a bug,
  not a fall-through). `402` -> `ProviderBlocked` (out of credits).
  `429` -> `ProviderRateLimited`. **UNVERIFIED:** Brave's docs say 429 for
  exceeding a limit and name no 402; both fall through, so guessing wrong
  here costs a health-ledger label, not correctness. `403` is operational
  (`ProviderBlocked`) -- the Exa precedent for an undocumented code.
- **No per-second ceiling worth pacing: live `X-RateLimit-Remaining` was
  `49, 0` then `48, 0` (so ~50 req/s).** Unpaced; a 429 falls through.
- **`X-RateLimit-Remaining` is recorded, not used.** It is a
  comma-separated "per-second, per-month" pair in *requests*, while this
  ledger is in micro-dollars, so feeding it to `reported_remaining` would
  corrupt the balance. Live, the monthly half read `0` on successful
  calls (credit-based plans apparently report no request quota), which
  would have zeroed the pool had it been trusted. The raw header is kept in `provider_metadata`.

I7 -- this adapter calls the web and news search endpoints and nothing
else. Brave also sells an LLM-context/"answers" product and image/video/
place endpoints; none are called here.
"""

from __future__ import annotations

import html
import re
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

WEB_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
NEWS_ENDPOINT = "https://api.search.brave.com/res/v1/news/search"
REQUEST_TIMEOUT_S = 10
DEFAULT_COUNT = 10
MAX_COUNT = {SearchType.WEB: 20, SearchType.NEWS: 50}  # per-vertical caps
NEWS_FRESHNESS = "pw"  # past week -- the same 7-day window every adapter uses
SNIPPET_MAX_CHARS = 400  # matches every other adapter's cap (SKETCH.md §9).

#: $5.00 per 1,000 requests = 5,000 micro-dollars each. Flat per request.
COST_PER_SEARCH_USD_MICRO = 5_000

# 401 is "I don't know who you are". 422 is handled separately below because
# Brave overloads it (see the module docstring).
_AUTH_STATUSES = frozenset({401})

_TAG_RE = re.compile(r"<[^>]+>")


def _plain(value: object) -> str:
    """Brave text -> plain text: drop tags, unescape entities, collapse
    whitespace."""
    return " ".join(html.unescape(_TAG_RE.sub("", str(value or ""))).split())


def _error_code(resp: httpx.Response) -> str:
    """The `error.code` of Brave's error body, upper-cased, or "" when the
    body is not the expected shape. Never raises."""
    try:
        body = resp.json()
    except Exception:
        return ""
    error = body.get("error") if isinstance(body, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    return str(code).upper() if code else ""


class BraveProvider:
    """Key-gated Brave Search adapter. Web and news search only (I7).

    `api_key` is an optional constructor override; when omitted, `search()`
    reads the key lazily from `keys.py` at CALL time, so importing this
    module never touches `keys.json`.
    """

    name = "brave"
    supports = frozenset({SearchType.WEB, SearchType.NEWS})
    requires_key = True

    def __init__(self, api_key: str | None = None) -> None:
        # I6: held only in this private attribute; never logged, echoed,
        # printed, or placed in a URL or an exception message.
        self._api_key = api_key

    def estimate_cost(self, search_type: SearchType, **opts: Any) -> CreditCost:
        """Pure, network-free. Flat 5,000 micro-dollars per search."""
        return CreditCost(
            credits=COST_PER_SEARCH_USD_MICRO,
            unit="usd_micro",
            basis="$5 per 1000 requests",
        )

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

        count = min(int(opts.get("count", DEFAULT_COUNT)), MAX_COUNT[resolved_type])
        params: dict[str, Any] = {"q": query, "count": count}
        if resolved_type is SearchType.NEWS:
            # I8: a real native news endpoint; freshness narrows it to a week.
            params["freshness"] = NEWS_FRESHNESS
            endpoint = NEWS_ENDPOINT
        else:
            endpoint = WEB_ENDPOINT

        # Lazy, call-time key resolution (I6).
        api_key = self._api_key or keys.get(self.name)

        owns_client = client is None
        http_client = client or httpx.AsyncClient()
        try:
            try:
                # I7: this is the ONLY httpx call this method makes.
                resp = await http_client.get(
                    endpoint,
                    params=params,
                    headers={
                        "X-Subscription-Token": api_key,
                        "Accept": "application/json",
                    },
                    timeout=REQUEST_TIMEOUT_S,
                )
            except httpx.TimeoutException as exc:
                # I6: type(exc).__name__ only -- never the exception text.
                raise ProviderTimeout(type(exc).__name__) from None
            except httpx.HTTPError as exc:
                raise ProviderUnavailable(type(exc).__name__) from None

            if resp.status_code in _AUTH_STATUSES:
                raise ProviderAuthError(self.name, resp.status_code)
            if resp.status_code == 422 and "TOKEN" in _error_code(resp):
                # Brave's rejected-key answer, by error code (UNVERIFIED --
                # see module docstring). A plain 422 is OUR bad request.
                raise ProviderAuthError(self.name, resp.status_code)
            if resp.status_code in (402, 403):
                raise ProviderBlocked(f"http {resp.status_code}")
            if resp.status_code == 429:
                raise ProviderRateLimited(f"http {resp.status_code}")
            if resp.status_code >= 500:
                raise ProviderUnavailable(f"http {resp.status_code}")
            if resp.status_code >= 400:
                # Not in I3's closed set -- propagates rather than falling
                # through (includes an ordinary 422 validation failure).
                resp.raise_for_status()

            try:
                payload = resp.json()
            except Exception as exc:
                raise ProviderMalformedResponse(type(exc).__name__) from None
            if not isinstance(payload, dict):
                raise ProviderMalformedResponse("non-dict response body")

            if resolved_type is SearchType.NEWS:
                container: Any = payload
            else:
                # Brave omits the `web` object entirely when nothing matched.
                container = payload.get("web", {})
                if not isinstance(container, dict):
                    raise ProviderMalformedResponse("non-dict 'web' field")
            raw_rows = container.get("results", [])
            if not isinstance(raw_rows, list):
                raise ProviderMalformedResponse("non-list 'results' field")

            rate_limit_remaining = resp.headers.get("X-RateLimit-Remaining")
        finally:
            if owns_client:
                await http_client.aclose()

        # Filter first, rank after -- keeps provider_rank 1-based and dense.
        kept: list[dict[str, Any]] = []
        for row in raw_rows:
            if not isinstance(row, dict):
                continue
            if not _plain(row.get("title")) or not str(row.get("url") or ""):
                continue
            kept.append(row)

        results: list[ResultItem] = []
        for rank, row in enumerate(kept, start=1):
            url = canonicalize(str(row.get("url") or ""))

            # `page_age` is the date; `age` is a relative string and is not.
            raw_date = row.get("page_age")
            published_date: str | None = None
            date_confidence = DateConfidence.NONE
            if isinstance(raw_date, str) and raw_date.strip():
                published_date = raw_date.strip()
                date_confidence = DateConfidence.REPORTED

            results.append(ResultItem(
                title=_plain(row.get("title")),
                url=url,
                snippet=_plain(row.get("description"))[:SNIPPET_MAX_CHARS],
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
                # In requests, not micro-dollars: must not reach the ledger.
                "reported_remaining": None,
                "rate_limit_remaining_raw": rate_limit_remaining,
            },
            usage=self.estimate_cost(resolved_type, count=count),
        )


# No key passed here -- search() resolves it lazily via keys.get("brave").
REGISTRY[Provider.BRAVE] = BraveProvider()
