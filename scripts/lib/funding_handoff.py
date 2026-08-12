"""Versioned JSON bridge from Research funding discovery to CRMx (or legacy Core).

Default path: convert typed funding events → CRMx CSV + evidence sidecar →
`norman.tools.ingest_csv --evidence`. Legacy `crm_funding_handoff.py` remains
only behind an explicit opt-in (`legacy_crm_core` / `--handoff-legacy-crm-core`).

CRMx has no verified typed funding-handoff CLI. Successful ingest_csv is
adapted into the ledger-compatible result shape; Research does not invent
Notion writes or Fit scores.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import urlsplit

from lib.candidates import CRMX_CSV_COLUMNS
from lib.crunchbase_saved_list import FundingObservation
from lib.funding_watcher_state import funding_event_key


REQUEST_SCHEMA_VERSION = "norman.research.funding_handoff.v1"
RESULT_SCHEMA_VERSION = "norman.crm_core.funding_handoff_result.v1"
EVIDENCE_SCHEMA_VERSION = "norman.research.crmx_evidence.v1"
HANDOFF_TARGETS = frozenset({"crmx", "legacy_crm_core"})
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
                "description": observation.description,
                "founded": observation.founded,
                "headquarters": observation.headquarters,
                "industries": list(observation.industries),
                "funding": {
                    "date": observation.funding_date,
                    "type": observation.funding_type,
                    "amountRaw": observation.funding_amount_raw,
                    "amountMinor": observation.funding_amount_minor,
                    "currency": observation.funding_currency,
                    "totalRaw": observation.total_funding_raw,
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
    """Validate a handoff result as an immutable answer to one exact request."""
    if not isinstance(payload, dict) or set(payload) != {
        "schemaVersion",
        "runId",
        "requestDigest",
        "mode",
        "complete",
        "events",
    }:
        raise ValueError("CRM result has an invalid shape")
    if payload["schemaVersion"] != RESULT_SCHEMA_VERSION:
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


def resolve_funding_handoff_target(
    *,
    legacy: bool = False,
    configured: str | None = None,
) -> str:
    """Return crmx (default) or legacy_crm_core. Unknown targets fail closed."""
    if legacy:
        return "legacy_crm_core"
    env = (os.environ.get("NORMAN_FUNDING_HANDOFF_TARGET") or "").strip()
    raw = (configured or env or "").strip()
    if not raw:
        watcher = ROOT / "config" / "funding-watcher.json"
        if watcher.is_file():
            try:
                payload = json.loads(watcher.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                payload = {}
            if isinstance(payload, dict):
                raw = str(payload.get("handoffTarget") or "").strip()
    if not raw:
        raw = "crmx"
    if raw not in HANDOFF_TARGETS:
        raise RuntimeError(
            f"unknown funding handoff target {raw!r}; expected one of "
            f"{sorted(HANDOFF_TARGETS)}. Fail closed — refusing to invent a "
            "Notion writer."
        )
    return raw


def write_crmx_funding_artifacts(
    request: dict[str, Any],
    *,
    csv_path: Path,
    evidence_path: Path,
) -> tuple[Path, Path]:
    """Map typed funding events into CRMx ingest_csv CSV + evidence sidecar.

    Unknown numeric amounts stay blank (never 0). Non-USD amounts stay blank in
    the CSV money columns — Research will not invent FX conversion. Richer
    funding facts travel in the evidence sidecar.
    """
    events = request.get("events")
    if not isinstance(events, list) or not events:
        raise ValueError("request events must be a non-empty list")
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, str]] = []
    evidence_candidates: list[dict[str, Any]] = []
    for event in events:
        if not isinstance(event, dict):
            raise ValueError("request event must be an object")
        funding = event.get("funding") if isinstance(event.get("funding"), dict) else {}
        amount_usd = _usd_dollars_from_minor(
            funding.get("amountMinor"),
            str(funding.get("currency") or ""),
        )
        # totalRaw is preserved in evidence; CSV total stays blank unless we
        # also have structured minor units on the request (we do not today).
        total_usd = None
        founders = event.get("founders") if isinstance(event.get("founders"), list) else []
        industries = (
            event.get("industries") if isinstance(event.get("industries"), list) else []
        )
        rows.append(
            {
                "Organization Name": str(event.get("company") or ""),
                "Organization Name URL": str(event.get("crunchbaseUrl") or ""),
                "Headquarters Location": str(event.get("headquarters") or ""),
                "Founded Date": str(event.get("founded") or ""),
                "Industries": ", ".join(str(item) for item in industries if item),
                "Last Funding Date": str(funding.get("date") or ""),
                "Last Funding Amount (in USD)": (
                    "" if amount_usd is None else f"{amount_usd:.0f}"
                ),
                "Description": str(event.get("description") or ""),
                "Website": str(event.get("website") or ""),
                "X (Twitter)": "",
                "LinkedIn": str(event.get("linkedin") or ""),
                "Founders": ", ".join(str(item) for item in founders if item),
                "Number of Funding Rounds": "",
                "Last Funding Type": str(funding.get("type") or ""),
                "Total Funding Amount (in USD)": (
                    "" if total_usd is None else f"{total_usd:.0f}"
                ),
                "Top 5 Investors": "",
            }
        )
        source_urls = [
            url
            for url in (
                str(event.get("sourceUrl") or ""),
                str(event.get("crunchbaseUrl") or ""),
            )
            if url
        ]
        funding_type = str(funding.get("type") or "").strip()
        evidence_candidates.append(
            {
                "company": event.get("company") or "",
                "website": event.get("website") or "",
                "linkedin": event.get("linkedin") or "",
                "crunchbase": event.get("crunchbaseUrl") or "",
                "one_liner": event.get("description") or "",
                "founders": ", ".join(str(item) for item in founders if item),
                "founded": event.get("founded") or "",
                "hq": event.get("headquarters") or "",
                "industries": ", ".join(str(item) for item in industries if item),
                "last_funding_date": funding.get("date") or "",
                "last_funding_usd": amount_usd,
                "last_funding_type": funding_type,
                "total_funding_usd": None,
                "num_rounds": None,
                "investors": "",
                "nyc_evidence": "",
                "nyc_angle": "none",
                "fit_hint": "none",
                "keyword_hits": [funding_type] if funding_type else [],
                "signal_notes": (
                    f"Crunchbase funding watcher event; "
                    f"amountRaw={funding.get('amountRaw') or ''}; "
                    f"currency={funding.get('currency') or ''}; "
                    f"totalRaw={funding.get('totalRaw') or ''}"
                ),
                "source_urls": source_urls,
                "mode": "funding",
                "lane": "crunchbase_funding_watcher",
                "event_key": event.get("eventKey") or "",
                "funding_amount_minor": funding.get("amountMinor"),
                "funding_currency": funding.get("currency") or "",
                "qualify_reason": "strict_funding_watcher",
            }
        )
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CRMX_CSV_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    evidence_payload = {
        "schemaVersion": EVIDENCE_SCHEMA_VERSION,
        "generated_at": str(request.get("generatedAt") or ""),
        "run_id": request.get("runId"),
        "count": len(evidence_candidates),
        "candidates": evidence_candidates,
        "note": (
            "Funding-watcher evidence for CRMx ingest_csv. Not a Fit score. "
            "CRMx owns SoR writes; Research never writes Notion as SoR. "
            "Adapter path: typed funding_handoff.v1 → CSV + evidence because "
            "no public CRMx typed funding CLI was verified."
        ),
    }
    evidence_path.write_text(
        json.dumps(evidence_payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return csv_path, evidence_path


def synthesize_crmx_handoff_result(
    request: dict[str, Any],
    *,
    mode: str,
) -> dict[str, Any]:
    """Ledger-compatible result after CRMx ingest_csv (or dry-run preview).

    ingest_csv does not return per-event terminal states. On accepted write,
    every event is recorded as `created` with a synthetic `crmx:ingest:…`
    pageId so the Research ledger can close. Follow-up: a typed CRMx funding
    CLI should replace this adapter synthesis.
    """
    if mode not in {"dry_run", "write"}:
        raise ValueError("mode must be dry_run or write")
    events = request.get("events")
    if not isinstance(events, list):
        raise ValueError("request events must be a list")
    result_events: list[dict[str, Any]] = []
    for event in events:
        key = str(event.get("eventKey") or "")
        item: dict[str, Any] = {
            "eventKey": key,
            "state": "created",
            "reason": (
                "crmx_ingest_csv_accepted"
                if mode == "write"
                else "crmx_ingest_csv_preview"
            ),
        }
        if mode == "write":
            item["pageId"] = f"crmx:ingest:{key}"
        result_events.append(item)
    return {
        "schemaVersion": RESULT_SCHEMA_VERSION,
        "runId": request.get("runId"),
        "requestDigest": hashlib.sha256(
            _canonical_request_bytes(request)
        ).hexdigest(),
        "mode": mode,
        "complete": True,
        "events": result_events,
    }


def invoke_crmx_funding_handoff(
    request_path: Path,
    result_path: Path,
    *,
    write: bool,
    timeout_seconds: int = 900,
) -> dict[str, Any]:
    """Promote funding events through CRMx ingest_csv + evidence (fail-closed)."""
    if not request_path.is_absolute() or not result_path.is_absolute():
        raise ValueError("handoff paths must be absolute")
    request = _load_json(request_path)
    out_dir = request_path.parent
    run_id = str(request.get("runId") or "unknown")
    csv_path = (out_dir / f"{run_id}.crmx.csv").resolve()
    evidence_path = (out_dir / f"{run_id}.evidence.json").resolve()
    write_crmx_funding_artifacts(
        request, csv_path=csv_path, evidence_path=evidence_path
    )

    try:
        from lib.sinks import SinkError, promote_via_crmx
    except ImportError as exc:  # pragma: no cover - package layout invariant
        raise RuntimeError("CRMx promote helper unavailable") from exc

    if not write:
        # Fail closed on missing CRMx path/DB/module/evidence by reusing the
        # promote preflight; ingest_csv has no verified dry-run, so the
        # expected SinkError means config is sound and we synthesize a preview.
        try:
            promote_via_crmx(
                csv_path,
                evidence_path=evidence_path,
                dry_run=True,
            )
        except SinkError as exc:
            if "dry-run" not in str(exc).casefold():
                raise RuntimeError(f"CRM handoff retryable: {exc}") from exc
        else:  # pragma: no cover - would mean CRMx gained a dry-run flag
            raise RuntimeError(
                "CRMx ingest_csv unexpectedly accepted dry_run; refusing to "
                "guess whether a write occurred"
            )
        result = synthesize_crmx_handoff_result(request, mode="dry_run")
        _atomic_write_json(result_path, result)
        return validate_result(result, request=request)

    try:
        promote_via_crmx(
            csv_path,
            evidence_path=evidence_path,
            dry_run=False,
        )
    except SinkError as exc:
        raise RuntimeError(f"CRM handoff retryable: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("CRM handoff retryable timeout") from exc

    result = synthesize_crmx_handoff_result(request, mode="write")
    _atomic_write_json(result_path, result)
    validated = validate_result(_load_json(result_path), request=request)
    if validated["mode"] != "write":
        raise ValueError("CRM result mode does not match invocation")
    return validated


def invoke_legacy_crm_handoff(
    request_path: Path,
    result_path: Path,
    *,
    write: bool,
    timeout_seconds: int = 900,
    core_path: Path | None = None,
) -> dict[str, Any]:
    """Explicit legacy shim: invoke crm-core's crm_funding_handoff.py."""
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


def invoke_crm_handoff(
    request_path: Path,
    result_path: Path,
    *,
    write: bool,
    timeout_seconds: int = 900,
    core_path: Path | None = None,
    target: str | None = None,
    legacy: bool = False,
) -> dict[str, Any]:
    """Dispatch funding handoff to CRMx (default) or explicit legacy crm-core.

    Passing `core_path` implies the legacy target (compat with older call sites
    that invoked the Core CLI through this name). Production should prefer
    `legacy=True` / `--handoff-legacy-crm-core` instead.
    """
    use_legacy = legacy or core_path is not None
    resolved = resolve_funding_handoff_target(
        legacy=use_legacy, configured=None if use_legacy else target
    )
    if resolved == "legacy_crm_core":
        return invoke_legacy_crm_handoff(
            request_path,
            result_path,
            write=write,
            timeout_seconds=timeout_seconds,
            core_path=core_path,
        )
    return invoke_crmx_funding_handoff(
        request_path,
        result_path,
        write=write,
        timeout_seconds=timeout_seconds,
    )


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


def _usd_dollars_from_minor(minor: Any, currency: str) -> float | None:
    """Convert minor units to USD dollars. Non-USD / missing → None (blank)."""
    if minor is None or isinstance(minor, bool):
        return None
    if not isinstance(minor, int):
        return None
    if (currency or "").strip().upper() != "USD":
        return None
    # Unknown ≠ 0: a zero minor amount is still a claim of $0 — keep it as 0.0
    # only when the source asserted USD minor units. Parser rejects missing
    # amounts before handoff.
    return minor / 100.0


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


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _aware_datetime(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ValueError(f"{label} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} must include timezone")
    return parsed
