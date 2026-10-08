"""Tests for sertchi.usage -- the shared credit ledger and health gate
(fable-prompt-build-v1.md §5 Phase 6).

Covers: the fixed-reserve affordability gate's boundary cases, free-vs-paid
allow_overage behaviour, monthly rollover, TOTAL deliberately NOT rolling
over, EXPIRING zeroing a lapsed balance, circuit-breaker doubling and
reset-on-success, DDG's 1.1s rate floor as a mechanism distinct from the
breaker, the reservation TTL sweep, and -- the decisive one -- a real
20-process concurrency test proving the flock covers the whole
read-modify-write.

CRITICAL: every test here repoints usage.CONFIG_DIR / USAGE_PATH / LOCK_PATH
at a tempfile.TemporaryDirectory and restores them in tearDown. No test in
this module may write into the real ~/.config/sertchi/.

The concurrency workers deserve a note: they run under the "spawn" start
method on purpose. "fork" would inherit the parent's already-patched module
globals AND its warm interpreter state, which both hides path-sandboxing
mistakes and weakens the race. Spawned children re-import sertchi.usage
fresh, with the REAL ~/.config/sertchi paths, so each worker must repoint the
globals itself from the temp path passed in as an argument -- monkeypatching
in the parent does not reach them.
"""

from __future__ import annotations

import fcntl
import json
import multiprocessing
import os
import stat
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from sertchi import usage
from sertchi.types import AttemptOutcome, CreditCost, InvalidRequest, QuotaKind

REPO_ROOT = str(Path(__file__).resolve().parents[2])

CONCURRENCY_WORKERS = 20


def _cost(credits: int, basis: str = "test") -> CreditCost:
    return CreditCost(credits=credits, unit="credits", basis=basis)


# --------------------------------------------------------------------------
# multiprocessing worker -- module level so "spawn" can pickle it by name
# --------------------------------------------------------------------------


def _concurrent_spend_worker(config_dir: str, barrier) -> None:
    """One worker: reserve 1 credit, then reconcile it as really spent.

    Repoints the freshly-imported module's path globals from the argument --
    a spawned child never sees the parent's monkeypatching. The barrier makes
    all workers collide on the lock at once, which is what turns a
    lost-update bug from "occasionally" into "almost always".
    """
    if REPO_ROOT not in sys.path:
        sys.path.insert(0, REPO_ROOT)

    from pathlib import Path as _Path

    from sertchi import usage as child_usage
    from sertchi.types import AttemptOutcome as _Outcome
    from sertchi.types import CreditCost as _CreditCost

    child_usage.CONFIG_DIR = _Path(config_dir)
    child_usage.USAGE_PATH = child_usage.CONFIG_DIR / "usage.json"
    child_usage.LOCK_PATH = child_usage.CONFIG_DIR / "usage.lock"

    one_credit = _CreditCost(credits=1, unit="credits", basis="concurrency-test")
    try:
        barrier.wait(timeout=60)
    except Exception:  # noqa: BLE001 -- a broken barrier must not skip the work
        pass

    reservation_id = child_usage.reserve("tavily", one_credit)
    child_usage.reconcile(
        "tavily",
        reservation_id,
        actual=one_credit,
        outcome=_Outcome.SUCCESS,
        latency_ms=1.0,
    )


def _concurrent_slot_worker(config_dir: str, barrier, out_path: str) -> None:
    """One worker: claim a DDG pacing slot and record where it landed.

    Mirrors _concurrent_spend_worker's shape exactly -- re-import fresh in
    the spawned child, repoint the three path globals from the argument (a
    spawned child never sees the parent's monkeypatching), wait on the
    barrier so all workers collide on the lock at the same instant, then
    claim. max_wait_s=3600 is deliberate: this test measures the claim
    arithmetic under contention, not patience -- workers never sleep, they
    only claim, so a real bug (slots colliding) cannot hide behind a wait.
    """
    if REPO_ROOT not in sys.path:
        sys.path.insert(0, REPO_ROOT)

    from pathlib import Path as _Path

    from sertchi import usage as child_usage

    child_usage.CONFIG_DIR = _Path(config_dir)
    child_usage.USAGE_PATH = child_usage.CONFIG_DIR / "usage.json"
    child_usage.LOCK_PATH = child_usage.CONFIG_DIR / "usage.lock"

    try:
        barrier.wait(timeout=60)
    except Exception:  # noqa: BLE001 -- a broken barrier must not skip the work
        pass

    grant = child_usage.reserve_slot("ddg", max_wait_s=3600)

    worker_file = _Path(out_path) / f"{os.getpid()}.txt"
    worker_file.write_text(grant.slot_at or "")


# --------------------------------------------------------------------------
# Sandbox base class -- mirrors tests/test_keys.py's _TempConfigDirTestCase
# --------------------------------------------------------------------------


class _TempUsageDirTestCase(unittest.TestCase):
    """Points usage.CONFIG_DIR/USAGE_PATH/LOCK_PATH at a fresh temp directory
    for each test, restoring the real module globals in tearDown regardless
    of outcome. All three move together -- repointing only CONFIG_DIR would
    leave writes landing in the real ~/.config/sertchi/."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._real = (usage.CONFIG_DIR, usage.USAGE_PATH, usage.LOCK_PATH)
        sandbox = Path(self._tmpdir.name) / "sertchi"
        usage.CONFIG_DIR = sandbox
        usage.USAGE_PATH = sandbox / "usage.json"
        usage.LOCK_PATH = sandbox / "usage.lock"

    def tearDown(self) -> None:
        usage.CONFIG_DIR, usage.USAGE_PATH, usage.LOCK_PATH = self._real
        self._tmpdir.cleanup()

    # -- helpers ---------------------------------------------------------

    def spend(self, provider: str, credits: int) -> None:
        """Drive local_credits_used through the real public API."""
        cost = _cost(credits, "test-setup")
        reservation_id = usage.reserve(provider, cost)
        usage.reconcile(
            provider,
            reservation_id,
            actual=cost,
            outcome=AttemptOutcome.SUCCESS,
            latency_ms=1.0,
        )

    def poke(self, provider: str, **fields) -> None:
        """Craft a stale on-disk record (an old quota_period, an aged
        reservation) that the public API has no legitimate way to produce."""
        usage.get_state(provider)  # ensure the record exists
        data = json.loads(usage.USAGE_PATH.read_text())
        data["providers"][provider].update(fields)
        usage.USAGE_PATH.write_text(json.dumps(data))

    def raw(self, provider: str) -> dict:
        return json.loads(usage.USAGE_PATH.read_text())["providers"][provider]


# --------------------------------------------------------------------------
# Defaults table
# --------------------------------------------------------------------------


class DefaultsTest(_TempUsageDirTestCase):
    def test_seeded_defaults_match_the_confirmed_table(self) -> None:
        expected = {
            "tavily": (1000, QuotaKind.MONTHLY, "free", 10),
            "firecrawl": (1000, QuotaKind.MONTHLY, "free", 10),
            "serper": (2500, QuotaKind.TOTAL, "free", 10),
            "ddg": (None, None, "free", 0),
        }
        for name, (quota, kind, plan, floor) in expected.items():
            with self.subTest(provider=name):
                state = usage.get_state(name)
                self.assertEqual(state.quota, quota)
                self.assertEqual(state.quota_kind, kind)
                self.assertEqual(state.plan, plan)
                self.assertEqual(self.raw(name)["reserve"], floor)

    def test_allow_overage_defaults_to_false_everywhere(self) -> None:
        for name in usage.DEFAULTS:
            with self.subTest(provider=name):
                usage.get_state(name)
                self.assertFalse(self.raw(name)["allow_overage"])

    def test_unknown_provider_raises_invalid_request(self) -> None:
        with self.assertRaises(InvalidRequest):
            usage.can_afford("bing", _cost(1))

    def test_usage_json_is_mode_0600(self) -> None:
        usage.get_state("tavily")
        mode = stat.S_IMODE(os.stat(usage.USAGE_PATH).st_mode)
        self.assertEqual(mode, 0o600)


# --------------------------------------------------------------------------
# can_afford
# --------------------------------------------------------------------------


class CanAffordTest(_TempUsageDirTestCase):
    def _setup_pool(self, remaining: int, floor: int, quota: int = 20) -> None:
        usage.configure("tavily", quota=quota, reserve_credits=floor)
        self.spend("tavily", quota - remaining)
        self.assertEqual(
            usage.get_state("tavily").local_credits_remaining_estimate, remaining
        )

    def test_remaining_17_cost_2_reserve_5_allows(self) -> None:
        self._setup_pool(remaining=17, floor=5)
        allowed, reason = usage.can_afford("tavily", _cost(2))
        self.assertTrue(allowed, reason)

    def test_remaining_6_cost_2_reserve_5_denies(self) -> None:
        self._setup_pool(remaining=6, floor=5)
        allowed, reason = usage.can_afford("tavily", _cost(2))
        self.assertFalse(allowed, reason)

    def test_exact_boundary_allows(self) -> None:
        # 7 - 2 == 5 == reserve. The rule is >=, so this is the last allowed
        # call; one more credit of cost would deny.
        self._setup_pool(remaining=7, floor=5)
        allowed, _ = usage.can_afford("tavily", _cost(2))
        self.assertTrue(allowed)

    def test_one_credit_past_the_boundary_denies(self) -> None:
        self._setup_pool(remaining=7, floor=5)
        allowed, _ = usage.can_afford("tavily", _cost(3))
        self.assertFalse(allowed)

    def test_free_plan_denial_is_a_hard_stop(self) -> None:
        self._setup_pool(remaining=6, floor=5)
        allowed, reason = usage.can_afford("tavily", _cost(2))
        self.assertFalse(allowed)
        self.assertIn("free plan", reason)

    def test_paid_plan_without_allow_overage_still_denies(self) -> None:
        self._setup_pool(remaining=6, floor=5)
        usage.configure("tavily", plan="paid")
        allowed, reason = usage.can_afford("tavily", _cost(2))
        self.assertFalse(allowed, "allow_overage defaults False -- must not spend")
        self.assertIn("allow_overage=False", reason)

    def test_paid_plan_with_allow_overage_permits_the_billed_call(self) -> None:
        self._setup_pool(remaining=6, floor=5)
        usage.configure("tavily", plan="paid", allow_overage=True)
        allowed, reason = usage.can_afford("tavily", _cost(2))
        self.assertTrue(allowed)
        self.assertIn("allow_overage=True", reason)

    def test_free_plan_with_allow_overage_still_denies(self) -> None:
        # allow_overage is meaningless without a paid plan -- a free account
        # has no overage to be billed for, only a quota error to hit.
        self._setup_pool(remaining=6, floor=5)
        usage.configure("tavily", allow_overage=True)
        allowed, _ = usage.can_afford("tavily", _cost(2))
        self.assertFalse(allowed)

    def test_ddg_is_never_quota_gated(self) -> None:
        allowed, reason = usage.can_afford("ddg", _cost(999_999))
        self.assertTrue(allowed)
        self.assertIn("not quota-gated", reason)

    def test_live_reservation_shrinks_headroom_before_reconcile(self) -> None:
        usage.configure("tavily", quota=20, reserve_credits=5)
        usage.reserve("tavily", _cost(12))
        # 20 - 12 held = 8 remaining; 8 - 2 = 6 >= 5 -> allow
        self.assertTrue(usage.can_afford("tavily", _cost(2))[0])
        # but a 4-credit call would land inside the reserve -> deny
        self.assertFalse(usage.can_afford("tavily", _cost(4))[0])

    def test_reported_remaining_wins_over_local_estimate(self) -> None:
        usage.configure("tavily", quota=1000, reserve_credits=10)
        reservation_id = usage.reserve("tavily", _cost(1))
        usage.reconcile(
            "tavily",
            reservation_id,
            actual=_cost(1),
            outcome=AttemptOutcome.SUCCESS,
            latency_ms=5.0,
            reported_remaining=12,
        )
        state = usage.get_state("tavily")
        self.assertEqual(state.provider_reported_remaining, 12)
        self.assertEqual(state.best_available_remaining, 12)
        # Local estimate says 999 left; the provider says 12. The provider
        # wins, so a 3-credit call must deny against a reserve of 10.
        self.assertEqual(state.local_credits_remaining_estimate, 999)
        self.assertFalse(usage.can_afford("tavily", _cost(3))[0])

    def test_reported_remaining_is_netted_against_live_reservations(self) -> None:
        usage.configure("tavily", quota=1000, reserve_credits=10)
        first = usage.reserve("tavily", _cost(1))
        usage.reconcile(
            "tavily",
            first,
            actual=_cost(1),
            outcome=AttemptOutcome.SUCCESS,
            latency_ms=5.0,
            reported_remaining=20,
        )
        # Provider said 20 left. With nothing in flight, a 5-credit call fits
        # (20 - 5 = 15 >= 10).
        self.assertTrue(usage.can_afford("tavily", _cost(5))[0])
        # Now hold 8 credits in flight. The provider's figure predates them,
        # so the gate must subtract: 20 - 8 = 12, and 12 - 5 = 7 < 10 -> deny.
        usage.reserve("tavily", _cost(8))
        self.assertFalse(usage.can_afford("tavily", _cost(5))[0])


# --------------------------------------------------------------------------
# Rollover
# --------------------------------------------------------------------------


class RolloverTest(_TempUsageDirTestCase):
    def test_monthly_rolls_on_period_change(self) -> None:
        self.spend("tavily", 300)
        self.assertEqual(usage.get_state("tavily").local_credits_used, 300)
        self.poke("tavily", quota_period="1999-01")
        state = usage.get_state("tavily")
        self.assertEqual(state.local_credits_used, 0)
        self.assertEqual(state.local_credits_remaining_estimate, 1000)
        self.assertEqual(state.quota_period, usage.current_period())

    def test_monthly_rollover_clears_stale_reported_remaining(self) -> None:
        reservation_id = usage.reserve("tavily", _cost(1))
        usage.reconcile(
            "tavily",
            reservation_id,
            actual=_cost(1),
            outcome=AttemptOutcome.SUCCESS,
            latency_ms=1.0,
            reported_remaining=3,
        )
        self.poke("tavily", quota_period="1999-01")
        state = usage.get_state("tavily")
        self.assertIsNone(
            state.provider_reported_remaining,
            "last period's reported balance must not gate this period",
        )
        self.assertEqual(state.best_available_remaining, 1000)

    def test_monthly_does_not_roll_within_the_same_period(self) -> None:
        self.spend("tavily", 42)
        usage.get_state("tavily")
        self.assertEqual(usage.get_state("tavily").local_credits_used, 42)

    def test_total_never_rolls_over(self) -> None:
        # Serper's 2500 is a one-time grant. Rolling it would silently refill
        # a balance that does not refill -- the exact silent-overspend this
        # phase exists to prevent.
        self.spend("serper", 400)
        self.poke("serper", quota_period="1999-01")
        state = usage.get_state("serper")
        self.assertEqual(state.local_credits_used, 400)
        self.assertEqual(state.local_credits_remaining_estimate, 2100)

    def test_expiring_zeroes_the_balance_once_lapsed(self) -> None:
        lapsed = (datetime.now() - timedelta(days=1)).isoformat(timespec="seconds")
        usage.configure(
            "serper", quota_kind=QuotaKind.EXPIRING, expires_at=lapsed
        )
        state = usage.get_state("serper")
        self.assertEqual(state.local_credits_remaining_estimate, 0)
        self.assertEqual(state.provider_reported_remaining, 0)
        self.assertFalse(usage.can_afford("serper", _cost(1))[0])

    def test_expiring_untouched_before_it_lapses(self) -> None:
        future = (datetime.now() + timedelta(days=30)).isoformat(timespec="seconds")
        usage.configure(
            "serper", quota_kind=QuotaKind.EXPIRING, expires_at=future
        )
        state = usage.get_state("serper")
        self.assertEqual(state.local_credits_remaining_estimate, 2500)
        self.assertTrue(usage.can_afford("serper", _cost(1))[0])

    def test_current_period_uses_local_machine_time(self) -> None:
        self.assertEqual(usage.current_period(), datetime.now().strftime("%Y-%m"))


# --------------------------------------------------------------------------
# Circuit breaker
# --------------------------------------------------------------------------


class BackoffTest(_TempUsageDirTestCase):
    def fail_once(self, provider: str = "tavily") -> None:
        reservation_id = usage.reserve(provider, _cost(1))
        usage.reconcile(
            provider,
            reservation_id,
            actual=None,
            outcome=AttemptOutcome.ERROR,
            latency_ms=None,
        )

    def cooldown_seconds(self, provider: str = "tavily") -> float:
        state = usage.get_state(provider)
        assert state.cooldown_until is not None
        return (
            datetime.fromisoformat(state.cooldown_until) - datetime.now()
        ).total_seconds()

    def test_cooldown_doubles_per_consecutive_failure(self) -> None:
        for expected in (30.0, 60.0, 120.0, 240.0):
            self.fail_once()
            self.assertAlmostEqual(self.cooldown_seconds(), expected, delta=3.0)

    def test_consecutive_failures_counts_up(self) -> None:
        for expected in (1, 2, 3):
            self.fail_once()
            self.assertEqual(usage.get_state("tavily").consecutive_failures, expected)

    def test_cooldown_is_capped(self) -> None:
        for _ in range(10):
            self.fail_once()
        self.assertAlmostEqual(self.cooldown_seconds(), 900.0, delta=3.0)
        self.assertEqual(usage.get_state("tavily").consecutive_failures, 10)

    def test_is_cooling_down_reports_the_breaker(self) -> None:
        self.fail_once()
        blocked, reason = usage.is_cooling_down("tavily")
        self.assertTrue(blocked)
        self.assertIn("circuit breaker", reason)

    def test_success_resets_the_breaker(self) -> None:
        self.fail_once()
        self.fail_once()
        self.spend("tavily", 1)  # a SUCCESS reconcile
        state = usage.get_state("tavily")
        self.assertEqual(state.consecutive_failures, 0)
        self.assertIsNone(state.cooldown_until)
        self.assertFalse(usage.is_cooling_down("tavily")[0])

    def test_empty_result_also_resets_the_breaker(self) -> None:
        # A 200 with zero rows proves the provider is reachable and healthy;
        # it is not a failure, it is just a quiet answer.
        self.fail_once()
        reservation_id = usage.reserve("tavily", _cost(1))
        usage.reconcile(
            "tavily",
            reservation_id,
            actual=_cost(1),
            outcome=AttemptOutcome.EMPTY,
            latency_ms=9.0,
        )
        self.assertEqual(usage.get_state("tavily").consecutive_failures, 0)

    def test_expired_cooldown_no_longer_blocks(self) -> None:
        self.fail_once()
        past = (datetime.now() - timedelta(seconds=1)).isoformat(timespec="seconds")
        self.poke("tavily", cooldown_until=past)
        self.assertFalse(usage.is_cooling_down("tavily")[0])

    def test_latency_samples_are_recorded_and_bounded(self) -> None:
        for _ in range(usage.LATENCY_SAMPLES + 5):
            self.spend("tavily", 0)
        self.assertEqual(
            len(usage.get_state("tavily").latency_ms_recent), usage.LATENCY_SAMPLES
        )


# --------------------------------------------------------------------------
# DDG's pacing schedule -- reserve-and-pace, not reject (replaces the old
# reject-if-too-soon rate floor; is_cooling_down() is breaker-only now)
# --------------------------------------------------------------------------


class SlotReservationTest(_TempUsageDirTestCase):
    def test_first_claim_on_a_fresh_ledger_is_free(self) -> None:
        # Replaces test_ddg_is_gated_for_1_1s_after_a_request -- the old
        # mechanism rejected the very first request; the new one grants it
        # immediately, since nothing has claimed a slot yet.
        grant = usage.reserve_slot("ddg")
        self.assertTrue(grant.granted)
        self.assertEqual(grant.wait_s, 0.0)

    def test_second_immediate_claim_waits_out_the_interval(self) -> None:
        # Replaces test_rate_floor_applies_to_a_perfectly_healthy_provider --
        # a perfectly healthy provider still gets paced, not rejected.
        usage.reserve_slot("ddg")
        grant = usage.reserve_slot("ddg")
        self.assertTrue(grant.granted)
        self.assertGreater(grant.wait_s, 1.0)
        self.assertLessEqual(grant.wait_s, 1.1)

    def test_third_immediate_claim_waits_out_two_intervals(self) -> None:
        # Replaces test_floor_clears_once_the_interval_has_passed's spirit --
        # a stacked burst is paced onto successive slots rather than clearing
        # after one interval and colliding.
        usage.reserve_slot("ddg")
        usage.reserve_slot("ddg")
        grant = usage.reserve_slot("ddg")
        self.assertTrue(grant.granted)
        self.assertGreater(grant.wait_s, 2.0)
        self.assertLessEqual(grant.wait_s, 2.2)

    def test_floor_value_is_1_1_seconds(self) -> None:
        self.assertEqual(usage.MIN_REQUEST_INTERVAL_S["ddg"], 1.1)

    def test_keyed_providers_have_no_pacing_schedule(self) -> None:
        # Replaces test_keyed_providers_have_no_rate_floor. A keyed provider
        # is granted immediately and never gets a next_slot_at written --
        # confirmed via the raw record, not just the returned grant.
        for name in ("tavily", "firecrawl", "serper"):
            with self.subTest(provider=name):
                grant = usage.reserve_slot(name)
                self.assertTrue(grant.granted)
                self.assertEqual(grant.wait_s, 0.0)
                # reserve_slot() on an unpaced provider returns before ever
                # taking the lock, so the ledger record may not exist yet --
                # get_state() seeds it before we inspect the raw file.
                usage.get_state(name)
                self.assertIsNone(self.raw(name)["next_slot_at"])

    def test_is_cooling_down_no_longer_reports_the_slot_schedule(self) -> None:
        # Deliberate reversal, not a regression: is_cooling_down() is
        # breaker-only now (Phase 2). Pacing moved to reserve_slot(), which
        # grants-with-a-wait instead of rejecting outright, so a request
        # issued immediately after another must read False here even though
        # a real caller would still have to wait for its claimed slot.
        usage.reserve_slot("ddg")
        self.assertFalse(usage.is_cooling_down("ddg")[0])


class SlotReservationN1Test(_TempUsageDirTestCase):
    def test_refused_claim_leaves_next_slot_at_untouched(self) -> None:
        # N1: a refusal must never advance next_slot_at -- that is what
        # bounds the backlog to now + max_wait_s + interval instead of
        # letting a burst buffer for hours.
        usage.reserve_slot("ddg")  # seed a real next_slot_at
        before = self.raw("ddg")["next_slot_at"]
        self.assertIsNotNone(before)

        grant = usage.reserve_slot("ddg", max_wait_s=0.0)
        self.assertFalse(grant.granted)

        after = self.raw("ddg")["next_slot_at"]
        self.assertEqual(after, before, "a refused claim must not mutate next_slot_at")


class SlotReservationLoadSheddingTest(_TempUsageDirTestCase):
    def test_fifty_refusals_finish_in_under_one_second(self) -> None:
        # Priority #2 in executable form: load-shed, never buffer. A refused
        # claim must be a fast no, not a wait -- 50 of them back to back must
        # not take anywhere near 50 * max_wait_s.
        far_future = (datetime.now() + timedelta(hours=1)).isoformat()
        self.poke("ddg", next_slot_at=far_future)

        start = time.monotonic()
        for _ in range(50):
            grant = usage.reserve_slot("ddg", max_wait_s=2.0)
            self.assertFalse(grant.granted)
        elapsed = time.monotonic() - start

        self.assertLess(
            elapsed,
            1.0,
            "50 refusals took too long -- reserve_slot() must not sleep or block",
        )


class SlotReservationBackfillTest(_TempUsageDirTestCase):
    def test_missing_next_slot_at_backfills_to_none_not_a_raise(self) -> None:
        usage.get_state("ddg")  # seed the record
        data = json.loads(usage.USAGE_PATH.read_text())
        del data["providers"]["ddg"]["next_slot_at"]
        usage.USAGE_PATH.write_text(json.dumps(data))

        state = usage.get_state("ddg")
        self.assertIsNone(state.next_slot_at)


# --------------------------------------------------------------------------
# Reserve / reconcile / TTL sweep
# --------------------------------------------------------------------------


class ReservationTest(_TempUsageDirTestCase):
    def test_reserve_holds_credits_then_reconcile_settles_them(self) -> None:
        reservation_id = usage.reserve("tavily", _cost(2))
        self.assertEqual(usage.get_state("tavily").local_credits_remaining_estimate, 998)
        self.assertEqual(usage.get_state("tavily").local_credits_used, 0)
        usage.reconcile(
            "tavily",
            reservation_id,
            actual=_cost(2),
            outcome=AttemptOutcome.SUCCESS,
            latency_ms=3.0,
        )
        state = usage.get_state("tavily")
        self.assertEqual(state.local_credits_used, 2)
        self.assertEqual(state.reservations, [])
        self.assertEqual(state.local_credits_remaining_estimate, 998)

    def test_actual_cost_overrides_the_estimate(self) -> None:
        reservation_id = usage.reserve("tavily", _cost(1))
        usage.reconcile(
            "tavily",
            reservation_id,
            actual=_cost(2),  # the adapter reports what it really cost (I4)
            outcome=AttemptOutcome.SUCCESS,
            latency_ms=3.0,
        )
        self.assertEqual(usage.get_state("tavily").local_credits_used, 2)

    def test_unknown_actual_cost_charges_the_estimate(self) -> None:
        # Over-count when uncertain: a timed-out call may well have been
        # billed, so it is charged at the reserved estimate rather than
        # forgiven.
        reservation_id = usage.reserve("tavily", _cost(2))
        usage.reconcile(
            "tavily",
            reservation_id,
            actual=None,
            outcome=AttemptOutcome.ERROR,
            latency_ms=None,
        )
        self.assertEqual(usage.get_state("tavily").local_credits_used, 2)

    def test_stale_reservation_is_swept(self) -> None:
        usage.reserve("tavily", _cost(5))
        self.assertEqual(usage.get_state("tavily").local_credits_remaining_estimate, 995)
        aged = datetime.now() - timedelta(seconds=usage.RESERVATION_TTL_S + 10)
        record = self.raw("tavily")
        record["reservations"][0]["created_at"] = aged.isoformat(timespec="seconds")
        self.poke("tavily", reservations=record["reservations"])
        state = usage.get_state("tavily")
        self.assertEqual(state.reservations, [])
        self.assertEqual(state.local_credits_remaining_estimate, 1000)
        self.assertEqual(state.local_credits_used, 0)

    def test_fresh_reservation_survives_the_sweep(self) -> None:
        usage.reserve("tavily", _cost(5))
        usage.get_state("tavily")
        usage.get_state("tavily")
        self.assertEqual(len(usage.get_state("tavily").reservations), 1)

    def test_reservation_ttl_is_120_seconds(self) -> None:
        self.assertEqual(usage.RESERVATION_TTL_S, 120)

    def test_duplicate_reconcile_cannot_double_charge(self) -> None:
        reservation_id = usage.reserve("tavily", _cost(2))
        for _ in range(3):
            usage.reconcile(
                "tavily",
                reservation_id,
                actual=None,
                outcome=AttemptOutcome.SUCCESS,
                latency_ms=1.0,
            )
        self.assertEqual(usage.get_state("tavily").local_credits_used, 2)


# --------------------------------------------------------------------------
# I5 -- reserve, unlock, request, lock, reconcile
# --------------------------------------------------------------------------


class MeteredCallTest(_TempUsageDirTestCase):
    def test_lock_is_not_held_across_the_body(self) -> None:
        """The structural I5 check. flock locks the open file description, so
        a second os.open in this same process is denied by a lock already
        held elsewhere -- meaning a successful LOCK_NB here proves the ledger
        lock really was released before the caller's request ran."""
        acquired = False
        with usage.metered_call("tavily", _cost(1)) as call:
            fd = os.open(usage.LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
            call.outcome = AttemptOutcome.SUCCESS
            call.actual = _cost(1)
        self.assertTrue(acquired, "usage lock was still held during the call body")

    def test_reconciles_on_the_happy_path(self) -> None:
        with usage.metered_call("tavily", _cost(1)) as call:
            call.outcome = AttemptOutcome.SUCCESS
            call.actual = _cost(1)
            call.reported_remaining = 900
        state = usage.get_state("tavily")
        self.assertEqual(state.local_credits_used, 1)
        self.assertEqual(state.reservations, [])
        self.assertEqual(state.provider_reported_remaining, 900)

    def test_reconciles_in_a_finally_when_the_body_raises(self) -> None:
        with self.assertRaises(ZeroDivisionError):
            with usage.metered_call("tavily", _cost(2)):
                raise ZeroDivisionError("provider blew up mid-call")
        state = usage.get_state("tavily")
        self.assertEqual(state.reservations, [], "the hold must be released")
        self.assertEqual(state.local_credits_used, 2, "over-count the unknown cost")
        self.assertEqual(state.consecutive_failures, 1, "the breaker must advance")

    def test_default_outcome_is_error(self) -> None:
        # A caller that forgets to set an outcome is treated as having
        # failed -- the direction that costs an unnecessary cooldown rather
        # than an unnoticed overspend.
        self.assertIs(usage.Settlement().outcome, AttemptOutcome.ERROR)

    def test_latency_is_measured_when_the_caller_does_not_supply_it(self) -> None:
        with usage.metered_call("tavily", _cost(1)) as call:
            call.outcome = AttemptOutcome.SUCCESS
            call.actual = _cost(1)
        samples = usage.get_state("tavily").latency_ms_recent
        self.assertEqual(len(samples), 1)
        self.assertGreaterEqual(samples[0], 0.0)


# --------------------------------------------------------------------------
# The decisive one: genuine cross-process concurrency
# --------------------------------------------------------------------------


class ConcurrencyTest(_TempUsageDirTestCase):
    def test_twenty_processes_each_spending_one_credit_total_exactly_twenty(
        self,
    ) -> None:
        """20 spawned processes each reserve+reconcile exactly 1 credit.

        If the flock does not cover the whole read-modify-write, this lands
        on 17 or 19 instead of 20 -- a double-spend, the precise bug this
        phase exists to prevent. A barrier makes every worker hit the lock at
        the same instant so a lost update shows up reliably rather than one
        run in five.
        """
        usage.get_state("tavily")  # seed the ledger before the stampede
        ctx = multiprocessing.get_context("spawn")
        barrier = ctx.Barrier(CONCURRENCY_WORKERS)
        config_dir = str(usage.CONFIG_DIR)

        workers = [
            ctx.Process(target=_concurrent_spend_worker, args=(config_dir, barrier))
            for _ in range(CONCURRENCY_WORKERS)
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=120)

        for index, worker in enumerate(workers):
            self.assertEqual(
                worker.exitcode, 0, f"worker {index} exited {worker.exitcode}"
            )

        state = usage.get_state("tavily")
        self.assertEqual(
            state.local_credits_used,
            CONCURRENCY_WORKERS,
            "lost update -- the lock is not covering the read-modify-write",
        )
        self.assertEqual(state.reservations, [], "every hold must have settled")
        self.assertEqual(
            state.local_credits_remaining_estimate, 1000 - CONCURRENCY_WORKERS
        )


class SlotReservationConcurrencyTest(_TempUsageDirTestCase):
    def test_twenty_processes_claim_slots_at_least_1_1s_apart(self) -> None:
        """The decisive proof for this phase: pacing holds across processes,
        not just arithmetic inside one.

        20 spawned processes each claim a DDG slot under a barrier so they
        collide on the lock at the same instant. If the claim were not
        atomic under the lock (two processes both reading the same stale
        next_slot_at before either writes back), slots would collapse
        together instead of being spaced 1.1s apart -- exactly what the
        negative control below proves by breaking the grant line on purpose.
        """
        ctx = multiprocessing.get_context("spawn")
        barrier = ctx.Barrier(CONCURRENCY_WORKERS)
        config_dir = str(usage.CONFIG_DIR)

        with tempfile.TemporaryDirectory() as out_dir:
            workers = [
                ctx.Process(
                    target=_concurrent_slot_worker,
                    args=(config_dir, barrier, out_dir),
                )
                for _ in range(CONCURRENCY_WORKERS)
            ]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=120)

            for index, worker in enumerate(workers):
                self.assertEqual(
                    worker.exitcode, 0, f"worker {index} exited {worker.exitcode}"
                )

            slot_files = list(Path(out_dir).glob("*.txt"))
            self.assertEqual(len(slot_files), CONCURRENCY_WORKERS)
            raw_slots = [f.read_text() for f in slot_files]

        self.assertTrue(all(raw_slots), "every worker must have been granted a slot")
        slots = sorted(datetime.fromisoformat(s) for s in raw_slots)

        self.assertEqual(
            len(set(slots)), CONCURRENCY_WORKERS, "all 20 claimed slots must be distinct"
        )
        for earlier, later in zip(slots, slots[1:]):
            gap = (later - earlier).total_seconds()
            self.assertGreaterEqual(
                gap,
                1.1,
                f"consecutive slots {earlier.isoformat()} -> {later.isoformat()} "
                f"only {gap:.3f}s apart -- the claim is not covering the whole "
                "read-modify-write",
            )


if __name__ == "__main__":
    unittest.main()


class HealthNeutralSettlementTest(_TempUsageDirTestCase):
    """`counts_against_health=False` -- added 2026-08-20 alongside the
    decision that a rejected API key is a CONFIGURATION error.

    Such a call really did fail, so the honest outcome is still ERROR, but
    the failure says nothing about whether the PROVIDER is healthy. Without
    this, fixing a bad key would leave the provider sitting in a cooldown of
    up to BACKOFF_CAP_S seconds -- punishing the user for the repair.
    """

    def fail_once(self, provider: str = "tavily") -> None:
        """Local copy of BackoffTest's helper -- that one hangs off a
        different TestCase, and this class only needs the ERROR path."""
        reservation_id = usage.reserve(provider, _cost(1))
        usage.reconcile(
            provider,
            reservation_id,
            actual=None,
            outcome=AttemptOutcome.ERROR,
            latency_ms=None,
        )

    def _settle(self, *, counts_against_health: bool, credits: int = 0) -> None:
        reservation_id = usage.reserve("tavily", _cost(1))
        usage.reconcile(
            "tavily",
            reservation_id,
            actual=CreditCost(credits=credits, unit="none", basis="test"),
            outcome=AttemptOutcome.ERROR,
            latency_ms=1.0,
            counts_against_health=counts_against_health,
        )

    def test_does_not_advance_the_breaker(self) -> None:
        self._settle(counts_against_health=False)
        state = usage.get_state("tavily")
        self.assertEqual(state.consecutive_failures, 0)
        self.assertIsNone(state.cooldown_until)

    def test_still_releases_the_reservation(self) -> None:
        """Health-neutral must not mean 'skip the settlement' -- a leaked
        hold would drag the balance down permanently."""
        self._settle(counts_against_health=False)
        self.assertEqual(usage.get_state("tavily").reservations, [])

    def test_zero_actual_charges_nothing(self) -> None:
        before = usage.get_state("tavily").local_credits_used
        self._settle(counts_against_health=False, credits=0)
        self.assertEqual(usage.get_state("tavily").local_credits_used, before)

    def test_is_neutral_in_both_directions(self) -> None:
        """It must not RESET an existing failure streak either. A real
        outage followed by a bad-key attempt should leave the outage's
        breaker state exactly where it was."""
        self.fail_once()
        self.fail_once()
        before = usage.get_state("tavily")
        self.assertEqual(before.consecutive_failures, 2)

        self._settle(counts_against_health=False)

        after = usage.get_state("tavily")
        self.assertEqual(after.consecutive_failures, 2)
        self.assertEqual(after.cooldown_until, before.cooldown_until)

    def test_default_still_advances_the_breaker(self) -> None:
        """The default is unchanged -- an ordinary ERROR still counts."""
        self._settle(counts_against_health=True)
        self.assertEqual(usage.get_state("tavily").consecutive_failures, 1)
