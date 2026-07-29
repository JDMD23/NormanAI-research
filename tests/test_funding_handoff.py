from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from lib.crunchbase_saved_list import FundingObservation
from lib.funding_handoff import (
    build_handoff,
    invoke_crm_handoff,
    validate_result,
    write_handoff,
)
from lib.funding_watcher_state import funding_event_key


SOURCE = (
    "https://www.crunchbase.com/discover/saved/"
    "main-funding-july-2026/730c458b-149c-4a0a-9684-7146e7258993"
)


def observation(company: str = "Weave") -> FundingObservation:
    return FundingObservation(
        source_name="Main Funding - July 2026",
        source_url=SOURCE,
        observed_at="2026-07-29T12:00:00+00:00",
        company=company,
        crunchbase_url=f"https://www.crunchbase.com/organization/{company.casefold()}",
        funding_date="2026-07-28",
        funding_type="Series A",
        funding_amount_raw="$13.5M",
        funding_amount_minor=1_350_000_000,
        funding_currency="USD",
        total_funding_raw="$14M",
        total_funding_amount_minor=1_400_000_000,
        total_funding_currency="USD",
        number_of_funding_rounds=2,
        website=f"https://{company.casefold()}.example",
        linkedin=f"https://linkedin.com/company/{company.casefold()}",
        headquarters="New York",
        founded="2024",
        description="Raw description",
        industries=("Software", "Artificial Intelligence"),
        founders=("Founder One", "Founder Two"),
        investors=("Investor",),
    )


def request_payload(*rows: FundingObservation) -> dict:
    return build_handoff(
        "20260729T120604Z",
        "2026-07-29T12:06:04+00:00",
        [(funding_event_key(row), row) for row in rows],
    )


def result_payload(request: dict, *, mode: str = "write") -> dict:
    result = json.loads(
        (
            Path(__file__).parent
            / "fixtures"
            / "funding_handoff_result_v1.json"
        ).read_text(encoding="utf-8")
    )
    request_bytes = (
        json.dumps(request, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    result["requestDigest"] = hashlib.sha256(request_bytes).hexdigest()
    result["mode"] = mode
    result["events"][0]["eventKey"] = request["events"][0]["eventKey"]
    if mode == "dry_run":
        result["events"][0].pop("pageId")
    return result


def test_build_handoff_matches_exact_contract_and_preserves_raw_facts() -> None:
    row = observation()
    payload = request_payload(row)

    assert set(payload) == {"schemaVersion", "runId", "generatedAt", "events"}
    assert payload["schemaVersion"] == "norman.research.funding_handoff.v1"
    event = payload["events"][0]
    assert isinstance(event["founders"], list)
    assert isinstance(event["industries"], list)
    assert event["funding"] == {
        "date": "2026-07-28",
        "type": "Series A",
        "amountRaw": "$13.5M",
        "amountMinor": 1_350_000_000,
        "currency": "USD",
        "totalRaw": "$14M",
    }


def test_build_handoff_rejects_wrong_or_duplicate_keys() -> None:
    row = observation()
    with pytest.raises(ValueError, match="event key"):
        build_handoff(
            "run", "2026-07-29T12:06:04+00:00", [("0" * 64, row)]
        )
    key = funding_event_key(row)
    with pytest.raises(ValueError, match="duplicate"):
        build_handoff(
            "run",
            "2026-07-29T12:06:04+00:00",
            [(key, row), (key, row)],
        )


def test_write_handoff_is_atomic_and_refuses_overwrite(tmp_path: Path) -> None:
    payload = request_payload(observation())
    path = tmp_path / "request.json"
    write_handoff(path, payload)
    expected = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
    assert path.read_bytes() == expected
    with pytest.raises(FileExistsError):
        write_handoff(path, payload)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda result: result.update(schemaVersion="wrong"),
        lambda result: result.update(runId="wrong"),
        lambda result: result.update(requestDigest="0" * 64),
        lambda result: result.update(complete=False),
        lambda result: result["events"].clear(),
        lambda result: result["events"].append(dict(result["events"][0])),
        lambda result: result["events"][0].update(eventKey="f" * 64),
        lambda result: result["events"][0].update(state="mystery"),
        lambda result: result["events"][0].pop("reason"),
        lambda result: result["events"][0].update(extra=True),
    ],
)
def test_validate_result_rejects_schema_identity_count_and_event_drift(
    mutation,
) -> None:
    request = request_payload(observation())
    result = result_payload(request)
    mutation(result)
    with pytest.raises(ValueError):
        validate_result(result, request=request)


def test_validate_result_rejects_reordered_event_keys() -> None:
    first, second = observation("Alpha"), observation("Beta")
    request = request_payload(first, second)
    result = result_payload(request)
    result["events"] = [
        {
            "eventKey": row["eventKey"],
            "state": "queued_existing",
            "reason": "existing_match",
            "pageId": f"page-{index}",
        }
        for index, row in enumerate(reversed(request["events"]))
    ]
    result["requestDigest"] = hashlib.sha256(
        (json.dumps(request, indent=2, sort_keys=True) + "\n").encode()
    ).hexdigest()
    with pytest.raises(ValueError, match="order"):
        validate_result(result, request=request)


def test_validate_result_requires_page_id_for_write_success() -> None:
    request = request_payload(observation())
    result = result_payload(request)
    result["events"][0].pop("pageId")
    with pytest.raises(ValueError, match="pageId"):
        validate_result(result, request=request)


def test_invoke_uses_absolute_core_cli_and_mode_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    core = tmp_path / "Core CRM"
    script = core / "scripts" / "crm_funding_handoff.py"
    script.parent.mkdir(parents=True)
    script.write_text("# placeholder\n", encoding="utf-8")
    request = request_payload(observation())
    request_path = tmp_path / "request.json"
    result_path = tmp_path / "result.json"
    write_handoff(request_path, request)
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        payload = result_payload(request, mode="dry_run")
        result_path.write_text(json.dumps(payload), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    validated = invoke_crm_handoff(
        request_path,
        result_path,
        write=False,
        core_path=core,
    )

    assert validated["mode"] == "dry_run"
    assert calls[0][1] == str(script.resolve())
    assert calls[0][-1] == "--dry-run"
    assert "--write" not in calls[0]


def test_invoke_write_uses_yes_and_nonzero_is_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    core = tmp_path / "core"
    script = core / "scripts" / "crm_funding_handoff.py"
    script.parent.mkdir(parents=True)
    script.write_text("# placeholder\n", encoding="utf-8")
    request = request_payload(observation())
    request_path = tmp_path / "request.json"
    write_handoff(request_path, request)
    result_path = tmp_path / "result.json"

    def fake_run(command, **kwargs):
        assert command[-2:] == ["--write", "--yes"]
        return subprocess.CompletedProcess(command, 75, "", "retry")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="retryable"):
        invoke_crm_handoff(
            request_path,
            result_path,
            write=True,
            core_path=core,
        )


def test_handoff_never_serializes_environment_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NOTION_TOKEN", "secret-notion-token")
    monkeypatch.setenv("XAI_API_KEY", "secret-xai-key")
    serialized = json.dumps(request_payload(observation()))
    assert "secret-notion-token" not in serialized
    assert "secret-xai-key" not in serialized
