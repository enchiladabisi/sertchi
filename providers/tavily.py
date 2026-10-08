"""Tavily adapter -- REST, key-gated, #3 in the provider order (NEW-PROVIDERS.md §5.3).

Folds in `stock-cpp/SKETCH.md` D13's live-verified findings (2026-07-24)
rather than rediscovering them: `published_date` is real when present (RFC-822
form), `score` is relevance not quality (a `finbold.com` SEO page outscored
`cnbc.com` -- D13), and `search_depth: basic|advanced` costs 1|2 credits.

**Verified against live Tavily docs 2026-08-20** (fable-prompt-build-v1.md §5
Phase 3's mandatory build-time check), fetched from
https://docs.tavily.com/documentation/api-reference/endpoint/search and
https://help.tavily.com/articles/3347142954-best-practices :

- **(a) Auth form -- the two in-tree copies disagree (§2.4).**
  `tributary-cpp/search.py:293`'s `Authorization: Bearer <key>` header is what
  Tavily's current docs show (`bearerAuth`, `scheme: bearer`; the docs' own
  curl example is `-H "Authorization: Bearer tvly-YOUR_API_KEY"`).
  `garudagent/websearch.py:147`'s `api_key` JSON-body field is NOT the
  documented form as of 2026-08-20 -- this adapter uses the Bearer header,
  both because the docs endorse it and because it keeps the key out of a
  request body Sertchi may one day log (I6).
- **(b) `search_depth`/`topic`/`days`.** `search_depth` still has that name;
  documented values are `basic` (default), `advanced`, `fast`, `ultra-fast`
  -- `advanced` costs 2 credits, the other three cost 1 (matches D13's
  basic=1/advanced=2 rule; `fast`/`ultra-fast` are out of this adapter's
  scope per the build plan's explicit basic|advanced decision, but they cost
  the same 1 credit as basic, so treating "anything not advanced" as
  1-credit "basic depth" for billing purposes is accurate even if the caller
  passes `fast`/`ultra-fast`). `topic` still has that name; values are
  `general` (default), `news`, `finance`. **`days`** is a genuine
  discrepancy between two Tavily-owned sources: it is ABSENT from the
  OpenAPI-derived `/search` reference page (which instead documents
  `time_range`/`start_date`/`end_date`), but Tavily's own help-center
  "best practices" article documents it explicitly: "When `topic` is set to
  `news`, you can use the `days` parameter to define the number of days back
  from the current date to include results," default `3`, `x >= 0`. Folding
  D13's live-verified use of `topic="news"` + `days=N` in rather than
  rediscovering it, per SKETCH.md §1/§2.5 -- **confirmed current as of
  2026-08-20** via the help-center article, not just assumed from a stale
  local finding.
- **(c) Remaining-credits figure.** **Confirmed absent.** The `/search`
  response exposes no account-wide remaining-balance field or header. The
  only usage-adjacent field is an OPT-IN `usage.credits` integer (returned
  only when the request sets `include_usage: true`), and it reports credits
  *consumed by this one call* -- the same number this adapter already
  derives itself via `estimate_cost()` -- not a remaining balance. Sertchi
  does not request `include_usage` (I7 -- do not reach for endpoint/response
  surface beyond plain search). `provider_metadata["reported_remaining"]` is
  therefore always `None` for this adapter; this is a documented absence,
  not an "undocumented as of 2026-08-20" unknown.

`published_date` itself is the one field this adapter parses that is not in
the formal schema at all (point (c) above governs remaining-credits only).
Since D13 observed it live and Tavily's docs simply don't enumerate every
response field, this adapter parses it defensively whenever present
(`date_confidence=REPORTED`) and falls back to `NONE` when absent, on either
`web` or `news` calls -- never assumes it will be there.
"""

from __future__ import annotations

import urllib.parse
from email.utils import parsedate_to_datetime
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

ENDPOINT = "https://api.tavily.com/search"
REQUEST_TIMEOUT_S = 10
DEFAULT_SEARCH_DEPTH = "basic"
DEFAULT_NEWS_DAYS = 7  # decision, fable-prompt-build-v1.md §5 Phase 3 -- Tavily's own
                        # help-center default is 3; Sertchi pins its own default of 7,
                        # overridable per-call via opts["days"].

# Status codes this provider uses to say "your key is wrong or missing".
# Raises types.ProviderAuthError, which ESCAPES past search() as a
# configuration error instead of falling through (user decision,
# 2026-08-20). Tavily documents 401 for an invalid API key; its 403 is a
# permission/plan refusal and stays operational. Contrast serper.py, which
# has to include 403 here.
_AUTH_STATUSES = frozenset({401})


def _parse_published_date(raw: object) -> tuple[str | None, DateConfidence]:
    """RFC-822 (`Wed, 22 Jul 2026 11:18:07 GMT`) -> ISO-8601 `YYYY-MM-DD`.
    Stdlib only (I1) -- `email.utils.parsedate_to_datetime`, no `dateutil`.
    Absent or unparseable -> (None, NONE); present and parseable -> REPORTED."""
    if not raw or not isinstance(raw, str):
        return None, DateConfidence.NONE
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        return None, DateConfidence.NONE
    if dt is None:
        return None, DateConfidence.NONE
    return dt.date().isoformat(), DateConfidence.REPORTED


class TavilyProvider:
    """Key-gated Tavily adapter. `search` endpoint only (I7) -- never
    `extract`/`crawl`/`map`, even though Tavily offers all three and the
    build plan flags this as the single biggest temptation in this phase.

    `api_key` is an optional constructor override (tests/callers that already
    hold a key in hand pass it explicitly, e.g. `TavilyProvider(api_key='x')`
    -- this keeps working unchanged). When omitted (`None`, the default),
    `search()` reads the key lazily from `keys.py` at CALL time -- never at
    construction/import time, so the module-level `REGISTRY` registration
    below never touches `keys.json` just by being imported (Phase 5).
    """

    name = "tavily"
    supports = frozenset({SearchType.WEB, SearchType.NEWS})
    requires_key = True

    def __init__(self, api_key: str | None = None) -> None:
        # I6: held only in this private attribute; resolved (constructor
        # value, else a lazy keys.get() read) at call time inside search(),
        # never logged/echoed/printed, never placed in a URL or an exception
        # message.
        self._api_key = api_key

    def estimate_cost(self, search_type: SearchType, **opts: Any) -> CreditCost:
        """Pure, network-free (SKETCH.md §11). Sertchi requests `basic` by
        default; `advanced` costs 2 credits only when the caller passes it
        explicitly -- everything else (`basic`, and Tavily's `fast`/
        `ultra-fast`, out of this adapter's documented scope but real) costs
        1, same as `basic`."""
        depth = opts.get("search_depth", DEFAULT_SEARCH_DEPTH)
        if depth == "advanced":
            return CreditCost(credits=2, unit="api_credit", basis="advanced depth")
        return CreditCost(credits=1, unit="api_credit", basis="basic depth")

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

        depth = opts.get("search_depth", DEFAULT_SEARCH_DEPTH)
        body: dict[str, Any] = {
            "query": query,
            "search_depth": depth,
            # D13: Sertchi does its own nothing -- these two fields only cost
            # bytes on Tavily's side.
            "include_answer": False,
            "include_raw_content": False,
        }
        if resolved_type is SearchType.NEWS:
            body["topic"] = "news"
            body["days"] = int(opts.get("days", DEFAULT_NEWS_DAYS))

        # Lazy, call-time key resolution (I6): an explicit constructor value
        # wins; otherwise read keys.json now, not at import time.
        api_key = self._api_key or keys.get(self.name)

        owns_client = client is None
        http_client = client or httpx.AsyncClient()
        try:
            try:
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
                # I6: type(exc).__name__ only -- exception text can echo the
                # request body, which carries no key here, but the *header*
                # construction is close enough upstream that the discipline
                # stays uniform across every raise site in this method.
                raise ProviderTimeout(type(exc).__name__) from None
            except httpx.HTTPError as exc:
                raise ProviderUnavailable(type(exc).__name__) from None

            # A rejected key is a CONFIGURATION error and escapes past
            # search() (user decision, 2026-08-20); everything below it is
            # I3's operational closed set and falls through to the next
            # provider. The 401/403 split is plain HTTP semantics: 401 is
            # "I don't know who you are" (the key is wrong or missing),
            # while 403 is "I know who you are and still refuse" -- which
            # for Tavily covers an exhausted plan or a region block, things
            # a different provider genuinely might succeed at.
            if resp.status_code in _AUTH_STATUSES:
                raise ProviderAuthError(self.name, resp.status_code)
            if resp.status_code == 403:
                raise ProviderBlocked(f"http {resp.status_code}")
            if resp.status_code == 429:
                raise ProviderRateLimited(f"http {resp.status_code}")
            if resp.status_code >= 500:
                raise ProviderUnavailable(f"http {resp.status_code}")
            if resp.status_code >= 400:
                # Any other 4xx (400/404/422/...) is not in I3's closed set --
                # propagates uncaught rather than being treated as a
                # fall-through-worthy operational failure (ddg.py's own
                # pattern, restated in providers/ddg.py's _search_one).
                resp.raise_for_status()

            try:
                payload = resp.json()
            except Exception as exc:
                raise ProviderMalformedResponse(type(exc).__name__) from None

            raw_results = payload.get("results") if isinstance(payload, dict) else None
            if not isinstance(raw_results, list):
                raise ProviderMalformedResponse("missing or non-list 'results' field")
        finally:
            if owns_client:
                await http_client.aclose()

        # Filter first, rank after -- keeps provider_rank 1-based and dense
        # over what's actually kept, matching ddg.py's parse-then-enumerate
        # pattern rather than enumerating the raw (possibly sparse) rows.
        kept: list[dict[str, Any]] = []
        for row in raw_results:
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
            published_date, date_confidence = _parse_published_date(row.get("published_date"))
            score = row.get("score")
            # D13: provider_score is recorded and NEVER acted on -- no
            # filtering, no reordering, no floor. `kept`/`enumerate` above
            # preserve Tavily's own response order verbatim; provider_rank
            # reflects that order, not a re-sort by score.
            results.append(ResultItem(
                title=title,
                url=url,
                snippet=" ".join(str(row.get("content") or "").split()),
                domain=urllib.parse.urlsplit(url).netloc.lower(),
                registrable_domain=registrable_domain(url),
                published_date=published_date,
                date_confidence=date_confidence,
                provider=self.name,
                provider_rank=rank,
                provider_score=float(score) if isinstance(score, (int, float)) else None,
                thumbnail_url=None,
            ))

        return ProviderResponse(
            provider=self.name,
            results=results,
            # (c) above: Tavily's /search never exposes a remaining-credit
            # balance -- this key is always None for this adapter, a
            # documented absence Phase 6 can rely on rather than probe for.
            provider_metadata={"reported_remaining": None},
            usage=self.estimate_cost(resolved_type, search_depth=depth),
        )


# No key passed here -- search() resolves it lazily via keys.get("tavily")
# at call time (Phase 5). Constructing with an explicit api_key (tests, or a
# caller that already holds one) still overrides this per-instance.
REGISTRY[Provider.TAVILY] = TavilyProvider()
