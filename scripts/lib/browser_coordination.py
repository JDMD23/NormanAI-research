"""Cross-repository browser lease and Crunchbase work-item protocol."""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo


_PRODUCTION_SHARED_ROOT = (
    Path.home() / "Library/Application Support/NormanAI/shared"
)
DEFAULT_BROWSER_LOCK = _PRODUCTION_SHARED_ROOT / "browser.lock"
DEFAULT_CRUNCHBASE_BUDGET = (
    _PRODUCTION_SHARED_ROOT / "crunchbase-budget.json"
)
BUDGET_SCHEMA_VERSION = "norman.shared.crunchbase_budget.v3"
LEGACY_BUDGET_SCHEMAS = {
    "norman.shared.crunchbase_budget.v1": 25,
    "norman.shared.crunchbase_budget.v2": 40,
}
APPROVED_CEILING = 40
CORE_NAVIGATION_LIMIT = 5
GROUP_LIMITS = {"core": 30, "research": 10}
LANE_GROUPS = {
    "crm_crunchbase": "core",
    "research-funding-watcher": "research",
    "research-funding-bootstrap": "research",
}
WATCHER_LANE = "research-funding-watcher"
NEW_YORK = ZoneInfo("America/New_York")
SHARED_STATE_ENV = "NORMANAI_SHARED_STATE_DIR"


def shared_state_root() -> Path:
    configured = os.environ.get(SHARED_STATE_ENV)
    if configured:
        return Path(configured).expanduser()
    return _PRODUCTION_SHARED_ROOT


def _guard_test_state_path(path: Path) -> Path:
    candidate = path.expanduser()
    if os.environ.get("NORMANAI_TEST_MODE") != "1":
        return candidate
    try:
        candidate.resolve().relative_to(_PRODUCTION_SHARED_ROOT.resolve())
    except ValueError:
        return candidate
    raise RuntimeError(
        "tests may not use the production shared state directory"
    )


@dataclass(frozen=True)
class CoreCompanyReservation:
    granted: bool
    navigation_limit: int
    schema_version: str


class BrowserLeaseUnavailable(RuntimeError):
    """Another process owns the one shared logged-in browser."""


class SharedBrowserLease:
    """Non-blocking process-wide lease shared by Core and Research."""

    def __init__(self, path: Path | None = None):
        self.path = _guard_test_state_path(
            path or (shared_state_root() / "browser.lock")
        )
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

    def __exit__(
        self,
        exc_type: object,
        exc: object,
        tb: object,
    ) -> None:
        if self._fd is None:
            return
        fcntl.flock(self._fd, fcntl.LOCK_UN)
        os.close(self._fd)
        self._fd = None


class DailyCrunchbaseBudget:
    """Atomic New-York-day work budget shared with CRM Core."""

    def __init__(
        self,
        path: Path | None = None,
        ceiling: int = APPROVED_CEILING,
    ):
        if (
            not isinstance(ceiling, int)
            or isinstance(ceiling, bool)
            or ceiling != APPROVED_CEILING
        ):
            raise ValueError("Crunchbase budget ceiling must equal 40")
        self.path = _guard_test_state_path(
            path or (shared_state_root() / "crunchbase-budget.json")
        )
        self.ceiling = ceiling
        self.lock_path = self.path.with_name("crunchbase-budget.lock")

    def claim(
        self,
        now: datetime,
        *,
        requested: int,
        lane: str,
        exact: bool = False,
    ) -> int:
        _require_aware(now)
        _require_requested(requested)
        if not isinstance(exact, bool):
            raise ValueError("exact must be boolean")
        group = _require_lane(lane)
        if lane == WATCHER_LANE and requested > 2:
            raise ValueError(
                "funding watcher may reserve at most 2 checks"
            )
        with self._locked():
            payload = self._payload_for(now)
            accounting_lane = lane
            lane_remaining = payload["ceiling"]
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
            available = payload["ceiling"] - payload["used"]
            if payload["schemaVersion"] == BUDGET_SCHEMA_VERSION:
                available = min(
                    available,
                    GROUP_LIMITS[group] - _group_used(payload, group),
                )
            available = min(available, lane_remaining)
            granted = (
                0
                if exact and available < requested
                else min(requested, available)
            )
            payload["used"] += granted
            payload["lanes"][accounting_lane] = (
                payload["lanes"].get(accounting_lane, 0) + granted
            )
            _atomic_write_json(self.path, payload)
            return granted

    def claim_core_company_session(
        self,
        now: datetime,
    ) -> CoreCompanyReservation:
        _require_aware(now)
        lane = "crm_crunchbase"
        with self._locked():
            payload = self._payload_for(now)
            schema_version = str(payload["schemaVersion"])
            requested = (
                1
                if schema_version == BUDGET_SCHEMA_VERSION
                else CORE_NAVIGATION_LIMIT
            )
            available = payload["ceiling"] - payload["used"]
            if schema_version == BUDGET_SCHEMA_VERSION:
                available = min(
                    available,
                    GROUP_LIMITS["core"] - _group_used(payload, "core"),
                )
            if available < requested:
                return CoreCompanyReservation(
                    granted=False,
                    navigation_limit=0,
                    schema_version=schema_version,
                )
            payload["used"] += requested
            payload["lanes"][lane] = (
                payload["lanes"].get(lane, 0) + requested
            )
            _atomic_write_json(self.path, payload)
            return CoreCompanyReservation(
                granted=True,
                navigation_limit=CORE_NAVIGATION_LIMIT,
                schema_version=schema_version,
            )

    def snapshot(self, now: datetime) -> dict[str, Any]:
        _require_aware(now)
        with self._locked():
            return self._payload_for(now)

    def capacity(self, now: datetime) -> dict[str, Any]:
        payload = self.snapshot(now)
        total_remaining = payload["ceiling"] - payload["used"]
        if payload["schemaVersion"] != BUDGET_SCHEMA_VERSION:
            return {
                "schemaVersion": payload["schemaVersion"],
                "unit": "legacy_pages",
                "total": {
                    "used": payload["used"],
                    "limit": payload["ceiling"],
                    "remaining": total_remaining,
                },
                "core": {
                    "used": payload["lanes"].get("crm_crunchbase", 0),
                    "limit": payload["ceiling"],
                    "remaining": total_remaining // CORE_NAVIGATION_LIMIT,
                },
                "research": {
                    "used": _group_used(payload, "research"),
                    "limit": payload["ceiling"],
                    "remaining": total_remaining,
                },
            }
        return {
            "schemaVersion": payload["schemaVersion"],
            "unit": "work_items",
            "total": {
                "used": payload["used"],
                "limit": payload["ceiling"],
                "remaining": total_remaining,
            },
            **{
                group: {
                    "used": _group_used(payload, group),
                    "limit": limit,
                    "remaining": limit - _group_used(payload, group),
                }
                for group, limit in GROUP_LIMITS.items()
            },
        }

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
            payload = json.loads(
                self.path.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                "invalid shared Crunchbase budget"
            ) from exc
        if not isinstance(payload, dict):
            raise RuntimeError("invalid shared Crunchbase budget")
        persisted_date = _validate_budget(payload)
        if persisted_date < local_date:
            return _new_payload(local_date.isoformat())
        if persisted_date > local_date:
            raise RuntimeError("future-dated shared Crunchbase budget")
        return payload


def _new_payload(ledger_date: str) -> dict[str, Any]:
    return {
        "schemaVersion": BUDGET_SCHEMA_VERSION,
        "date": ledger_date,
        "ceiling": APPROVED_CEILING,
        "used": 0,
        "lanes": {},
    }


def _validate_budget(payload: dict[str, Any]) -> date:
    if set(payload) != {
        "schemaVersion",
        "date",
        "ceiling",
        "used",
        "lanes",
    }:
        raise RuntimeError("invalid shared Crunchbase budget shape")
    version_and_ceiling = (
        payload["schemaVersion"],
        payload["ceiling"],
    )
    supported = {
        (BUDGET_SCHEMA_VERSION, APPROVED_CEILING),
        *{
            (schema, ceiling)
            for schema, ceiling in LEGACY_BUDGET_SCHEMAS.items()
        },
    }
    if version_and_ceiling not in supported:
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
    effective_ceiling = payload["ceiling"]
    if (
        not isinstance(effective_ceiling, int)
        or isinstance(effective_ceiling, bool)
        or not isinstance(used, int)
        or isinstance(used, bool)
        or not 0 <= used <= effective_ceiling
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
    if payload["schemaVersion"] == BUDGET_SCHEMA_VERSION:
        if any(_lane_group(name) is None for name in lanes):
            raise RuntimeError("invalid shared Crunchbase lane")
        if any(
            _group_used(payload, group) > limit
            for group, limit in GROUP_LIMITS.items()
        ):
            raise RuntimeError(
                "invalid shared Crunchbase allocation"
            )
    return parsed_date


def _require_aware(now: datetime) -> None:
    if (
        not isinstance(now, datetime)
        or now.tzinfo is None
        or now.utcoffset() is None
    ):
        raise ValueError("datetime must be timezone-aware")


def _require_requested(requested: int) -> None:
    if (
        not isinstance(requested, int)
        or isinstance(requested, bool)
        or requested < 0
    ):
        raise ValueError("requested must be a non-negative integer")


def _require_lane(lane: str) -> str:
    if not isinstance(lane, str) or not lane.strip():
        raise ValueError("lane must be a non-empty string")
    group = _lane_group(lane)
    if group is None:
        raise ValueError(f"unapproved Crunchbase lane: {lane}")
    return group


def _lane_group(lane: str) -> str | None:
    prefix = f"{WATCHER_LANE}@"
    if lane.startswith(prefix):
        try:
            slot = datetime.fromisoformat(lane.removeprefix(prefix))
        except ValueError:
            return None
        if (
            slot.tzinfo is None
            or slot.utcoffset() is None
            or slot.minute != 0
            or slot.second != 0
            or slot.microsecond != 0
        ):
            return None
        return "research"
    return LANE_GROUPS.get(lane)


def _group_used(payload: dict[str, Any], group: str) -> int:
    return sum(
        amount
        for lane, amount in payload["lanes"].items()
        if _lane_group(lane) == group
    )


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
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
