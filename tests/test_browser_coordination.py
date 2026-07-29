from __future__ import annotations

import json
import multiprocessing
import os
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


def _claim(path: str) -> int:
    return DailyCrunchbaseBudget(Path(path)).claim(
        datetime(2026, 7, 29, 10, tzinfo=NEW_YORK),
        requested=2,
        lane=f"worker-{os.getpid()}",
    )


def test_default_shared_paths_match_the_cross_repository_contract() -> None:
    root = Path.home() / "Library/Application Support/NormanAI/shared"
    assert DEFAULT_BROWSER_LOCK == root / "browser.lock"
    assert DEFAULT_CRUNCHBASE_BUDGET == root / "crunchbase-budget.json"


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


def test_concurrent_budget_claims_never_exceed_25(tmp_path: Path) -> None:
    path = tmp_path / "budget.json"
    context = multiprocessing.get_context("fork")
    with context.Pool(20) as pool:
        results = [
            pool.apply_async(_claim, (str(path),))
            for _ in range(20)
        ]
        grants = [result.get(timeout=10) for result in results]

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert sum(grants) == 25
    assert payload["used"] == 25
    assert sum(payload["lanes"].values()) == 25


def test_budget_persists_the_shared_core_json_contract(tmp_path: Path) -> None:
    """Catches schema drift that would make Core and Research misread state."""
    path = tmp_path / "budget.json"
    budget = DailyCrunchbaseBudget(path)
    now = datetime(2026, 7, 29, 10, tzinfo=NEW_YORK)

    assert budget.claim(now, requested=2, lane="research-funding-watcher") == 2
    assert budget.claim(now, requested=3, lane="crm-core") == 3
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "schemaVersion": "norman.shared.crunchbase_budget.v1",
        "date": "2026-07-29",
        "ceiling": 25,
        "used": 5,
        "lanes": {
            "crm-core": 3,
            "research-funding-watcher": 2,
        },
    }


def test_detector_can_reserve_no_more_than_two_pages_per_source(
    tmp_path: Path,
) -> None:
    budget = DailyCrunchbaseBudget(tmp_path / "budget.json")
    now = datetime(2026, 7, 29, 10, tzinfo=NEW_YORK)

    with pytest.raises(ValueError, match="at most 2"):
        budget.claim(now, requested=3, lane="research-funding-watcher")
    assert budget.claim(now, requested=2, lane="research-funding-watcher") == 2


def test_budget_resets_on_the_new_york_date(tmp_path: Path) -> None:
    budget = DailyCrunchbaseBudget(tmp_path / "budget.json")
    first = datetime(2026, 7, 29, 23, 59, tzinfo=NEW_YORK)
    second = datetime(2026, 7, 30, 0, 1, tzinfo=NEW_YORK)
    assert budget.claim(first, requested=2, lane="research-funding-watcher") == 2
    assert budget.snapshot(second)["used"] == 0
