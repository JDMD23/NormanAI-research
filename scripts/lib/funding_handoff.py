"""Versioned bridge from Research discovery to CRMx SQLite funding ingest.

Primary path (ADR 0018): observations → Crunchbase-shaped CSV →
``uv run python -m norman.tools.funding_ingest`` → ``reconcile_sweep --apply``
→ narrow ``score_batch`` for the run cohort. Notion is projection only.

Legacy crm-core Notion handoff remains importable for tests but is not used
by the live watcher production dependencies.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from lib.crunchbase_saved_list import FundingObservation
from lib.funding_watcher_state import funding_event_key


REQUEST_SCHEMA_VERSION = "norman.research.funding_handoff.v1"
RESULT_SCHEMA_VERSION = "norman.crmx.funding_handoff_result.v1"
LEGACY_RESULT_SCHEMA_VERSION = "norman.crm_core.funding_handoff_result.v1"
TERMINAL_STATES = {
    "created",
    "queued_existing",
    "duplicate_event",
    "rejected_identity",
    "ambiguous_review",
}
ROOT = Path(__file__).resolve().parents[2]
LINKEDIN_COMPANY_PATH = re.compile(
    r"/company/(?P<slug>[a-z0-9]+(?:-[a-z0-9]+)*)(?:/about)?/?"
)
NEW_YORK = ZoneInfo("America/New_York")

# Exact CRMx EXPECTED_COLUMNS order (no extras — header drift fails closed).
CRMX_CSV_COLUMNS = [
    "Organization Name",
    "Organization Name URL",
    "Headquarters Location",
    "Founded Date",
    "Founded Date Precision",
    "Industries",
    "Last Funding Date",
    "Last Funding Amount",
    "Last Funding Amount Currency",
    "Last Funding Amount (in USD)",
    "Description",
    "Website",
    "X (Twitter)",
    "LinkedIn",
    "Founders",
    "Number of Funding Rounds",
    "Last Funding Type",
    "Total Funding Amount",
    "Total Funding Amount Currency",
    "Total Funding Amount (in USD)",
    "Top 5 Investors",
    "Lead Investors",
    "Full Description",
]


def _canonical_linkedin_company_url(value: str) -> str:
    raw = (value or "").strip()
    if not raw or any(character.isspace() for character in raw):
        return ""
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError:
        return ""
    host = (parsed.hostname or "").casefold()
    match = LINKEDIN_COMPANY_PATH.fullmatch(parsed.path.casefold())
    if (
        parsed.scheme.casefold() != "https"
        or host not in {"linkedin.com", "www.linkedin.com"}
        or parsed.username
        or parsed.password
        or port is not None
        or parsed.query
        or parsed.fragment
        or match is None
    ):
        return ""
    return f"https://www.linkedin.com/company/{match.group('slug')}"


def build_handoff(
    run_id: str,
    generated_at: str,
    events: Sequence[tuple[str, FundingObservation]],
) -> dict[str, Any]:
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError("run_id must be non-empty")
    _aware_datetime(generated_at, "generated_at")
    if not events:
        raise ValueError("handoff must contain at least one event")
    keys: list[str] = []
    serialized: list[dict[str, Any]] = []
    for key, observation in events:
        approved = funding_event_key(observation)
        if key != approved:
            raise ValueError("event key does not match approved fingerprint")
        if key in keys:
            raise ValueError("duplicate event key")
        keys.append(key)
        serialized.append(
            {
                "eventKey": key,
                "sourceName": observation.source_name,
                "sourceUrl": observation.source_url,
                "observedAt": observation.observed_at,
                "company": observation.company,
                "crunchbaseUrl": observation.crunchbase_url,
                "website": observation.website,
                "linkedin": _canonical_linkedin_company_url(observation.linkedin),
                "founders": list(observation.founders),
                "investors": list(observation.investors),
                "description": observation.description,
                "founded": observation.founded,
                "headquarters": observation.headquarters,
                "industries": list(observation.industries),
                "numberOfFundingRounds": observation.number_of_funding_rounds,
                "funding": {
                    "date": observation.funding_date,
                    "type": observation.funding_type,
                    "amountRaw": observation.funding_amount_raw,
                    "amountMinor": observation.funding_amount_minor,
                    "currency": observation.funding_currency,
                    "totalRaw": observation.total_funding_raw,
                    "totalAmountMinor": observation.total_funding_amount_minor,
                    "totalCurrency": observation.total_funding_currency,
                },
            }
        )
    return {
        "schemaVersion": REQUEST_SCHEMA_VERSION,
        "runId": run_id,
        "generatedAt": generated_at,
        "events": serialized,
    }


def write_handoff(path: Path, payload: dict[str, Any]) -> None:
    """Create an immutable canonical handoff request."""
    encoded = _canonical_request_bytes(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary_path, path)
        temporary_path.unlink()
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary_path.unlink(missing_ok=True)


def validate_result(
    payload: Any, *, request: dict[str, Any]
) -> dict[str, Any]:
    """Validate a CRMx (or legacy Core) result as an immutable answer."""
    if not isinstance(payload, dict) or set(payload) != {
        "schemaVersion",
        "runId",
        "requestDigest",
        "mode",
        "complete",
        "events",
    }:
        raise ValueError("CRM result has an invalid shape")
    if payload["schemaVersion"] not in {
        RESULT_SCHEMA_VERSION,
        LEGACY_RESULT_SCHEMA_VERSION,
    }:
        raise ValueError("CRM result schemaVersion is invalid")
    if payload["runId"] != request.get("runId"):
        raise ValueError("CRM result runId does not match request")
    expected_digest = hashlib.sha256(
        _canonical_request_bytes(request)
    ).hexdigest()
    if payload["requestDigest"] != expected_digest:
        raise ValueError("CRM result requestDigest does not match request")
    if payload["mode"] not in {"dry_run", "write"}:
        raise ValueError("CRM result mode is invalid")
    if payload["complete"] is not True:
        raise ValueError("CRM result is incomplete")
    raw_events = payload["events"]
    requested_events = request.get("events")
    if not isinstance(raw_events, list) or not isinstance(requested_events, list):
        raise ValueError("CRM result events must be a list")
    expected_keys = [event.get("eventKey") for event in requested_events]
    actual_keys: list[str] = []
    for event in raw_events:
        if not isinstance(event, dict):
            raise ValueError("CRM event result must be an object")
        allowed = {"eventKey", "state", "reason", "pageId"}
        required = {"eventKey", "state", "reason"}
        if set(event) - allowed or not required.issubset(event):
            raise ValueError("CRM event result has an invalid shape")
        key, state, reason = (
            event["eventKey"],
            event["state"],
            event["reason"],
        )
        if not all(isinstance(value, str) and value for value in (key, state, reason)):
            raise ValueError("CRM event result strings must be non-empty")
        if state not in TERMINAL_STATES:
            raise ValueError("complete CRM event result state must be terminal")
        page_id = event.get("pageId")
        if page_id is not None and (
            not isinstance(page_id, str) or not page_id.strip()
        ):
            raise ValueError("CRM event result pageId must be non-empty")
        if (
            payload["mode"] == "write"
            and state in {"created", "queued_existing"}
            and not page_id
        ):
            raise ValueError("write success requires pageId")
        actual_keys.append(key)
    if len(raw_events) != len(requested_events):
        raise ValueError("CRM result event count does not match request")
    if actual_keys != expected_keys:
        raise ValueError("CRM result event key order does not match request")
    return payload


def write_crmx_csv(path: Path, events: Sequence[dict[str, Any]]) -> None:
    """Write a Crunchbase-shaped CSV CRMx funding_ingest accepts."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CRMX_CSV_COLUMNS)
        writer.writeheader()
        for event in events:
            writer.writerow(_event_to_csv_row(event))


def _event_to_csv_row(event: dict[str, Any]) -> dict[str, str]:
    funding = event.get("funding") or {}
    if not isinstance(funding, dict):
        funding = {}
    founded_date, founded_precision = _founded_fields(str(event.get("founded") or ""))
    amount_minor = funding.get("amountMinor")
    currency = str(funding.get("currency") or "")
    total_minor = funding.get("totalAmountMinor")
    total_currency = str(funding.get("totalCurrency") or "")
    rounds = event.get("numberOfFundingRounds")
    investors = event.get("investors") if isinstance(event.get("investors"), list) else []
    founders = event.get("founders") if isinstance(event.get("founders"), list) else []
    industries = event.get("industries") if isinstance(event.get("industries"), list) else []
    return {
        "Organization Name": str(event.get("company") or ""),
        "Organization Name URL": str(event.get("crunchbaseUrl") or ""),
        "Headquarters Location": str(event.get("headquarters") or ""),
        "Founded Date": founded_date,
        "Founded Date Precision": founded_precision,
        "Industries": ", ".join(str(item) for item in industries if item),
        "Last Funding Date": str(funding.get("date") or ""),
        "Last Funding Amount": str(funding.get("amountRaw") or ""),
        "Last Funding Amount Currency": currency,
        "Last Funding Amount (in USD)": _usd_dollars(amount_minor, currency),
        "Description": str(event.get("description") or ""),
        "Website": str(event.get("website") or ""),
        "X (Twitter)": "",
        "LinkedIn": str(event.get("linkedin") or ""),
        "Founders": ", ".join(str(item) for item in founders if item),
        "Number of Funding Rounds": (
            str(rounds) if isinstance(rounds, int) and rounds > 0 else ""
        ),
        "Last Funding Type": str(funding.get("type") or ""),
        "Total Funding Amount": str(funding.get("totalRaw") or ""),
        "Total Funding Amount Currency": total_currency,
        "Total Funding Amount (in USD)": _usd_dollars(total_minor, total_currency),
        "Top 5 Investors": ", ".join(str(item) for item in investors if item),
        "Lead Investors": "",
        "Full Description": "",
    }


def _founded_fields(founded: str) -> tuple[str, str]:
    text = " ".join(founded.split())
    if not text:
        return "", ""
    if re.fullmatch(r"\d{4}", text):
        return f"{text}-01-01", "year"
    if re.fullmatch(r"\d{4}-\d{2}", text):
        return f"{text}-01", "month"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text, "day"
    # Browser sometimes yields "Jan 2024" etc. — leave Unknown rather than guess.
    return "", ""


def _usd_dollars(minor: Any, currency: str) -> str:
    if currency != "USD" or not isinstance(minor, int) or isinstance(minor, bool):
        return ""
    dollars = minor // 100
    if dollars <= 0:
        return ""
    return str(dollars)


def invoke_crm_handoff(
    request_path: Path,
    result_path: Path,
    *,
    write: bool,
    timeout_seconds: int = 900,
    core_path: Path | None = None,
) -> dict[str, Any]:
    """Invoke CRMx funding ingest (+ reconcile + cohort score) and validate."""
    del core_path  # legacy kwarg retained for call-site compatibility
    if not request_path.is_absolute() or not result_path.is_absolute():
        raise ValueError("handoff paths must be absolute")
    request = _load_json(request_path)
    crmx_root, db_path, added_from_prefix = _configured_crmx()
    csv_path = request_path.with_suffix(".crmx.csv")
    write_crmx_csv(csv_path, request["events"])
    generated = _aware_datetime(str(request["generatedAt"]), "generatedAt")
    local_day = generated.astimezone(NEW_YORK).date().isoformat()
    added_from = f"{added_from_prefix}:{local_day}"

    if not write:
        result = _synthesize_result(
            request,
            mode="dry_run",
            events=[
                {
                    "eventKey": event["eventKey"],
                    "state": "created",
                    "reason": "crmx_dry_run_preview",
                }
                for event in request["events"]
            ],
        )
        _write_json(result_path, result)
        return validate_result(result, request=request)

    pre_existing = _lookup_entities(db_path, [e.get("crunchbaseUrl") for e in request["events"]])
    _run_uv(
        crmx_root,
        [
            "python",
            "-m",
            "norman.tools.funding_ingest",
            str(csv_path),
            str(db_path),
            "--added-from",
            added_from,
        ],
        timeout_seconds=timeout_seconds,
        label="funding_ingest",
    )
    _run_uv(
        crmx_root,
        [
            "python",
            "-m",
            "norman.tools.reconcile_sweep",
            str(db_path),
            "--apply",
        ],
        timeout_seconds=timeout_seconds,
        label="reconcile_sweep",
    )
    # JD wants auto-score for newly ingested cohort. score_batch --apply is
    # attended-only under A19; funding-watcher is the explicit exception for
    # this cohort-scoped path (narrow --added-from date tag + --all).
    score = _run_uv(
        crmx_root,
        [
            "python",
            "-m",
            "norman.tools.score_batch",
            str(db_path),
            "--added-from",
            added_from,
            "--all",
            "--apply",
        ],
        timeout_seconds=timeout_seconds,
        label="score_batch",
        allow_failure=True,
    )
    post_existing = _lookup_entities(
        db_path, [e.get("crunchbaseUrl") for e in request["events"]]
    )
    result_events: list[dict[str, Any]] = []
    for event in request["events"]:
        url = str(event.get("crunchbaseUrl") or "")
        entity_id = post_existing.get(url.casefold())
        was = pre_existing.get(url.casefold())
        if entity_id and was:
            result_events.append(
                {
                    "eventKey": event["eventKey"],
                    "state": "queued_existing",
                    "reason": "crunchbase_url",
                    "pageId": entity_id,
                }
            )
        elif entity_id:
            result_events.append(
                {
                    "eventKey": event["eventKey"],
                    "state": "created",
                    "reason": "crmx_sqlite_ingest",
                    "pageId": entity_id,
                }
            )
        else:
            result_events.append(
                {
                    "eventKey": event["eventKey"],
                    "state": "rejected_identity",
                    "reason": "crmx_ingest_no_entity",
                }
            )
    result = _synthesize_result(request, mode="write", events=result_events)
    result_meta = {
        "addedFrom": added_from,
        "csvPath": str(csv_path),
        "scoreBatch": {
            "ok": score.returncode == 0,
            "stdout": (score.stdout or "")[-2000:],
            "stderr": (score.stderr or "")[-1000:],
        },
    }
    _write_json(result_path, result)
    meta_path = result_path.with_suffix(".crmx-meta.json")
    _write_json(meta_path, result_meta)
    return validate_result(result, request=request)


def invoke_legacy_crm_core_handoff(
    request_path: Path,
    result_path: Path,
    *,
    write: bool,
    timeout_seconds: int = 900,
    core_path: Path | None = None,
) -> dict[str, Any]:
    """Legacy Notion-era Core CLI (tests / emergency only)."""
    if not request_path.is_absolute() or not result_path.is_absolute():
        raise ValueError("handoff paths must be absolute")
    request = _load_json(request_path)
    resolved_core = (core_path or _configured_core_path()).expanduser().resolve()
    script = resolved_core / "scripts" / "crm_funding_handoff.py"
    if not script.is_file():
        raise RuntimeError(f"CRM funding handoff CLI is missing: {script}")
    command = [
        sys.executable,
        str(script),
        "--handoff",
        str(request_path),
        "--result",
        str(result_path),
    ]
    command.extend(["--write", "--yes"] if write else ["--dry-run"])
    raw_dispatcher_fd = str(
        os.environ.get("NORMANAI_CORE_DISPATCH_FD") or ""
    ).strip()
    pass_fds = (int(raw_dispatcher_fd),) if raw_dispatcher_fd else ()
    try:
        completed = subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
            pass_fds=pass_fds,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("CRM handoff retryable timeout") from exc
    if completed.returncode:
        raise RuntimeError(f"CRM handoff retryable exit {completed.returncode}")
    if not result_path.exists():
        raise RuntimeError("CRM handoff retryable missing result")
    result = validate_result(_load_json(result_path), request=request)
    expected_mode = "write" if write else "dry_run"
    if result["mode"] != expected_mode:
        raise ValueError("CRM result mode does not match invocation")
    return result


def _synthesize_result(
    request: dict[str, Any],
    *,
    mode: str,
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    digest = hashlib.sha256(_canonical_request_bytes(request)).hexdigest()
    return {
        "schemaVersion": RESULT_SCHEMA_VERSION,
        "runId": request["runId"],
        "requestDigest": digest,
        "mode": mode,
        "complete": True,
        "events": events,
    }


def _configured_crmx() -> tuple[Path, Path, str]:
    override_root = os.environ.get("NORMAN_CRMX_PATH")
    override_db = os.environ.get("NORMAN_CRMX_DB")
    config = _load_json(ROOT / "config" / "research.json")
    block = config.get("crmx") or {}
    if override_root:
        root = Path(override_root).expanduser()
    else:
        value = block.get("path")
        if not isinstance(value, str) or not value.strip():
            raise RuntimeError("crmx.path is not configured")
        root = Path(value).expanduser()
        if not root.is_absolute():
            root = (ROOT / root).resolve()
    if not root.is_absolute():
        raise RuntimeError("NORMAN_CRMX_PATH / crmx.path must be absolute")
    root = root.resolve()
    if override_db:
        db = Path(override_db).expanduser().resolve()
    else:
        rel = block.get("db") or "data/norman.db"
        db = Path(rel).expanduser()
        if not db.is_absolute():
            db = (root / db).resolve()
    prefix = block.get("addedFromPrefix") or "crunchbase-watcher"
    if not isinstance(prefix, str) or not prefix.strip():
        raise RuntimeError("crmx.addedFromPrefix is invalid")
    if not root.is_dir():
        raise RuntimeError(f"CRMx checkout missing: {root}")
    if not db.is_file():
        raise RuntimeError(f"CRMx SQLite missing: {db}")
    return root, db, prefix.strip()


def _configured_core_path() -> Path:
    override = os.environ.get("NORMAN_CRM_CORE_PATH")
    if override:
        path = Path(override).expanduser()
        if not path.is_absolute():
            raise RuntimeError("NORMAN_CRM_CORE_PATH must be absolute")
        return path.resolve()
    config = _load_json(ROOT / "config" / "research.json")
    value = (config.get("crmCore") or {}).get("path")
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError("crmCore.path is not configured")
    return ROOT / value


def _run_uv(
    crmx_root: Path,
    args: list[str],
    *,
    timeout_seconds: int,
    label: str,
    allow_failure: bool = False,
) -> subprocess.CompletedProcess[str]:
    command = ["uv", "run", *args]
    try:
        completed = subprocess.run(
            command,
            cwd=str(crmx_root),
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("uv is required for CRMx funding handoff") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"CRMx {label} retryable timeout") from exc
    if completed.returncode and not allow_failure:
        detail = (completed.stderr or completed.stdout or "")[:500]
        raise RuntimeError(f"CRMx {label} exit {completed.returncode}: {detail}")
    return completed


def _lookup_entities(db_path: Path, urls: Sequence[Any]) -> dict[str, str]:
    wanted = {
        str(url).strip().casefold()
        for url in urls
        if isinstance(url, str) and url.strip()
    }
    if not wanted:
        return {}
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT entity_id, crunchbase_url FROM companies "
            "WHERE crunchbase_url IS NOT NULL AND trim(crunchbase_url) != ''"
        ).fetchall()
    finally:
        conn.close()
    found: dict[str, str] = {}
    for entity_id, url in rows:
        key = str(url).strip().casefold()
        if key in wanted:
            found[key] = str(entity_id)
    return found


def _canonical_request_bytes(payload: dict[str, Any]) -> bytes:
    return (
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode("utf-8")


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"unreadable JSON artifact: {path}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"JSON artifact must be an object: {path}")
    return payload


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _aware_datetime(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ValueError(f"{label} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} must include timezone")
    return parsed
