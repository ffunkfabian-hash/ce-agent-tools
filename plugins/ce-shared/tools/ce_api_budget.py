#!/usr/bin/env python3
"""Atomic local API cost limits for UTC calendar months, independent of billing."""
import json
import os
from pathlib import Path
import sys
import tempfile
try:
    import fcntl
except ImportError:  # (0.31) Windows: heavy_lock's LockFileEx with flock semantics
    fcntl = None
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import heavy_lock
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext


class BudgetError(RuntimeError):
    """Invalid input or an unreadable ledger; sending must stop."""


class BudgetExceeded(BudgetError):
    """A request or monthly limit was exceeded."""


def _money(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise BudgetError("Cost must be a Decimal, decimal string or integer")
    try:
        result = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise BudgetError("Invalid cost") from exc
    if not result.is_finite() or result < 0:
        raise BudgetError("Cost must be finite and nonnegative")
    return result


def _add(left, right):
    # Do not let the caller's Decimal context round money during accumulation.
    with localcontext() as context:
        context.prec = max(left.adjusted(), right.adjusted()) - min(
            left.as_tuple().exponent, right.as_tuple().exponent) + 2
        return left + right


def _period(value):
    if not isinstance(value, str) or len(value) != 7:
        raise BudgetError("Invalid ledger period")
    try:
        if datetime.strptime(value, "%Y-%m").strftime("%Y-%m") != value:
            raise ValueError(value)
    except ValueError as exc:
        raise BudgetError("Invalid ledger period") from exc
    return value


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise BudgetError("Duplicate ledger key")
        result[key] = value
    return result


class BudgetLedger:
    """Reserve before sending; leave unknown outcomes reserved until reconciled.

    ``cancel_before_send`` is only for requests known never to have been sent.
    There is deliberately no reservation expiry or automatic retry. Amounts in
    snapshots are decimal strings. Costs settled across a month boundary count
    in the settlement month; outstanding reservations always consume its limit.
    """

    def __init__(self, path, monthly_limit, request_limit, *, clock=None):
        self.path = Path(path).expanduser().absolute()
        self.monthly_limit = _money(monthly_limit)
        self.request_limit = _money(request_limit)
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _now_period(self):
        now = self.clock()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise BudgetError("Budget clock must return a timezone-aware datetime")
        return now.astimezone(timezone.utc).strftime("%Y-%m")

    def _load(self, period):
        try:
            if self.path.is_symlink():
                raise BudgetError("Ledger must not be a symbolic link")
            with self.path.open(encoding="utf-8") as stream:
                data = json.load(stream, object_pairs_hook=_unique_pairs)
        except FileNotFoundError:
            return {"period": period, "spent": "0", "reservations": {}}
        except (OSError, ValueError) as exc:
            raise BudgetError("Cannot read cost ledger") from exc
        if not isinstance(data, dict) or set(data) != {"period", "spent", "reservations"}:
            raise BudgetError("Invalid ledger schema")
        ledger_period = _period(data["period"])
        if ledger_period > period:
            raise BudgetError("Ledger period is in the future")
        if not isinstance(data["spent"], str) or not isinstance(data["reservations"], dict):
            raise BudgetError("Invalid ledger amounts")
        _money(data["spent"])
        for key, item in data["reservations"].items():
            if (not isinstance(key, str) or len(key) != 32
                    or any(char not in "0123456789abcdef" for char in key)
                    or not isinstance(item, dict) or set(item) != {"amount", "period"}
                    or not isinstance(item["amount"], str)):
                raise BudgetError("Invalid reservation")
            _money(item["amount"])
            if _period(item["period"]) > ledger_period:
                raise BudgetError("Reservation period is in the future")
        if ledger_period != period:
            data["period"], data["spent"] = period, "0"
        return data

    def _write(self, data):
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                                             dir=self.path.parent, delete=False) as stream:
                temporary = stream.name
                if fcntl is not None:  # (0.31) Windows: the profile's ACL guards it, chmod only sets read-only
                    os.fchmod(stream.fileno(), 0o600)
                json.dump(data, stream, sort_keys=True, allow_nan=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            if fcntl is None:  # (0.31) Windows opens no directory; NTFS journals the rename itself
                return
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError as exc:
            raise BudgetError("Cannot persist cost ledger") from exc
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)

    @staticmethod
    def _usage(data):
        usage = _money(data["spent"])
        for item in data["reservations"].values():
            usage = _add(usage, _money(item["amount"]))
        return usage

    def _transaction(self, operation):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if fcntl is None:
                return self._transaction_windows(operation)
            descriptor = os.open(str(self.path) + ".lock",
                                 os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "a") as lock:
                os.fchmod(lock.fileno(), 0o600)
                fcntl.flock(lock, fcntl.LOCK_EX)
                data = self._load(self._now_period())
                result, error = operation(data)
                self._write(data)
                if error:
                    raise error
                return result
        except OSError as exc:
            raise BudgetError("Cannot lock cost ledger") from exc

    def _transaction_windows(self, operation):
        """(0.31) The same transaction on Windows: no O_NOFOLLOW there, so a linked lock file is refused before it is
        opened; the lock is held until the ledger is written and freed at once (unlock_fd)."""
        lock_path = str(self.path) + ".lock"
        if os.path.islink(lock_path):
            raise BudgetError("Cannot lock cost ledger")
        try:
            descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | heavy_lock.OPEN_FLAGS, 0o600)
        except OSError as exc:
            raise BudgetError("Cannot lock cost ledger") from exc
        try:
            try:
                heavy_lock.lock_fd(descriptor, exclusive=True, wait=True)
            except OSError as exc:
                raise BudgetError("Cannot lock cost ledger") from exc
            try:
                data = self._load(self._now_period())
                result, error = operation(data)
                self._write(data)
                if error:
                    raise error
                return result
            finally:
                heavy_lock.unlock_fd(descriptor)
        finally:
            os.close(descriptor)

    def reserve(self, amount):
        amount = _money(amount)
        if amount > self.request_limit:
            raise BudgetExceeded("Request cost limit exceeded")

        def operation(data):
            if _add(self._usage(data), amount) > self.monthly_limit:
                raise BudgetExceeded("UTC calendar-month cost limit exceeded")
            reservation_id = uuid.uuid4().hex
            data["reservations"][reservation_id] = {
                "amount": str(amount), "period": data["period"]}
            return reservation_id, None
        return self._transaction(operation)

    def settle(self, reservation_id, actual_cost):
        actual_cost = _money(actual_cost)

        def operation(data):
            item = data["reservations"].pop(reservation_id, None)
            if item is None:
                raise BudgetError("Unknown or already closed reservation")
            data["spent"] = str(_add(_money(data["spent"]), actual_cost))
            exceeded = (actual_cost > _money(item["amount"])
                        or actual_cost > self.request_limit
                        or self._usage(data) > self.monthly_limit)
            error = BudgetExceeded("Actual cost exceeded reserved budget; cost recorded") if exceeded else None
            return data, error
        return self._transaction(operation)

    def cancel_before_send(self, reservation_id):
        """Release only when the caller knows the request was never sent."""
        def operation(data):
            if data["reservations"].pop(reservation_id, None) is None:
                raise BudgetError("Unknown or already closed reservation")
            return data, None
        return self._transaction(operation)

    def snapshot(self):
        return self._transaction(lambda data: (data, None))
