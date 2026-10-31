"""Local budget reservations remain conservative through races and uncertainty."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
import json
import multiprocessing
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest import mock

TOOLS = Path(__file__).resolve().parents[1] / "plugins/ce-shared/tools"
sys.path.insert(0, str(TOOLS))
from ce_api_budget import BudgetError, BudgetExceeded, BudgetLedger

NOW = datetime(2026, 10, 31, 23, 59, tzinfo=timezone.utc)


def reserve_worker(path, start, results):
    start.wait(10)
    try:
        BudgetLedger(path, "1", "0.1").reserve("0.1")
        results.put("reserved")
    except BudgetExceeded:
        results.put("limited")
    except Exception as exc:
        results.put(type(exc).__name__)


class ApiBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "budget.json"
        self.now = NOW
        self.ledger = BudgetLedger(self.path, "1", "0.6", clock=lambda: self.now)

    def test_reservation_precedes_settlement_and_files_are_private(self):
        reservation = self.ledger.reserve("0.6")
        with self.assertRaises(BudgetExceeded):
            self.ledger.reserve("0.5")
        state = self.ledger.settle(reservation, "0.2")
        self.assertEqual(state, {"period": "2026-10", "spent": "0.2", "reservations": {}})
        self.ledger.reserve("0.6")
        for path in (self.path, Path(str(self.path) + ".lock")):
            if sys.platform == "win32":  # (0.31) NTFS has no mode bits; the profile's ACL keeps them private
                self.assertTrue(path.is_file())
            else:
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_limits_and_costs_reject_missing_negative_boolean_nan_and_float(self):
        invalid = [None, True, False, -1, "-0.001", "NaN", "Infinity", Decimal("sNaN"), 0.1, {}, ""]
        for amount in invalid:
            with self.subTest(amount=repr(amount)):
                with self.assertRaises(BudgetError):
                    BudgetLedger(self.path, amount, "1")
                with self.assertRaises(BudgetError):
                    BudgetLedger(self.path, "1", amount)
                with self.assertRaises(BudgetError):
                    self.ledger.reserve(amount)
        with self.assertRaises(BudgetExceeded):
            self.ledger.reserve("0.60000000000000000000000000001")
        self.assertFalse(self.path.exists())

    def test_unknown_outcome_keeps_full_reservation_and_no_expiry(self):
        reservation = self.ledger.reserve("0.6")
        with self.assertRaises(BudgetError):
            self.ledger.settle(reservation, None)
        self.now = NOW.replace(year=2030, month=2, day=1)
        self.assertEqual(self.ledger.snapshot()["reservations"][reservation]["amount"], "0.6")
        with self.assertRaises(BudgetExceeded):
            self.ledger.reserve("0.5")

    def test_actual_overrun_is_recorded_without_clipping_even_above_monthly_limit(self):
        reservation = self.ledger.reserve("0.4")
        with self.assertRaises(BudgetExceeded):
            self.ledger.settle(reservation, "1.23456789012345678901234567890123456789")
        state = self.ledger.snapshot()
        self.assertEqual(state["spent"], "1.23456789012345678901234567890123456789")
        self.assertEqual(state["reservations"], {})
        with self.assertRaises(BudgetExceeded):
            self.ledger.reserve("0")
        with self.assertRaises(BudgetError):
            self.ledger.settle(reservation, "0")

    def test_underestimated_request_records_actual_cost_then_stops(self):
        reservation = self.ledger.reserve("0.1")
        with self.assertRaises(BudgetExceeded):
            self.ledger.settle(reservation, "0.2")
        self.assertEqual(self.ledger.snapshot()["spent"], "0.2")

    def test_ledger_and_lock_symlinks_are_rejected(self):
        target = self.path.parent / "target"
        target.write_text("secret")
        for link in (self.path, Path(str(self.path) + ".lock")):
            link.unlink(missing_ok=True)
            link.symlink_to(target)
            with self.assertRaises(BudgetError):
                self.ledger.reserve("0.1")
            self.assertEqual(target.read_text(), "secret")
            link.unlink()

    def test_windows_transaction_locks_writes_and_refuses_linked_locks(self):
        """(0.31) The Windows path (no fcntl: heavy_lock's LockFileEx, no O_NOFOLLOW, no directory fsync), driven on
        every system with heavy_lock's lock_fd/unlock_fd standing in."""
        import ce_api_budget
        import heavy_lock
        calls = []
        lock, unlock = heavy_lock.lock_fd, heavy_lock.unlock_fd
        stand_in = mock.Mock(OPEN_FLAGS=heavy_lock.OPEN_FLAGS,
                             lock_fd=lambda fd, **kw: (calls.append(("lock", kw)), lock(fd, **kw)),
                             unlock_fd=lambda fd: (calls.append(("unlock", {})), unlock(fd)))
        with mock.patch.object(ce_api_budget, "fcntl", None), \
                mock.patch.object(ce_api_budget, "heavy_lock", stand_in, create=True):
            reservation = self.ledger.reserve("0.6")
            with self.assertRaises(BudgetExceeded):
                self.ledger.reserve("0.5")
            self.assertEqual(self.ledger.settle(reservation, "0.2")["spent"], "0.2")
            self.assertEqual(calls[:2], [("lock", {"exclusive": True, "wait": True}), ("unlock", {})])
            self.assertEqual(len(calls), 6)  # every transaction locks and unlocks, also the refused one
            lock_path = Path(str(self.path) + ".lock")
            lock_path.unlink()
            with mock.patch.object(ce_api_budget.os.path, "islink", side_effect=lambda p: str(p) == str(lock_path)):
                with self.assertRaisesRegex(BudgetError, "Cannot lock"):
                    self.ledger.reserve("0.1")
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["spent"], "0.2")

    def test_double_and_unknown_settle_or_cancel_fail_closed(self):
        reservation = self.ledger.reserve("0.3")
        self.ledger.settle(reservation, "0.2")
        for operation in (lambda: self.ledger.settle(reservation, "0.2"),
                          lambda: self.ledger.cancel_before_send(reservation),
                          lambda: self.ledger.settle("unknown", "0")):
            with self.assertRaises(BudgetError):
                operation()
        self.assertEqual(self.ledger.snapshot()["spent"], "0.2")
        unsent = self.ledger.reserve("0.6")
        self.ledger.cancel_before_send(unsent)
        self.assertEqual(self.ledger.snapshot()["reservations"], {})

    def test_month_rollover_preserves_pending_and_late_settlement_counts_now(self):
        done = self.ledger.reserve("0.3")
        self.ledger.settle(done, "0.3")
        pending = self.ledger.reserve("0.6")
        self.now += timedelta(minutes=2)
        state = self.ledger.snapshot()
        self.assertEqual((state["period"], state["spent"]), ("2026-11", "0"))
        self.assertEqual(state["reservations"][pending]["period"], "2026-10")
        with self.assertRaises(BudgetExceeded):
            self.ledger.reserve("0.5")
        self.ledger.settle(pending, "0.4")
        self.assertEqual(self.ledger.snapshot()["spent"], "0.4")
        self.ledger.reserve("0.6")

    def test_period_is_utc_and_clock_regression_or_naive_clock_is_rejected(self):
        self.now = datetime(2026, 11, 1, 0, 30, tzinfo=timezone(timedelta(hours=2)))
        self.assertEqual(self.ledger.snapshot()["period"], "2026-10")
        self.now = NOW.replace(month=9, day=30)
        with self.assertRaises(BudgetError):
            self.ledger.snapshot()
        self.now = NOW.replace(tzinfo=None)
        with self.assertRaises(BudgetError):
            self.ledger.snapshot()

    def test_addition_is_exact_under_small_decimal_context(self):
        ledger = BudgetLedger(self.path, "1", "1", clock=lambda: self.now)
        with localcontext() as context:
            context.prec = 2
            for amount in ("0.12345678901234567890123456789", "0.00000000000000000000000000001"):
                ledger.settle(ledger.reserve(amount), amount)
            self.assertEqual(ledger.snapshot()["spent"], "0.12345678901234567890123456790")
        ledger.reserve("0.87654321098765432109876543210")
        with self.assertRaises(BudgetExceeded):
            ledger.reserve("0.00000000000000000000000000001")

    def test_corruption_never_reinitializes_or_modifies_ledger(self):
        valid = {"period": "2026-10", "spent": "0", "reservations": {}}
        corrupt = ["", " ", "{", "null", "[]", '{}',
                   '{"period":"2026-10","spent":"0","spent":"0","reservations":{}}']
        for field, value in [("spent", None), ("spent", True), ("spent", "-1"),
                             ("spent", "NaN"), ("spent", float("nan")),
                             ("period", "2026-13"), ("period", "2027-01"),
                             ("reservations", {"a" * 32: {"amount": True, "period": "2026-10"}}),
                             ("reservations", {"a" * 32: {"period": "2026-10"}}),
                             ("reservations", {"a" * 32: {"amount": "1", "period": "2026-11"}})]:
            corrupt.append(json.dumps({**valid, field: value}))
        for contents in corrupt:
            with self.subTest(contents=contents):
                self.path.write_text(contents)
                with self.assertRaises(BudgetError):
                    self.ledger.reserve("0.1")
                self.assertEqual(self.path.read_text(), contents)

    def test_atomic_write_failure_keeps_previous_reservation(self):
        reservation = self.ledger.reserve("0.6")
        before = self.path.read_bytes()
        with mock.patch("ce_api_budget.os.replace", side_effect=OSError("disk unavailable")):
            with self.assertRaises(BudgetError):
                self.ledger.settle(reservation, "0.1")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(sorted(item.name for item in self.path.parent.iterdir()),
                         ["budget.json", "budget.json.lock"])

    def test_threads_cannot_overspend_same_month(self):
        def attempt(_):
            try:
                BudgetLedger(self.path, "1", "0.1").reserve("0.1")
                return 1
            except BudgetExceeded:
                return 0
        with ThreadPoolExecutor(max_workers=16) as executor:
            self.assertEqual(sum(executor.map(attempt, range(40))), 10)
        state = BudgetLedger(self.path, "1", "0.1").snapshot()
        self.assertEqual(len(state["reservations"]), 10)

    def test_processes_cannot_overspend_same_month(self):
        context = multiprocessing.get_context("spawn")
        start, results = context.Event(), context.Queue()
        workers = [context.Process(target=reserve_worker, args=(str(self.path), start, results))
                   for _ in range(16)]
        try:
            for worker in workers:
                worker.start()
            start.set()
            outcomes = [results.get(timeout=20) for _ in workers]
            self.assertEqual(outcomes.count("reserved"), 10)
            self.assertEqual(outcomes.count("limited"), 6)
            for worker in workers:
                worker.join(20)
                self.assertEqual(worker.exitcode, 0)
        finally:
            for worker in workers:
                if worker.is_alive():
                    worker.terminate()
                    worker.join(5)
            results.close()
