"""Sertchi's credit ledger and provider health gate (fable-prompt-build-v1.md
§5 Phase 6).

This module answers three questions and persists the answers:

1. **Can we afford this call?** -- ``can_afford()``, a fixed-reserve
   affordability gate over a reconciled credit estimate.
2. **Is this provider healthy enough to try?** -- ``is_cooling_down()``, the
   consecutive-failure circuit breaker (breaker-only as of the slot-pacing
   feature; DDG's minimum inter-request interval used to be a second
   mechanism here but is now paced, not rejected -- see ``reserve_slot()``).
3. **What did the call actually cost?** -- ``reserve()`` /``reconcile()``, a
   reserve-then-reconcile cycle that survives a crashed caller.

ONE MACHINE, ONE LEDGER
-----------------------
``USAGE_PATH`` is ``~/.config/sertchi/usage.json`` -- resolved the same
centralized way as ``keys.py``'s ``KEYS_PATH``, never package-relative. This
is the load-bearing consequence of centralizing keys (2026-08-20): every copy
of Sertchi on this machine -- the centralized install at
``~/Documents/CODE/sertchi`` and any future vendored copy -- now reads the
SAME key for a given provider, which means they are all drawing against ONE
real account balance. They must therefore share ONE ledger for that provider.
Two copies keeping independent balances would each believe the full quota was
available and could jointly overspend a real account, which is priority #2
("never spend the user's money or quota unintentionally") failing silently.

``CONFIG_DIR``, ``USAGE_PATH`` and ``LOCK_PATH`` are module globals read at
call time (``keys.py``'s pattern). A test that sandboxes this module MUST
repoint all three together -- repointing only ``CONFIG_DIR`` would leave
writes landing in the real ``~/.config/sertchi/``.

WHAT THIS MODULE DOES NOT KNOW (I4)
-----------------------------------
It contains **no billing rules whatsoever.** It never derives what a call
costs; it only records "provider X reports this call cost N credits" from the
``CreditCost`` an adapter hands it. Every provider's cost rule lives on that
provider's own adapter (``providers/*.py``, whose ``estimate_cost()`` is the
adapter-side half of the affordability gate). That separation is what lets a
future provider drop in without teaching this file its billing model -- and
it is why you will not find a per-provider arithmetic rule anywhere below.

NEVER HOLD THE LOCK ACROSS A NETWORK CALL (I5)
----------------------------------------------
The cycle is **reserve -> unlock -> request -> lock -> reconcile**, with the
reconcile in a ``finally``. ``reserve()`` and ``reconcile()`` each take and
release the lock on their own; neither one spans the request. If a caller
ever wrapped an HTTP call inside a single held lock, one slow provider would
stall every other project sharing this installation. ``metered_call()`` exists
so callers get that ordering by construction rather than by remembering it.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from .types import (
    AttemptOutcome,
    CreditCost,
    InvalidRequest,
    Provider,
    QuotaKind,
    UsageState,
)

# --------------------------------------------------------------------------
# Paths -- centralized, mirroring keys.py. Repoint all three together.
# --------------------------------------------------------------------------

CONFIG_DIR = Path.home() / ".config" / "sertchi"
USAGE_PATH = CONFIG_DIR / "usage.json"
LOCK_PATH = CONFIG_DIR / "usage.lock"

SCHEMA_VERSION = 1

# --------------------------------------------------------------------------
# Policy constants (fable-prompt-build-v1.md §6 Q3-Q6, confirmed 2026-08-20)
# --------------------------------------------------------------------------

#: A reservation older than this is presumed orphaned by a crashed caller and
#: is swept on the next lock acquisition, releasing its hold on the balance.
RESERVATION_TTL_S = 120

#: Circuit breaker: first failure waits this long, each further consecutive
#: failure doubles it, capped at BACKOFF_CAP_S, reset to zero on any success.
BACKOFF_BASE_S = 30.0
BACKOFF_CAP_S = 900.0

#: Guards the doubling against absurd exponents after a long failure streak
#: (a float would overflow well before this matters, and the cap dominates).
_BACKOFF_MAX_SHIFT = 32

#: Minimum interval BETWEEN REQUESTS, per provider. This is a rate floor, NOT
#: a failure cooldown: it applies to a perfectly healthy provider with zero
#: consecutive failures. DDG's ~1 req/sec ecosystem-wide throttle (SKETCH.md
#: §2) is rate-driven rather than outage-driven, so spacing requests is the
#: thing that keeps it working -- backing off only after it blocks is too
#: late. No other provider has a floor; a keyed API sells you the throughput.
#: Requests are spaced onto reserved slots (``reserve_slot()``) rather than
#: rejected inside the window -- see the "two mechanisms" note above
#: ``is_cooling_down()``.
MIN_REQUEST_INTERVAL_S: dict[str, float] = {Provider.DDG.value: 1.1}

#: How long a caller will wait for a paced provider's next free slot before
#: giving up on it. D1 / user decision 2026-08-20: 3.0s, tighter than the
#: draft's 5.0s -- DDG is last in quality order, so this delay stacks onto
#: search()'s fallback chain before it can move on; the correct response to
#: an oversized burst is to shed load (fall through to the next provider),
#: not to buffer requests for minutes. A caller may override per call.
DEFAULT_MAX_WAIT_S = 3.0

#: How many recent latency samples to retain per provider. Recorded for a
#: future adaptive-ordering pass; nothing in v1 acts on it (SKETCH.md §7).
LATENCY_SAMPLES = 20


@dataclass(frozen=True, slots=True)
class ProviderDefaults:
    """Seed values for a provider's ledger record on first write.

    ``reserve`` is a FIXED credit count, deliberately not a percentage of the
    quota (SKETCH.md §7, round 4 #2): a percentage scales badly across pools
    of different sizes -- 95% of Serper's 2500 would hold back 125 credits
    while 95% of Tavily's 1000 holds back only 50, for no principled reason.
    A fixed reserve keeps the same predictable safety margin regardless of
    pool size.
    """

    quota: int | None
    quota_kind: QuotaKind | None
    plan: Literal["free", "paid"]
    reserve: int
    allow_overage: bool = False


#: Confirmed with the user 2026-08-20 (§6 Q3). Serper's grant is TOTAL, not
#: MONTHLY, and that distinction is the whole point: its one-time 2500-credit
#: signup allowance NEVER refills, so rolling it over monthly would silently
#: hand back credits that do not exist. DDG is keyless and uncapped -- it
#: carries no quota and is never quota-gated, only rate/backoff-gated.
DEFAULTS: dict[str, ProviderDefaults] = {
    Provider.KEENABLE.value: ProviderDefaults(
        quota=100_000, quota_kind=QuotaKind.MONTHLY, plan="free", reserve=10
    ),
    Provider.EXA.value: ProviderDefaults(
        quota=10_000_000, quota_kind=QuotaKind.MONTHLY, plan="free", reserve=50_000
    ),
    # Brave: the $5/month free credit, in micro-dollars like Exa (the adapter
    # bills 5,000 per search, so ~1,000 searches). Reserve = five searches.
    Provider.BRAVE.value: ProviderDefaults(
        quota=5_000_000, quota_kind=QuotaKind.MONTHLY, plan="free", reserve=25_000
    ),
    Provider.TAVILY.value: ProviderDefaults(
        quota=1000, quota_kind=QuotaKind.MONTHLY, plan="free", reserve=10
    ),
    Provider.FIRECRAWL.value: ProviderDefaults(
        quota=1000, quota_kind=QuotaKind.MONTHLY, plan="free", reserve=10
    ),
    Provider.SERPER.value: ProviderDefaults(
        quota=2500, quota_kind=QuotaKind.TOTAL, plan="free", reserve=10
    ),
    Provider.DDG.value: ProviderDefaults(
        quota=None, quota_kind=None, plan="free", reserve=0
    ),
}


# --------------------------------------------------------------------------
# Time helpers
# --------------------------------------------------------------------------


def _now() -> datetime:
    """LOCAL machine time, naive (§6 Q5 -- confirmed local, not UTC, because
    the user reads these numbers on this box and a month boundary should turn
    over when their calendar says it does)."""
    return datetime.now()


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def _iso_precise(moment: datetime) -> str:
    """Full-precision ISO, for slot timestamps.

    _iso() truncates to whole seconds, which is fine for a `last_reconciled`
    audit stamp but destroys a 1.1s pacing interval: two claims would round
    onto the same second and both fire at once. Slot arithmetic is the one
    place in this module that needs sub-second resolution.
    """
    return moment.isoformat()


def _parse(value: object) -> datetime | None:
    """Parse a stored ISO timestamp back to a naive local datetime. Returns
    None for anything unparseable, so a hand-edited or corrupt field degrades
    to "no timestamp recorded" rather than raising mid-ledger."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        # Everything this module writes is naive local time; normalize an
        # aware value (e.g. a hand-set expires_at) so comparisons never hit
        # the naive/aware TypeError.
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def current_period() -> str:
    """The rollover bucket for a MONTHLY quota, e.g. ``"2026-08"``."""
    return _now().strftime("%Y-%m")


def _provider_name(provider: str | Provider) -> str:
    """Accept either a ``Provider`` member or its plain string value.

    ``Provider`` is a ``str`` Enum, but ``str(Provider.TAVILY)`` renders as
    ``"Provider.TAVILY"`` on modern Python -- so the ``.value`` unwrap is
    required, not cosmetic.
    """
    if isinstance(provider, Provider):
        return provider.value
    name = str(provider)
    if name not in DEFAULTS:
        raise InvalidRequest(
            f"unknown provider {name!r}; known providers: {sorted(DEFAULTS)}"
        )
    return name


# --------------------------------------------------------------------------
# On-disk plumbing: ensure dir, atomic write, exclusive lock
# --------------------------------------------------------------------------


def ensure_config_dir() -> None:
    """Create ``CONFIG_DIR`` mode 0700 if absent. Idempotent.

    Reuse sweep (Iron rule 4): ``keys.ensure_config_dir()`` already does this
    for the same directory, but it ALSO scaffolds ``keys.json``. Calling it
    from here would couple the two modules' test sandboxes -- a test that
    repointed only ``usage.CONFIG_DIR`` would have this module create a real
    ``~/.config/sertchi/keys.json`` behind its back. The two lines are
    duplicated deliberately to keep the sandboxes independent.
    """
    CONFIG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    # mkdir's `mode` is subject to umask and is a no-op on an existing
    # directory -- chmod explicitly so the result is exactly 0700 either way.
    os.chmod(CONFIG_DIR, 0o700)


def _atomic_write(path: Path, payload: dict) -> None:
    """Write ``payload`` to ``path`` atomically: temp file inside the SAME
    directory (so ``os.replace`` stays a same-filesystem atomic rename),
    fsynced, ``chmod``'d 0600 BEFORE the replace -- ``keys.py``'s ordering,
    for the same reason: a mode applied after the file is already live at its
    final name leaves a window where a concurrent reader sees the wider
    umask-derived mode.

    The fsync is not ceremony here. This file is a money ledger shared across
    processes; a torn write after a power loss would resurrect spent credits.
    """
    tmp = tempfile.NamedTemporaryFile(
        mode="w",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    )
    try:
        with tmp:
            json.dump(payload, tmp, indent=2, sort_keys=True)
            tmp.write("\n")
            tmp.flush()
            os.fsync(tmp.fileno())
        os.chmod(tmp.name, 0o600)  # before replace -- see docstring.
        os.replace(tmp.name, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp.name)
        raise


def _read_ledger() -> dict:
    """Read the whole ledger, or an empty one if it is missing/unreadable.

    A corrupt ledger resets to "nothing spent", which is the optimistic
    direction and therefore the uncomfortable one. There is no better option
    locally -- the bytes are gone -- and it is exactly what
    ``provider_reported_remaining`` exists to correct: the first reconcile
    that carries a provider-reported figure overwrites the guess with the
    account's real balance.
    """
    try:
        with USAGE_PATH.open("r") as handle:
            data = json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
        data = None
    if not isinstance(data, dict) or not isinstance(data.get("providers"), dict):
        return {"version": SCHEMA_VERSION, "providers": {}}
    data.setdefault("version", SCHEMA_VERSION)
    return data


@contextlib.contextmanager
def _locked() -> Iterator[dict]:
    """Exclusive read-decide-mutate-write over the shared ledger.

    ``fcntl.flock`` on the sidecar ``usage.lock``. The lock is on the open
    file description, so two *processes* -- two different projects each
    calling Sertchi on this machine -- genuinely exclude each other; this is
    not merely thread coordination inside one interpreter. That cross-process
    case is the real one now that the ledger is centralized.

    The whole read-modify-write happens inside one acquisition. Splitting it
    (read under the lock, write under a later one) is the classic lost-update
    bug: two processes each read "5 spent", each add 1, and the ledger ends
    at 6 having actually spent 7.

    NEVER wrap a network call in this. See the module docstring's I5 note.

    The lock file itself is created once and never unlinked -- deleting it
    would let a process holding a lock on the old inode coexist with one
    locking the new inode, silently dissolving the exclusion.
    """
    ensure_config_dir()
    fd = os.open(LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            ledger = _read_ledger()
            _housekeep(ledger)
            yield ledger
            _atomic_write(USAGE_PATH, ledger)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


# --------------------------------------------------------------------------
# Record shape, seeding, and per-acquisition housekeeping
# --------------------------------------------------------------------------


def _seed(name: str) -> dict:
    defaults = DEFAULTS[name]
    return {
        "provider": name,
        "quota": defaults.quota,
        "quota_kind": defaults.quota_kind.value if defaults.quota_kind else None,
        # Only a MONTHLY quota has a period to roll; TOTAL and "no quota"
        # carry None so nothing downstream is tempted to roll them.
        "quota_period": (
            current_period() if defaults.quota_kind is QuotaKind.MONTHLY else None
        ),
        "plan": defaults.plan,
        "local_credits_used": 0,
        "local_credits_remaining_estimate": defaults.quota or 0,
        "provider_reported_remaining": None,
        "last_reconciled": None,
        "consecutive_failures": 0,
        "cooldown_until": None,
        "last_request_at": None,
        "next_slot_at": None,
        "latency_ms_recent": [],
        "reservations": [],
        "expires_at": None,
        # Policy, not part of UsageState's schema -- kept alongside it so a
        # per-provider override survives without editing types.py.
        "reserve": defaults.reserve,
        "allow_overage": defaults.allow_overage,
    }


def _record(ledger: dict, name: str) -> dict:
    """Fetch (seeding if absent) one provider's record, housekept."""
    providers = ledger["providers"]
    record = providers.get(name)
    if not isinstance(record, dict):
        record = _seed(name)
        providers[name] = record
    else:
        # Backfill any field a ledger written by an older build lacks.
        for key, value in _seed(name).items():
            record.setdefault(key, value)
    _housekeep_record(record)
    return record


def _housekeep(ledger: dict) -> None:
    for record in ledger.get("providers", {}).values():
        if isinstance(record, dict):
            _housekeep_record(record)


def _housekeep_record(record: dict) -> None:
    """Run on EVERY lock acquisition, before any caller decision: sweep stale
    reservations, roll a monthly period, zero a lapsed expiring balance, then
    recompute the estimate."""
    _sweep_reservations(record)
    _roll_period(record)
    _apply_expiry(record)
    _recompute_estimate(record)


def _sweep_reservations(record: dict) -> None:
    """Drop reservations older than ``RESERVATION_TTL_S``.

    A reservation is a hold placed just before a request and released by the
    matching reconcile. If the caller's process dies mid-call the reconcile
    never runs, and without a sweep that hold would depress the balance
    forever -- every future call seeing less headroom than really exists,
    until the provider is eventually gated off entirely for no reason.

    The sweep releases the hold rather than settling it as spend. That is the
    documented behaviour (§5 Phase 6) and it does mean a crashed caller's
    real spend goes uncounted for a while -- the correction path for that is
    ``provider_reported_remaining``, which overwrites the local guess with the
    account's own figure on the next reconcile that carries one.
    """
    reservations = record.get("reservations")
    if not isinstance(reservations, list) or not reservations:
        return
    now = _now()
    cutoff = timedelta(seconds=RESERVATION_TTL_S)
    kept = []
    for held in reservations:
        if not isinstance(held, dict):
            continue
        created = _parse(held.get("created_at"))
        if created is not None and (now - created) > cutoff:
            continue
        kept.append(held)
    record["reservations"] = kept


def _roll_period(record: dict) -> None:
    """Roll a MONTHLY quota on a calendar-month change. Nothing else rolls.

    TOTAL never rolls: Serper's 2500-credit grant is one-time, and treating
    it as monthly would 'refill' a balance that does not refill -- the exact
    silent-overspend failure priority #2 is about.
    """
    if record.get("quota_kind") != QuotaKind.MONTHLY.value:
        return
    period = current_period()
    if record.get("quota_period") == period:
        return
    record["quota_period"] = period
    record["local_credits_used"] = 0
    # A remaining figure the provider reported last period says nothing about
    # this one; keeping it would gate against a balance that no longer exists.
    record["provider_reported_remaining"] = None
    quota = record.get("quota")
    record["local_credits_remaining_estimate"] = quota if quota is not None else 0
    # Live reservations survive the boundary -- those calls are still in
    # flight -- and _recompute_estimate nets them back out afterwards.


def _apply_expiry(record: dict) -> None:
    """Zero an EXPIRING balance once ``expires_at`` has passed (Serper's paid
    credit packs lapse; SKETCH.md §7)."""
    if record.get("quota_kind") != QuotaKind.EXPIRING.value:
        return
    expires_at = _parse(record.get("expires_at"))
    if expires_at is None or _now() < expires_at:
        return
    quota = record.get("quota")
    record["local_credits_used"] = quota if quota is not None else 0
    # Must zero the reported figure too: best_available_remaining prefers it,
    # so a stale positive reading would keep authorizing calls against a pool
    # that has already lapsed.
    record["provider_reported_remaining"] = 0


def _held_credits(record: dict) -> int:
    return sum(
        int(held.get("credits", 0))
        for held in record.get("reservations", [])
        if isinstance(held, dict)
    )


def _recompute_estimate(record: dict) -> None:
    """``quota - settled spend - live reservations`` (SKETCH.md §7's
    "net of live reservations"). Allowed to go negative: an honest negative
    reads as "already over", which denies, whereas clamping at zero would
    hide the overshoot."""
    quota = record.get("quota")
    if quota is None:
        record["local_credits_remaining_estimate"] = 0
        return
    used = int(record.get("local_credits_used", 0))
    record["local_credits_remaining_estimate"] = int(quota) - used - _held_credits(
        record
    )


def _state(record: dict) -> UsageState:
    kind = record.get("quota_kind")
    return UsageState(
        provider=record["provider"],
        quota=record.get("quota"),
        quota_kind=QuotaKind(kind) if kind else None,
        quota_period=record.get("quota_period"),
        plan=record.get("plan", "free"),
        local_credits_used=int(record.get("local_credits_used", 0)),
        local_credits_remaining_estimate=int(
            record.get("local_credits_remaining_estimate", 0)
        ),
        provider_reported_remaining=record.get("provider_reported_remaining"),
        last_reconciled=record.get("last_reconciled"),
        consecutive_failures=int(record.get("consecutive_failures", 0)),
        cooldown_until=record.get("cooldown_until"),
        last_request_at=record.get("last_request_at"),
        latency_ms_recent=list(record.get("latency_ms_recent", [])),
        reservations=list(record.get("reservations", [])),
        expires_at=record.get("expires_at"),
        next_slot_at=record.get("next_slot_at"),
    )


# --------------------------------------------------------------------------
# The affordability gate
# --------------------------------------------------------------------------


def _effective_remaining(record: dict) -> int:
    """The credit headroom to gate against, over-counting where uncertain.

    Starts from ``UsageState.best_available_remaining`` -- the provider's own
    reported figure when it gave one, else the local estimate. When the
    reported figure wins, live reservations are subtracted from it as well:
    that figure is a snapshot from the last reconcile and cannot know about
    calls this machine has in flight right now. (The local estimate already
    nets reservations out, so subtracting there too would double-count.)
    """
    state = _state(record)
    base = state.best_available_remaining or 0
    if state.provider_reported_remaining is not None:
        base -= _held_credits(record)
    return int(base)


def _decide(record: dict, name: str, cost: CreditCost) -> tuple[bool, str]:
    if record.get("quota") is None:
        return True, f"{name}: no declared quota; not quota-gated"

    remaining = _effective_remaining(record)
    floor = int(record.get("reserve", 0))
    need = int(cost.credits)

    if remaining - need >= floor:
        return (
            True,
            f"{name}: {remaining} credits available, this call costs {need}, "
            f"reserve {floor}",
        )

    detail = (
        f"{remaining} credits left, this call costs {need}, reserve {floor}"
    )
    if record.get("plan") == "paid":
        if bool(record.get("allow_overage", False)):
            # The one place Sertchi will knowingly spend into the margin, and
            # only because a per-provider flag was explicitly turned on.
            return True, f"{name}: paid plan, allow_overage=True -- {detail}"
        return (
            False,
            f"{name}: paid plan but allow_overage=False -- {detail}",
        )
    return False, f"{name}: free plan, hard stop at reserve -- {detail}"


def can_afford(
    provider: str | Provider, estimated_cost: CreditCost
) -> tuple[bool, str]:
    """Whether ``provider`` may be called, given what the call is estimated
    to cost. Returns ``(allowed, human-readable reason)``.

    The rule is ``best_available_remaining - cost.credits >= reserve``: a
    fixed credit reserve, never a percentage of the quota, and evaluated
    against the ACTUAL next call rather than a blanket fraction (remaining 17,
    next call 2, reserve 5 -> 15 >= 5 -> allow).

    On a **free** plan, failing that check is a hard stop -- the caller drops
    the provider from its candidate list rather than risk a quota error. On a
    **paid** plan the reserve is where a billed overage *may* be permitted,
    and only when that provider's ``allow_overage`` has been turned on; it is
    ``False`` by default, because priority #2 says never spend by default.

    DDG carries no quota and is never gated here -- only by
    ``is_cooling_down()``.

    Note the check-then-reserve window: this releases the lock before
    ``reserve()`` takes it, so two processes can both pass a marginal check
    and both reserve. The fixed reserve is precisely the buffer that absorbs
    that overshoot -- which is why it is a real credit margin and not zero.
    """
    name = _provider_name(provider)
    with _locked() as ledger:
        return _decide(_record(ledger, name), name, estimated_cost)


# --------------------------------------------------------------------------
# Health gate: circuit breaker only. Pacing moved to reserve_slot() below.
# --------------------------------------------------------------------------


def _breaker_block(record: dict, name: str, now: datetime) -> tuple[bool, str]:
    """The consecutive-failure circuit breaker.

    Triggered only by failures. A provider having a bad minute stops being
    retried on every single call during that minute, and recovers by itself
    once healthy -- no manual reset.

    This used to be one of two mechanisms consulted by ``is_cooling_down()``;
    the other (the minimum inter-request rate floor) has been replaced by
    ``reserve_slot()``, which paces requests onto reserved slots instead of
    rejecting them outright -- see that function's docstring.
    """
    until = _parse(record.get("cooldown_until"))
    if until is None or now >= until:
        return False, ""
    failures = int(record.get("consecutive_failures", 0))
    left = (until - now).total_seconds()
    return (
        True,
        f"{name}: circuit breaker open after {failures} consecutive "
        f"failure(s); {left:.1f}s remaining",
    )


def is_cooling_down(provider: str | Provider) -> tuple[bool, str]:
    """Whether ``provider`` must be skipped without a request being made.

    Returns ``(blocked, reason)``. Breaker-only now -- pacing (the former
    "mechanism 2", the minimum inter-request rate floor) moved to
    ``reserve_slot()``, which reserves a future slot instead of rejecting the
    call outright.
    """
    name = _provider_name(provider)
    now = _now()
    with _locked() as ledger:
        record = _record(ledger, name)
        return _breaker_block(record, name, now)


# --------------------------------------------------------------------------
# Slot reservation: pace requests onto a shared per-provider schedule
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SlotGrant:
    """The result of one ``reserve_slot()`` call."""

    granted: bool
    wait_s: float  # seconds to sleep before calling (0.0 when free)
    slot_at: str | None  # the claimed slot, ISO, None when unpaced
    reason: str  # human-readable; "" when granted


def reserve_slot(
    provider: str | Provider, *, max_wait_s: float | None = None
) -> SlotGrant:
    """Claim the next free slot on ``provider``'s pacing schedule, or refuse.

    Replaces the old reject-if-too-soon rate floor with reserve-and-pace: a
    caller arriving inside the interval is handed a future slot to sleep
    until, rather than being turned away outright, up to ``max_wait_s`` (the
    caller's patience, default ``DEFAULT_MAX_WAIT_S``) -- beyond that the
    claim is refused so the caller can fall through to another provider
    instead of buffering (priority #2: load-shed, never buffer).

    The claim itself is atomic under the ledger lock: reading
    ``next_slot_at``, computing the new slot, and writing it back all happen
    inside one ``_locked()`` acquisition, so two concurrent callers can never
    be handed the same slot -- arrival order at the lock is slot order, and
    fairness is structural rather than policed. A backwards system-clock
    jump can only ever push a stored ``next_slot_at`` further into the
    (apparent) future; it can never pull it earlier, so claims simply refuse
    until the schedule drains -- the safe direction, since it can only make
    Sertchi more conservative, never less.

    I5: this function takes and releases the lock and returns. It never
    sleeps, never awaits, and never calls out -- the caller (the
    orchestrator) is the one that sleeps ``wait_s`` seconds, with no lock
    held while it does.
    """
    name = _provider_name(provider)
    interval = MIN_REQUEST_INTERVAL_S.get(name)
    if interval is None:
        # Five of six providers are unpaced (D2); multi_search() calls this
        # once per attempt, so an unpaced provider must cost exactly what it
        # costs today -- no lock, no ledger write, no latency added.
        return SlotGrant(True, 0.0, None, "")

    ceiling = DEFAULT_MAX_WAIT_S if max_wait_s is None else float(max_wait_s)
    if ceiling < 0:
        raise InvalidRequest(f"max_wait_s must be >= 0, got {ceiling!r}")

    with _locked() as ledger:
        record = _record(ledger, name)
        now = _now()
        pending = _parse(record.get("next_slot_at"))
        slot = pending if pending is not None and pending > now else now
        wait = (slot - now).total_seconds()

        if wait > ceiling:
            # N1: refusal never mutates next_slot_at -- this is what bounds
            # the backlog to now + max_wait_s + interval instead of letting
            # a burst buffer for hours.
            return SlotGrant(
                False,
                wait,
                _iso_precise(slot),
                f"{name}: next slot in {wait:.1f}s, over max_wait_s={ceiling:.1f}s",
            )

        record["next_slot_at"] = _iso_precise(slot + timedelta(seconds=interval))
        return SlotGrant(True, max(0.0, wait), _iso_precise(slot), "")


# --------------------------------------------------------------------------
# Reserve / reconcile
# --------------------------------------------------------------------------


def reserve(provider: str | Provider, estimated_cost: CreditCost) -> str:
    """Place a hold for a call about to be made; returns a reservation id.

    The hold shrinks ``local_credits_remaining_estimate`` immediately, so a
    concurrent ``can_afford()`` in another process sees the in-flight call
    rather than the pre-call balance. It also stamps ``last_request_at``,
    which is what the rate floor spaces from -- reserve happens immediately
    before the request, so it marks the request's start.

    The lock is taken and released here. It is NOT held for the caller's
    request -- see ``metered_call()``, which enforces that ordering.

    Every ``reserve()`` MUST be matched by a ``reconcile()`` in a ``finally``.
    An unmatched hold is released by the TTL sweep after
    ``RESERVATION_TTL_S``, but until then it depresses the balance.
    """
    name = _provider_name(provider)
    reservation_id = uuid.uuid4().hex
    stamp = _iso(_now())
    with _locked() as ledger:
        record = _record(ledger, name)
        record["reservations"].append(
            {
                "id": reservation_id,
                "credits": int(estimated_cost.credits),
                "unit": estimated_cost.unit,
                "basis": estimated_cost.basis,
                "created_at": stamp,
            }
        )
        record["last_request_at"] = stamp
        _recompute_estimate(record)
    return reservation_id


def _apply_outcome(
    record: dict, outcome: AttemptOutcome, *, counts_against_health: bool = True
) -> None:
    """Update the circuit breaker from one attempt's outcome.

    Only ``ERROR`` counts as a failure. ``EMPTY`` is a healthy provider that
    answered with zero rows -- it proves reachability just as ``SUCCESS``
    does, so it resets the streak. The two ``SKIPPED_*`` outcomes leave the
    breaker untouched; no request was made, so there is no evidence either
    way (and they should not reach reconcile at all, since a skip never
    reserves).

    ``counts_against_health=False`` settles the attempt without moving the
    breaker in either direction. The attempt really did fail, so the honest
    ``outcome`` is still ``ERROR`` -- but some failures say nothing about
    whether the PROVIDER is healthy. A rejected API key
    (``types.ProviderAuthError``) is the case this exists for: it is the
    user's config that is wrong, and advancing the breaker would leave the
    provider in a cooldown of up to BACKOFF_CAP_S seconds *after* they fix
    the key, punishing them for the repair.
    """
    if not counts_against_health:
        return
    if outcome is AttemptOutcome.ERROR:
        failures = int(record.get("consecutive_failures", 0)) + 1
        record["consecutive_failures"] = failures
        shift = min(failures - 1, _BACKOFF_MAX_SHIFT)
        cooldown = min(BACKOFF_BASE_S * (2**shift), BACKOFF_CAP_S)
        record["cooldown_until"] = _iso(_now() + timedelta(seconds=cooldown))
    elif outcome in (AttemptOutcome.SUCCESS, AttemptOutcome.EMPTY):
        record["consecutive_failures"] = 0
        record["cooldown_until"] = None


def reconcile(
    provider: str | Provider,
    reservation_id: str,
    *,
    actual: CreditCost | None,
    outcome: AttemptOutcome,
    latency_ms: float | None,
    reported_remaining: int | None = None,
    counts_against_health: bool = True,
) -> None:
    """Settle a reservation against what the call really cost.

    ``actual`` is the ``CreditCost`` the ADAPTER reports (I4 -- this module
    never derives it). When ``actual`` is ``None`` -- a timeout, a connection
    failure, anything that left the real cost unknown -- the reservation's own
    estimate is charged instead. That deliberately over-counts: an uncertain
    call is assumed to have cost what we expected. Skipping a provider we
    could have called is a degraded answer; blowing a cap is a real bill.

    ``reported_remaining``, when the provider's response carried its own
    balance, overwrites the local guess and becomes authoritative from here
    on (``UsageState.best_available_remaining`` prefers it).

    ``counts_against_health=False`` releases the hold and records the spend
    as usual but leaves the circuit breaker untouched -- see
    ``_apply_outcome()``. Pair it with ``actual=CreditCost(credits=0, ...)``
    for a call that provably cost nothing, such as a rejected API key.

    Idempotency: a reservation id already settled (or already swept for age)
    matches nothing, and with ``actual=None`` charges nothing -- a duplicate
    reconcile cannot double-charge a hold that is no longer there.
    """
    name = _provider_name(provider)
    with _locked() as ledger:
        record = _record(ledger, name)

        held = None
        for index, candidate in enumerate(record["reservations"]):
            if isinstance(candidate, dict) and candidate.get("id") == reservation_id:
                held = record["reservations"].pop(index)
                break

        if actual is not None:
            charge = int(actual.credits)
        elif held is not None:
            charge = int(held.get("credits", 0))  # over-count, see docstring
        else:
            charge = 0
        record["local_credits_used"] = int(record.get("local_credits_used", 0)) + charge

        if reported_remaining is not None:
            record["provider_reported_remaining"] = int(reported_remaining)
        record["last_reconciled"] = _iso(_now())

        _apply_outcome(record, outcome, counts_against_health=counts_against_health)

        if latency_ms is not None:
            samples = list(record.get("latency_ms_recent", []))
            samples.append(float(latency_ms))
            record["latency_ms_recent"] = samples[-LATENCY_SAMPLES:]

        _recompute_estimate(record)


@dataclass(slots=True)
class Settlement:
    """The handle ``metered_call()`` yields, for the caller to fill in.

    ``outcome`` defaults to ``ERROR`` on purpose. If the body raises, or
    returns without saying how it went, the safe reading is "that call may
    have cost us something" -- which charges the reservation's estimate
    rather than silently forgiving it.
    """

    outcome: AttemptOutcome = AttemptOutcome.ERROR
    actual: CreditCost | None = None
    reported_remaining: int | None = None
    latency_ms: float | None = None
    #: Set False for a failure that says nothing about provider health -- a
    #: rejected API key. Releases the hold and records the (zero) spend
    #: without advancing the circuit breaker, so fixing the key makes the
    #: provider usable again immediately. See _apply_outcome().
    counts_against_health: bool = True


@contextlib.contextmanager
def metered_call(
    provider: str | Provider, estimated_cost: CreditCost
) -> Iterator[Settlement]:
    """Reserve, run the caller's request, reconcile in a ``finally``.

    This is I5 made structural: the reserve and the reconcile each take and
    release the lock on their own, and the caller's request happens in
    between with NO lock held. A caller who wraps their HTTP call in a single
    held lock would stall every other project sharing this installation; the
    point of this helper is that doing it correctly requires no discipline.

    Usage -- note the ``await`` sits inside a plain ``with``; this context
    manager never awaits, so it composes with async callers unchanged::

        cost = adapter.estimate_cost(search_type, **opts)
        allowed, why = usage.can_afford(name, cost)
        ...
        with usage.metered_call(name, cost) as call:
            response = await adapter.search(query, search_type)
            call.outcome = AttemptOutcome.SUCCESS
            call.actual = response.usage
            call.reported_remaining = response.provider_metadata.get(
                "reported_remaining"
            )

    If the body raises, the ``finally`` still reconciles with the default
    ``ERROR`` outcome -- so the hold is released, the estimate is charged,
    and the circuit breaker advances.
    """
    settlement = Settlement()
    reservation_id = reserve(provider, estimated_cost)
    started = time.monotonic()
    try:
        yield settlement
    finally:
        latency = settlement.latency_ms
        if latency is None:
            latency = (time.monotonic() - started) * 1000.0
        reconcile(
            provider,
            reservation_id,
            actual=settlement.actual,
            outcome=settlement.outcome,
            latency_ms=latency,
            reported_remaining=settlement.reported_remaining,
            counts_against_health=settlement.counts_against_health,
        )


# --------------------------------------------------------------------------
# Inspection / configuration
# --------------------------------------------------------------------------


def get_state(provider: str | Provider) -> UsageState:
    """One provider's current ledger state, housekept (stale reservations
    swept, period rolled, lapsed balance zeroed) before being returned."""
    name = _provider_name(provider)
    with _locked() as ledger:
        return _state(_record(ledger, name))


def searches_left(provider: str | Provider, cost: CreditCost) -> float | None:
    """How many more calls priced at ``cost`` fit above the reserve floor.

    ``None`` means the provider declares no quota (DDG) -- it has no balance
    to weigh, which is not the same as a balance of zero. Same headroom
    ``_decide()`` gates on (``_effective_remaining`` minus ``reserve``), but
    divided by the call's own cost, so pools kept in different units
    (Exa's ``usd_micro`` vs everyone else's credits) become comparable.
    Pure read; the cost figure is the adapter's (I4).
    """
    name = _provider_name(provider)
    with _locked() as ledger:
        record = _record(ledger, name)
        if record.get("quota") is None:
            return None
        headroom = _effective_remaining(record) - int(record.get("reserve", 0))
    return max(headroom, 0) / max(int(cost.credits), 1)


def snapshot() -> dict[str, UsageState]:
    """Every known provider's state, housekept. Seeds any provider missing
    from the ledger so the view is always complete."""
    with _locked() as ledger:
        return {name: _state(_record(ledger, name)) for name in DEFAULTS}


def configure(
    provider: str | Provider,
    *,
    plan: Literal["free", "paid"] | None = None,
    reserve_credits: int | None = None,
    allow_overage: bool | None = None,
    quota: int | None = None,
    quota_kind: QuotaKind | None = None,
    expires_at: str | None = None,
) -> None:
    """Override a provider's stored policy/quota descriptor.

    This is how a paid plan, a raised reserve, a top-up, or an expiry date
    gets recorded. ``allow_overage`` is the only switch that can cause a
    billed call, and it does nothing unless ``plan`` is also ``"paid"``
    (``_decide`` checks the plan first).
    """
    name = _provider_name(provider)
    if plan is not None and plan not in ("free", "paid"):
        raise InvalidRequest(f"plan must be 'free' or 'paid', got {plan!r}")
    with _locked() as ledger:
        record = _record(ledger, name)
        if plan is not None:
            record["plan"] = plan
        if reserve_credits is not None:
            record["reserve"] = int(reserve_credits)
        if allow_overage is not None:
            record["allow_overage"] = bool(allow_overage)
        if quota is not None:
            record["quota"] = int(quota)
        if quota_kind is not None:
            record["quota_kind"] = quota_kind.value
            record["quota_period"] = (
                current_period() if quota_kind is QuotaKind.MONTHLY else None
            )
        if expires_at is not None:
            record["expires_at"] = expires_at
        _housekeep_record(record)
