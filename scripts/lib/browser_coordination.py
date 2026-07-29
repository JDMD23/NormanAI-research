"""Cross-repository browser lease and Crunchbase page-budget protocol."""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo


DEFAULT_BROWSER_LOCK = (
    Path.home() / "Library/Application Support/NormanAI/shared/browser.lock"
)
DEFAULT_CRUNCHBASE_BUDGET = (
    Path.home()
    / "Library/Application Support/NormanAI/shared/crunchbase-budget.json"
)
BUDGET_SCHEMA_VERSION = "norman.shared.crunchbase_budget.v1"
APPROVED_CEILING = 25
WATCHER_LANE = "research-funding-watcher"
NEW_YORK = ZoneInfo("America/New_York")


class BrowserLeaseUnavailable(RuntimeError):
    """Another process owns the one shared logged-in browser."""


class SharedBrowserLease:
    """Non-blocking process-wide lease shared by Core and Research."""

    def __init__(self, path: Path = DEFAULT_BROWSER_LOCK):
        self.path = path
        self._fd: int | None = None

    def __enter__(self) -> "SharedBrowserLease":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(self.path, flags, 0o600)
        try:
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            raise BrowserLeaseUnavailable(
                "shared browser lease is unavailable"
            ) from exc
        except Exception:
            os.close(fd)
            raise
        self._fd = fd
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self._fd is None:
            return
        fcntl.flock(self._fd, fcntl.LOCK_UN)
        os.close(self._fd)
        self._fd = None


class DailyCrunchbaseBudget:
    """Atomic New-York-day page reservation budget shared with CRM Core."""

    def __init__(
        self,
        path: Path = DEFAULT_CRUNCHBASE_BUDGET,
        ceiling: int = APPROVED_CEILING,
    ):
        if (
            not isinstance(ceiling, int)
            or isinstance(ceiling, bool)
            or ceiling != APPROVED_CEILING
        ):
            raise ValueError("Crunchbase budget ceiling must equal 25")
        self.path = path
        self.ceiling = ceiling
        self.lock_path = path.with_name("crunchbase-budget.lock")

    def claim(self, now: datetime, *, requested: int, lane: str) -> int:
        _require_aware(now)
        if (
            not isinstance(requested, int)
            or isinstance(requested, bool)
            or requested < 0
        ):
            raise ValueError("requested must be a non-negative integer")
        if not isinstance(lane, str) or not lane.strip():
            raise ValueError("lane must be a non-empty string")
        if lane == WATCHER_LANE and requested > 2:
            raise ValueError("funding watcher may reserve at most 2 pages")
        with self._locked():
            payload = self._payload_for(now)
            accounting_lane = lane
            lane_remaining = self.ceiling
            if lane == WATCHER_LANE:
                slot = now.astimezone(NEW_YORK).replace(
                    minute=0, second=0, microsecond=0
                )
                accounting_lane = f"{lane}@{slot.isoformat()}"
                lane_used = (
                    payload["lanes"].get(lane, 0)
                    + payload["lanes"].get(accounting_lane, 0)
                )
                lane_remaining = max(0, 2 - lane_used)
            granted = min(
                requested,
                self.ceiling - payload["used"],
                lane_remaining,
            )
            payload["used"] += granted
            payload["lanes"][accounting_lane] = (
                payload["lanes"].get(accounting_lane, 0) + granted
            )
            _atomic_write_json(self.path, payload)
            return granted

    def snapshot(self, now: datetime) -> dict[str, Any]:
        _require_aware(now)
        with self._locked():
            return self._payload_for(now)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(self.lock_path, flags, 0o600)
        try:
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _payload_for(self, now: datetime) -> dict[str, Any]:
        local_date = now.astimezone(NEW_YORK).date()
        if not self.path.exists():
            return _new_payload(local_date.isoformat())
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("invalid shared Crunchbase budget") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("invalid shared Crunchbase budget")
        persisted_date = _validate_budget(payload)
        if persisted_date < local_date:
            return _new_payload(local_date.isoformat())
        if persisted_date > local_date:
            raise RuntimeError("future-dated shared Crunchbase budget")
        return payload


def _new_payload(date: str) -> dict[str, Any]:
    return {
        "schemaVersion": BUDGET_SCHEMA_VERSION,
        "date": date,
        "ceiling": APPROVED_CEILING,
        "used": 0,
        "lanes": {},
    }


def _validate_budget(payload: dict[str, Any]) -> date:
    if set(payload) != {"schemaVersion", "date", "ceiling", "used", "lanes"}:
        raise RuntimeError("invalid shared Crunchbase budget shape")
    if (
        payload["schemaVersion"] != BUDGET_SCHEMA_VERSION
        or payload["ceiling"] != APPROVED_CEILING
    ):
        raise RuntimeError("unsupported shared Crunchbase budget")
    persisted_date = payload["date"]
    if not isinstance(persisted_date, str):
        raise RuntimeError("invalid shared Crunchbase budget values")
    try:
        parsed_date = date.fromisoformat(persisted_date)
    except ValueError as exc:
        raise RuntimeError("invalid shared Crunchbase budget values") from exc
    if parsed_date.isoformat() != persisted_date:
        raise RuntimeError("invalid shared Crunchbase budget values")
    used, lanes = payload["used"], payload["lanes"]
    if (
        not isinstance(used, int)
        or isinstance(used, bool)
        or not 0 <= used <= APPROVED_CEILING
        or not isinstance(lanes, dict)
        or any(
            not isinstance(name, str)
            or not name.strip()
            or not isinstance(count, int)
            or isinstance(count, bool)
            or count < 0
            for name, count in lanes.items()
        )
        or sum(lanes.values()) != used
    ):
        raise RuntimeError("invalid shared Crunchbase budget values")
    return parsed_date


def _require_aware(now: datetime) -> None:
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
