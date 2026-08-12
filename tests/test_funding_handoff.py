from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from lib.crunchbase_saved_list import FundingObservation
from lib.funding_handoff import (
    _configured_core_path,
    _lookup_entities,
    build_handoff,
    invoke_crm_handoff,
    invoke_legacy_crm_core_handoff,
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
    assert set(event) == {
        "eventKey",
        "sourceName",
        "sourceUrl",
        "observedAt",
        "company",
        "crunchbaseUrl",
        "website",
        "linkedin",
        "founders",
        "investors",
        "description",
        "founded",
        "headquarters",
        "industries",
        "numberOfFundingRounds",
        "funding",
    }
    assert isinstance(event["founders"], list)
    assert isinstance(event["industries"], list)
    assert isinstance(event["investors"], list)
    assert event["numberOfFundingRounds"] == 2
    assert event["funding"] == {
        "date": "2026-07-28",
        "type": "Series A",
        "amountRaw": "$13.5M",
        "amountMinor": 1_350_000_000,
        "currency": "USD",
        "totalRaw": "$14M",
        "totalAmountMinor": 1_400_000_000,
        "totalCurrency": "USD",
    }


def test_build_handoff_canonicalizes_or_drops_noncanonical_linkedin_urls() -> None:
    about = replace(
        observation("Quinbrook"),
        crunchbase_url="https://www.crunchbase.com/organization/quinbrook",
        linkedin=(
            "https://www.linkedin.com/company/"
            "quinbrook-infrastructure-partners/about/"
        ),
    )
    malformed = replace(
        observation("The Fundworks"),
        crunchbase_url="https://www.crunchbase.com/organization/the-fundworks",
        linkedin="https://www.linkedin.com/company/linkedin.comthefundworksllc",
    )

    payload = request_payload(about, malformed)

    assert payload["events"][0]["linkedin"] == (
        "https://www.linkedin.com/company/quinbrook-infrastructure-partners"
    )
    assert payload["events"][1]["linkedin"] == ""


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


def test_write_handoff_does_not_expose_partial_target_during_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = request_payload(observation())
    path = tmp_path / "request.json"
    real_fdopen = os.fdopen

    class ObservedWriter:
        def __init__(self, fd: int, mode: str) -> None:
            self._handle = real_fdopen(fd, mode)

        def __enter__(self):
            self._handle.__enter__()
            return self

        def __exit__(self, *args):
            return self._handle.__exit__(*args)

        def write(self, data: bytes) -> int:
            first = self._handle.write(data[:1])
            self._handle.flush()
            assert not path.exists(), "partial request was visible at target path"
            remainder = self._handle.write(data[1:])
            return first + remainder

        def flush(self) -> None:
            self._handle.flush()

        def fileno(self) -> int:
            return self._handle.fileno()

    monkeypatch.setattr(os, "fdopen", ObservedWriter)
    write_handoff(path, payload)

    assert json.loads(path.read_text(encoding="utf-8")) == payload


@pytest.mark.parametrize(
    "mutation",
    [
        lambda result: result.update(schemaVersion="wrong"),
        lambda result: result.update(runId="wrong"),
        lambda result: result.update(requestDigest="0" * 64),
        lambda result: result.update(mode="preview"),
        lambda result: result.update(complete=False),
        lambda result: result.update(extra=True),
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


def test_validate_result_rejects_retryable_event_in_complete_result() -> None:
    request = request_payload(observation())
    result = result_payload(request)
    result["events"][0].update(
        state="retryable_failure",
        reason="transient_core_failure",
    )
    result["events"][0].pop("pageId")

    with pytest.raises(ValueError, match="terminal"):
        validate_result(result, request=request)


def test_lookup_entities_opens_normal_path_sqlite_and_maps_urls(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "norman.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TABLE companies ("
            "entity_id TEXT NOT NULL, "
            "crunchbase_url TEXT)"
        )
        conn.executemany(
            "INSERT INTO companies (entity_id, crunchbase_url) VALUES (?, ?)",
            [
                ("ent-weave", "https://www.crunchbase.com/organization/weave"),
                ("ent-other", "https://www.crunchbase.com/organization/other"),
                ("ent-blank", "  "),
                ("ent-null", None),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    found = _lookup_entities(
        db_path,
        [
            "https://www.crunchbase.com/organization/Weave",
            "https://www.crunchbase.com/organization/missing",
            "",
            None,
        ],
    )

    assert found == {
        "https://www.crunchbase.com/organization/weave": "ent-weave",
    }


def test_invoke_crmx_dry_run_writes_csv_and_skips_uv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    crmx = tmp_path / "NormanAI-CRMx"
    db = crmx / "data" / "norman.db"
    db.parent.mkdir(parents=True)
    db.write_bytes(b"")
    (crmx / "src/norman/tools").mkdir(parents=True)
    (crmx / "src/norman/tools/funding_ingest.py").write_text("# stub\n")
    research = Path(__file__).resolve().parents[1] / "config" / "research.json"
    # Point env overrides at temp CRMx
    monkeypatch.setenv("NORMAN_CRMX_PATH", str(crmx))
    monkeypatch.setenv("NORMAN_CRMX_DB", str(db))
    request = request_payload(observation())
    request_path = tmp_path / "request.json"
    result_path = tmp_path / "result.json"
    write_handoff(request_path, request)
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    validated = invoke_crm_handoff(
        request_path,
        result_path,
        write=False,
    )

    assert validated["mode"] == "dry_run"
    assert validated["events"][0]["reason"] == "crmx_dry_run_preview"
    assert request_path.with_suffix(".crmx.csv").is_file()
    assert calls == []


def test_legacy_invoke_forwards_core_dispatcher_lease_fd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lib.funding_handoff import invoke_legacy_crm_core_handoff

    core = tmp_path / "Core CRM"
    script = core / "scripts" / "crm_funding_handoff.py"
    script.parent.mkdir(parents=True)
    script.write_text("# placeholder\n", encoding="utf-8")
    request = request_payload(observation())
    request_path = tmp_path / "request.json"
    result_path = tmp_path / "result.json"
    write_handoff(request_path, request)
    captured: dict = {}
    monkeypatch.setenv("NORMANAI_CORE_DISPATCH_FD", "23")

    def fake_run(command, **kwargs):
        captured.update(kwargs)
        payload = result_payload(request, mode="write")
        # legacy validator accepts crmx or legacy schema; force legacy
        payload["schemaVersion"] = "norman.crm_core.funding_handoff_result.v1"
        result_path.write_text(json.dumps(payload), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    invoke_legacy_crm_core_handoff(
        request_path,
        result_path,
        write=True,
        core_path=core,
    )

    assert captured["pass_fds"] == (23,)


def test_zero_exit_does_not_return_complete_result_with_retryable_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    core = tmp_path / "core"
    script = core / "scripts" / "crm_funding_handoff.py"
    script.parent.mkdir(parents=True)
    script.write_text("# placeholder\n", encoding="utf-8")
    request = request_payload(observation())
    request_path = tmp_path / "request.json"
    result_path = tmp_path / "result.json"
    write_handoff(request_path, request)

    def fake_run(command, **kwargs):
        result = result_payload(request)
        result["events"][0].update(
            state="retryable_failure",
            reason="transient_core_failure",
        )
        result["events"][0].pop("pageId")
        result_path.write_text(json.dumps(result), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(ValueError, match="terminal"):
        invoke_legacy_crm_core_handoff(
            request_path,
            result_path,
            write=True,
            core_path=core,
        )


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
        invoke_legacy_crm_core_handoff(
            request_path,
            result_path,
            write=True,
            core_path=core,
        )


def test_nonzero_core_error_does_not_copy_environment_secret_into_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    core = tmp_path / "core"
    script = core / "scripts" / "crm_funding_handoff.py"
    script.parent.mkdir(parents=True)
    script.write_text("# placeholder\n", encoding="utf-8")
    request_path = tmp_path / "request.json"
    write_handoff(request_path, request_payload(observation()))
    secret = "secret-notion-token"
    monkeypatch.setenv("NOTION_TOKEN", secret)

    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(
            command,
            75,
            f"stdout leaked {secret}",
            f"stderr leaked {secret}",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="retryable") as exc_info:
        invoke_legacy_crm_core_handoff(
            request_path,
            tmp_path / "result.json",
            write=True,
            core_path=core,
        )

    assert secret not in str(exc_info.value)


def test_timeout_remains_retryable_instead_of_consuming_stale_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    core = tmp_path / "core"
    script = core / "scripts" / "crm_funding_handoff.py"
    script.parent.mkdir(parents=True)
    script.write_text("# placeholder\n", encoding="utf-8")
    request = request_payload(observation())
    request_path = tmp_path / "request.json"
    result_path = tmp_path / "result.json"
    write_handoff(request_path, request)
    result_path.write_text(
        json.dumps(result_payload(request)),
        encoding="utf-8",
    )

    def fake_run(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="retryable timeout"):
        invoke_legacy_crm_core_handoff(
            request_path,
            result_path,
            write=True,
            timeout_seconds=17,
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


def test_explicit_core_path_override_supports_cross_repo_acceptance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    core = tmp_path / "Core CRM"
    core.mkdir()
    monkeypatch.setenv("NORMAN_CRM_CORE_PATH", str(core))
    assert _configured_core_path() == core.resolve()


def test_core_path_override_must_be_absolute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NORMAN_CRM_CORE_PATH", "../Core CRM")
    with pytest.raises(RuntimeError, match="absolute"):
        _configured_core_path()
