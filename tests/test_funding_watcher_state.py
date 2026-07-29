from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from lib.crunchbase_saved_list import FundingObservation
from lib.funding_watcher_state import (
    FundingWatcherLedger,
    funding_event_key,
    migrate_legacy_state,
    new_york_slot,
    write_immutable_receipt,
)


NEW_YORK = ZoneInfo("America/New_York")
SOURCE = (
    "https://www.crunchbase.com/discover/saved/"
    "main-funding-july-2026/730c458b-149c-4a0a-9684-7146e7258993"
)


def observation(**overrides: object) -> FundingObservation:
    payload: dict[str, object] = {
        "source_name": "Main Funding - July 2026",
        "source_url": SOURCE,
        "observed_at": "2026-07-29T14:00:00+00:00",
        "company": "Weave",
        "crunchbase_url": "https://www.crunchbase.com/organization/weave-f27a",
        "funding_date": "2026-07-29",
        "funding_type": "Series A",
        "funding_amount_raw": "$13.5M",
        "funding_amount_minor": 1_350_000_000,
        "funding_currency": "USD",
        "total_funding_raw": "$14M",
        "total_funding_amount_minor": 1_400_000_000,
        "total_funding_currency": "USD",
        "number_of_funding_rounds": 2,
        "website": "https://weave.example",
        "linkedin": "https://linkedin.com/company/weave",
        "headquarters": "New York",
        "founded": "2024",
        "description": "Description",
        "industries": ("Artificial Intelligence",),
        "founders": ("Founder",),
        "investors": ("Investor",),
    }
    payload.update(overrides)
    return FundingObservation(**payload)  # type: ignore[arg-type]


def _legacy_payload(*, source: str = SOURCE) -> dict[str, object]:
    states = ["created"] * 10 + ["baseline"] * 179
    events = {
        hashlib.sha256(f"event-{index}".encode()).hexdigest(): {
            "state": state,
            "observed_at": "2026-07-29T12:06:04+00:00",
            "details": {
                "company": f"Company {index}",
                "crunchbase_url": (
                    f"https://www.crunchbase.com/organization/company-{index}"
                ),
                "source_url": source,
                **({"page_id": f"page-{index}"} if state == "created" else {}),
            },
        }
        for index, state in enumerate(states)
    }
    return {
        "schemaVersion": "norman.crm_core.crunchbase_funding_ledger.v1",
        "events": events,
        "bootstraps": {
            source: {"completed_at": "2026-07-29T12:06:04+00:00"}
        },
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_url", SOURCE.replace("main-funding", "other-funding")),
        ("crunchbase_url", "https://www.crunchbase.com/organization/other"),
        ("funding_date", "2026-07-30"),
        ("funding_type", "Series B"),
        ("funding_amount_minor", 1_350_000_001),
        ("funding_currency", "GBP"),
    ],
)
def test_event_key_contains_all_six_identity_fields(
    field: str, value: object
) -> None:
    first = observation()
    assert funding_event_key(first) != funding_event_key(
        replace(first, **{field: value})
    )


def test_event_key_ignores_descriptive_fields_and_display_formatting() -> None:
    first = observation()
    changed = replace(
        first,
        company="WEAVE",
        description="New prose",
        investors=("Different Investor",),
        funding_amount_raw="$13,500,000",
        funding_type="  SERIES   A ",
    )
    assert funding_event_key(first) == funding_event_key(changed)


def test_event_key_matches_the_canonical_core_contract_fixture() -> None:
    row = observation(
        company="Example",
        crunchbase_url="https://www.crunchbase.com/organization/example",
        funding_date="2026-07-28",
        funding_type="Series A",
        funding_amount_raw="$10,000,000",
        funding_amount_minor=1_000_000_000,
        funding_currency="USD",
    )
    assert funding_event_key(row) == (
        "85c24e86a3de5679ecb4ce9abd86109d47d3981f4b1407f59c0ab1097aaed6af"
    )


def test_ledger_enforces_event_lifecycle_and_retryable_is_not_terminal(
    tmp_path: Path,
) -> None:
    """Catches transitions that exist only in memory or mark retries terminal."""
    event_key = hashlib.sha256(b"event").hexdigest()
    path = tmp_path / "ledger.json"
    ledger = FundingWatcherLedger(path)
    ledger.observe(event_key, observed_at="2026-07-29T14:00:00+00:00")
    ledger.mark_handoff_pending(
        event_key, run_id="run-1", observed_at="2026-07-29T14:01:00+00:00"
    )
    ledger.mark_retryable(
        event_key, reason="timeout", observed_at="2026-07-29T14:02:00+00:00"
    )
    ledger = FundingWatcherLedger(path)
    assert ledger.events[event_key]["state"] == "retryable"
    assert not ledger.is_terminal(event_key)
    ledger.mark_handoff_pending(
        event_key, run_id="run-2", observed_at="2026-07-29T14:03:00+00:00"
    )
    ledger.mark_terminal(
        event_key,
        outcome="created",
        page_id="page-1",
        observed_at="2026-07-29T14:04:00+00:00",
    )
    ledger = FundingWatcherLedger(path)
    assert ledger.events[event_key]["state"] == "terminal"
    assert ledger.is_terminal(event_key)

    with pytest.raises(ValueError, match="terminal"):
        ledger.mark_retryable(
            event_key, reason="late", observed_at="2026-07-29T14:05:00+00:00"
        )


def test_invalid_lifecycle_transition_is_rejected(tmp_path: Path) -> None:
    ledger = FundingWatcherLedger(tmp_path / "ledger.json")
    event_key = hashlib.sha256(b"missing").hexdigest()
    with pytest.raises(ValueError, match="observed"):
        ledger.mark_handoff_pending(
            event_key, run_id="run", observed_at="2026-07-29T14:00:00+00:00"
        )


def test_immutable_receipt_refuses_overwrite(tmp_path: Path) -> None:
    """Catches receipts moving outside receipts/<runId>.json or overwriting."""
    payload = {"runId": "20260729T140000Z", "status": "complete"}
    receipt = write_immutable_receipt(tmp_path, payload)
    assert receipt == tmp_path / "receipts" / "20260729T140000Z.json"
    assert json.loads(receipt.read_text(encoding="utf-8")) == payload
    with pytest.raises(FileExistsError):
        write_immutable_receipt(tmp_path, payload)


def test_new_york_slot_normalizes_timezone() -> None:
    local = datetime(2026, 7, 29, 13, 4, tzinfo=NEW_YORK)
    utc = datetime.fromisoformat("2026-07-29T17:04:00+00:00")
    assert new_york_slot(local, [6, 10, 13, 16, 19]) == (
        "2026-07-29T13:00:00-04:00"
    )
    assert new_york_slot(utc, [6, 10, 13, 16, 19]) == (
        "2026-07-29T13:00:00-04:00"
    )
    assert new_york_slot(
        datetime(2026, 7, 29, 14, 4, tzinfo=NEW_YORK),
        [6, 10, 13, 16, 19],
    ) is None


def test_migration_is_read_only_exact_and_idempotent(tmp_path: Path) -> None:
    """Catches legacy mutation, key loss, or incomplete migration receipts."""
    legacy = tmp_path / "legacy"
    research = tmp_path / "research"
    legacy.mkdir()
    legacy_path = legacy / "ledger.json"
    legacy_path.write_text(
        json.dumps(_legacy_payload(), sort_keys=True), encoding="utf-8"
    )
    legacy_note = legacy / "operator-note.txt"
    legacy_note.write_text("leave this file alone\n", encoding="utf-8")
    before = {
        path.relative_to(legacy): path.read_bytes()
        for path in sorted(legacy.rglob("*"))
        if path.is_file()
    }

    first = migrate_legacy_state(
        legacy, research, expected_source_url=SOURCE
    )
    second = migrate_legacy_state(
        legacy, research, expected_source_url=SOURCE
    )

    assert first == second
    assert {
        path.relative_to(legacy): path.read_bytes()
        for path in sorted(legacy.rglob("*"))
        if path.is_file()
    } == before
    assert first == {
        "schemaVersion": "norman.research.funding_legacy_migration.v1",
        "legacyPath": str(legacy_path),
        "researchPath": str(research / "ledger.json"),
        "sourceUrl": SOURCE,
        "counts": {
            "events": 189,
            "baseline": 179,
            "created": 10,
            "bootstraps": 1,
        },
        "eventKeyDigest": (
            "e314bbac273ab87ed46962916b35827ec50f52c78be9b81b90b4c65042c074ad"
        ),
    }
    assert first["counts"] == {
        "events": 189,
        "baseline": 179,
        "created": 10,
        "bootstraps": 1,
    }
    migrated = FundingWatcherLedger(research / "ledger.json")
    assert set(migrated.events) == set(_legacy_payload()["events"])
    assert sum(migrated.is_terminal(key) for key in migrated.events) == 189
    assert (research / "migration-receipt.json").exists()


def test_migration_accepts_legacy_created_rows_without_repeated_source(
    tmp_path: Path,
) -> None:
    legacy = tmp_path / "legacy"
    research = tmp_path / "research"
    legacy.mkdir()
    payload = _legacy_payload()
    for record in payload["events"].values():
        if record["state"] == "created":
            record["details"].pop("source_url")
    (legacy / "ledger.json").write_text(json.dumps(payload), encoding="utf-8")

    receipt = migrate_legacy_state(
        legacy, research, expected_source_url=SOURCE
    )
    assert receipt["counts"]["created"] == 10


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.update(schemaVersion="wrong"),
        lambda payload: payload["events"].pop(next(iter(payload["events"]))),
        lambda payload: payload["events"].update(
            {
                hashlib.sha256(b"extra").hexdigest(): {
                    "state": "baseline",
                    "observed_at": "2026-07-29T12:06:04+00:00",
                    "details": {"source_url": SOURCE},
                }
            }
        ),
        lambda payload: payload["bootstraps"].update(
            {"https://www.crunchbase.com/discover/saved/other/id": {}}
        ),
        lambda payload: next(
            record
            for record in payload["events"].values()
            if record["state"] == "created"
        ).update(state="baseline"),
        lambda payload: next(iter(payload["events"].values()))["details"].update(
            source_url=SOURCE.replace("main-funding", "other-funding")
        ),
        lambda payload: payload["events"].update(
            {"not-a-sha256": payload["events"].pop(next(iter(payload["events"])))}
        ),
    ],
)
def test_migration_mismatch_aborts_before_writing(
    tmp_path: Path, mutate
) -> None:
    """Catches validation that writes before rejecting malformed legacy state."""
    legacy = tmp_path / "legacy"
    research = tmp_path / "research"
    legacy.mkdir()
    payload = _legacy_payload()
    mutate(payload)
    (legacy / "ledger.json").write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises((RuntimeError, ValueError)):
        migrate_legacy_state(legacy, research, expected_source_url=SOURCE)
    assert not research.exists()
