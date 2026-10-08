"""Sertchi's normalized schema — every enum and dataclass in the shared
result/usage shape, pure and network-free (Phase 1 of
``fable-prompt-build-v1.md``).

Nothing in this module makes a request, reads a file, or imports another
Sertchi module. Adapters (``providers/*.py``, Phase 2+) build ``ResultItem``s
and a ``ProviderResponse``; only the orchestrator in ``__init__.py``
(Phase 7) assembles a ``SearchResult`` on top of one or more
``ProviderResponse``s (I2 — the split is deliberate: an adapter never learns
its position in a fallback chain).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Literal


class SearchType(str, Enum):
    """The two search shapes Sertchi normalizes in v1 (SKETCH.md §3).
    No IMAGE — deferred (different result shape, no current consumer)."""

    WEB = "web"
    NEWS = "news"


class Provider(str, Enum):
    """The six providers Sertchi wraps.

    Declared in ABUNDANCE-THEN-QUALITY ORDER: Keenable #1, Exa #2, Tavily #3,
    Firecrawl #4, Serper #5, DDG #6/last (user decision 2026-09-13; evidence,
    including the independent Artificial Analysis Search Index, in
    etc/planning/NEW-PROVIDERS.md §5.3). This is NOT a pure quality ranking,
    and that is deliberate: Keenable leads because its 100,000/month free
    pool dwarfs every other renewing pool, so normal traffic costs nothing
    and the scarce pools stay banked for fallback. Do not "correct" it to a
    benchmark order. It replaced SKETCH.md §2's original Tavily-first order
    (a pure prepend of KEENABLE and EXA -- the other four kept their relative
    order).

    This declaration order is the SINGLE SOURCE OF TRUTH for §5's
    single-search fallback chain and §6's multi-search dedupe tiebreak (on a
    URL collision, the row from the earlier-declared provider wins -- see
    NEW-PROVIDERS.md §5.4 for the open question of those two uses having
    diverged). Nothing else in the codebase may hard-code a second ordering;
    consumers iterate `list(Provider)`. `tests/test_types.py` pins it.
    """

    KEENABLE = "keenable"
    EXA = "exa"
    TAVILY = "tavily"
    FIRECRAWL = "firecrawl"
    SERPER = "serper"
    DDG = "ddg"


class DateConfidence(str, Enum):
    """How much a ResultItem's published_date can be trusted."""

    REPORTED = "reported"
    INFERRED = "inferred"
    NONE = "none"


class AttemptOutcome(str, Enum):
    """One provider attempt's result, as recorded in AttemptRecord/attempts."""

    SUCCESS = "success"
    ERROR = "error"
    EMPTY = "empty"
    SKIPPED_QUOTA = "skipped_quota"
    SKIPPED_BACKOFF = "skipped_backoff"
    THROTTLED = "throttled"
    """A slot was not free inside the caller's max_wait_s, so no request was
    made — distinct from SKIPPED_BACKOFF, which means the provider is
    unhealthy or out of quota, while THROTTLED means the provider is
    perfectly fine and it is simply not this caller's turn."""


class SearchStatus(str, Enum):
    """Overall outcome of a search()/multi_search() call.

    NOTE: PARTIAL is declared here but never assigned by the v1 orchestrator
    (fable-prompt-build-v1.md §6 Q8 — a fallback success that took more than
    one attempt still reads OK, not PARTIAL; `attempts` is where a caller
    finds out it took three tries). PARTIAL stays reserved for a future
    per-result degradation signal (e.g. some rows missing dates), not for
    "the chain needed a fallback."
    """

    OK = "ok"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"
    EMPTY = "empty"


class QuotaKind(str, Enum):
    """How a provider's credit pool refills. Defined above UsageState,
    which annotates a field against it."""

    MONTHLY = "monthly"
    TOTAL = "total"
    EXPIRING = "expiring"


@dataclass(slots=True)
class ResultItem:
    """One normalized search result row, regardless of which provider or
    search_type produced it (SKETCH.md §3).

    `provider_rank`/`provider_score` are the load-bearing names (round 4,
    §3/§6) — the bare `rank`/`normalized_rank` stay reserved for a future
    cross-provider merged rank in multi_search()'s dedupe. Do not rename
    these back.
    """

    title: str
    url: str
    snippet: str
    domain: str
    registrable_domain: str
    published_date: str | None
    date_confidence: DateConfidence
    provider: str
    provider_rank: int
    provider_score: float | None = None
    thumbnail_url: str | None = None


@dataclass(frozen=True, slots=True)
class CreditCost:
    """What one provider call cost, in that provider's own credit unit
    (SKETCH.md §7 — an adapter reports this; usage.py never derives it)."""

    credits: int
    unit: str
    basis: str


@dataclass(slots=True)
class AttemptRecord:
    """One entry in a search's attempt log (SKETCH.md §3).

    latency_ms is None for both SKIPPED_* outcomes — no request was made,
    so there is nothing to time.
    """

    provider: str
    outcome: AttemptOutcome
    latency_ms: float | None
    error: str | None


@dataclass(slots=True)
class ProviderResponse:
    """What ONE provider adapter returns (SKETCH.md §3, round 4 split).

    Never a SearchResult (I2) — an adapter describes only its own call: its
    rows, its own identity, its raw metadata, and what the call cost. It has
    no business setting status/provider_used/attempts; those are
    orchestration facts assembled only in __init__.py.
    """

    provider: str
    results: list[ResultItem]
    provider_metadata: dict
    usage: CreditCost


@dataclass(slots=True)
class SearchResult:
    """One call's worth of results, assembled by the orchestrator
    (__init__.py, Phase 7) — never by a provider (SKETCH.md §3)."""

    query: str
    search_type: SearchType
    status: SearchStatus
    provider_used: str | None
    attempts: list[AttemptRecord] | None
    results: list[ResultItem]
    limitations: list[str]


@dataclass(slots=True)
class UsageState:
    """Per-provider credit/health state, persisted at
    ~/.config/sertchi/usage.json (SKETCH.md §7; fields Phase 6 needs are
    included here per fable-prompt-build-v1.md §5 Phase 1, even though
    Phase 6 is the one that populates and mutates them)."""

    provider: str
    quota: int | None
    quota_kind: QuotaKind | None
    quota_period: str | None
    plan: Literal["free", "paid"]
    local_credits_used: int
    local_credits_remaining_estimate: int
    provider_reported_remaining: int | None
    last_reconciled: str | None
    # Phase 6 fields (§7's prose, not yet in SKETCH.md's draft class body):
    consecutive_failures: int
    cooldown_until: str | None
    last_request_at: str | None
    latency_ms_recent: list[float]
    reservations: list[dict]
    expires_at: str | None
    next_slot_at: str | None
    """The earliest moment the next request to this provider may be issued;
    None for a provider with no pacing interval."""

    @property
    def best_available_remaining(self) -> int | None:
        """provider_reported_remaining when the provider's own API exposed
        one, else the local reconciled estimate (SKETCH.md §7 #1)."""
        if self.provider_reported_remaining is not None:
            return self.provider_reported_remaining
        return self.local_credits_remaining_estimate


class SertchiError(Exception):
    """Base of every exception Sertchi itself raises."""


class InvalidRequest(SertchiError, ValueError):
    """A caller/programmer error — e.g. a search_type that isn't a valid
    SearchType value. Per I3 this must escape past search()/multi_search()
    uncaught; it is never treated as an operational failure. Operational
    errors (timeout, 429, 5xx, provider block, malformed response) live in
    providers/base.py — Phase 2's job, not this module's."""


class ProviderAuthError(SertchiError):
    """A provider rejected the configured API key — a CONFIGURATION error,
    not an operational one (user decision, 2026-08-20).

    This module holds the exceptions that must ESCAPE past search(); the
    hierarchy in providers/base.py holds the ones that FALL THROUGH to the
    next provider. That split is the whole of I3, and this class sits
    deliberately on the escaping side: falling through on a bad key would
    hand the caller a quietly degraded answer from a lower-ranked provider
    while the real problem — a key that needs fixing — stayed invisible.
    Note it is NOT a ProviderOperationalError subclass; that is what makes
    the orchestrator's `except ProviderOperationalError` miss it.

    A provider that is merely *out of credit* or region-blocked is a
    different thing and stays operational — see each adapter's own
    _AUTH_STATUSES for which status codes it reads as "your key is wrong".

    I6: the message names the provider and the status code only. It must
    never carry the key, a request body, or a response body."""

    def __init__(self, provider: str, status_code: int) -> None:
        self.provider = provider
        self.status_code = status_code
        super().__init__(
            f"{provider} rejected the configured API key (HTTP {status_code}); "
            f"check it with: python3 -m sertchi keys set {provider}"
        )
