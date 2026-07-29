from __future__ import annotations

import contextlib
import hashlib
import json
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
from lib.browser_coordination import DailyCrunchbaseBudget
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

    def claim(
        self,
        now,
        *,
        requested: int,
        lane: str,
        exact: bool = False,
    ):
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
        "sharedWorkItemCeiling": 40,
        "researchDailyCheckLimit": 10,
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


def test_research_watcher_cannot_exceed_ten_daily_checks(
    tmp_path: Path,
) -> None:
    budget = DailyCrunchbaseBudget(tmp_path / "shared-budget.json")
    browser = FakeBrowser([])
    deps = dependencies(tmp_path, browser, budget=budget)

    receipts = [
        run_check(
            config(tmp_path),
            deps,
            write=True,
            now=NOW,
            enforce_schedule=False,
        )
        for _ in range(6)
    ]

    assert [receipt["status"] for receipt in receipts[:5]] == [
        "complete"
    ] * 5
    assert receipts[5]["status"] == "budget_exhausted"
    assert browser.calls == [2] * 5


def test_bootstrap_refuses_partial_capacity_without_consuming_it(
    tmp_path: Path,
) -> None:
    budget = DailyCrunchbaseBudget(tmp_path / "shared-budget.json")
    assert budget.claim(
        NOW, requested=2, lane="research-funding-watcher"
    ) == 2
    assert budget.claim(
        NOW, requested=2, lane="research-funding-watcher"
    ) == 2
    assert budget.claim(
        NOW, requested=1, lane="research-funding-watcher"
    ) == 1
    browser = FakeBrowser([observation()])
    deps = dependencies(tmp_path, browser, budget=budget)

    receipt = run_bootstrap(
        config(tmp_path),
        deps,
        seed_top=10,
        write=True,
        now=NOW,
    )

    assert receipt["status"] == "budget_exhausted"
    assert receipt["pagesReserved"] == 0
    assert browser.calls == []
    assert budget.snapshot(NOW)["used"] == 5


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
    assert receipt["stopReason"] == "incomplete_snapshot_coverage"
    assert calls == []
    assert deps.ledger.events == {}


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
