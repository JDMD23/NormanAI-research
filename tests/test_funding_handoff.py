from __future__ import annotations

import csv
import hashlib
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from lib.crunchbase_saved_list import FundingObservation
from lib.candidates import CRMX_CSV_COLUMNS
from lib.funding_handoff import (
    _configured_core_path,
    build_handoff,
    invoke_crm_handoff,
    invoke_crmx_funding_handoff,
    resolve_funding_handoff_target,
    synthesize_crmx_handoff_result,
    validate_result,
    write_crmx_funding_artifacts,
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
        "description",
        "founded",
        "headquarters",
        "industries",
        "funding",
    }
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
    assert calls[0] == [
        sys.executable,
        str(script.resolve()),
        "--handoff",
        str(request_path),
        "--result",
        str(result_path),
        "--dry-run",
    ]


def test_invoke_forwards_core_dispatcher_lease_fd(
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
    captured: dict = {}
    monkeypatch.setenv("NORMANAI_CORE_DISPATCH_FD", "23")

    def fake_run(command, **kwargs):
        captured.update(kwargs)
        payload = result_payload(request, mode="write")
        result_path.write_text(json.dumps(payload), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    invoke_crm_handoff(
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
        invoke_crm_handoff(
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
        invoke_crm_handoff(
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
        invoke_crm_handoff(
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
        invoke_crm_handoff(
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


def _fake_crmx(tmp_path: Path) -> Path:
    root = tmp_path / "NormanAI-CRMx"
    module = root / "src" / "norman" / "tools" / "ingest_csv.py"
    module.parent.mkdir(parents=True)
    module.write_text("# mock ingest\n", encoding="utf-8")
    (root / "src" / "norman" / "tools" / "__init__.py").write_text("")
    (root / "src" / "norman" / "__init__.py").write_text("")
    return root


def test_resolve_funding_handoff_target_defaults_to_crmx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NORMAN_FUNDING_HANDOFF_TARGET", raising=False)
    assert resolve_funding_handoff_target() == "crmx"
    assert resolve_funding_handoff_target(legacy=True) == "legacy_crm_core"
    with pytest.raises(RuntimeError, match="unknown funding handoff target"):
        resolve_funding_handoff_target(configured="notion")


def test_write_crmx_funding_artifacts_maps_csv_evidence_and_keeps_unknown_blank(
    tmp_path: Path,
) -> None:
    request = request_payload(observation())
    # Non-USD must not invent FX into the CSV money column.
    request["events"][0]["funding"]["currency"] = "EUR"
    csv_path = tmp_path / "out.crmx.csv"
    evidence_path = tmp_path / "out.evidence.json"
    write_crmx_funding_artifacts(
        request, csv_path=csv_path, evidence_path=evidence_path
    )
    with csv_path.open(encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        assert reader.fieldnames == CRMX_CSV_COLUMNS
        row = next(reader)
    assert row["Organization Name"] == "Weave"
    assert row["Last Funding Amount (in USD)"] == ""
    assert "0" not in {
        row["Last Funding Amount (in USD)"],
        row["Total Funding Amount (in USD)"],
        row["Number of Funding Rounds"],
    }
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence["schemaVersion"] == "norman.research.crmx_evidence.v1"
    assert evidence["candidates"][0]["event_key"]
    assert evidence["candidates"][0]["source_urls"]
    assert evidence["candidates"][0]["mode"] == "funding"
    assert "fit_score" not in evidence["candidates"][0]


def test_write_crmx_funding_artifacts_converts_usd_minor_units(
    tmp_path: Path,
) -> None:
    request = request_payload(observation())
    csv_path = tmp_path / "out.crmx.csv"
    evidence_path = tmp_path / "out.evidence.json"
    write_crmx_funding_artifacts(
        request, csv_path=csv_path, evidence_path=evidence_path
    )
    with csv_path.open(encoding="utf-8") as handle:
        row = next(csv.DictReader(handle))
    assert row["Last Funding Amount (in USD)"] == "13500000"
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence["candidates"][0]["last_funding_usd"] == 13_500_000.0


def test_synthesize_crmx_result_is_ledger_compatible() -> None:
    request = request_payload(observation())
    dry = synthesize_crmx_handoff_result(request, mode="dry_run")
    assert dry["mode"] == "dry_run"
    assert "pageId" not in dry["events"][0]
    validate_result(dry, request=request)
    written = synthesize_crmx_handoff_result(request, mode="write")
    assert written["events"][0]["pageId"].startswith("crmx:ingest:")
    validate_result(written, request=request)


def test_invoke_crmx_funding_handoff_calls_ingest_csv_with_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _fake_crmx(tmp_path)
    db = tmp_path / "norman.sqlite"
    db.write_text("")
    monkeypatch.setenv("NORMAN_CRMX_PATH", str(root))
    monkeypatch.setenv("NORMAN_CRMX_DB", str(db))
    monkeypatch.setattr("lib.sinks.shutil.which", lambda _: "/usr/bin/uv")

    request = request_payload(observation())
    request_path = (tmp_path / "handoffs" / f"{request['runId']}.request.json").resolve()
    result_path = (tmp_path / "handoffs" / f"{request['runId']}.result.json").resolve()
    write_handoff(request_path, request)
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        assert kwargs["cwd"] == str(root)
        return subprocess.CompletedProcess(
            cmd, 0, '{"counts":{"created":1}}\n', ""
        )

    monkeypatch.setattr("lib.sinks.subprocess.run", fake_run)
    result = invoke_crmx_funding_handoff(
        request_path, result_path, write=True
    )
    assert result["mode"] == "write"
    assert result["events"][0]["state"] == "created"
    assert calls and calls[0][:5] == [
        "uv",
        "run",
        "python",
        "-m",
        "norman.tools.ingest_csv",
    ]
    assert "--evidence" in calls[0]
    evidence_arg = Path(calls[0][calls[0].index("--evidence") + 1])
    assert evidence_arg.is_file()
    assert (request_path.parent / f"{request['runId']}.crmx.csv").is_file()


def test_invoke_crmx_dry_run_preflights_without_calling_ingest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _fake_crmx(tmp_path)
    db = tmp_path / "norman.sqlite"
    db.write_text("")
    monkeypatch.setenv("NORMAN_CRMX_PATH", str(root))
    monkeypatch.setenv("NORMAN_CRMX_DB", str(db))
    monkeypatch.setattr("lib.sinks.shutil.which", lambda _: "/usr/bin/uv")

    request = request_payload(observation())
    request_path = (tmp_path / f"{request['runId']}.request.json").resolve()
    result_path = (tmp_path / f"{request['runId']}.result.json").resolve()
    write_handoff(request_path, request)
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("lib.sinks.subprocess.run", fake_run)
    result = invoke_crmx_funding_handoff(
        request_path, result_path, write=False
    )
    assert result["mode"] == "dry_run"
    assert calls == []
    assert result_path.is_file()


def test_invoke_crmx_fails_closed_without_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _fake_crmx(tmp_path)
    monkeypatch.setenv("NORMAN_CRMX_PATH", str(root))
    monkeypatch.delenv("NORMAN_CRMX_DB", raising=False)
    from lib import config

    config.load_config.cache_clear()
    monkeypatch.setitem(config.research_config()["crmx"], "dbPath", "")
    request = request_payload(observation())
    request_path = (tmp_path / "request.json").resolve()
    write_handoff(request_path, request)
    with pytest.raises(RuntimeError, match="retryable"):
        invoke_crmx_funding_handoff(
            request_path, (tmp_path / "result.json").resolve(), write=True
        )
    config.load_config.cache_clear()


def test_invoke_crm_handoff_defaults_to_crmx_without_core_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _fake_crmx(tmp_path)
    db = tmp_path / "norman.sqlite"
    db.write_text("")
    monkeypatch.setenv("NORMAN_CRMX_PATH", str(root))
    monkeypatch.setenv("NORMAN_CRMX_DB", str(db))
    monkeypatch.setattr("lib.sinks.shutil.which", lambda _: "/usr/bin/uv")
    request = request_payload(observation())
    request_path = (tmp_path / "request.json").resolve()
    result_path = (tmp_path / "result.json").resolve()
    write_handoff(request_path, request)

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, '{"counts":{}}\n', "")

    monkeypatch.setattr("lib.sinks.subprocess.run", fake_run)
    result = invoke_crm_handoff(request_path, result_path, write=True)
    assert result["events"][0]["reason"] == "crmx_ingest_csv_accepted"


def test_invoke_crm_handoff_legacy_flag_uses_core_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    core = tmp_path / "Core CRM"
    script = core / "scripts" / "crm_funding_handoff.py"
    script.parent.mkdir(parents=True)
    script.write_text("# placeholder\n", encoding="utf-8")
    request = request_payload(observation())
    request_path = (tmp_path / "request.json").resolve()
    result_path = (tmp_path / "result.json").resolve()
    write_handoff(request_path, request)
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        payload = result_payload(request, mode="write")
        result_path.write_text(json.dumps(payload), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    validated = invoke_crm_handoff(
        request_path,
        result_path,
        write=True,
        legacy=True,
        core_path=core,
    )
    assert validated["mode"] == "write"
    assert "crm_funding_handoff.py" in calls[0][1]
