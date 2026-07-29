#!/usr/bin/env python3
"""Guarded Research-owned Crunchbase funding detector and CRM handoff."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, ContextManager

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from lib.browser_coordination import (  # noqa: E402
    BrowserLeaseUnavailable,
    DailyCrunchbaseBudget,
    SharedBrowserLease,
)
from lib.crunchbase_saved_list import (  # noqa: E402
    CrunchbaseSavedListBlocked,
    CrunchbaseSavedListBrowser,
    CrunchbaseSavedListDrift,
    FundingObservation,
    load_watcher_config,
)
from lib.funding_handoff import (  # noqa: E402
    build_handoff,
    invoke_crm_handoff,
    write_handoff,
)
from lib.funding_watcher_state import (  # noqa: E402
    FundingWatcherLedger,
    exclusive_run_lock,
    funding_event_key,
    migrate_legacy_state,
    new_york_slot,
    write_immutable_receipt,
)


RETRYABLE_STATUSES = {"busy", "budget_exhausted", "crm_retryable", "browser_retryable"}
CONFIG_ERROR_STATUSES = {
    "captcha",
    "security_challenge",
    "auth_wall",
    "source_drift",
    "result_schema_mismatch",
}
TERMINAL_CORE_STATES = {
    "created",
    "queued_existing",
    "duplicate_event",
    "rejected_identity",
    "ambiguous_review",
}


@dataclass
class WatcherDependencies:
    browser: Any
    budget: Any
    ledger: FundingWatcherLedger
    invoke_handoff: Callable[[dict[str, Any], bool], dict[str, Any]]
    browser_lease: Callable[[], ContextManager[Any]]
    run_lock: Callable[[], ContextManager[bool]]
    notify: Callable[[list[str]], None]


def run_check(
    config: dict[str, Any],
    dependencies: WatcherDependencies,
    *,
    write: bool,
    now: datetime,
    enforce_schedule: bool,
) -> dict[str, Any]:
    run_id = _run_id(now)
    receipt = _base_receipt("check", run_id, now, write)
    state_root = dependencies.ledger.path.parent
    with dependencies.run_lock() as acquired:
        if not acquired:
            return _finish(state_root, receipt, "busy", "run_lock_unavailable")
        if not config["enabled"]:
            return _finish(state_root, receipt, "disabled", "watcher_disabled")
        slot = new_york_slot(now, config["scheduleHours"]) if enforce_schedule else None
        receipt["slot"] = slot
        if enforce_schedule and slot is None:
            return _finish(state_root, receipt, "outside_schedule", "outside_schedule")
        if slot and dependencies.ledger.slot_complete(slot):
            return _finish(
                state_root, receipt, "already_checked_slot", "slot_already_complete"
            )

        max_pages = config["scheduledMaxPagesPerSource"]
        snapshots = []
        try:
            for source in config["sourceDefinitions"]:
                granted = dependencies.budget.claim(
                    now,
                    requested=max_pages,
                    lane="research-funding-watcher",
                )
                if granted == 0:
                    return _finish(
                        state_root, receipt, "budget_exhausted", "no_page_budget"
                    )
                receipt["pagesReserved"] += granted
                with dependencies.browser_lease():
                    snapshots.append(
                        dependencies.browser.read_source(
                            source,
                            observed_at=now.astimezone(timezone.utc).isoformat(),
                            max_pages=granted,
                        )
                    )
        except BrowserLeaseUnavailable:
            return _finish(
                state_root, receipt, "busy", "shared_browser_lease_unavailable"
            )
        except CrunchbaseSavedListBlocked as exc:
            receipt["error"] = {"reason": exc.reason, "evidence": exc.evidence}
            status = exc.reason if exc.reason in CONFIG_ERROR_STATUSES else "browser_retryable"
            return _finish(state_root, receipt, status, "browser_blocked")
        except CrunchbaseSavedListDrift as exc:
            receipt["error"] = {"reason": "source_drift", "evidence": str(exc)}
            return _finish(state_root, receipt, "source_drift", "source_contract_failed")
        except Exception as exc:
            receipt["error"] = {
                "reason": type(exc).__name__,
                "evidence": str(exc)[:500],
            }
            return _finish(state_root, receipt, "browser_retryable", "browser_error")

        pending: list[tuple[str, FundingObservation]] = []
        for snapshot in snapshots:
            receipt["sources"].append(_snapshot_summary(snapshot))
            receipt["counts"]["rejected_parse"] += len(snapshot.rejections)
            for row in snapshot.observations:
                key = funding_event_key(row)
                if dependencies.ledger.is_terminal(key):
                    receipt["counts"]["already_terminal"] += 1
                else:
                    pending.append((key, row))
        receipt["counts"]["new_events"] = len(pending)

        if not pending:
            if write and slot:
                dependencies.ledger.mark_slot_complete(slot, run_id=run_id)
            return _finish(state_root, receipt, "complete", "")

        request = build_handoff(
            run_id,
            now.astimezone(timezone.utc).isoformat(),
            pending,
        )
        if not write:
            try:
                result = dependencies.invoke_handoff(request, False)
            except Exception as exc:
                receipt["error"] = {
                    "reason": type(exc).__name__,
                    "evidence": str(exc)[:500],
                }
                return _finish(
                    state_root, receipt, "crm_retryable", "crm_preview_failed"
                )
            receipt["events"] = list(result["events"])
            receipt["counts"]["would_handoff"] = len(pending)
            return _finish(state_root, receipt, "complete", "")

        for key, row in pending:
            dependencies.ledger.observe(
                key,
                observed_at=row.observed_at,
                details=_observation_details(row),
            )
            dependencies.ledger.mark_handoff_pending(
                key, run_id=run_id, observed_at=now.isoformat()
            )
        try:
            result = dependencies.invoke_handoff(request, True)
            _require_result_alignment(result, pending)
        except Exception as exc:
            for key, _ in pending:
                dependencies.ledger.mark_retryable(
                    key, reason=str(exc)[:500], observed_at=now.isoformat()
                )
            receipt["error"] = {
                "reason": type(exc).__name__,
                "evidence": str(exc)[:500],
            }
            status = (
                "result_schema_mismatch"
                if isinstance(exc, ValueError)
                else "crm_retryable"
            )
            return _finish(state_root, receipt, status, "crm_handoff_failed")

        notifications: list[str] = []
        for event, (key, row) in zip(result["events"], pending):
            state = event["state"]
            if state not in TERMINAL_CORE_STATES:
                dependencies.ledger.mark_retryable(
                    key,
                    reason=f"nonterminal_core_state:{state}",
                    observed_at=now.isoformat(),
                )
                return _finish(
                    state_root, receipt, "crm_retryable", "nonterminal_core_result"
                )
            dependencies.ledger.mark_terminal(
                key,
                outcome=state,
                page_id=event.get("pageId"),
                observed_at=now.isoformat(),
            )
            receipt["counts"][state] = receipt["counts"].get(state, 0) + 1
            notifications.append(f"{row.company} — {row.crunchbase_url}")
        receipt["events"] = list(result["events"])
        if slot:
            dependencies.ledger.mark_slot_complete(slot, run_id=run_id)
        if notifications:
            dependencies.notify(notifications)
        return _finish(state_root, receipt, "complete", "")


def run_bootstrap(
    config: dict[str, Any],
    dependencies: WatcherDependencies,
    *,
    seed_top: int,
    write: bool,
    now: datetime,
) -> dict[str, Any]:
    run_id = _run_id(now)
    receipt = _base_receipt("bootstrap", run_id, now, write)
    state_root = dependencies.ledger.path.parent
    with dependencies.run_lock() as acquired:
        if not acquired:
            return _finish(state_root, receipt, "busy", "run_lock_unavailable")
        if not config["enabled"]:
            return _finish(state_root, receipt, "disabled", "watcher_disabled")
        sources = config["sourceDefinitions"]
        if all(dependencies.ledger.bootstrap_complete(source.url) for source in sources):
            return _finish(
                state_root, receipt, "already_bootstrapped", "bootstrap_complete"
            )
        max_pages = config["bootstrapMaxPagesPerSource"]
        snapshots = []
        try:
            for source in sources:
                granted = dependencies.budget.claim(
                    now,
                    requested=max_pages,
                    lane="research-funding-bootstrap",
                )
                if granted < max_pages:
                    return _finish(
                        state_root,
                        receipt,
                        "budget_exhausted",
                        "incomplete_bootstrap_budget",
                    )
                receipt["pagesReserved"] += granted
                with dependencies.browser_lease():
                    snapshots.append(
                        dependencies.browser.read_source(
                            source,
                            observed_at=now.astimezone(timezone.utc).isoformat(),
                            max_pages=granted,
                        )
                    )
        except BrowserLeaseUnavailable:
            return _finish(state_root, receipt, "busy", "shared_browser_lease_unavailable")
        except CrunchbaseSavedListBlocked as exc:
            receipt["error"] = {"reason": exc.reason, "evidence": exc.evidence}
            return _finish(state_root, receipt, exc.reason, "browser_blocked")
        except CrunchbaseSavedListDrift as exc:
            receipt["error"] = {"reason": "source_drift", "evidence": str(exc)}
            return _finish(state_root, receipt, "source_drift", "source_contract_failed")
        except Exception as exc:
            receipt["error"] = {
                "reason": type(exc).__name__,
                "evidence": str(exc)[:500],
            }
            return _finish(state_root, receipt, "browser_retryable", "browser_error")

        observations: list[FundingObservation] = []
        for snapshot in snapshots:
            receipt["sources"].append(_snapshot_summary(snapshot))
            if snapshot.result_count != len(snapshot.observations) + len(snapshot.rejections):
                return _finish(
                    state_root, receipt, "source_drift", "incomplete_bootstrap_coverage"
                )
            observations.extend(snapshot.observations)
        if (
            not isinstance(seed_top, int)
            or isinstance(seed_top, bool)
            or seed_top <= 0
        ):
            raise ValueError("seed_top must be a positive integer")
        candidates, baseline = observations[:seed_top], observations[seed_top:]
        all_pairs = [(funding_event_key(row), row) for row in candidates]
        pairs = [
            pair for pair in all_pairs
            if not dependencies.ledger.is_terminal(pair[0])
        ]
        already_terminal = len(all_pairs) - len(pairs)
        if not pairs:
            if not all_pairs:
                return _finish(state_root, receipt, "source_drift", "empty_bootstrap")
            if write:
                added = _baseline_and_complete(
                    dependencies, sources, baseline, now
                )
                receipt["counts"] = {
                    "created": 0,
                    "queued_existing": 0,
                    "baselined": added,
                    "already_terminal": already_terminal,
                }
            else:
                receipt["counts"]["would_baseline"] = len(baseline)
                receipt["counts"]["already_terminal"] = already_terminal
            return _finish(state_root, receipt, "complete", "")
        request = build_handoff(
            run_id, now.astimezone(timezone.utc).isoformat(), pairs
        )
        if not write:
            try:
                result = dependencies.invoke_handoff(request, False)
            except Exception as exc:
                receipt["error"] = {
                    "reason": type(exc).__name__,
                    "evidence": str(exc)[:500],
                }
                return _finish(state_root, receipt, "crm_retryable", "crm_preview_failed")
            receipt["events"] = list(result["events"])
            receipt["counts"]["would_handoff"] = len(candidates)
            receipt["counts"]["would_baseline"] = len(baseline)
            return _finish(state_root, receipt, "complete", "")

        for key, row in pairs:
            dependencies.ledger.observe(
                key, observed_at=row.observed_at, details=_observation_details(row)
            )
            dependencies.ledger.mark_handoff_pending(
                key, run_id=run_id, observed_at=now.isoformat()
            )
        try:
            result = dependencies.invoke_handoff(request, True)
            _require_result_alignment(result, pairs)
        except Exception as exc:
            for key, _ in pairs:
                dependencies.ledger.mark_retryable(
                    key, reason=str(exc)[:500], observed_at=now.isoformat()
                )
            status = (
                "result_schema_mismatch"
                if isinstance(exc, ValueError)
                else "crm_retryable"
            )
            return _finish(state_root, receipt, status, "crm_handoff_failed")

        notifications = []
        counts = {
            "created": 0,
            "queued_existing": 0,
            "baselined": 0,
            "already_terminal": already_terminal,
        }
        for event, (key, row) in zip(result["events"], pairs):
            state = event["state"]
            if state not in TERMINAL_CORE_STATES:
                dependencies.ledger.mark_retryable(
                    key,
                    reason=f"nonterminal_core_state:{state}",
                    observed_at=now.isoformat(),
                )
                return _finish(
                    state_root, receipt, "crm_retryable", "nonterminal_core_result"
                )
            dependencies.ledger.mark_terminal(
                key,
                outcome=state,
                page_id=event.get("pageId"),
                observed_at=now.isoformat(),
            )
            if state in counts:
                counts[state] += 1
            notifications.append(f"{row.company} — {row.crunchbase_url}")
        counts["baselined"] += _baseline_and_complete(
            dependencies, sources, baseline, now
        )
        receipt["events"] = list(result["events"])
        receipt["counts"] = counts
        dependencies.notify(notifications)
        return _finish(state_root, receipt, "complete", "")


def _base_receipt(
    action: str, run_id: str, now: datetime, write: bool
) -> dict[str, Any]:
    return {
        "schemaVersion": "norman.research.crunchbase_funding_receipt.v1",
        "runId": run_id,
        "action": action,
        "mode": "write" if write else "dry_run",
        "startedAt": now.isoformat(),
        "status": "",
        "stopReason": "",
        "slot": None,
        "pagesReserved": 0,
        "sources": [],
        "events": [],
        "counts": {
            "new_events": 0,
            "already_terminal": 0,
            "rejected_parse": 0,
            "would_handoff": 0,
        },
    }


def _finish(
    state_root: Path,
    receipt: dict[str, Any],
    status: str,
    reason: str,
) -> dict[str, Any]:
    receipt["status"] = status
    receipt["stopReason"] = reason
    write_immutable_receipt(state_root, receipt)
    _atomic_write_json(state_root / "latest.json", receipt)
    return receipt


def _snapshot_summary(snapshot: Any) -> dict[str, Any]:
    return {
        "name": snapshot.source.name,
        "url": snapshot.source.url,
        "resultCount": snapshot.result_count,
        "pageCount": snapshot.page_count,
        "observations": len(snapshot.observations),
        "rejections": len(snapshot.rejections),
        "topFundingDate": snapshot.top_funding_date,
    }


def _observation_details(row: FundingObservation) -> dict[str, Any]:
    return {
        "company": row.company,
        "crunchbase_url": row.crunchbase_url,
        "source_url": row.source_url,
    }


def _require_result_alignment(
    result: dict[str, Any],
    pairs: list[tuple[str, FundingObservation]],
) -> None:
    events = result.get("events")
    if not isinstance(events, list):
        raise ValueError("CRM result events must be a list")
    expected = [key for key, _ in pairs]
    actual = [
        event.get("eventKey") if isinstance(event, dict) else None
        for event in events
    ]
    if actual != expected:
        raise ValueError("CRM result event key order does not match request")


def _baseline_and_complete(
    dependencies: WatcherDependencies,
    sources: list[Any] | tuple[Any, ...],
    baseline: list[FundingObservation],
    now: datetime,
) -> int:
    added = 0
    for row in baseline:
        key = funding_event_key(row)
        if dependencies.ledger.is_terminal(key):
            continue
        dependencies.ledger.observe(
            key, observed_at=row.observed_at, details=_observation_details(row)
        )
        dependencies.ledger.mark_terminal(
            key,
            outcome="baseline",
            page_id=None,
            observed_at=now.isoformat(),
        )
        added += 1
    for source in sources:
        dependencies.ledger.mark_bootstrap_complete(
            source.url, now.isoformat()
        )
    return added


def _run_id(now: datetime) -> str:
    return (
        now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        + "-"
        + uuid.uuid4().hex[:8]
    )


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


def _notification(items: list[str]) -> None:
    if not items:
        return
    body = "\n".join(items[:10])
    subprocess.run(
        [
            "osascript",
            "-e",
            f'display notification {json.dumps(body)} with title '
            f'{json.dumps("Norman funding watcher")}',
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )


def _production_dependencies(config: dict[str, Any]) -> WatcherDependencies:
    state_root = Path(config["stateDirectory"]).expanduser()
    ledger = FundingWatcherLedger(state_root / "ledger.json")

    def invoke(request: dict[str, Any], write: bool) -> dict[str, Any]:
        out = state_root / "handoffs"
        request_path = (out / f"{request['runId']}.request.json").resolve()
        result_path = (out / f"{request['runId']}.result.json").resolve()
        write_handoff(request_path, request)
        return invoke_crm_handoff(request_path, result_path, write=write)

    return WatcherDependencies(
        browser=CrunchbaseSavedListBrowser(),
        budget=DailyCrunchbaseBudget(),
        ledger=ledger,
        invoke_handoff=invoke,
        browser_lease=lambda: SharedBrowserLease(),
        run_lock=lambda: exclusive_run_lock(state_root / "watcher.lock"),
        notify=_notification,
    )


def _exit_code(receipt: dict[str, Any]) -> int:
    status = receipt["status"]
    if status in RETRYABLE_STATUSES:
        return 75
    if status in CONFIG_ERROR_STATUSES:
        return 78
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=ROOT / "config" / "funding-watcher.json",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("check", "bootstrap"):
        command = commands.add_parser(name)
        mode = command.add_mutually_exclusive_group(required=True)
        mode.add_argument("--dry-run", action="store_true")
        mode.add_argument("--write", action="store_true")
        command.add_argument("--yes", action="store_true")
        if name == "check":
            command.add_argument("--enforce-schedule", action="store_true")
        else:
            command.add_argument("--seed-top", type=int, default=10)
    commands.add_parser("migrate-legacy-state")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code == 0 else 64
    if getattr(args, "write", False) and not getattr(args, "yes", False):
        print("--write requires --yes", file=sys.stderr)
        return 64
    if getattr(args, "dry_run", False) and getattr(args, "yes", False):
        print("--yes is valid only with --write", file=sys.stderr)
        return 64
    try:
        config = load_watcher_config(args.config)
        if args.command == "migrate-legacy-state":
            source = config["sourceDefinitions"][0]
            receipt = migrate_legacy_state(
                Path(config["legacyStateDirectory"]).expanduser(),
                Path(config["stateDirectory"]).expanduser(),
                expected_source_url=source.url,
            )
            print(json.dumps(receipt, indent=2, sort_keys=True))
            return 0
        deps = _production_dependencies(config)
        now = datetime.now(timezone.utc)
        if args.command == "check":
            receipt = run_check(
                config,
                deps,
                write=args.write,
                now=now,
                enforce_schedule=args.enforce_schedule,
            )
        else:
            receipt = run_bootstrap(
                config,
                deps,
                seed_top=args.seed_top,
                write=args.write,
                now=now,
            )
        print(json.dumps(receipt, indent=2, sort_keys=True))
        return _exit_code(receipt)
    except (ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 78


if __name__ == "__main__":
    raise SystemExit(main())
