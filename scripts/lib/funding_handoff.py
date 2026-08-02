"""Versioned JSON bridge from Research discovery to CRM Core mutation."""

from __future__ import annotations

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

from lib.crunchbase_saved_list import FundingObservation
from lib.funding_watcher_state import funding_event_key


REQUEST_SCHEMA_VERSION = "norman.research.funding_handoff.v1"
RESULT_SCHEMA_VERSION = "norman.crm_core.funding_handoff_result.v1"
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
    """Validate a Core result as an immutable answer to one exact request."""
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


def invoke_crm_handoff(
    request_path: Path,
    result_path: Path,
    *,
    write: bool,
    timeout_seconds: int = 900,
    core_path: Path | None = None,
) -> dict[str, Any]:
    """Invoke Core only through its public CLI and validate its result file."""
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


def _aware_datetime(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ValueError(f"{label} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} must include timezone")
    return parsed
