from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from lib.browser_coordination import DailyCrunchbaseBudget
from lib.crunchbase_saved_list import load_watcher_config


ROOT = Path(__file__).resolve().parents[1]


def test_live_watcher_config_exposes_expanded_capacity_contract() -> None:
    config = load_watcher_config(ROOT / "config/funding-watcher.json")

    assert config["scheduledMaxPagesPerSource"] == 3
    assert config["sharedWorkItemCeiling"] == 55
    assert config["researchDailyCheckLimit"] == 15


def test_readme_describes_the_v4_shared_capacity_contract() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "40 Core company sessions and 15 Research watcher checks" in readme
    assert "30 Core company sessions and ten Research watcher checks" not in readme


def test_shared_budget_enforces_40_core_plus_15_research(
    tmp_path: Path,
) -> None:
    budget = DailyCrunchbaseBudget(tmp_path / "budget.json", ceiling=55)
    now = datetime(2026, 7, 31, 14, tzinfo=timezone.utc)

    assert budget.claim(now, requested=40, lane="crm_crunchbase") == 40
    assert budget.claim(now, requested=1, lane="crm_crunchbase") == 0
    assert budget.claim(
        now,
        requested=15,
        lane="research-funding-bootstrap",
    ) == 15
    assert budget.claim(
        now,
        requested=1,
        lane="research-funding-bootstrap",
    ) == 0
    assert budget.snapshot(now)["used"] == 55


def test_watcher_slot_accepts_three_but_never_four(
    tmp_path: Path,
) -> None:
    budget = DailyCrunchbaseBudget(tmp_path / "budget.json", ceiling=55)
    now = datetime(2026, 7, 31, 14, tzinfo=timezone.utc)

    with pytest.raises(ValueError, match="at most 3"):
        budget.claim(
            now,
            requested=4,
            lane="research-funding-watcher",
        )
    assert budget.claim(
        now,
        requested=3,
        lane="research-funding-watcher",
    ) == 3


def test_same_day_v3_ledger_stays_at_40_then_rolls_to_v4(
    tmp_path: Path,
) -> None:
    path = tmp_path / "budget.json"
    path.write_text(
        json.dumps(
            {
                "schemaVersion": "norman.shared.crunchbase_budget.v3",
                "date": "2026-07-30",
                "ceiling": 40,
                "used": 39,
                "lanes": {
                    "crm_crunchbase": 29,
                    "research-funding-bootstrap": 10,
                },
            }
        ),
        encoding="utf-8",
    )
    budget = DailyCrunchbaseBudget(path, ceiling=55)
    same_day = datetime(2026, 7, 30, 20, tzinfo=timezone.utc)

    assert budget.claim(same_day, requested=5, lane="crm_crunchbase") == 1
    assert budget.snapshot(same_day)["ceiling"] == 40
    rolled = budget.snapshot(datetime(2026, 7, 31, 5, tzinfo=timezone.utc))
    assert rolled == {
        "schemaVersion": "norman.shared.crunchbase_budget.v4",
        "date": "2026-07-31",
        "ceiling": 55,
        "used": 0,
        "lanes": {},
    }
