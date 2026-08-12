"""Promote wiring: CRMx by default, legacy behind an explicit flag, no Notion write."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from lib import config
from lib.candidates import CRMX_CSV_COLUMNS, Candidate
from lib.sinks import (
    SinkError,
    promote,
    promote_via_crmx,
    resolve_promote_target,
    write_crmx_intake_csv,
    write_evidence,
)


def _clear_config_cache() -> None:
    config.load_config.cache_clear()


@pytest.fixture(autouse=True)
def _reset_config_cache():
    _clear_config_cache()
    yield
    _clear_config_cache()


def _candidate(**overrides) -> Candidate:
    base = dict(
        company="Acme Space",
        website="https://acme.example",
        linkedin="https://www.linkedin.com/company/acme",
        crunchbase="https://www.crunchbase.com/organization/acme",
        one_liner="Builds desks",
        hq="NYC",
        industries="Real Estate",
        last_funding_usd=None,
        num_rounds=None,
        nyc_evidence="Opened a 40k sqft office in Manhattan",
        nyc_angle="strong",
        keyword_hits=["opened office", "Manhattan"],
        source_urls=["https://example.com/lease"],
        mode="office_expansion",
        lane="web/office",
    )
    base.update(overrides)
    return Candidate(**base)


def _fake_crmx(tmp_path: Path) -> Path:
    root = tmp_path / "NormanAI-CRMx"
    module = root / "src" / "norman" / "tools" / "ingest_csv.py"
    module.parent.mkdir(parents=True)
    module.write_text("# mock ingest\n")
    (root / "src" / "norman" / "tools" / "__init__.py").write_text("")
    (root / "src" / "norman" / "__init__.py").write_text("")
    return root


def test_promote_defaults_off_and_targets_crmx() -> None:
    assert resolve_promote_target() is None
    assert resolve_promote_target(promote=True) == "crmx"
    assert resolve_promote_target(legacy=True) == "legacy_crm_core"


def test_unknown_promote_target_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(config.research_config()["promote"], "target", "notion")
    with pytest.raises(SinkError, match="unknown promote.target"):
        resolve_promote_target(promote=True)


def test_crmx_csv_preserves_unknown_as_blank_and_maps_columns(
    tmp_path: Path,
) -> None:
    cand = _candidate()
    path = write_crmx_intake_csv([cand], tmp_path / "crmx.csv")
    text = path.read_text(encoding="utf-8")
    header = text.splitlines()[0].split(",")
    assert header == CRMX_CSV_COLUMNS
    assert "Organization Name" in text
    assert "Acme Space" in text
    # Unknown ≠ 0: null money fields stay empty cells, not 0.
    row = text.splitlines()[1]
    assert ",0," not in f",{row},"
    assert "Last Funding Amount (in USD)" in text


def test_evidence_sidecar_keeps_qualification_fields(tmp_path: Path) -> None:
    cand = _candidate()
    path = write_evidence([cand], tmp_path / "evidence.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schemaVersion"] == "norman.research.crmx_evidence.v1"
    assert payload["candidates"][0]["nyc_evidence"]
    assert payload["candidates"][0]["source_urls"] == ["https://example.com/lease"]
    assert payload["candidates"][0]["keyword_hits"] == ["opened office", "Manhattan"]
    assert "fit_score" not in payload["candidates"][0]


def test_promote_via_crmx_invokes_uv_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _fake_crmx(tmp_path)
    db = tmp_path / "norman.sqlite"
    db.write_text("")
    csv_path = write_crmx_intake_csv([_candidate()], tmp_path / "in.csv")
    evidence = write_evidence([_candidate()], tmp_path / "ev.json")

    monkeypatch.setenv("NORMAN_CRMX_PATH", str(root))
    monkeypatch.setenv("NORMAN_CRMX_DB", str(db))

    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        assert kwargs["cwd"] == str(root)
        return SimpleNamespace(returncode=0, stdout='{"counts":{"created":1}}\n', stderr="")

    monkeypatch.setattr("lib.sinks.subprocess.run", fake_run)
    monkeypatch.setattr("lib.sinks.shutil.which", lambda _: "/usr/bin/uv")

    summary = promote_via_crmx(csv_path, evidence_path=evidence)
    assert summary["target"] == "crmx"
    assert summary["counts"] == {"created": 1}
    assert summary["evidence"] == str(evidence)
    assert calls[0][:5] == ["uv", "run", "python", "-m", "norman.tools.ingest_csv"]
    assert calls[0][5] == str(csv_path.resolve())
    assert calls[0][6] == str(db.resolve())
    assert "--added-from" in calls[0]
    assert calls[0][calls[0].index("--added-from") + 1].startswith("research:")
    assert "--evidence" in calls[0]
    assert calls[0][calls[0].index("--evidence") + 1] == str(evidence.resolve())
    assert "NOT passed" not in (summary.get("note") or "")


def test_promote_via_crmx_fails_closed_without_evidence_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _fake_crmx(tmp_path)
    db = tmp_path / "norman.sqlite"
    db.write_text("")
    monkeypatch.setenv("NORMAN_CRMX_PATH", str(root))
    monkeypatch.setenv("NORMAN_CRMX_DB", str(db))
    csv_path = write_crmx_intake_csv([_candidate()], tmp_path / "in.csv")
    with pytest.raises(SinkError, match="requires an evidence sidecar"):
        promote_via_crmx(csv_path)


def test_promote_via_crmx_fails_closed_when_evidence_file_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _fake_crmx(tmp_path)
    db = tmp_path / "norman.sqlite"
    db.write_text("")
    monkeypatch.setenv("NORMAN_CRMX_PATH", str(root))
    monkeypatch.setenv("NORMAN_CRMX_DB", str(db))
    csv_path = write_crmx_intake_csv([_candidate()], tmp_path / "in.csv")
    missing = tmp_path / "missing-evidence.json"
    with pytest.raises(SinkError, match="evidence sidecar missing"):
        promote_via_crmx(csv_path, evidence_path=missing)


def test_promote_via_crmx_fails_closed_without_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _fake_crmx(tmp_path)
    monkeypatch.setenv("NORMAN_CRMX_PATH", str(root))
    monkeypatch.delenv("NORMAN_CRMX_DB", raising=False)
    monkeypatch.setitem(config.research_config()["crmx"], "dbPath", "")
    csv_path = write_crmx_intake_csv([_candidate()], tmp_path / "in.csv")
    with pytest.raises(SinkError, match="NORMAN_CRMX_DB"):
        promote_via_crmx(csv_path, evidence_path=tmp_path / "ev.json")


def test_promote_via_crmx_fails_closed_without_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "missing-crmx"
    monkeypatch.setenv("NORMAN_CRMX_PATH", str(missing))
    monkeypatch.setenv("NORMAN_CRMX_DB", str(tmp_path / "db.sqlite"))
    with pytest.raises(SinkError, match="checkout not found"):
        promote_via_crmx(tmp_path / "x.csv", evidence_path=tmp_path / "ev.json")


def test_promote_via_crmx_fails_closed_without_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "empty-crmx"
    root.mkdir()
    db = tmp_path / "norman.sqlite"
    db.write_text("")
    monkeypatch.setenv("NORMAN_CRMX_PATH", str(root))
    monkeypatch.setenv("NORMAN_CRMX_DB", str(db))
    with pytest.raises(SinkError, match="ingest module"):
        promote_via_crmx(tmp_path / "x.csv", evidence_path=tmp_path / "ev.json")


def test_promote_via_crmx_does_not_treat_core_intake_as_crmx_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail closed: bare core/intake.py must not satisfy ingest_csv preflight.

    `"intake" in "norman.tools.ingest_csv"` is true, so a substring check against
    an unrelated intake file was fail-open. Promote must require the real module.
    """
    root = tmp_path / "not-really-crmx"
    decoy = root / "core" / "intake.py"
    decoy.parent.mkdir(parents=True)
    decoy.write_text("# unrelated intake decoy\n", encoding="utf-8")
    db = tmp_path / "norman.sqlite"
    db.write_text("")
    evidence = write_evidence([_candidate()], tmp_path / "ev.json")
    monkeypatch.setenv("NORMAN_CRMX_PATH", str(root))
    monkeypatch.setenv("NORMAN_CRMX_DB", str(db))
    with pytest.raises(SinkError, match="ingest module"):
        promote_via_crmx(tmp_path / "x.csv", evidence_path=evidence)


def test_promote_via_crmx_refuses_dry_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _fake_crmx(tmp_path)
    db = tmp_path / "norman.sqlite"
    db.write_text("")
    evidence = write_evidence([_candidate()], tmp_path / "ev.json")
    monkeypatch.setenv("NORMAN_CRMX_PATH", str(root))
    monkeypatch.setenv("NORMAN_CRMX_DB", str(db))
    with pytest.raises(SinkError, match="dry-run"):
        promote_via_crmx(tmp_path / "x.csv", evidence_path=evidence, dry_run=True)


def test_promote_dispatcher_routes_legacy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    core = tmp_path / "Core CRM"
    script = core / "scripts" / "crm_intake.py"
    script.parent.mkdir(parents=True)
    script.write_text("print('legacy')\n")
    (core / "state").mkdir()
    (core / "state" / "intake_latest.json").write_text(
        json.dumps({"counts": {"created": 2}})
    )
    monkeypatch.setenv("NORMAN_CRM_CORE_PATH", str(core))

    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr("lib.sinks.subprocess.run", fake_run)
    csv_path = tmp_path / "legacy.csv"
    csv_path.write_text("Company\nAcme\n")
    summary = promote(csv_path, target="legacy_crm_core")
    assert summary["target"] == "legacy_crm_core"
    assert summary["counts"] == {"created": 2}
    assert any("crm_intake.py" in part for part in calls[0])


def test_promote_dispatcher_refuses_unknown_target(tmp_path: Path) -> None:
    with pytest.raises(SinkError, match="unknown promote target"):
        promote(tmp_path / "x.csv", target="notion_writer")


def test_pipeline_promote_wires_crmx(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lib import pipeline

    root = _fake_crmx(tmp_path)
    db = tmp_path / "norman.sqlite"
    db.write_text("")
    monkeypatch.setenv("NORMAN_CRMX_PATH", str(root))
    monkeypatch.setenv("NORMAN_CRMX_DB", str(db))
    # Keep config.ROOT at the real repo so research.json still loads; only
    # redirect pipeline artifacts + the seen-store into tmp_path.
    monkeypatch.setattr(pipeline, "ROOT", tmp_path)
    monkeypatch.setitem(
        config.research_config()["dedup"],
        "localStore",
        str(tmp_path / "research.db"),
    )

    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return SimpleNamespace(
            returncode=0, stdout='{"counts":{"created":1}}\n', stderr=""
        )

    monkeypatch.setattr("lib.sinks.subprocess.run", fake_run)
    monkeypatch.setattr("lib.sinks.shutil.which", lambda _: "/usr/bin/uv")
    monkeypatch.setattr(pipeline, "notion_existing_keys", lambda: set())

    args = SimpleNamespace(
        write=True,
        promote=True,
        promote_legacy_crm_core=False,
        show_rejects=False,
        no_notion_check=True,
        mode=None,
    )
    outcome = pipeline.run([_candidate()], [], args, label="test", slug="unit")
    assert outcome["promote_target"] == "crmx"
    assert outcome["promote"] == {"created": 1}
    assert Path(outcome["crmx_csv"]).is_file()
    assert Path(outcome["evidence"]).is_file()
    evidence = json.loads(Path(outcome["evidence"]).read_text())
    assert evidence["candidates"][0]["keyword_hits"]
    assert calls and calls[0][4] == "norman.tools.ingest_csv"
    assert "--evidence" in calls[0]
    evidence_arg = calls[0][calls[0].index("--evidence") + 1]
    assert Path(evidence_arg).is_file()
    assert Path(evidence_arg).resolve() == Path(outcome["evidence"]).resolve()
