from __future__ import annotations

import json
import multiprocessing
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from lib import chrome
from lib.browser_coordination import (
    BrowserLeaseUnavailable,
    DEFAULT_BROWSER_LOCK,
    DEFAULT_CRUNCHBASE_BUDGET,
    DailyCrunchbaseBudget,
    SharedBrowserLease,
)


NEW_YORK = ZoneInfo("America/New_York")


def _claim(args: tuple[str, str, int]) -> int:
    path, lane, hour = args
    return DailyCrunchbaseBudget(Path(path)).claim(
        datetime(2026, 7, 29, hour, tzinfo=NEW_YORK),
        requested=2,
        lane=lane,
    )


def _claim_watcher_slot(path: str) -> int:
    return DailyCrunchbaseBudget(Path(path)).claim(
        datetime(2026, 7, 29, 10, 30, tzinfo=NEW_YORK),
        requested=2,
        lane="research-funding-watcher",
    )


def test_default_shared_paths_resolve_from_injected_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NORMANAI_SHARED_STATE_DIR", str(tmp_path))

    assert SharedBrowserLease().path == tmp_path / "browser.lock"
    assert DailyCrunchbaseBudget().path == tmp_path / "crunchbase-budget.json"


def test_test_runtime_rejects_explicit_production_state_paths() -> None:
    with pytest.raises(RuntimeError, match="production shared state"):
        SharedBrowserLease(DEFAULT_BROWSER_LOCK)
    with pytest.raises(RuntimeError, match="production shared state"):
        DailyCrunchbaseBudget(DEFAULT_CRUNCHBASE_BUDGET)


def test_shared_browser_lease_is_nonblocking_and_mode_0600(tmp_path: Path) -> None:
    lock = tmp_path / "browser.lock"
    with SharedBrowserLease(lock):
        assert lock.stat().st_mode & 0o777 == 0o600
        with pytest.raises(RuntimeError, match="unavailable"):
            with SharedBrowserLease(lock):
                pass


def test_chrome_lease_translates_shared_contention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches Chrome reverting to a private lock or leaking protocol errors."""
    lock = tmp_path / "browser.lock"
    monkeypatch.setattr(
        chrome, "SharedBrowserLease", lambda: SharedBrowserLease(lock)
    )

    with SharedBrowserLease(lock):
        with pytest.raises(chrome.ChromeUnavailable) as caught:
            with chrome.lease():
                pass

    assert isinstance(caught.value.__cause__, BrowserLeaseUnavailable)


def test_concurrent_budget_claims_never_exceed_40(tmp_path: Path) -> None:
    path = tmp_path / "budget.json"
    context = multiprocessing.get_context("fork")
    with context.Pool(20) as pool:
        args = [
            (str(path), "crm_crunchbase", 10)
            for _index in range(15)
        ] + [
            (str(path), "research-funding-watcher", hour)
            for hour in (6, 10, 13, 16, 19)
        ]
        results = [pool.apply_async(_claim, (item,)) for item in args]
        grants = [result.get(timeout=10) for result in results]

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert sum(grants) == 40
    assert payload["used"] == 40
    assert sum(payload["lanes"].values()) == 40


def test_core_and_research_daily_allocations_are_atomic(
    tmp_path: Path,
) -> None:
    budget = DailyCrunchbaseBudget(tmp_path / "budget.json")
    now = datetime(2026, 7, 30, 10, tzinfo=NEW_YORK)

    assert budget.claim(now, requested=30, lane="crm_crunchbase") == 30
    assert budget.claim(now, requested=1, lane="crm_crunchbase") == 0
    for hour in (6, 10, 13, 16):
        assert (
            budget.claim(
                now.replace(hour=hour),
                requested=2,
                lane="research-funding-watcher",
            )
            == 2
        )
    assert (
        budget.claim(
            now,
            requested=6,
            lane="research-funding-bootstrap",
        )
        == 2
    )


def test_unknown_lane_is_rejected(tmp_path: Path) -> None:
    budget = DailyCrunchbaseBudget(tmp_path / "budget.json")

    with pytest.raises(ValueError, match="unapproved Crunchbase lane"):
        budget.claim(
            datetime(2026, 7, 30, 10, tzinfo=NEW_YORK),
            requested=1,
            lane="typo-lane",
        )


def test_malformed_persisted_watcher_slot_lane_is_rejected(
    tmp_path: Path,
) -> None:
    path = tmp_path / "budget.json"
    path.write_text(
        json.dumps(
            {
                "schemaVersion": "norman.shared.crunchbase_budget.v3",
                "date": "2026-07-30",
                "ceiling": 40,
                "used": 2,
                "lanes": {
                    "research-funding-watcher@not-a-slot": 2
                },
            }
        ),
        encoding="utf-8",
    )
    budget = DailyCrunchbaseBudget(path)
    with pytest.raises(RuntimeError, match="invalid shared Crunchbase"):
        budget.snapshot(
            datetime(2026, 7, 30, 10, tzinfo=NEW_YORK)
        )


def test_detector_can_reserve_no_more_than_two_pages_per_source(
    tmp_path: Path,
) -> None:
    budget = DailyCrunchbaseBudget(tmp_path / "budget.json")
    now = datetime(2026, 7, 29, 10, tzinfo=NEW_YORK)

    with pytest.raises(ValueError, match="at most 2"):
        budget.claim(now, requested=3, lane="research-funding-watcher")
    assert budget.claim(now, requested=2, lane="research-funding-watcher") == 2


def test_exact_claim_never_partially_consumes_remaining_capacity(
    tmp_path: Path,
) -> None:
    budget = DailyCrunchbaseBudget(tmp_path / "budget.json")
    now = datetime(2026, 7, 30, 10, tzinfo=NEW_YORK)
    assert budget.claim(
        now,
        requested=9,
        lane="research-funding-bootstrap",
    ) == 9

    assert budget.claim(
        now,
        requested=2,
        lane="research-funding-watcher",
        exact=True,
    ) == 0
    assert budget.snapshot(now)["used"] == 9


def test_detector_reservations_are_capped_per_new_york_slot_across_processes(
    tmp_path: Path,
) -> None:
    """Catches repeated claims accumulating beyond two pages in one slot."""
    path = tmp_path / "budget.json"
    context = multiprocessing.get_context("fork")
    with context.Pool(4) as pool:
        grants = [
            result.get(timeout=10)
            for result in [
                pool.apply_async(_claim_watcher_slot, (str(path),))
                for _ in range(4)
            ]
        ]

    budget = DailyCrunchbaseBudget(path)
    next_slot = datetime(2026, 7, 29, 13, 5, tzinfo=NEW_YORK)
    assert sum(grants) == 2
    assert budget.claim(
        next_slot, requested=2, lane="research-funding-watcher"
    ) == 2
    assert budget.snapshot(next_slot)["lanes"] == {
        "research-funding-watcher@2026-07-29T10:00:00-04:00": 2,
        "research-funding-watcher@2026-07-29T13:00:00-04:00": 2,
    }


def test_budget_resets_on_the_new_york_date(tmp_path: Path) -> None:
    budget = DailyCrunchbaseBudget(tmp_path / "budget.json")
    first = datetime(2026, 7, 29, 23, 59, tzinfo=NEW_YORK)
    second = datetime(2026, 7, 30, 0, 1, tzinfo=NEW_YORK)
    assert budget.claim(first, requested=2, lane="research-funding-watcher") == 2
    rolled = budget.snapshot(second)
    assert rolled["used"] == 0
    assert rolled["ceiling"] == 40
    assert rolled["schemaVersion"] == "norman.shared.crunchbase_budget.v3"


def test_legacy_25_page_budget_is_honored_until_new_york_midnight(
    tmp_path: Path,
) -> None:
    path = tmp_path / "budget.json"
    path.write_text(
        json.dumps(
            {
                "schemaVersion": "norman.shared.crunchbase_budget.v1",
                "date": "2026-07-29",
                "ceiling": 25,
                "used": 24,
                "lanes": {"crm_crunchbase": 24},
            }
        ),
        encoding="utf-8",
    )
    budget = DailyCrunchbaseBudget(path)
    now = datetime(2026, 7, 29, 10, tzinfo=NEW_YORK)

    assert budget.claim(
        now,
        requested=2,
        lane="research-funding-watcher",
    ) == 1


def test_v2_page_ledger_rolls_to_v3_work_items(tmp_path: Path) -> None:
    path = tmp_path / "budget.json"
    path.write_text(
        json.dumps(
            {
                "schemaVersion": "norman.shared.crunchbase_budget.v2",
                "date": "2026-07-29",
                "ceiling": 40,
                "used": 15,
                "lanes": {"crm_crunchbase": 15},
            }
        ),
        encoding="utf-8",
    )
    budget = DailyCrunchbaseBudget(path)

    rolled = budget.snapshot(
        datetime(2026, 7, 30, 0, 1, tzinfo=NEW_YORK)
    )

    assert rolled == {
        "schemaVersion": "norman.shared.crunchbase_budget.v3",
        "date": "2026-07-30",
        "ceiling": 40,
        "used": 0,
        "lanes": {},
    }


@pytest.mark.parametrize(
    "payload",
    [
        {"foo": 1},
        {
            "schemaVersion": "norman.shared.crunchbase_budget.v3",
            "date": "not-a-date",
            "ceiling": 40,
            "used": 0,
            "lanes": {},
        },
        {
            "schemaVersion": "norman.shared.crunchbase_budget.v3",
            "date": "2026-07-31",
            "ceiling": 40,
            "used": 0,
            "lanes": {},
        },
    ],
)
def test_invalid_or_future_ledgers_fail_closed(
    tmp_path: Path,
    payload: dict,
) -> None:
    path = tmp_path / "budget.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RuntimeError):
        DailyCrunchbaseBudget(path).snapshot(
            datetime(2026, 7, 30, 10, tzinfo=NEW_YORK)
        )


@pytest.mark.parametrize(
    "payload",
    [
        {
            "schemaVersion": "norman.shared.crunchbase_budget.v1",
            "date": "2026-07-28",
            "ceiling": 25,
            "used": 25,
            "lanes": {"crm_crunchbase": 24},
        },
        {
            "schemaVersion": "norman.shared.crunchbase_budget.v1",
            "date": "2026-07-28",
            "ceiling": 26,
            "used": 25,
            "lanes": {"crm_crunchbase": 25},
        },
        {
            "schemaVersion": "norman.shared.crunchbase_budget.v1",
            "date": "not-a-date",
            "ceiling": 25,
            "used": 25,
            "lanes": {"crm_crunchbase": 25},
        },
    ],
)
def test_corrupt_prior_day_budget_fails_closed_without_rewrite(
    tmp_path: Path,
    payload: dict,
) -> None:
    """A date change must not reopen allowance before validating old state."""
    path = tmp_path / "budget.json"
    serialized = json.dumps(payload, sort_keys=True)
    path.write_text(serialized, encoding="utf-8")
    budget = DailyCrunchbaseBudget(path)
    today = datetime(2026, 7, 29, 10, tzinfo=NEW_YORK)

    with pytest.raises(RuntimeError, match="shared Crunchbase budget"):
        budget.claim(today, requested=1, lane="research-funding-watcher")

    assert path.read_text(encoding="utf-8") == serialized


def test_future_dated_budget_fails_closed_without_rewrite(tmp_path: Path) -> None:
    path = tmp_path / "budget.json"
    payload = {
        "schemaVersion": "norman.shared.crunchbase_budget.v1",
        "date": "2026-07-30",
        "ceiling": 25,
        "used": 25,
        "lanes": {"crm_crunchbase": 25},
    }
    serialized = json.dumps(payload, sort_keys=True)
    path.write_text(serialized, encoding="utf-8")
    budget = DailyCrunchbaseBudget(path)
    today = datetime(2026, 7, 29, 10, tzinfo=NEW_YORK)

    with pytest.raises(RuntimeError, match="future-dated"):
        budget.snapshot(today)

    assert path.read_text(encoding="utf-8") == serialized
