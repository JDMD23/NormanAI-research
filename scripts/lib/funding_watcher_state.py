"""Durable, crash-safe state for the Research funding detector."""

from __future__ import annotations

import hashlib
import fcntl
import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence
from contextlib import contextmanager
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from lib.crunchbase_saved_list import FundingObservation, validate_saved_list_url


LEDGER_SCHEMA_VERSION = "norman.research.crunchbase_funding_ledger.v1"
LEGACY_SCHEMA_VERSION = "norman.crm_core.crunchbase_funding_ledger.v1"
NEW_YORK = ZoneInfo("America/New_York")
EVENT_STATES = {"observed", "handoff_pending", "retryable", "terminal"}
TERMINAL_OUTCOMES = {
    "baseline",
    "created",
    "queued_existing",
    "queued_drop",
    "duplicate_event",
    "rejected_identity",
    "ambiguous_review",
    "duplicate",
    "rejected_parse",
    "failed_terminal",
}
SHA256 = re.compile(r"[0-9a-f]{64}")


def funding_event_key(observation: FundingObservation) -> str:
    source_url = validate_saved_list_url(observation.source_url)
    organization_url = _canonical_organization_url(observation.crunchbase_url)
    if not organization_url:
        raise ValueError("funding event requires canonical Crunchbase identity")
    funding_date = observation.funding_date
    try:
        parsed_date = datetime.strptime(funding_date, "%Y-%m-%d")
    except (TypeError, ValueError) as exc:
        raise ValueError("funding event date must be YYYY-MM-DD") from exc
    if parsed_date.strftime("%Y-%m-%d") != funding_date:
        raise ValueError("funding event date must be YYYY-MM-DD")
    amount = observation.funding_amount_minor
    if (
        not isinstance(amount, int)
        or isinstance(amount, bool)
        or amount <= 0
    ):
        raise ValueError("funding amount must be positive integer minor units")
    currency = str(observation.funding_currency).strip().upper()
    if not (len(currency) == 3 and currency.isascii() and currency.isalpha()):
        raise ValueError("funding currency must be a three-letter ISO code")
    funding_type = " ".join(str(observation.funding_type).casefold().split())
    if not funding_type:
        raise ValueError("funding type is required")
    identity = {
        "source_url": source_url,
        "crunchbase_url": organization_url,
        "funding_date": funding_date,
        "funding_type": funding_type,
        "funding_amount_minor": amount,
        "funding_currency": currency,
    }
    encoded = json.dumps(
        identity, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class FundingWatcherLedger:
    """Atomic event ledger with explicit lifecycle transitions."""

    def __init__(self, path: Path):
        self.path = path
        if path.exists():
            payload = _load_json(path)
            _validate_research_ledger(payload)
            self._payload = payload
        else:
            self._payload = {
                "schemaVersion": LEDGER_SCHEMA_VERSION,
                "events": {},
                "bootstraps": {},
                "completedSlots": {},
            }

    @property
    def events(self) -> dict[str, dict[str, Any]]:
        return self._payload["events"]

    @property
    def bootstraps(self) -> dict[str, dict[str, Any]]:
        return self._payload["bootstraps"]

    def is_terminal(self, event_key: str) -> bool:
        return (self.events.get(event_key) or {}).get("state") == "terminal"

    def observe(
        self, event_key: str, *, observed_at: str, details: dict[str, Any] | None = None
    ) -> None:
        _require_event_key(event_key)
        existing = self.events.get(event_key)
        if existing and existing["state"] == "terminal":
            return
        self.events[event_key] = {
            "state": "observed",
            "observedAt": observed_at,
            "details": dict(details or {}),
        }
        self._save()

    def mark_handoff_pending(
        self, event_key: str, *, run_id: str, observed_at: str
    ) -> None:
        record = self._record(event_key)
        if record["state"] not in {"observed", "retryable", "handoff_pending"}:
            raise ValueError("handoff_pending requires an observed event")
        record.update(
            state="handoff_pending", runId=run_id, updatedAt=observed_at
        )
        self._save()

    def mark_retryable(
        self, event_key: str, *, reason: str, observed_at: str
    ) -> None:
        record = self._record(event_key)
        if record["state"] not in {"handoff_pending", "retryable"}:
            raise ValueError(
                "retryable requires a pending handoff and cannot replace terminal"
            )
        record.update(
            state="retryable", reason=reason, updatedAt=observed_at
        )
        self._save()

    def mark_terminal(
        self,
        event_key: str,
        *,
        outcome: str,
        page_id: str | None,
        observed_at: str,
    ) -> None:
        record = self._record(event_key)
        if outcome not in TERMINAL_OUTCOMES:
            raise ValueError("unsupported terminal outcome")
        required_state = "observed" if outcome == "baseline" else "handoff_pending"
        if record["state"] != required_state:
            raise ValueError(f"{outcome} terminal requires {required_state}")
        record.update(
            state="terminal",
            outcome=outcome,
            pageId=page_id,
            updatedAt=observed_at,
        )
        self._save()

    def mark_bootstrap_complete(self, source_url: str, completed_at: str) -> None:
        source_url = validate_saved_list_url(source_url)
        self.bootstraps[source_url] = {"completedAt": completed_at}
        self._save()

    def bootstrap_complete(self, source_url: str) -> bool:
        return source_url in self.bootstraps

    def slot_complete(self, slot: str) -> bool:
        return slot in self._payload["completedSlots"]

    def mark_slot_complete(self, slot: str, *, run_id: str) -> None:
        self._payload["completedSlots"][slot] = {"runId": run_id}
        self._save()

    def _record(self, event_key: str) -> dict[str, Any]:
        _require_event_key(event_key)
        try:
            return self.events[event_key]
        except KeyError as exc:
            raise ValueError("event must first be observed") from exc

    def _save(self) -> None:
        _atomic_write_json(self.path, self._payload)


def write_immutable_receipt(root: Path, payload: dict[str, Any]) -> Path:
    run_id = payload.get("runId")
    if (
        not isinstance(run_id, str)
        or not run_id
        or "/" in run_id
        or run_id in {".", ".."}
    ):
        raise ValueError("receipt runId is required")
    directory = root / "receipts"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{run_id}.json"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        parent = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return path


def new_york_slot(
    now: datetime, schedule_hours: Sequence[int]
) -> str | None:
    _require_aware(now)
    if any(
        not isinstance(hour, int)
        or isinstance(hour, bool)
        or not 0 <= hour <= 23
        for hour in schedule_hours
    ):
        raise ValueError("schedule hours must be integers from 0 to 23")
    local = now.astimezone(NEW_YORK)
    if local.hour not in set(schedule_hours):
        return None
    return local.replace(minute=0, second=0, microsecond=0).isoformat()


@contextmanager
def exclusive_run_lock(path: Path):
    """Yield whether a stable non-blocking process lock was acquired."""
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    acquired = False
    try:
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        if acquired:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def migrate_legacy_state(
    legacy_root: Path,
    research_root: Path,
    *,
    expected_source_url: str,
) -> dict[str, Any]:
    """Validate the entire legacy ledger before atomically creating Research state."""
    source = validate_saved_list_url(expected_source_url)
    legacy_path = legacy_root / "ledger.json"
    legacy = _load_json(legacy_path)
    if legacy.get("schemaVersion") != LEGACY_SCHEMA_VERSION:
        raise RuntimeError("unsupported legacy watcher schema")
    events = legacy.get("events")
    bootstraps = legacy.get("bootstraps")
    if not isinstance(events, dict) or not isinstance(bootstraps, dict):
        raise RuntimeError("invalid legacy watcher structure")
    if len(events) != 189:
        raise RuntimeError("legacy migration requires exactly 189 events")
    if set(bootstraps) != {source}:
        raise RuntimeError("legacy migration requires one exact bootstrap source")
    baseline = created = 0
    migrated_events: dict[str, dict[str, Any]] = {}
    for key, record in events.items():
        _require_event_key(key)
        if not isinstance(record, dict):
            raise RuntimeError("invalid legacy event record")
        state = record.get("state")
        if state == "baseline":
            baseline += 1
        elif state == "created":
            created += 1
        else:
            raise RuntimeError("legacy migration found unsupported event state")
        details = record.get("details")
        if not isinstance(details, dict):
            raise RuntimeError("invalid legacy event details")
        recorded_source = details.get("source_url")
        if (
            (state == "baseline" and recorded_source != source)
            or (recorded_source is not None and recorded_source != source)
        ):
            raise RuntimeError("legacy event source mismatch")
        migrated_events[key] = {
            "state": "terminal",
            "outcome": state,
            "pageId": details.get("page_id"),
            "observedAt": record.get("observed_at"),
            "updatedAt": record.get("observed_at"),
            "details": dict(details),
            "migration": {"legacyState": state},
        }
    if baseline != 179 or created != 10:
        raise RuntimeError("legacy migration requires 179 baseline and 10 created")
    completed = bootstraps[source]
    if not isinstance(completed, dict) or not isinstance(
        completed.get("completed_at"), str
    ):
        raise RuntimeError("invalid legacy bootstrap record")
    digest = hashlib.sha256(
        "\n".join(sorted(events)).encode("ascii")
    ).hexdigest()
    receipt = {
        "schemaVersion": "norman.research.funding_legacy_migration.v1",
        "legacyPath": str(legacy_path),
        "researchPath": str(research_root / "ledger.json"),
        "sourceUrl": source,
        "counts": {
            "events": 189,
            "baseline": 179,
            "created": 10,
            "bootstraps": 1,
        },
        "eventKeyDigest": digest,
    }
    new_payload = {
        "schemaVersion": LEDGER_SCHEMA_VERSION,
        "events": migrated_events,
        "bootstraps": {
            source: {"completedAt": completed["completed_at"], "migrated": True}
        },
        "completedSlots": {},
    }
    _validate_research_ledger(new_payload)

    ledger_path = research_root / "ledger.json"
    receipt_path = research_root / "migration-receipt.json"
    if ledger_path.exists() or receipt_path.exists():
        if not (ledger_path.exists() and receipt_path.exists()):
            raise RuntimeError("partial Research migration state")
        existing_ledger = _load_json(ledger_path)
        existing_receipt = _load_json(receipt_path)
        _validate_research_ledger(existing_ledger)
        migrated_bootstrap = new_payload["bootstraps"][source]
        migration_event_keys = {
            key
            for key, record in existing_ledger["events"].items()
            if "migration" in record
        }
        original_events_match = all(
            existing_ledger["events"].get(key) == record
            for key, record in migrated_events.items()
        )
        if (
            existing_receipt != receipt
            or not original_events_match
            or migration_event_keys != set(migrated_events)
            or existing_ledger["bootstraps"].get(source)
            != migrated_bootstrap
        ):
            raise RuntimeError("Research migration state does not match legacy")
        return receipt

    if research_root.exists():
        raise RuntimeError(
            "Research migration state directory exists without complete "
            "migration artifacts"
        )
    try:
        research_root.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise RuntimeError(
            "Research migration state directory exists without complete "
            "migration artifacts"
        ) from exc
    try:
        _atomic_write_json(ledger_path, new_payload)
        _atomic_write_json(receipt_path, receipt)
    except Exception:
        ledger_path.unlink(missing_ok=True)
        receipt_path.unlink(missing_ok=True)
        try:
            research_root.rmdir()
        except OSError:
            pass
        raise
    return receipt


def _canonical_organization_url(value: str) -> str:
    try:
        parsed = urlparse(value)
    except ValueError:
        return ""
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").casefold() != "www.crunchbase.com"
        or parsed.query
        or parsed.fragment
        or parsed.params
        or not re.fullmatch(
            r"/organization/[a-z0-9]+(?:-[a-z0-9]+)*",
            parsed.path.casefold(),
        )
    ):
        return ""
    slug = parsed.path.casefold().rsplit("/", 1)[-1]
    return f"crunchbase.com/organization/{slug}"


def _valid_aware_datetime(value: Any) -> bool:
    if not isinstance(value, str) or not value or value != value.strip():
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _valid_nonempty_raw_string(value: Any) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value == value.strip()
    )


def _valid_event_record(record: Any) -> bool:
    if not isinstance(record, dict):
        return False
    state = record.get("state")
    common = {"state", "observedAt", "details"}
    if (
        not isinstance(state, str)
        or state not in EVENT_STATES
        or not _valid_aware_datetime(record.get("observedAt"))
        or not isinstance(record.get("details"), dict)
    ):
        return False
    if state == "observed":
        return set(record) == common

    pending = common | {"runId", "updatedAt"}
    if not _valid_aware_datetime(record.get("updatedAt")):
        return False
    if state == "handoff_pending":
        if (
            not _valid_nonempty_raw_string(record.get("runId"))
            or set(record) not in (pending, pending | {"reason"})
        ):
            return False
        return (
            "reason" not in record
            or _valid_nonempty_raw_string(record["reason"])
        )
    if state == "retryable":
        return (
            set(record) == pending | {"reason"}
            and _valid_nonempty_raw_string(record.get("runId"))
            and _valid_nonempty_raw_string(record.get("reason"))
        )

    terminal_required = common | {"updatedAt", "outcome", "pageId"}
    terminal_allowed = terminal_required | {
        "runId",
        "reason",
        "migration",
    }
    outcome = record.get("outcome")
    page_id = record.get("pageId")
    if (
        not terminal_required.issubset(record)
        or not set(record).issubset(terminal_allowed)
        or not isinstance(outcome, str)
        or outcome not in TERMINAL_OUTCOMES
        or (
            page_id is not None
            and not _valid_nonempty_raw_string(page_id)
        )
        or (
            "reason" in record
            and not _valid_nonempty_raw_string(record["reason"])
        )
    ):
        return False
    migration = record.get("migration")
    if "migration" in record:
        if (
            not isinstance(migration, dict)
            or set(migration) != {"legacyState"}
            or not isinstance(migration["legacyState"], str)
            or migration["legacyState"] not in {"baseline", "created"}
            or outcome != migration["legacyState"]
            or "runId" in record
            or "reason" in record
        ):
            return False
    elif outcome == "baseline":
        if "runId" in record or "reason" in record:
            return False
    elif not _valid_nonempty_raw_string(record.get("runId")):
        return False
    if outcome in {"created", "queued_existing"}:
        return "migration" in record or _valid_nonempty_raw_string(page_id)
    return page_id is None


def _valid_bootstrap(source_url: Any, record: Any) -> bool:
    if not isinstance(source_url, str):
        return False
    try:
        canonical_source = validate_saved_list_url(source_url)
    except ValueError:
        return False
    return (
        canonical_source == source_url
        and isinstance(record, dict)
        and set(record) in ({"completedAt"}, {"completedAt", "migrated"})
        and _valid_aware_datetime(record.get("completedAt"))
        and ("migrated" not in record or record["migrated"] is True)
    )


def _valid_completed_slot(slot: Any, record: Any) -> bool:
    if not isinstance(slot, str) or not _valid_aware_datetime(slot):
        return False
    parsed = datetime.fromisoformat(slot.replace("Z", "+00:00"))
    expected = parsed.astimezone(NEW_YORK).replace(
        minute=0,
        second=0,
        microsecond=0,
    ).isoformat()
    return (
        slot == expected
        and isinstance(record, dict)
        and set(record) == {"runId"}
        and _valid_nonempty_raw_string(record.get("runId"))
    )


def _validate_research_ledger(payload: dict[str, Any]) -> None:
    if set(payload) != {
        "schemaVersion",
        "events",
        "bootstraps",
        "completedSlots",
    } or payload["schemaVersion"] != LEDGER_SCHEMA_VERSION:
        raise RuntimeError("invalid Research funding ledger")
    if not all(isinstance(payload[key], dict) for key in (
        "events", "bootstraps", "completedSlots"
    )):
        raise RuntimeError("invalid Research funding ledger structure")
    for key, record in payload["events"].items():
        try:
            _require_event_key(key)
        except ValueError as exc:
            raise RuntimeError("invalid Research funding ledger event key") from exc
        if not _valid_event_record(record):
            raise RuntimeError("invalid Research funding ledger event record")
    if any(
        not _valid_bootstrap(source_url, record)
        for source_url, record in payload["bootstraps"].items()
    ):
        raise RuntimeError("invalid Research funding ledger bootstrap")
    if any(
        not _valid_completed_slot(slot, record)
        for slot, record in payload["completedSlots"].items()
    ):
        raise RuntimeError("invalid Research funding ledger completed slot")


def _require_event_key(value: str) -> None:
    if not isinstance(value, str) or not SHA256.fullmatch(value):
        raise ValueError("event key must be a lowercase SHA-256 digest")


def _require_aware(now: datetime) -> None:
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid JSON state: {path}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"invalid JSON object: {path}")
    return payload


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
