"""Sertchi -- shared multi-provider search utility. THE ORCHESTRATOR
(fable-prompt-build-v1.md §5 Phase 7).

This module is the only place in Sertchi where a ``SearchResult`` is
assembled (I2). Adapters return a ``ProviderResponse`` describing their own
call and nothing else; ``status``, ``provider_used`` and ``attempts`` are
orchestration facts, decided here and nowhere else.

PUBLIC API
----------
``search()``   -- walk the fallback chain, stop at the first provider that
                  returns at least one row (SKETCH.md §5).
``multi_search()`` -- fan out to every eligible provider concurrently and
                  merge with ``dedupe.merge()`` (SKETCH.md §6).

Both are ``async``. A non-async front door (``sync.py``) is Phase 8 and does
not exist yet.

THE FIVE CANDIDATE GATES, IN THIS EXACT ORDER (SKETCH.md §4/§5, gate 5 added
by the throttling build, fable-prompt-throttling-v1.md §5 Phase 4)
--------------------------------------------------------------------------
1. Provider **declaration order** in ``types.py`` -- or the caller's
   ``providers`` subset, which is still subject to every gate below.
   (Since 2026-09-26 this is the order gates run in and ``multi_search()``'s
   dedupe tiebreak -- NOT ``search()``'s try order, which is a weighted
   random draw over the survivors; see ``_draw_order()``.)
   ``types.py`` is the single source of truth for that order; the draw is
   the only other ordering in this file (``_resolve_providers()`` still
   re-sorts a caller's subset back into declaration order rather than
   letting a caller's argument order become a rival ranking).
2. ``requires_key`` and no configured key -> dropped. "No key -> not an
   option, not try-and-fail" (§5).
3. ``search_type`` not in the adapter's ``supports`` -> dropped (**I8**: a
   ``news`` call never silently degrades to ``web``).
4. ``usage.can_afford()`` fails -> ``SKIPPED_QUOTA``; ``usage.is_cooling_down()``
   says wait -> ``SKIPPED_BACKOFF``. Both are decided locally, with **no
   network call**.
5. ``usage.reserve_slot()`` refuses -> ``THROTTLED``. Unlike gates 2-4, this
   one is claimed inside ``_attempt()``, not ``_select_candidates()`` -- see
   that function's docstring for why a side-effecting claim cannot live in
   selection.

Gates 2 and 3 produce no ``AttemptRecord`` -- ``AttemptOutcome`` has no
member for "no key" or "wrong search type", and inventing one is out of
scope for this phase. Both surface as a ``limitations`` line instead, which
is what a caller actually reads. Gate 5 is different: it DOES produce an
``AttemptRecord`` (``THROTTLED``), always, in ``attempts`` -- but its
human-readable ``limitations`` note is included only when the search's
final ``status`` is not ``OK`` (user decision 2026-08-20; see the I5
section below and ``_attempt()``'s docstring). A throttled provider that
was skipped in favour of a lower-ranked success is not something a caller
who got a good answer needs to be told about; a throttled provider that
left the search with nothing is.

STATUS (fable-prompt-build-v1.md §6 Q8 -- CHANGED from the plan's draft)
-----------------------------------------------------------------------
=========================================================  ============
condition                                                   status
=========================================================  ============
>=1 result, no matter how many earlier attempts failed      ``OK``
0 results, every attempt was ``EMPTY`` (no errors/skips)    ``EMPTY``
0 results and >=1 ``ERROR``/``SKIPPED_*`` (or nothing       ``UNAVAILABLE``
even eligible to try)
=========================================================  ============

``PARTIAL`` is declared in ``SearchStatus`` but is **never assigned by this
module**. Getting rows from provider #3 after #1 and #2 failed is a clean
``OK``; ``attempts`` is where the caller learns it took three tries.
``PARTIAL`` stays reserved for a future *per-result* degradation signal.

I3 -- FAIL-OPEN, IN BOTH DIRECTIONS
-----------------------------------
``ProviderOperationalError`` (the closed set in ``providers/base.py``:
timeout, connection/DNS failure, 429, 5xx, provider block, malformed
payload) is caught, recorded as an ``ERROR`` attempt, and the chain falls
through to the next provider. **Everything else propagates** --
``InvalidRequest``, ``ProviderAuthError``, ``TypeError``, ``KeyError``,
``AttributeError``, an internal schema violation, a missing adapter. There
is no bare ``except Exception`` anywhere in this file. The module has
exactly four ``except`` clauses: ``ProviderOperationalError`` (the chain's
fall-through), ``ProviderAuthError`` (which settles the ledger and then
re-raises -- see below), and two naming ``ValueError`` -- the enum-coercion
catches in ``_resolve_search_type()``/``_resolve_providers()``, which
re-raise as ``InvalidRequest`` rather than swallowing anything. A
``search_type`` that is not a ``SearchType`` value raises ``InvalidRequest``
on the FIRST line of the entry point, before any provider is looked up, let
alone contacted.

A rejected API key is a CONFIGURATION error, not an operational one (user
decision, 2026-08-20). ``types.ProviderAuthError`` therefore sits outside
``ProviderOperationalError``'s closed set and escapes past ``search()``
rather than falling through: no other provider can fix a key the user has
to correct, and falling through would return a quietly degraded answer from
a lower-ranked provider while the real problem stayed invisible. The one
thing this module does before letting it escape is settle the ledger
honestly -- zero credits charged, circuit breaker untouched -- so a bad key
neither bills for work never done nor leaves the provider in cooldown once
the key is fixed. Being merely out of credit or region-blocked is a
different thing and stays operational.

``multi_search()`` needs one extra guard: ``asyncio.gather(...,
return_exceptions=True)`` captures *everything*, including the bugs I3 says
must escape. So the gather's results are re-scanned and any ``BaseException``
is re-raised rather than folded into a quiet ``UNAVAILABLE``.

I5 -- reserve -> unlock -> call -> lock -> reconcile
---------------------------------------------------
Every attempt runs inside ``usage.metered_call()``, Phase 6's helper, which
makes that ordering structural: it reserves (taking and releasing the lock),
yields, and reconciles in a ``finally`` (taking and releasing the lock
again). The network call happens in between with no lock held. Nothing in
this file touches the ledger's lock directly.

Gate 5's slot claim (``usage.reserve_slot()``) is the same shape one level
earlier: it takes and releases the lock and returns before
``metered_call()`` is ever entered. The ``await asyncio.sleep(grant.wait_s)``
that follows a grant sits outside any lock, exactly like the network call
that follows it -- pacing never holds the ledger lock across a wait any
more than a request holds it across an ``await``.

LIMITATIONS SUPPRESSION FOR THROTTLED ATTEMPTS (user decision 2026-08-20)
--------------------------------------------------------------------------
A ``THROTTLED`` ``AttemptRecord`` is unconditionally present in
``attempts`` -- that part of "honest reporting" never changes. Its
human-readable note (``f"{name} skipped: throttled, next slot beyond
max_wait_s"``) is different: it is folded into ``limitations`` only when
the search's final ``status`` is not ``SearchStatus.OK``. A search that
succeeded via a fallback provider discards the throttled notes rather than
reporting them -- a future reader should not assume this matches gate 2/3's
skip notes, which are unconditional because they have no ``AttemptRecord``
to fall back on.
"""

from __future__ import annotations

import asyncio
import math
import random
import time
from collections.abc import Sequence
from dataclasses import dataclass

import httpx

from . import dedupe, keys, usage
from .providers.base import REGISTRY, ProviderOperationalError, SearchProvider

# Importing the adapter modules is what populates REGISTRY -- each one
# registers its instance at module scope. Nothing else in this file knows a
# provider's identity, so this is the only provider-specific line here.
from .providers import ddg as _ddg  # noqa: F401  (registration side effect)
from .providers import exa as _exa  # noqa: F401
from .providers import firecrawl as _firecrawl  # noqa: F401
from .providers import keenable as _keenable  # noqa: F401
from .providers import serper as _serper  # noqa: F401
from .providers import tavily as _tavily  # noqa: F401
from .types import (
    AttemptOutcome,
    AttemptRecord,
    CreditCost,
    DateConfidence,
    InvalidRequest,
    Provider,
    ProviderAuthError,
    ProviderResponse,
    QuotaKind,
    ResultItem,
    SearchResult,
    SearchStatus,
    SearchType,
    SertchiError,
)

__all__ = [
    "search",
    "multi_search",
    "SearchType",
    "Provider",
    "ResultItem",
    "SearchResult",
    "AttemptRecord",
    "SertchiError",
    "InvalidRequest",
    "ProviderAuthError",
]

__version__ = "1.0.0"


# --------------------------------------------------------------------------
# Argument resolution -- all of it runs BEFORE any provider is contacted (I3)
# --------------------------------------------------------------------------


def _resolve_search_type(search_type: SearchType | str) -> SearchType:
    """Coerce the caller's ``search_type`` to a ``SearchType``, or raise.

    I3's other direction, and the reason this is the first statement of both
    entry points: ``search(q, "nonsense")`` is a CALLER error, so it raises
    ``InvalidRequest`` with zero requests issued -- never a quiet
    ``UNAVAILABLE`` that looks exactly like an outage.
    """
    if isinstance(search_type, SearchType):
        return search_type
    if isinstance(search_type, str):
        try:
            return SearchType(search_type)
        except ValueError as exc:
            valid = ", ".join(repr(t.value) for t in SearchType)
            raise InvalidRequest(
                f"invalid search_type {search_type!r}; expected one of {valid}"
            ) from exc
    raise InvalidRequest(
        "search_type must be a SearchType or one of its string values, got "
        f"{type(search_type).__name__}"
    )


def _resolve_query(query: str) -> str:
    """A non-string or blank query is a caller error, not an outage.

    Deliberately raising rather than searching: a blank query would spend a
    real credit on a call that cannot mean anything (priority #2).
    """
    if not isinstance(query, str):
        raise InvalidRequest(f"query must be a str, got {type(query).__name__}")
    if not query.strip():
        raise InvalidRequest("query must not be empty or whitespace-only")
    return query


def _resolve_providers(providers: Sequence[Provider] | None) -> list[Provider]:
    """The candidate list, always in ``types.py`` declaration order.

    A caller's subset is a FILTER, not a rival ranking: it is de-duplicated
    and re-sorted back into declaration order, because ``Provider``'s
    declaration order is the single source of truth for both the fallback
    chain (§5) and ``dedupe.merge()``'s tiebreak (§6). Letting a caller's
    argument order win would create the second ordering the build plan
    forbids -- and would silently change which provider wins a dedupe
    collision in ``multi_search()``.
    """
    if providers is None:
        return list(Provider)
    # A bare Provider (which is also a str) or a bare string is a common
    # slip; both are iterable-ish enough to fail confusingly later.
    if isinstance(providers, (Provider, str, bytes)):
        raise InvalidRequest(
            "providers must be a sequence of Provider values, got a single "
            f"{type(providers).__name__}"
        )
    requested: set[Provider] = set()
    for item in providers:
        if isinstance(item, Provider):
            requested.add(item)
            continue
        if isinstance(item, str):
            try:
                requested.add(Provider(item))
            except ValueError as exc:
                valid = ", ".join(repr(p.value) for p in Provider)
                raise InvalidRequest(
                    f"unknown provider {item!r}; expected one of {valid}"
                ) from exc
            continue
        raise InvalidRequest(
            "providers entries must be Provider values or their string names, "
            f"got {type(item).__name__}"
        )
    if not requested:
        raise InvalidRequest(
            "providers must not be empty; pass None for the full chain"
        )
    return [provider for provider in Provider if provider in requested]


# --------------------------------------------------------------------------
# Candidate selection -- the four gates
# --------------------------------------------------------------------------


@dataclass(slots=True)
class _Candidate:
    """One provider that survived all four gates, with its pre-call estimate.

    The cost is computed once, during selection, and reused for the
    reservation -- so the figure the affordability gate approved is exactly
    the figure that gets held against the balance.
    """

    provider: Provider
    adapter: SearchProvider
    cost: CreditCost


@dataclass(slots=True)
class _Selection:
    candidates: list[_Candidate]
    #: Keyed by the ``Provider`` MEMBER, not by the adapter's ``name`` string.
    #: The enum member is the slot the adapter occupies in the chain and is
    #: what ordering is derived from; ``adapter.name`` is merely what the
    #: adapter calls itself. Keying by the string would make an attempt record
    #: silently disappear from ``attempts`` (and therefore from the status
    #: decision) whenever the two disagreed -- a stand-in adapter, a rename, a
    #: typo. The enum key cannot drift.
    skipped: dict[Provider, AttemptRecord]
    limitations: list[str]


def _select_candidates(
    chosen: Sequence[Provider], search_type: SearchType, opts: dict
) -> _Selection:
    """Run the four gates in order, over ``chosen`` (already in declaration
    order). Returns the survivors, the skip records, and the human-readable
    notes explaining every drop.

    NOTHING HERE ISSUES A REQUEST. Both ``SKIPPED_*`` outcomes are decided
    locally from the ledger, which is the whole point: a provider that is out
    of quota or in a cooldown costs nothing to skip.

    A NOTE ON THE LEDGER AND SKIPS (inherited from Phase 6): the plan's prose
    says every attempt "including the two skip outcomes" is reported to
    ``usage.py``. Phase 6 deliberately built no function for that, and this
    module does not invent one. A skip made no request, so it cost nothing
    and proves nothing about the provider's health -- feeding it to the
    circuit breaker would either double-count a cooldown that is already
    open, or reset one on the strength of a call that never happened. The
    skip is recorded where a caller actually needs it: the ``attempts`` list.
    """
    candidates: list[_Candidate] = []
    skipped: dict[Provider, AttemptRecord] = {}
    limitations: list[str] = []

    for provider in chosen:
        adapter = REGISTRY.get(provider)
        if adapter is None:
            # A declared provider with no registered adapter is a missing
            # internal dependency, which I3 says must raise -- never a quiet
            # drop that reads as "that provider was unlucky today".
            raise InvalidRequest(
                f"no adapter registered for provider {provider.value!r}"
            )
        name = adapter.name

        # Gate 2 -- key.
        if adapter.requires_key and not keys.get(name):
            limitations.append(f"{name} skipped: no API key configured")
            continue

        # Gate 3 -- search type (I8: never silently degrade news to web).
        if search_type not in adapter.supports:
            limitations.append(
                f"{name} skipped: does not support {search_type.value}"
            )
            continue

        # The adapter owns its cost rule (I4); this is the pure, network-free
        # estimate the affordability gate needs.
        cost = adapter.estimate_cost(search_type, **opts)

        # Gate 4a -- quota.
        affordable, why = usage.can_afford(name, cost)
        if not affordable:
            skipped[provider] = AttemptRecord(
                provider=name,
                outcome=AttemptOutcome.SKIPPED_QUOTA,
                latency_ms=None,  # no request was made
                error=why,
            )
            limitations.append(f"{name} skipped: quota reserve reached")
            continue

        # Gate 4b -- circuit breaker / rate floor.
        cooling, why = usage.is_cooling_down(name)
        if cooling:
            skipped[provider] = AttemptRecord(
                provider=name,
                outcome=AttemptOutcome.SKIPPED_BACKOFF,
                latency_ms=None,  # no request was made
                error=why,
            )
            limitations.append(f"{name} skipped: in backoff cooldown")
            continue

        candidates.append(_Candidate(provider=provider, adapter=adapter, cost=cost))

    return _Selection(candidates=candidates, skipped=skipped, limitations=limitations)


# --------------------------------------------------------------------------
# search()'s draw order -- weighted random, not declaration order
# --------------------------------------------------------------------------

#: Weight multiplier for a one-time (``QuotaKind.TOTAL``) grant. A monthly
#: pool left unspent is lost at the rollover; a one-time grant never refills,
#: so it should be drawn on more slowly than its raw size suggests. Serper's
#: 2,500-credit grant weighs like ~625 renewing searches.
ONE_TIME_GRANT_DISCOUNT = 0.25

#: The draw's randomness source. Module-level so a test can seed it.
_rng = random.Random()


def _draw_weight(candidate: _Candidate) -> float | None:
    """Searches left at this call's price, discounted for a one-time grant.
    ``None`` -> the provider has no quota to weigh (DDG): last resort only."""
    name = candidate.adapter.name
    left = usage.searches_left(name, candidate.cost)
    if left is None:
        return None
    if usage.get_state(name).quota_kind is QuotaKind.TOTAL:
        left *= ONE_TIME_GRANT_DISCOUNT
    # A candidate that survived gate 4 can afford this call, so its weight is
    # never really zero; the floor only keeps the log() below defined.
    return max(left, 1e-9)


def _draw_order(candidates: Sequence[_Candidate]) -> list[_Candidate]:
    """``search()``'s try order (user decision 2026-09-26): weighted random
    by searches left, without replacement, then every unmetered provider
    (DDG) after all of them, in declaration order.

    Efraimidis-Spirakis: each candidate draws key ``log(u) / w`` and the
    highest key goes first, so the first pick lands on a candidate with
    probability ``w / sum(w)`` and every later pick is the same draw over
    whoever is left -- the fallback chain is itself weighted.

    ``multi_search()`` never calls this: it asks everyone, and its dedupe
    tiebreak stays on declaration order.
    """
    weighted: list[tuple[float, _Candidate]] = []
    last_resort: list[_Candidate] = []
    for candidate in candidates:
        weight = _draw_weight(candidate)
        if weight is None:
            last_resort.append(candidate)
        else:
            u = 1.0 - _rng.random()  # (0, 1], so log() is defined
            weighted.append((math.log(u) / weight, candidate))
    weighted.sort(key=lambda pair: pair[0], reverse=True)
    return [candidate for _, candidate in weighted] + last_resort


# --------------------------------------------------------------------------
# One metered attempt
# --------------------------------------------------------------------------


async def _attempt(
    candidate: _Candidate,
    query: str,
    search_type: SearchType,
    client: httpx.AsyncClient | None,
    opts: dict,
    *,
    max_wait_s: float | None,
) -> tuple[AttemptRecord, ProviderResponse | None]:
    """Run one provider call under the usage meter.

    GATE 5 IS CLAIMED HERE, NOT IN ``_select_candidates()``. Gates 1-4 are
    pure reads of the ledger/keys/adapter capabilities -- selection can run
    them over every candidate for free. A slot claim is different: it has a
    side effect (``usage.reserve_slot()`` advances the provider's
    ``next_slot_at`` on grant). ``search()`` stops at the first candidate
    that returns a row, so claiming a slot during selection would burn slots
    for providers that are never actually called -- exactly the buffering
    priority #2 forbids. Claiming per-attempt means only a provider that is
    genuinely invoked ever consumes its own pacing schedule.

    On refusal, this returns a plain ``THROTTLED`` record -- **not** an
    exception. No credit is reserved and ``last_request_at``/``next_slot_at``
    are not touched (N1), because no request was made; ``metered_call()`` is
    never entered on this path.

    On grant with ``wait_s > 0``, this awaits ``asyncio.sleep(grant.wait_s)``
    -- never ``time.sleep``, which would block the event loop and serialize
    ``multi_search()``'s whole fan-out. The sleep sits outside any lock (I5):
    ``usage.reserve_slot()`` has already taken and released the ledger lock
    by the time control reaches here.

    I5 is also satisfied structurally by ``usage.metered_call()``, entered
    only after a grant: it reserves (lock taken and released), yields here
    for the ``await``, and reconciles in a ``finally`` (lock taken and
    released again). The request itself holds no lock.

    I3 lives in the ``except`` clause below, and it names exactly one type.
    ``ProviderOperationalError`` -> recorded as ``ERROR``, ``None`` returned,
    caller falls through. Anything else -- a bug, a caller error, an internal
    schema violation -- propagates out of this coroutine untouched. When it
    does, ``metered_call()``'s ``finally`` still settles the reservation with
    its default ``ERROR`` outcome, which is the deliberately conservative
    reading: a call that blew up mid-flight may still have cost money.
    """
    name = candidate.adapter.name

    grant = usage.reserve_slot(name, max_wait_s=max_wait_s)
    if not grant.granted:
        return AttemptRecord(
            provider=name,
            outcome=AttemptOutcome.THROTTLED,
            latency_ms=None,  # no request was made
            error=grant.reason,
        ), None
    if grant.wait_s > 0:
        await asyncio.sleep(grant.wait_s)  # I5: no lock held here

    response: ProviderResponse | None = None
    error_text: str | None = None

    with usage.metered_call(name, candidate.cost) as call:
        started = time.monotonic()
        try:
            response = await candidate.adapter.search(
                query, search_type, client=client, **opts
            )
        except ProviderAuthError:
            # A rejected key is a CONFIGURATION error, not an operational
            # one (user decision, 2026-08-20): it escapes past search()
            # instead of falling through, because no other provider can fix
            # a key the user needs to correct, and a quiet fallback would
            # hand back a degraded answer while hiding the real problem.
            #
            # Settle honestly before it escapes. The call provably cost
            # nothing -- the provider refused it at the door -- so charge 0
            # rather than metered_call()'s conservative "assume it cost the
            # estimate" default, and leave the circuit breaker alone: the
            # provider is not unhealthy, and advancing it would leave this
            # provider in cooldown right after the user fixes their key.
            call.latency_ms = (time.monotonic() - started) * 1000.0
            call.outcome = AttemptOutcome.ERROR
            call.actual = CreditCost(
                credits=0, unit="none", basis="auth rejected; no work performed"
            )
            call.counts_against_health = False
            raise
        except ProviderOperationalError as exc:
            call.latency_ms = (time.monotonic() - started) * 1000.0
            call.outcome = AttemptOutcome.ERROR
            # I6: every adapter builds these messages from
            # `type(exc).__name__` or a bare status code -- never a response
            # body, never a header, so no key can reach this string.
            detail = str(exc)
            error_text = f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__
        else:
            call.latency_ms = (time.monotonic() - started) * 1000.0
            # An EMPTY result set is an operational outcome, not an error --
            # it falls through to the next provider, and Phase 6 resets the
            # circuit breaker on it (a 200 with zero rows still proves the
            # provider is reachable).
            call.outcome = (
                AttemptOutcome.SUCCESS if response.results else AttemptOutcome.EMPTY
            )
            # I4: the ADAPTER states what its own call cost; this module only
            # passes that figure through to the ledger.
            call.actual = response.usage
            metadata = response.provider_metadata
            if isinstance(metadata, dict):
                call.reported_remaining = metadata.get("reported_remaining")

    record = AttemptRecord(
        provider=name,
        outcome=call.outcome,
        latency_ms=call.latency_ms,
        error=error_text,
    )
    return record, response


# --------------------------------------------------------------------------
# Assembling the SearchResult -- I2's "only here" territory
# --------------------------------------------------------------------------


def _ordered_attempts(
    records: dict[Provider, AttemptRecord], chosen: Sequence[Provider]
) -> list[AttemptRecord]:
    """Attempts in provider declaration order -- one record per provider.

    For ``search()`` that is also chain order, and it puts a skip at the
    position in the chain where it happened rather than in a separate
    bucket. For ``multi_search()``, where the calls finish in whatever order
    the network decides, declaration order is the only stable one available.
    """
    return [records[p] for p in chosen if p in records]


def _status(
    results: Sequence[ResultItem], attempts: Sequence[AttemptRecord]
) -> SearchStatus:
    """The Q8 table, and the ONLY place a ``SearchStatus`` is chosen.

    ``PARTIAL`` is unreachable by construction -- it is not written anywhere
    in this function or this module. A fallback success is a plain ``OK``.

    Zero attempts (nothing was even eligible to try -- every provider lacked
    a key, or none supported this ``search_type``) reads ``UNAVAILABLE``, not
    ``EMPTY``: "no provider could be asked" is an availability statement,
    while ``EMPTY`` means providers were asked and genuinely had nothing.
    """
    if results:
        return SearchStatus.OK
    if attempts and all(a.outcome is AttemptOutcome.EMPTY for a in attempts):
        return SearchStatus.EMPTY
    return SearchStatus.UNAVAILABLE


def _result_limitations(
    results: Sequence[ResultItem], search_type: SearchType
) -> list[str]:
    """Human-readable caveats about the rows themselves (§3's examples)."""
    notes: list[str] = []
    if not results:
        return notes
    if search_type is SearchType.NEWS and any(
        row.provider == Provider.DDG.value for row in results
    ):
        # I8's one sanctioned exception, surfaced rather than hidden: DDG has
        # no news endpoint, so its "news" is a recency-biased web query and
        # its adapter marks every such row INFERRED.
        notes.append("ddg emulates news; dates are inferred")
    inferred = sum(
        1 for row in results if row.date_confidence is DateConfidence.INFERRED
    )
    if inferred:
        notes.append(f"date inferred, not reported, for {inferred} of {len(results)} results")
    undated = sum(1 for row in results if row.published_date is None)
    if undated:
        notes.append(f"date unavailable for {undated} of {len(results)} results")
    return notes


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------


async def search(
    query: str,
    search_type: SearchType | str = SearchType.WEB,
    *,
    providers: Sequence[Provider] | None = None,
    include_attempts: bool = True,
    client: httpx.AsyncClient | None = None,
    max_wait_s: float | None = None,
    **opts,
) -> SearchResult:
    """Search with automatic fallback -- the single-search front door (§5).

    Walks the surviving candidates in a weighted-random order (``_draw_order``:
    by searches left, one-time grants discounted, DDG last) and stops at the
    **first provider that returns at least one row**. An
    operational failure or an empty result set falls through to the next
    provider; a caller error or a bug raises (I3).

    Args:
        query: what to search for. Blank or non-string raises
            ``InvalidRequest``.
        search_type: ``SearchType.WEB`` (default) or ``SearchType.NEWS``, or
            either one's string value. Anything else raises
            ``InvalidRequest`` before any provider is contacted.
        providers: optional subset. Still subject to all four gates, then
            drawn like any other candidate set -- it filters, it does not
            rank.
        include_attempts: when ``False``, ``attempts`` is set to ``None``
            *after* collection. The data is still gathered and still
            reported to the ledger; only the caller's view is trimmed.
        client: an ``httpx.AsyncClient`` to reuse (the test-injection seam
            from ``garudagent/websearch.py:101``). Production callers omit
            it and each adapter opens and closes its own.
        max_wait_s: seconds to wait for a paced provider's next free slot
            before falling through to the next candidate (gate 5). ``None``
            means ``usage.DEFAULT_MAX_WAIT_S``. Today only DDG is paced
            (``usage.MIN_REQUEST_INTERVAL_S``). Keyword-only and never
            forwarded to an adapter (N2) -- it never enters ``**opts``.
        **opts: passed through to the adapter unchanged (e.g.
            ``search_depth`` for Tavily, ``num`` for Serper, ``limit`` for
            Firecrawl/DDG). They also reach ``estimate_cost()``, so the
            affordability gate prices the call the caller actually asked for.

    Returns:
        A ``SearchResult``. ``status`` follows the Q8 table; ``PARTIAL`` is
        never returned.

    Raises:
        InvalidRequest: bad ``search_type``, blank ``query``, unknown
            provider name, or a provider with no registered adapter.
        Exception: anything a provider adapter raises that is NOT a
            ``ProviderOperationalError`` propagates unchanged.
    """
    search_type = _resolve_search_type(search_type)  # first -- I3, zero requests
    query = _resolve_query(query)
    chosen = _resolve_providers(providers)

    selection = _select_candidates(chosen, search_type, opts)
    records: dict[Provider, AttemptRecord] = dict(selection.skipped)
    limitations = list(selection.limitations)

    results: list[ResultItem] = []
    provider_used: str | None = None
    throttled_notes: list[str] = []

    tried: list[Provider] = []
    for candidate in _draw_order(selection.candidates):
        record, response = await _attempt(
            candidate, query, search_type, client, opts, max_wait_s=max_wait_s
        )
        records[candidate.provider] = record
        tried.append(candidate.provider)
        if record.outcome is AttemptOutcome.THROTTLED:
            throttled_notes.append(
                f"{record.provider} skipped: throttled, next slot beyond max_wait_s"
            )
        if response is not None and response.results:
            results = list(response.results)
            provider_used = response.provider
            break  # first provider with >=1 row wins; the rest are not called

    # Chronological: gate-4 skips (decided at selection, declaration order),
    # then the calls in the order the draw made them.
    attempts = _ordered_attempts(dict(selection.skipped), chosen) + [
        records[p] for p in tried
    ]
    limitations.extend(_result_limitations(results, search_type))
    status = _status(results, attempts)
    # Gate 5's suppression rule (§6 Q2): a throttled attempt always stays in
    # `attempts`; its note reaches `limitations` only when the search did
    # NOT ultimately succeed. A caller who got a good answer from a
    # lower-ranked provider is not told about a slot it never needed.
    if status is not SearchStatus.OK:
        limitations.extend(throttled_notes)

    return SearchResult(
        query=query,
        search_type=search_type,
        status=status,
        provider_used=provider_used,
        # include_attempts trims the VIEW, after collection and after every
        # ledger write -- never the collection itself (§3).
        attempts=attempts if include_attempts else None,
        results=results,
        limitations=limitations,
    )


async def multi_search(
    query: str,
    search_type: SearchType | str = SearchType.WEB,
    *,
    providers: Sequence[Provider] | None = None,
    include_attempts: bool = True,
    client: httpx.AsyncClient | None = None,
    max_wait_s: float | None = None,
    **opts,
) -> SearchResult:
    """Concurrent fan-out across every eligible provider, merged (§6).

    Same four gates, same ``SearchResult``, same status table -- the only
    differences are that every survivor is called (concurrently, one request
    each) instead of just the first, and the rows are merged by
    ``dedupe.merge()``: round-robin by ``provider_rank``, with a URL
    collision resolved in favour of the higher-ranked provider.

    This spends every eligible provider's quota on one call, which is exactly
    why the quota gate applies here too. It is never the default; a caller
    opts in.

    ``max_wait_s`` (gate 5, keyword-only, N2) is forwarded to every
    concurrent ``_attempt()`` call identically -- each candidate claims its
    own slot independently. ``None`` means ``usage.DEFAULT_MAX_WAIT_S``.

    ``provider_used`` is a comma-joined list of the providers that actually
    contributed a surviving row, in declaration order (e.g. ``"tavily,ddg"``)
    -- SKETCH.md §3 defines the field only for single-search, where it is
    "the one that answered". ``None`` when nothing was returned.

    Raises:
        Same as ``search()``. ``asyncio.gather(return_exceptions=True)``
        captures every exception including the ones I3 says must escape, so
        the results are re-scanned and any non-operational exception is
        re-raised rather than being folded into a quiet ``UNAVAILABLE``.
    """
    search_type = _resolve_search_type(search_type)  # first -- I3, zero requests
    query = _resolve_query(query)
    chosen = _resolve_providers(providers)

    selection = _select_candidates(chosen, search_type, opts)
    records: dict[Provider, AttemptRecord] = dict(selection.skipped)
    limitations = list(selection.limitations)

    settled = await asyncio.gather(
        *(
            _attempt(
                candidate, query, search_type, client, opts, max_wait_s=max_wait_s
            )
            for candidate in selection.candidates
        ),
        return_exceptions=True,
    )

    # I3, the direction gather() would otherwise silently break: an
    # operational failure was already handled inside _attempt(), so anything
    # that reaches here as an exception is by definition NOT operational --
    # a bug, a caller error, a cancellation. Re-raise it. Every sibling task
    # has already finished and reconciled by now (return_exceptions=True
    # waits for all of them), so nothing is left dangling.
    for item in settled:
        if isinstance(item, BaseException):
            raise item

    responses: list[ProviderResponse] = []
    throttled_notes: list[str] = []
    # zip(strict=True): gather() preserves the order of the coroutines it was
    # given, so settled[i] belongs to candidates[i]. Pairing them back up by
    # position is what lets a record be keyed by its enum slot rather than by
    # whatever the adapter calls itself.
    for candidate, (record, response) in zip(
        selection.candidates, settled, strict=True
    ):
        records[candidate.provider] = record
        if record.outcome is AttemptOutcome.THROTTLED:
            throttled_notes.append(
                f"{record.provider} skipped: throttled, next slot beyond max_wait_s"
            )
        if response is not None:
            responses.append(response)

    results = dedupe.merge(
        responses, order=[candidate.provider for candidate in selection.candidates]
    )

    contributors = [
        candidate.adapter.name
        for candidate in selection.candidates
        if any(row.provider == candidate.adapter.name for row in results)
    ]

    attempts = _ordered_attempts(records, chosen)
    limitations.extend(_result_limitations(results, search_type))
    status = _status(results, attempts)
    # Gate 5's suppression rule (§6 Q2) -- same as search(), see its comment.
    if status is not SearchStatus.OK:
        limitations.extend(throttled_notes)

    return SearchResult(
        query=query,
        search_type=search_type,
        status=status,
        provider_used=",".join(contributors) if contributors else None,
        attempts=attempts if include_attempts else None,
        results=results,
        limitations=limitations,
    )
