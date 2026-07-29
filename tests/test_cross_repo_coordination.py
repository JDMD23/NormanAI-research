from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from lib.crunchbase_saved_list import (
    load_watcher_config,
    parse_saved_list_snapshot,
)
from lib.funding_handoff import build_handoff, validate_result, write_handoff
from lib.funding_watcher_state import funding_event_key


RESEARCH_ROOT = Path(__file__).resolve().parents[1]
CORE_ROOT = Path(
    "/Users/normanai/Documents/Core CRM/.worktrees/crunchbase-funding-watcher"
)
NEW_YORK_ACCEPTANCE_TIME = "2026-07-29T10:15:00-04:00"
SHARED_RELATIVE_ROOT = Path(
    "Library/Application Support/NormanAI/shared"
)
WATCHER_LANE = "research-funding-watcher"

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
    env["HOME"] = str(home)
    return env


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
    home: Path,
    now: str,
    requested: int,
    lane: str,
) -> int:
    return _probe(
        CORE_ROOT,
        CORE_PROBE,
        home,
        "claim",
        now,
        str(requested),
        lane,
    )


def test_default_constructors_share_real_lease_and_one_public_v1_budget(
    tmp_path: Path,
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
            CORE_ROOT,
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

    research_lane = "acceptance-research"
    core_lane = "acceptance-core"
    assert _research_claim(
        home, NEW_YORK_ACCEPTANCE_TIME, 10, research_lane
    ) == 10
    assert _core_claim(home, NEW_YORK_ACCEPTANCE_TIME, 15, core_lane) == 15
    assert _research_claim(
        home, NEW_YORK_ACCEPTANCE_TIME, 1, research_lane
    ) == 0
    assert _core_claim(home, NEW_YORK_ACCEPTANCE_TIME, 1, core_lane) == 0

    public_state = _probe(
        CORE_ROOT,
        CORE_PROBE,
        home,
        "snapshot",
        NEW_YORK_ACCEPTANCE_TIME,
    )
    assert public_state == {
        "schemaVersion": "norman.shared.crunchbase_budget.v1",
        "date": "2026-07-29",
        "ceiling": 25,
        "used": 25,
        "lanes": {
            research_lane: 10,
            core_lane: 15,
        },
    }
    assert (
        home / SHARED_RELATIVE_ROOT / "crunchbase-budget.json"
    ).is_file()


def test_scheduled_research_lanes_decorate_and_cap_each_new_york_hour(
    tmp_path: Path,
) -> None:
    """Catches slot decoration or prefix aggregation bypassing the two-page cap."""
    home = tmp_path / "scheduled-home"
    ten_o_five = "2026-07-29T10:05:00-04:00"
    ten_fifty_nine = "2026-07-29T10:59:00-04:00"
    one_o_five = "2026-07-29T13:05:00-04:00"
    one_fifty_nine = "2026-07-29T13:59:00-04:00"

    assert _research_claim(home, ten_o_five, 2, WATCHER_LANE) == 2
    assert _research_claim(home, ten_fifty_nine, 2, WATCHER_LANE) == 0
    assert _research_claim(home, one_o_five, 2, WATCHER_LANE) == 2
    assert _research_claim(home, one_fifty_nine, 2, WATCHER_LANE) == 0

    public_state = _probe(
        CORE_ROOT,
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
        "research-funding-watcher@2026-07-29T10:00:00-04:00": 2,
        "research-funding-watcher@2026-07-29T13:00:00-04:00": 2,
    }
    assert all(pages <= 2 for pages in scheduled_lanes.values())
    assert sum(scheduled_lanes.values()) == public_state["used"] == 4


def test_canonical_research_fixture_passes_the_real_core_dry_run_cli(
    tmp_path: Path,
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
    snapshot = parse_saved_list_snapshot(
        snapshot_payload,
        config["sourceDefinitions"][0],
        observed_at,
    )
    observation = snapshot.observations[0]
    event_key = funding_event_key(observation)
    request = build_handoff(
        "20260729T140000Z",
        observed_at,
        [(event_key, observation)],
    )
    request_path = tmp_path / "handoff" / "request.json"
    result_path = tmp_path / "handoff" / "result.json"
    write_handoff(request_path, request)

    assert request["schemaVersion"] == "norman.research.funding_handoff.v1"
    assert request["events"][0]["eventKey"] == event_key
    assert set(request["events"][0]) == {
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
    assert set(request["events"][0]["funding"]) == {
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
        cwd=CORE_ROOT,
        env=_isolated_env(tmp_path / "core-cli-home"),
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr

    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert validate_result(result, request=request) == result
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
