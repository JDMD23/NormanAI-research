from __future__ import annotations

import contextlib
import hashlib
import json
import subprocess
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import funding_watcher
from lib.browser_coordination import DailyCrunchbaseBudget
from lib.crunchbase_saved_list import (
    CrunchbaseSavedListBlocked,
    CrunchbaseSavedListDrift,
    FundingObservation,
    SavedListSnapshot,
)
from lib.funding_watcher_state import FundingWatcherLedger, funding_event_key
from funding_watcher import WatcherDependencies, run_bootstrap, run_check


NEW_YORK = ZoneInfo("America/New_York")
NOW = datetime(2026, 7, 29, 10, 5, tzinfo=NEW_YORK)
SOURCE = (
    "https://www.crunchbase.com/discover/saved/"
    "main-funding-july-2026/730c458b-149c-4a0a-9684-7146e7258993"
)


def observation(index: int = 0) -> FundingObservation:
    return FundingObservation(
        source_name="Main Funding - July 2026",
        source_url=SOURCE,
        observed_at="2026-07-29T14:05:00+00:00",
        company=f"Company {index}",
        crunchbase_url=f"https://www.crunchbase.com/organization/company-{index}",
        funding_date="2026-07-28",
        funding_type="Series A",
        funding_amount_raw=f"${10 + index}M",
        funding_amount_minor=(10 + index) * 100_000_000,
        funding_currency="USD",
        total_funding_raw=f"${12 + index}M",
        total_funding_amount_minor=None,
        total_funding_currency="USD",
        number_of_funding_rounds=2,
        website=f"https://company-{index}.example",
        linkedin=f"https://linkedin.com/company/company-{index}",
        headquarters="New York",
        founded="2024",
        description="Description",
        industries=("Software",),
        founders=("Founder",),
        investors=("Investor",),
    )


class FakeBrowser:
    def __init__(
        self,
        rows: list[FundingObservation],
        *,
        rejections: int = 0,
        result_count: int | None = None,
        error: Exception | None = None,
    ):
        self.rows = rows
        self.rejections = rejections
        self.result_count = result_count
        self.error = error
        self.calls: list[int] = []

    def read_source(self, source, *, observed_at: str, max_pages: int):
        self.calls.append(max_pages)
        if self.error:
            raise self.error
        return SavedListSnapshot(
            source=source,
            title="Main Funding - July 2026",
            result_type="Companies",
            new_at_top=True,
            filter_text="Funding date after Jul 1 2026 $5M+",
            result_count=(
                self.result_count
                if self.result_count is not None
                else len(self.rows) + self.rejections
            ),
            page_count=max_pages,
            top_funding_date=self.rows[0].funding_date if self.rows else None,
            observations=tuple(self.rows),
            rejections=tuple(
                {"company": f"Rejected {i}", "reason": "invalid"}
                for i in range(self.rejections)
            ),
        )


class FakeBudget:
    def __init__(self, granted: int = 25):
        self.granted = granted
        self.claims: list[int] = []

    def claim(self, now, *, requested: int, lane: str):
        self.claims.append(requested)
        return min(requested, self.granted)


def config(tmp_path: Path, *, enabled: bool = True) -> dict:
    from lib.crunchbase_saved_list import load_watcher_config
    import json

    payload = {
        "schemaVersion": "norman.research.crunchbase_funding_watcher.v1",
        "enabled": enabled,
        "timeZone": "America/New_York",
        "scheduleHours": [6, 10, 13, 16, 19],
        "scheduledMaxPagesPerSource": 2,
        "bootstrapMaxPagesPerSource": 6,
        "bootstrapSeedTop": 10,
        "dailyPageLoadCeiling": 25,
        "stateDirectory": str(tmp_path / "state"),
        "legacyStateDirectory": str(tmp_path / "legacy"),
        "crmResultSchemaVersion": "norman.crm_core.funding_handoff_result.v1",
        "sources": [
            {
                "name": "Main Funding - July 2026",
                "url": SOURCE,
                "expectedResultType": "Companies",
                "expectedSort": "NEW AT TOP",
                "expectedFundingAfter": "2026-07-01",
                "expectedMinimumAmount": 5_000_000,
            }
        ],
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return load_watcher_config(path)


def core_result(
    request: dict,
    *,
    existing: bool = False,
    write: bool = True,
) -> dict:
    request_digest = hashlib.sha256(
        (
            json.dumps(
                request,
                indent=2,
                sort_keys=True,
                ensure_ascii=False,
            )
            + "\n"
        ).encode("utf-8")
    ).hexdigest()
    return {
        "schemaVersion": "norman.crm_core.funding_handoff_result.v1",
        "runId": request["runId"],
        "requestDigest": request_digest,
        "mode": "write" if write else "dry_run",
        "complete": True,
        "events": [
            {
                "eventKey": event["eventKey"],
                "state": "queued_existing" if existing else "created",
                "reason": "existing" if existing else "new",
                "pageId": f"page-{index}",
            }
            for index, event in enumerate(request["events"])
        ],
    }


def dependencies(
    tmp_path: Path,
    browser: FakeBrowser,
    *,
    budget: FakeBudget | None = None,
    invoke=None,
    lock_acquired: bool = True,
) -> WatcherDependencies:
    ledger = FundingWatcherLedger(tmp_path / "state" / "ledger.json")
    return WatcherDependencies(
        browser=browser,
        budget=budget or FakeBudget(),
        ledger=ledger,
        invoke_handoff=invoke
        or (lambda request, write: core_result(request, write=write)),
        browser_lease=lambda: contextlib.nullcontext(),
        run_lock=lambda: contextlib.nullcontext(lock_acquired),
        notify=lambda names: None,
    )


def test_outside_slot_and_disabled_never_touch_budget_or_browser(
    tmp_path: Path,
) -> None:
    browser, budget = FakeBrowser([]), FakeBudget()
    deps = dependencies(tmp_path, browser, budget=budget)
    receipt = run_check(
        config(tmp_path),
        deps,
        write=True,
        now=datetime(2026, 7, 29, 11, tzinfo=NEW_YORK),
        enforce_schedule=True,
    )
    assert receipt["status"] == "outside_schedule"
    assert not browser.calls and not budget.claims

    receipt = run_check(
        config(tmp_path, enabled=False),
        deps,
        write=True,
        now=NOW,
        enforce_schedule=True,
    )
    assert receipt["status"] == "disabled"
    assert not browser.calls and not budget.claims


def test_second_successful_run_in_same_slot_does_no_work(tmp_path: Path) -> None:
    browser = FakeBrowser([])
    deps = dependencies(tmp_path, browser)
    first = run_check(
        config(tmp_path), deps, write=True, now=NOW, enforce_schedule=True
    )
    second = run_check(
        config(tmp_path), deps, write=True, now=NOW, enforce_schedule=True
    )
    assert first["status"] == "complete"
    assert second["status"] == "already_checked_slot"
    assert len(browser.calls) == 1


def test_busy_and_budget_exhausted_are_distinct_retryable_receipts(
    tmp_path: Path,
) -> None:
    busy = dependencies(tmp_path, FakeBrowser([]), lock_acquired=False)
    assert run_check(
        config(tmp_path), busy, write=True, now=NOW, enforce_schedule=True
    )["status"] == "busy"

    exhausted = dependencies(
        tmp_path, FakeBrowser([]), budget=FakeBudget(granted=0)
    )
    receipt = run_check(
        config(tmp_path), exhausted, write=True, now=NOW, enforce_schedule=True
    )
    assert receipt["status"] == "budget_exhausted"
    assert not exhausted.browser.calls


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (CrunchbaseSavedListBlocked("captcha", "evidence"), "captcha"),
        (CrunchbaseSavedListBlocked("auth_wall", "evidence"), "auth_wall"),
        (CrunchbaseSavedListDrift("partial pagination"), "source_drift"),
    ],
)
def test_browser_failures_are_classified(
    tmp_path: Path, error: Exception, status: str
) -> None:
    receipt = run_check(
        config(tmp_path),
        dependencies(tmp_path, FakeBrowser([], error=error)),
        write=True,
        now=NOW,
        enforce_schedule=True,
    )
    assert receipt["status"] == status


def test_dry_run_previews_new_event_without_mutating_ledger(
    tmp_path: Path,
) -> None:
    row = observation()
    calls: list[tuple[dict, bool]] = []
    deps = dependencies(
        tmp_path,
        FakeBrowser([row]),
        invoke=lambda request, write: (
            calls.append((request, write))
            or core_result(request, write=write)
        ),
    )
    before = dict(deps.ledger.events)
    receipt = run_check(
        config(tmp_path), deps, write=False, now=NOW, enforce_schedule=False
    )
    assert receipt["status"] == "complete"
    assert receipt["counts"]["would_handoff"] == 1
    assert calls[0][1] is False
    assert deps.ledger.events == before


def test_live_check_records_pending_before_core_and_terminal_after(
    tmp_path: Path,
) -> None:
    row = observation()
    event_key = funding_event_key(row)
    deps: WatcherDependencies

    def invoke(request: dict, write: bool):
        assert deps.ledger.events[event_key]["state"] == "handoff_pending"
        return core_result(request, write=write)

    deps = dependencies(tmp_path, FakeBrowser([row]), invoke=invoke)
    receipt = run_check(
        config(tmp_path), deps, write=True, now=NOW, enforce_schedule=False
    )
    assert receipt["events"][0]["state"] == "created"
    assert deps.ledger.is_terminal(event_key)


def test_core_retry_leaves_event_retryable(tmp_path: Path) -> None:
    row = observation()

    def invoke(request: dict, write: bool):
        raise RuntimeError("CRM handoff retryable timeout")

    deps = dependencies(tmp_path, FakeBrowser([row]), invoke=invoke)
    receipt = run_check(
        config(tmp_path), deps, write=True, now=NOW, enforce_schedule=False
    )
    assert receipt["status"] == "crm_retryable"
    assert deps.ledger.events[funding_event_key(row)]["state"] == "retryable"


def test_invalid_core_result_is_configuration_failure_but_remains_retryable(
    tmp_path: Path,
) -> None:
    row = observation()

    def invoke(request: dict, write: bool):
        result = core_result(request, write=write)
        result["events"][0]["eventKey"] = "f" * 64
        return result

    deps = dependencies(tmp_path, FakeBrowser([row]), invoke=invoke)
    receipt = run_check(
        config(tmp_path), deps, write=True, now=NOW, enforce_schedule=False
    )
    assert receipt["status"] == "result_schema_mismatch"
    assert deps.ledger.events[funding_event_key(row)]["state"] == "retryable"


def test_existing_and_new_results_are_preserved(tmp_path: Path) -> None:
    first, second = observation(1), observation(2)

    def invoke(request: dict, write: bool):
        result = core_result(request, write=write)
        result["events"][1]["state"] = "queued_existing"
        result["events"][1]["reason"] = "existing"
        return result

    deps = dependencies(tmp_path, FakeBrowser([first, second]), invoke=invoke)
    receipt = run_check(
        config(tmp_path), deps, write=True, now=NOW, enforce_schedule=False
    )
    assert [event["state"] for event in receipt["events"]] == [
        "created",
        "queued_existing",
    ]


def test_imported_terminal_events_are_not_replayed(tmp_path: Path) -> None:
    rows = [observation(i) for i in range(10)]
    deps = dependencies(tmp_path, FakeBrowser(rows))
    for row in rows:
        key = funding_event_key(row)
        deps.ledger.observe(key, observed_at=row.observed_at)
        deps.ledger.mark_handoff_pending(
            key, run_id="imported", observed_at=row.observed_at
        )
        deps.ledger.mark_terminal(
            key,
            outcome="created",
            page_id="page",
            observed_at=row.observed_at,
        )
    calls: list[dict] = []
    deps.invoke_handoff = lambda request, write: calls.append(request)
    receipt = run_check(
        config(tmp_path), deps, write=True, now=NOW, enforce_schedule=False
    )
    assert receipt["counts"]["already_terminal"] == 10
    assert calls == []


def test_bootstrap_requires_complete_coverage_before_any_write(
    tmp_path: Path,
) -> None:
    rows = [observation(i) for i in range(11)]
    deps = dependencies(
        tmp_path, FakeBrowser(rows, result_count=12, rejections=0)
    )
    receipt = run_bootstrap(
        config(tmp_path),
        deps,
        seed_top=10,
        write=True,
        now=NOW,
    )
    assert receipt["status"] == "source_drift"
    assert deps.ledger.events == {}


def test_bootstrap_handoffs_top_ten_then_baselines_remainder(
    tmp_path: Path,
) -> None:
    rows = [observation(i) for i in range(189)]
    names: list[str] = []
    deps = dependencies(tmp_path, FakeBrowser(rows))
    deps.notify = lambda values: names.extend(values)
    receipt = run_bootstrap(
        config(tmp_path),
        deps,
        seed_top=10,
        write=True,
        now=NOW,
    )
    assert receipt["counts"] == {
        "created": 10,
        "queued_existing": 0,
        "baselined": 179,
        "already_terminal": 0,
    }
    assert len(deps.ledger.events) == 189
    assert all(deps.ledger.is_terminal(key) for key in deps.ledger.events)
    assert names == [
        f"Company {index} — https://www.crunchbase.com/organization/company-{index}"
        for index in range(10)
    ]


def test_bootstrap_resumes_after_top_ten_were_terminal_before_baseline(
    tmp_path: Path,
) -> None:
    rows = [observation(i) for i in range(12)]
    deps = dependencies(tmp_path, FakeBrowser(rows))
    for row in rows[:10]:
        key = funding_event_key(row)
        deps.ledger.observe(key, observed_at=row.observed_at)
        deps.ledger.mark_handoff_pending(
            key, run_id="bootstrap-resume", observed_at=row.observed_at
        )
        deps.ledger.mark_terminal(
            key,
            outcome="created",
            page_id="page",
            observed_at=row.observed_at,
        )
    receipt = run_bootstrap(
        config(tmp_path),
        deps,
        seed_top=10,
        write=True,
        now=NOW,
    )
    assert receipt["status"] == "complete"
    assert receipt["counts"]["already_terminal"] == 10
    assert receipt["counts"]["baselined"] == 2
    assert deps.ledger.bootstrap_complete(SOURCE)


def test_dry_bootstrap_does_not_record_state(tmp_path: Path) -> None:
    rows = [observation(i) for i in range(12)]
    deps = dependencies(tmp_path, FakeBrowser(rows))
    receipt = run_bootstrap(
        config(tmp_path),
        deps,
        seed_top=10,
        write=False,
        now=NOW,
    )
    assert receipt["counts"]["would_baseline"] == 2
    assert deps.ledger.events == {}
    assert not deps.ledger.bootstrap_complete(SOURCE)


def test_already_bootstrapped_does_not_touch_budget_or_browser(
    tmp_path: Path,
) -> None:
    browser, budget = FakeBrowser([]), FakeBudget()
    deps = dependencies(tmp_path, browser, budget=budget)
    deps.ledger.mark_bootstrap_complete(SOURCE, NOW.isoformat())

    receipt = run_bootstrap(
        config(tmp_path),
        deps,
        seed_top=10,
        write=True,
        now=NOW,
    )

    assert receipt["status"] == "already_bootstrapped"
    assert browser.calls == []
    assert budget.claims == []


def test_no_observations_is_complete_zero_change(tmp_path: Path) -> None:
    receipt = run_check(
        config(tmp_path),
        dependencies(tmp_path, FakeBrowser([])),
        write=True,
        now=NOW,
        enforce_schedule=False,
    )
    assert receipt["status"] == "complete"
    assert receipt["counts"]["new_events"] == 0


def test_retry_in_same_scheduled_slot_cannot_reserve_more_than_two_pages(
    tmp_path: Path,
) -> None:
    row = observation()
    budget = DailyCrunchbaseBudget(tmp_path / "shared-budget.json")

    def retryable_core(request: dict, write: bool) -> dict:
        raise RuntimeError("CRM retry")

    browser = FakeBrowser([row])
    deps = dependencies(
        tmp_path,
        browser,
        budget=budget,
        invoke=retryable_core,
    )

    first = run_check(
        config(tmp_path),
        deps,
        write=True,
        now=NOW,
        enforce_schedule=True,
    )
    second = run_check(
        config(tmp_path),
        deps,
        write=True,
        now=NOW,
        enforce_schedule=True,
    )

    assert first["status"] == "crm_retryable"
    assert second["status"] == "budget_exhausted"
    assert browser.calls == [2]
    snapshot = budget.snapshot(NOW)
    assert snapshot["used"] == 2
    assert snapshot["lanes"] == {
        "research-funding-watcher@2026-07-29T10:00:00-04:00": 2
    }


def test_check_rejects_incomplete_snapshot_before_diff_or_core(
    tmp_path: Path,
) -> None:
    calls: list[dict] = []
    deps = dependencies(
        tmp_path,
        FakeBrowser([observation(1), observation(2)], result_count=3),
        invoke=lambda request, write: calls.append(request),
    )

    receipt = run_check(
        config(tmp_path),
        deps,
        write=True,
        now=NOW,
        enforce_schedule=False,
    )

    assert receipt["status"] == "source_drift"
    assert receipt["stopReason"] == "missing_terminal_high_water_anchor"
    assert calls == []
    assert deps.ledger.events == {}


def _mark_terminal(
    ledger: FundingWatcherLedger,
    row: FundingObservation,
    *,
    run_id: str = "prior-run",
) -> None:
    key = funding_event_key(row)
    ledger.observe(key, observed_at=row.observed_at)
    ledger.mark_handoff_pending(
        key,
        run_id=run_id,
        observed_at=row.observed_at,
    )
    ledger.mark_terminal(
        key,
        outcome="created",
        page_id=f"page-{key[:8]}",
        observed_at=row.observed_at,
    )


def test_truncated_new_at_top_snapshot_hands_off_only_prefix_before_anchor_once(
    tmp_path: Path,
) -> None:
    rows = [observation(index) for index in range(100)]
    requests: list[dict] = []
    deps = dependencies(tmp_path, FakeBrowser(rows, result_count=195))
    _mark_terminal(deps.ledger, rows[3])

    def invoke(request: dict, write: bool) -> dict:
        requests.append(request)
        return core_result(request, write=write)

    deps.invoke_handoff = invoke

    first = run_check(
        config(tmp_path),
        deps,
        write=True,
        now=NOW,
        enforce_schedule=False,
    )
    second = run_check(
        config(tmp_path),
        deps,
        write=True,
        now=NOW,
        enforce_schedule=False,
    )

    assert first["status"] == "complete"
    assert first["counts"]["new_events"] == 3
    assert first["counts"]["already_terminal"] == 1
    assert [event["eventKey"] for event in requests[0]["events"]] == [
        funding_event_key(row) for row in rows[:3]
    ]
    assert second["status"] == "complete"
    assert second["counts"]["new_events"] == 0
    assert second["counts"]["already_terminal"] == 1
    assert len(requests) == 1
    assert funding_event_key(rows[4]) not in deps.ledger.events


def test_truncated_new_at_top_snapshot_with_first_row_anchor_is_zero_change(
    tmp_path: Path,
) -> None:
    rows = [observation(index) for index in range(100)]
    calls: list[dict] = []
    deps = dependencies(
        tmp_path,
        FakeBrowser(rows, result_count=195),
        invoke=lambda request, write: calls.append(request),
    )
    _mark_terminal(deps.ledger, rows[0])

    receipt = run_check(
        config(tmp_path),
        deps,
        write=True,
        now=NOW,
        enforce_schedule=False,
    )

    assert receipt["status"] == "complete"
    assert receipt["counts"]["new_events"] == 0
    assert receipt["counts"]["already_terminal"] == 1
    assert calls == []
    assert len(deps.ledger.events) == 1


def test_truncated_all_new_window_fails_before_core_or_state_mutation(
    tmp_path: Path,
) -> None:
    rows = [observation(index) for index in range(100)]
    calls: list[dict] = []
    deps = dependencies(
        tmp_path,
        FakeBrowser(rows, result_count=195),
        invoke=lambda request, write: calls.append(request),
    )

    receipt = run_check(
        config(tmp_path),
        deps,
        write=True,
        now=NOW,
        enforce_schedule=False,
    )

    assert receipt["status"] == "source_drift"
    assert receipt["stopReason"] == "missing_terminal_high_water_anchor"
    assert calls == []
    assert deps.ledger.events == {}
    assert not deps.ledger.path.exists()


def test_truncated_snapshot_parse_rejection_is_ambiguous_and_fails_closed(
    tmp_path: Path,
) -> None:
    rows = [observation(index) for index in range(99)]
    calls: list[dict] = []
    deps = dependencies(
        tmp_path,
        FakeBrowser(rows, result_count=195, rejections=1),
        invoke=lambda request, write: calls.append(request),
    )
    _mark_terminal(deps.ledger, rows[2])
    before = deps.ledger.path.read_bytes()

    receipt = run_check(
        config(tmp_path),
        deps,
        write=True,
        now=NOW,
        enforce_schedule=False,
    )

    assert receipt["status"] == "source_drift"
    assert receipt["stopReason"] == "ambiguous_truncated_snapshot"
    assert calls == []
    assert deps.ledger.path.read_bytes() == before


def test_truncated_snapshot_requires_new_at_top_sort_before_diff(
    tmp_path: Path,
) -> None:
    rows = [observation(index) for index in range(100)]
    browser = FakeBrowser(rows, result_count=195)
    original_read = browser.read_source

    def drifted_read(source, *, observed_at: str, max_pages: int):
        snapshot = original_read(
            source,
            observed_at=observed_at,
            max_pages=max_pages,
        )
        return SavedListSnapshot(
            source=snapshot.source,
            title=snapshot.title,
            result_type=snapshot.result_type,
            new_at_top=False,
            filter_text=snapshot.filter_text,
            result_count=snapshot.result_count,
            page_count=snapshot.page_count,
            top_funding_date=snapshot.top_funding_date,
            observations=snapshot.observations,
            rejections=snapshot.rejections,
        )

    browser.read_source = drifted_read
    calls: list[dict] = []
    deps = dependencies(
        tmp_path,
        browser,
        invoke=lambda request, write: calls.append(request),
    )
    _mark_terminal(deps.ledger, rows[2])
    before = deps.ledger.path.read_bytes()

    receipt = run_check(
        config(tmp_path),
        deps,
        write=True,
        now=NOW,
        enforce_schedule=False,
    )

    assert receipt["status"] == "source_drift"
    assert receipt["stopReason"] == "new_at_top_contract_failed"
    assert calls == []
    assert deps.ledger.path.read_bytes() == before


def test_check_sends_only_nonterminal_event_keys(tmp_path: Path) -> None:
    terminal, unseen = observation(1), observation(2)
    deps = dependencies(tmp_path, FakeBrowser([terminal, unseen]))
    terminal_key = funding_event_key(terminal)
    deps.ledger.observe(terminal_key, observed_at=terminal.observed_at)
    deps.ledger.mark_handoff_pending(
        terminal_key,
        run_id="prior",
        observed_at=terminal.observed_at,
    )
    deps.ledger.mark_terminal(
        terminal_key,
        outcome="created",
        page_id="page-prior",
        observed_at=terminal.observed_at,
    )
    requests: list[dict] = []

    def invoke(request: dict, write: bool) -> dict:
        requests.append(request)
        return core_result(request, write=write)

    deps.invoke_handoff = invoke
    receipt = run_check(
        config(tmp_path),
        deps,
        write=True,
        now=NOW,
        enforce_schedule=False,
    )

    assert receipt["counts"]["already_terminal"] == 1
    assert [event["eventKey"] for event in requests[0]["events"]] == [
        funding_event_key(unseen)
    ]


def test_dry_run_rejects_incomplete_core_result_without_ledger_changes(
    tmp_path: Path,
) -> None:
    row = observation()

    def invoke(request: dict, write: bool) -> dict:
        result = core_result(request, write=write)
        result["complete"] = False
        return result

    deps = dependencies(tmp_path, FakeBrowser([row]), invoke=invoke)
    receipt = run_check(
        config(tmp_path),
        deps,
        write=False,
        now=NOW,
        enforce_schedule=False,
    )

    assert receipt["status"] == "result_schema_mismatch"
    assert deps.ledger.events == {}
    assert not deps.ledger.path.exists()


def test_bootstrap_rejects_mixed_terminal_result_before_any_terminalization(
    tmp_path: Path,
) -> None:
    rows = [observation(index) for index in range(12)]

    def invoke(request: dict, write: bool) -> dict:
        result = core_result(request, write=write)
        result["events"][1]["state"] = "retryable_failure"
        return result

    deps = dependencies(tmp_path, FakeBrowser(rows), invoke=invoke)
    receipt = run_bootstrap(
        config(tmp_path),
        deps,
        seed_top=10,
        write=True,
        now=NOW,
    )

    assert receipt["status"] == "result_schema_mismatch"
    assert receipt["error"]["reason"] == "ValueError"
    assert "terminal" in receipt["error"]["evidence"]
    assert {
        record["state"] for record in deps.ledger.events.values()
    } == {"retryable"}
    assert len(deps.ledger.events) == 10
    assert not deps.ledger.bootstrap_complete(SOURCE)
    assert funding_event_key(rows[10]) not in deps.ledger.events


def test_dry_bootstrap_counts_only_nonterminal_handoffs(
    tmp_path: Path,
) -> None:
    rows = [observation(index) for index in range(12)]
    deps = dependencies(tmp_path, FakeBrowser(rows))
    for row in rows[:4]:
        key = funding_event_key(row)
        deps.ledger.observe(key, observed_at=row.observed_at)
        deps.ledger.mark_handoff_pending(
            key,
            run_id="prior-bootstrap",
            observed_at=row.observed_at,
        )
        deps.ledger.mark_terminal(
            key,
            outcome="created",
            page_id=f"page-{key[:8]}",
            observed_at=row.observed_at,
        )

    receipt = run_bootstrap(
        config(tmp_path),
        deps,
        seed_top=10,
        write=False,
        now=NOW,
    )

    assert receipt["counts"]["already_terminal"] == 4
    assert receipt["counts"]["would_handoff"] == 6
    assert receipt["counts"]["would_baseline"] == 2


def test_detector_receipt_is_durable_before_live_core_invocation(
    tmp_path: Path,
) -> None:
    row = observation()
    deps: WatcherDependencies

    def invoke(request: dict, write: bool) -> dict:
        receipt_paths = list(
            (deps.ledger.path.parent / "receipts").glob("*.json")
        )
        assert len(receipt_paths) == 1
        detector_receipt = json.loads(
            receipt_paths[0].read_text(encoding="utf-8")
        )
        assert detector_receipt["runId"] == request["runId"]
        assert detector_receipt["status"] == "handoff_pending"
        return core_result(request, write=write)

    deps = dependencies(tmp_path, FakeBrowser([row]), invoke=invoke)
    assert run_check(
        config(tmp_path),
        deps,
        write=True,
        now=NOW,
        enforce_schedule=False,
    )["status"] == "complete"


def test_latest_receipt_is_durable_before_notification(tmp_path: Path) -> None:
    row = observation()
    deps = dependencies(tmp_path, FakeBrowser([row]))

    def notify(items: list[str]) -> None:
        latest = json.loads(
            (deps.ledger.path.parent / "latest.json").read_text(
                encoding="utf-8"
            )
        )
        assert latest["status"] == "complete"
        assert latest["events"][0]["state"] == "created"
        assert items == [f"{row.company} — {row.crunchbase_url}"]

    deps.notify = notify
    assert run_check(
        config(tmp_path),
        deps,
        write=True,
        now=NOW,
        enforce_schedule=False,
    )["status"] == "complete"


def test_notification_preserves_literal_unicode_and_escapes_injection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], dict]] = []

    def fake_run(command: list[str], **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(funding_watcher.subprocess, "run", fake_run)
    company = 'München — "AI" \\ Labs\nend tell & do shell script "false"'
    profile = "https://www.crunchbase.com/organization/münchen-ai"

    funding_watcher._notification([f"{company} — {profile}"])

    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command[:2] == ["osascript", "-e"]
    script = command[2]
    assert "München —" in script
    assert "münchen-ai" in script
    assert "\\u00" not in script
    assert '\\"AI\\"' in script
    assert '\\"false\\"' in script
    assert "\\nend tell" in script
    assert kwargs == {
        "capture_output": True,
        "text": True,
        "timeout": 15,
        "check": False,
    }


def test_notification_transport_failure_does_not_reopen_terminal_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = observation()
    deps = dependencies(tmp_path, FakeBrowser([row]))
    deps.notify = funding_watcher._notification

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("osascript", 15)

    monkeypatch.setattr(funding_watcher.subprocess, "run", timeout)
    receipt = run_check(
        config(tmp_path),
        deps,
        write=True,
        now=NOW,
        enforce_schedule=False,
    )

    key = funding_event_key(row)
    assert receipt["status"] == "complete"
    assert deps.ledger.is_terminal(key)
    assert json.loads(
        (deps.ledger.path.parent / "latest.json").read_text(encoding="utf-8")
    )["status"] == "complete"


@pytest.mark.parametrize("action", ["check", "bootstrap"])
def test_corrupt_budget_is_schema_failure_before_browser(
    tmp_path: Path,
    action: str,
) -> None:
    budget_path = tmp_path / "shared-budget.json"
    budget_path.write_text(
        json.dumps(
            {
                "schemaVersion": "wrong",
                "date": "2026-07-29",
                "ceiling": 25,
                "used": 0,
                "lanes": {},
            }
        ),
        encoding="utf-8",
    )
    browser = FakeBrowser([observation()])
    deps = dependencies(
        tmp_path,
        browser,
        budget=DailyCrunchbaseBudget(budget_path),
    )

    if action == "check":
        receipt = run_check(
            config(tmp_path),
            deps,
            write=True,
            now=NOW,
            enforce_schedule=False,
        )
    else:
        receipt = run_bootstrap(
            config(tmp_path),
            deps,
            seed_top=10,
            write=True,
            now=NOW,
        )

    assert receipt["status"] == "budget_state_invalid"
    assert receipt["stopReason"] == "budget_contract_failed"
    assert receipt["error"]["reason"] == "RuntimeError"
    assert "budget" in receipt["error"]["evidence"].casefold()
    assert funding_watcher._exit_code(receipt) == 78
    assert browser.calls == []


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("complete", 0),
        ("outside_schedule", 0),
        ("already_checked_slot", 0),
        ("already_bootstrapped", 0),
        ("busy", 75),
        ("budget_exhausted", 75),
        ("crm_retryable", 75),
        ("browser_retryable", 75),
        ("captcha", 78),
        ("security_challenge", 78),
        ("auth_wall", 78),
        ("source_drift", 78),
        ("result_schema_mismatch", 78),
        ("budget_state_invalid", 78),
    ],
)
def test_cli_exit_contract(
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    expected: int,
) -> None:
    monkeypatch.setattr(
        funding_watcher,
        "load_watcher_config",
        lambda path: {},
    )
    monkeypatch.setattr(
        funding_watcher,
        "_production_dependencies",
        lambda loaded: object(),
    )
    monkeypatch.setattr(
        funding_watcher,
        "run_check",
        lambda *args, **kwargs: {"status": status},
    )

    assert funding_watcher.main(["check", "--dry-run"]) == expected


def test_invalid_seed_top_is_cli_misuse_before_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    touched: list[str] = []
    monkeypatch.setattr(
        funding_watcher,
        "load_watcher_config",
        lambda path: touched.append("config") or {},
    )
    monkeypatch.setattr(
        funding_watcher,
        "_production_dependencies",
        lambda loaded: touched.append("dependencies") or object(),
    )

    assert funding_watcher.main(
        ["bootstrap", "--dry-run", "--seed-top", "0"]
    ) == 64
    assert touched == []


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["check", "--write"],
        ["check", "--dry-run", "--yes"],
        ["bootstrap", "--write"],
        ["unknown-command"],
    ],
)
def test_cli_command_misuse_exits_64(argv: list[str]) -> None:
    assert funding_watcher.main(argv) == 64


def test_migration_schema_failure_exits_78_without_runtime_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    touched: list[str] = []
    monkeypatch.setattr(
        funding_watcher,
        "load_watcher_config",
        lambda path: {
            "sourceDefinitions": [SimpleNamespace(url=SOURCE)],
            "legacyStateDirectory": "/isolated/legacy",
            "stateDirectory": "/isolated/research",
        },
    )
    monkeypatch.setattr(
        funding_watcher,
        "_production_dependencies",
        lambda loaded: touched.append("dependencies"),
    )
    monkeypatch.setattr(
        funding_watcher,
        "migrate_legacy_state",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("legacy schema mismatch")
        ),
    )

    assert funding_watcher.main(["migrate-legacy-state"]) == 78
    assert touched == []


def test_partial_research_migration_directory_exits_78(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy = tmp_path / "legacy"
    research = tmp_path / "research"
    legacy.mkdir()
    research.mkdir()
    marker = research / "operator-note.txt"
    marker.write_text("preserve\n", encoding="utf-8")
    states = ["created"] * 10 + ["baseline"] * 179
    events = {
        hashlib.sha256(f"event-{index}".encode()).hexdigest(): {
            "state": state,
            "observed_at": "2026-07-29T12:06:04+00:00",
            "details": {
                "company": f"Company {index}",
                "crunchbase_url": (
                    "https://www.crunchbase.com/organization/"
                    f"company-{index}"
                ),
                "source_url": SOURCE,
                **(
                    {"page_id": f"page-{index}"}
                    if state == "created"
                    else {}
                ),
            },
        }
        for index, state in enumerate(states)
    }
    (legacy / "ledger.json").write_text(
        json.dumps(
            {
                "schemaVersion": (
                    "norman.crm_core.crunchbase_funding_ledger.v1"
                ),
                "events": events,
                "bootstraps": {
                    SOURCE: {
                        "completed_at": "2026-07-29T12:06:04+00:00"
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        funding_watcher,
        "load_watcher_config",
        lambda path: {
            "sourceDefinitions": [SimpleNamespace(url=SOURCE)],
            "legacyStateDirectory": str(legacy),
            "stateDirectory": str(research),
        },
    )

    assert funding_watcher.main(["migrate-legacy-state"]) == 78
    assert marker.read_text(encoding="utf-8") == "preserve\n"
    assert not (research / "ledger.json").exists()
    assert not (research / "migration-receipt.json").exists()
