from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from lib.crunchbase_saved_list import (
    load_watcher_config,
    parse_saved_list_snapshot,
)
from lib.funding_handoff import build_handoff, validate_result, write_handoff
from lib.funding_watcher_state import funding_event_key


RESEARCH_ROOT = Path(__file__).resolve().parents[1]
CORE_ACCEPTANCE_ENV = "NORMAN_CRM_CORE_ACCEPTANCE_PATH"
NEW_YORK_ACCEPTANCE_TIME = "2026-07-29T10:15:00-04:00"
SHARED_RELATIVE_ROOT = Path("shared-state")
WATCHER_LANE = "research-funding-watcher"
CORE_REQUIRED_PATHS = (
    Path("scripts/lib/browser_coordination.py"),
    Path("scripts/crm_funding_handoff.py"),
)

RESEARCH_PROBE = r"""
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / "scripts"))
from lib.browser_coordination import DailyCrunchbaseBudget

operation = sys.argv[1]
now = datetime.fromisoformat(sys.argv[2])
budget = DailyCrunchbaseBudget()
if operation == "claim":
    result = budget.claim(
        now,
        requested=int(sys.argv[3]),
        lane=sys.argv[4],
    )
elif operation == "snapshot":
    result = budget.snapshot(now)
else:
    raise RuntimeError(f"unknown operation: {operation}")
print(json.dumps(result, sort_keys=True))
"""

CORE_PROBE = r"""
import json
import sys
from datetime import datetime

from scripts.lib.browser_coordination import (
    BrowserLeaseUnavailable,
    DailyCrunchbaseBudget,
    SharedBrowserLease,
)

operation = sys.argv[1]
if operation == "lease":
    try:
        with SharedBrowserLease():
            result = {"contention": False}
    except BrowserLeaseUnavailable:
        result = {"contention": True}
else:
    now = datetime.fromisoformat(sys.argv[2])
    budget = DailyCrunchbaseBudget()
    if operation == "claim":
        result = budget.claim(
            now,
            requested=int(sys.argv[3]),
            lane=sys.argv[4],
        )
    elif operation == "snapshot":
        result = budget.snapshot(now)
    else:
        raise RuntimeError(f"unknown operation: {operation}")
print(json.dumps(result, sort_keys=True))
"""

RESEARCH_LEASE_HOLDER = r"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / "scripts"))
from lib.browser_coordination import SharedBrowserLease

with SharedBrowserLease():
    print(json.dumps({"held": True}), flush=True)
    sys.stdin.readline()
"""

CORE_CLI_HARNESS = r"""
import sys

from scripts import crm_funding_handoff as handoff


def forbidden_call(*args, **kwargs):
    raise AssertionError("acceptance dry-run reached a Notion mutation boundary")


class NoMutationWriter:
    def preflight(self, *, write):
        if write:
            raise AssertionError("acceptance CLI must remain dry-run")
        return {"ready": True, "write": False}

    def load_index(self):
        return []

    create = forbidden_call
    patch = forbidden_call
    get_page = forbidden_call


handoff.nc.notion = forbidden_call
handoff.crm_intake.create_page = forbidden_call
handoff.nc.patch_page_properties_verified = forbidden_call
handoff.nc.load_token = lambda: "acceptance-token"
handoff.FundingHandoffWriter = lambda token: NoMutationWriter()
raise SystemExit(handoff.main(sys.argv[1:]))
"""


def _isolated_env(home: Path) -> dict[str, str]:
    home.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["HOME"] = str(home / "home")
    env["NORMANAI_SHARED_STATE_DIR"] = str(
        home / SHARED_RELATIVE_ROOT
    )
    return env


def _core_compatible_funding_request(request: dict[str, Any]) -> dict[str, Any]:
    """Project CRMx-rich funding handoff into the pinned Core event allowlist."""
    events: list[dict[str, Any]] = []
    for event in request["events"]:
        funding = event.get("funding") if isinstance(event.get("funding"), dict) else {}
        events.append(
            {
                "eventKey": event["eventKey"],
                "sourceName": event["sourceName"],
                "sourceUrl": event["sourceUrl"],
                "observedAt": event["observedAt"],
                "company": event["company"],
                "crunchbaseUrl": event["crunchbaseUrl"],
                "website": event["website"],
                "linkedin": event["linkedin"],
                "founders": event["founders"],
                "description": event["description"],
                "founded": event["founded"],
                "headquarters": event["headquarters"],
                "industries": event["industries"],
                "funding": {
                    "date": funding["date"],
                    "type": funding["type"],
                    "amountRaw": funding["amountRaw"],
                    "amountMinor": funding["amountMinor"],
                    "currency": funding["currency"],
                    "totalRaw": funding["totalRaw"],
                },
            }
        )
    return {
        "schemaVersion": request["schemaVersion"],
        "runId": request["runId"],
        "generatedAt": request["generatedAt"],
        "events": events,
    }


def _resolve_core_root() -> Path | None:
    declared = os.environ.get(CORE_ACCEPTANCE_ENV)
    if declared:
        candidate = Path(declared).expanduser()
        if not candidate.is_absolute():
            raise ValueError(f"{CORE_ACCEPTANCE_ENV} must be absolute")
        candidate = candidate.resolve()
        if not all((candidate / relative).is_file() for relative in CORE_REQUIRED_PATHS):
            raise ValueError(
                f"{CORE_ACCEPTANCE_ENV} is not a NormanAI-crm-core checkout"
            )
        return candidate

    candidates = [
        RESEARCH_ROOT.parent / "NormanAI-crm-core",
        RESEARCH_ROOT.parent / "normanai-crm-core",
        RESEARCH_ROOT.parent / "crunchbase-funding-watcher",
    ]
    if len(RESEARCH_ROOT.parents) > 1:
        candidates.append(RESEARCH_ROOT.parents[1])
    for candidate in candidates:
        if all((candidate / relative).is_file() for relative in CORE_REQUIRED_PATHS):
            return candidate.resolve()
    return None


@pytest.fixture(scope="module")
def core_root() -> Path:
    try:
        resolved = _resolve_core_root()
    except ValueError as exc:
        pytest.fail(str(exc))
    if resolved is None:
        if os.environ.get("NORMAN_REQUIRE_CROSS_REPO") == "1":
            pytest.fail(
                f"required cross-repository acceptance checkout is missing; "
                f"set {CORE_ACCEPTANCE_ENV}"
            )
        pytest.skip(
            f"cross-repository acceptance requires {CORE_ACCEPTANCE_ENV} "
            "or a validated sibling Core checkout"
        )
    return resolved


def test_required_cross_repo_checkout_fails_instead_of_skipping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NORMAN_REQUIRE_CROSS_REPO", "1")
    monkeypatch.delenv(CORE_ACCEPTANCE_ENV, raising=False)
    monkeypatch.setattr(
        sys.modules[__name__], "_resolve_core_root", lambda: None
    )
    with pytest.raises(pytest.fail.Exception, match="required"):
        core_root.__wrapped__()


def _probe(
    root: Path,
    program: str,
    home: Path,
    *arguments: str,
) -> Any:
    completed = subprocess.run(
        [sys.executable, "-c", program, *arguments],
        cwd=root,
        env=_isolated_env(home),
        text=True,
        capture_output=True,
        check=False,
        timeout=20,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def _failed_probe(
    root: Path,
    program: str,
    home: Path,
    *arguments: str,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        [sys.executable, "-c", program, *arguments],
        cwd=root,
        env=_isolated_env(home),
        text=True,
        capture_output=True,
        check=False,
        timeout=20,
    )
    assert completed.returncode != 0
    return completed


def _research_claim(
    home: Path,
    now: str,
    requested: int,
    lane: str,
) -> int:
    return _probe(
        RESEARCH_ROOT,
        RESEARCH_PROBE,
        home,
        "claim",
        now,
        str(requested),
        lane,
    )


def _core_claim(
    core_root: Path,
    home: Path,
    now: str,
    requested: int,
    lane: str,
) -> int:
    return _probe(
        core_root,
        CORE_PROBE,
        home,
        "claim",
        now,
        str(requested),
        lane,
    )


def test_default_constructors_share_real_lease_and_one_public_v3_budget(
    tmp_path: Path,
    core_root: Path,
) -> None:
    """Catches either repository drifting to a private default coordination path."""
    home = tmp_path / "coordination-home"
    holder = subprocess.Popen(
        [sys.executable, "-c", RESEARCH_LEASE_HOLDER],
        cwd=RESEARCH_ROOT,
        env=_isolated_env(home),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    try:
        assert holder.stdout is not None
        ready = holder.stdout.readline()
        if not ready:
            assert holder.stderr is not None
            raise AssertionError(holder.stderr.read())
        assert json.loads(ready) == {"held": True}
        assert _probe(
            core_root,
            CORE_PROBE,
            home,
            "lease",
        ) == {"contention": True}
    finally:
        if holder.stdin is not None:
            holder.stdin.write("\n")
            holder.stdin.flush()
        try:
            holder.wait(timeout=5)
        except subprocess.TimeoutExpired:
            holder.kill()
            holder.wait(timeout=5)
    assert holder.returncode == 0

    research_times = (
        "2026-07-29T06:05:00-04:00",
        "2026-07-29T10:05:00-04:00",
        "2026-07-29T13:05:00-04:00",
        "2026-07-29T16:05:00-04:00",
        "2026-07-29T19:05:00-04:00",
    )
    assert [
        _research_claim(home, now, 3, WATCHER_LANE)
        for now in research_times
    ] == [3, 3, 3, 3, 3]
    assert _core_claim(
        core_root,
        home,
        NEW_YORK_ACCEPTANCE_TIME,
        40,
        "crm_crunchbase",
    ) == 40
    assert _research_claim(
        home, research_times[-1], 1, WATCHER_LANE
    ) == 0
    assert _core_claim(
        core_root,
        home,
        NEW_YORK_ACCEPTANCE_TIME,
        1,
        "crm_crunchbase",
    ) == 0

    public_state = _probe(
        core_root,
        CORE_PROBE,
        home,
        "snapshot",
        NEW_YORK_ACCEPTANCE_TIME,
    )
    assert public_state == {
        "schemaVersion": "norman.shared.crunchbase_budget.v4",
        "date": "2026-07-29",
        "ceiling": 55,
        "used": 55,
        "lanes": {
            "crm_crunchbase": 40,
            "research-funding-watcher@2026-07-29T06:00:00-04:00": 3,
            "research-funding-watcher@2026-07-29T10:00:00-04:00": 3,
            "research-funding-watcher@2026-07-29T13:00:00-04:00": 3,
            "research-funding-watcher@2026-07-29T16:00:00-04:00": 3,
            "research-funding-watcher@2026-07-29T19:00:00-04:00": 3,
        },
    }
    assert (
        home / SHARED_RELATIVE_ROOT / "crunchbase-budget.json"
    ).is_file()


def test_scheduled_research_lanes_decorate_and_cap_each_new_york_hour(
    tmp_path: Path,
    core_root: Path,
) -> None:
    """Catches slot decoration or prefix aggregation bypassing the three-page cap."""
    home = tmp_path / "scheduled-home"
    ten_o_five = "2026-07-29T10:05:00-04:00"
    ten_fifty_nine = "2026-07-29T10:59:00-04:00"
    one_o_five = "2026-07-29T13:05:00-04:00"
    one_fifty_nine = "2026-07-29T13:59:00-04:00"

    assert _research_claim(home, ten_o_five, 3, WATCHER_LANE) == 3
    assert _research_claim(home, ten_fifty_nine, 1, WATCHER_LANE) == 0
    assert _research_claim(home, one_o_five, 3, WATCHER_LANE) == 3
    assert _research_claim(home, one_fifty_nine, 1, WATCHER_LANE) == 0

    public_state = _probe(
        core_root,
        CORE_PROBE,
        home,
        "snapshot",
        one_fifty_nine,
    )
    scheduled_lanes = {
        key: value
        for key, value in public_state["lanes"].items()
        if key.startswith(f"{WATCHER_LANE}@")
    }
    assert scheduled_lanes == {
        "research-funding-watcher@2026-07-29T10:00:00-04:00": 3,
        "research-funding-watcher@2026-07-29T13:00:00-04:00": 3,
    }
    assert all(pages <= 3 for pages in scheduled_lanes.values())
    assert sum(scheduled_lanes.values()) == public_state["used"] == 6


def test_canonical_research_fixture_passes_the_real_core_dry_run_cli(
    tmp_path: Path,
    core_root: Path,
) -> None:
    """Catches request-schema or fingerprint drift at the public Core boundary."""
    config = load_watcher_config(
        RESEARCH_ROOT / "config" / "funding-watcher.json"
    )
    snapshot_payload = json.loads(
        (
            RESEARCH_ROOT
            / "tests"
            / "fixtures"
            / "crunchbase_saved_list_snapshot.json"
        ).read_text(encoding="utf-8")
    )
    observed_at = "2026-07-29T14:00:00+00:00"
    fixture_source = replace(
        config["sourceDefinitions"][0],
        name="Main Funding - July 2026",
        url=(
            "https://www.crunchbase.com/discover/saved/"
            "main-funding-july-2026/730c458b-149c-4a0a-9684-7146e7258993"
        ),
        expected_funding_after="2026-07-01",
    )
    snapshot = parse_saved_list_snapshot(
        snapshot_payload,
        fixture_source,
        observed_at,
    )
    observation = snapshot.observations[0]
    event_key = funding_event_key(observation)
    request = build_handoff(
        "20260729T140000Z",
        observed_at,
        [(event_key, observation)],
    )
    # Production handoff is CRMx-rich (investors/rounds/totals for full CSV).
    # Pinned Core still allowlists the pre-CRMx event shape — project for the
    # legacy dry-run boundary without changing the Research→CRMx request.
    core_request = _core_compatible_funding_request(request)
    request_path = tmp_path / "handoff" / "request.json"
    result_path = tmp_path / "handoff" / "result.json"
    write_handoff(request_path, core_request)

    assert request["schemaVersion"] == "norman.research.funding_handoff.v1"
    assert request["events"][0]["eventKey"] == event_key
    assert "investors" in request["events"][0]
    assert "numberOfFundingRounds" in request["events"][0]
    assert set(core_request["events"][0]) == {
        "eventKey",
        "sourceName",
        "sourceUrl",
        "observedAt",
        "company",
        "crunchbaseUrl",
        "website",
        "linkedin",
        "founders",
        "description",
        "founded",
        "headquarters",
        "industries",
        "funding",
    }
    assert set(core_request["events"][0]["funding"]) == {
        "date",
        "type",
        "amountRaw",
        "amountMinor",
        "currency",
        "totalRaw",
    }

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            CORE_CLI_HARNESS,
            "--handoff",
            str(request_path),
            "--result",
            str(result_path),
            "--dry-run",
        ],
        cwd=core_root,
        env=_isolated_env(tmp_path / "core-cli-home"),
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr

    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert validate_result(result, request=core_request) == result
    assert result["schemaVersion"] == (
        "norman.crm_core.funding_handoff_result.v1"
    )
    assert result["mode"] == "dry_run"
    assert result["complete"] is True
    assert result["events"] == [
        {
            "eventKey": event_key,
            "state": "created",
            "reason": "no_hard_match",
        }
    ]


@pytest.mark.parametrize(
    ("program_name", "home_name"),
    [
        ("research", "research-stale-valid"),
        ("core", "core-stale-valid"),
    ],
)
def test_both_repositories_reset_only_a_valid_prior_day_budget(
    tmp_path: Path,
    core_root: Path,
    program_name: str,
    home_name: str,
) -> None:
    home = tmp_path / home_name
    state = home / SHARED_RELATIVE_ROOT / "crunchbase-budget.json"
    state.parent.mkdir(parents=True)
    state.write_text(
        json.dumps(
            {
                "schemaVersion": "norman.shared.crunchbase_budget.v1",
                "date": "2026-07-28",
                "ceiling": 25,
                "used": 25,
                "lanes": {"prior": 25},
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    root, probe = (
        (RESEARCH_ROOT, RESEARCH_PROBE)
        if program_name == "research"
        else (core_root, CORE_PROBE)
    )

    assert _probe(
        root,
        probe,
        home,
        "snapshot",
        NEW_YORK_ACCEPTANCE_TIME,
    ) == {
        "schemaVersion": "norman.shared.crunchbase_budget.v4",
        "date": "2026-07-29",
        "ceiling": 55,
        "used": 0,
        "lanes": {},
    }


@pytest.mark.parametrize(
    ("persisted_date", "lanes"),
    [
        ("2026-07-28", {"prior": 24}),
        ("2026-07-30", {"future": 25}),
    ],
)
@pytest.mark.parametrize("program_name", ["research", "core"])
def test_both_repositories_fail_closed_without_rewriting_invalid_date_state(
    tmp_path: Path,
    core_root: Path,
    persisted_date: str,
    lanes: dict[str, int],
    program_name: str,
) -> None:
    home = tmp_path / f"{program_name}-{persisted_date}"
    state = home / SHARED_RELATIVE_ROOT / "crunchbase-budget.json"
    state.parent.mkdir(parents=True)
    payload = {
        "schemaVersion": "norman.shared.crunchbase_budget.v1",
        "date": persisted_date,
        "ceiling": 25,
        "used": 25,
        "lanes": lanes,
    }
    serialized = json.dumps(payload, sort_keys=True)
    state.write_text(serialized, encoding="utf-8")
    root, probe = (
        (RESEARCH_ROOT, RESEARCH_PROBE)
        if program_name == "research"
        else (core_root, CORE_PROBE)
    )

    _failed_probe(
        root,
        probe,
        home,
        "snapshot",
        NEW_YORK_ACCEPTANCE_TIME,
    )

    assert state.read_text(encoding="utf-8") == serialized
