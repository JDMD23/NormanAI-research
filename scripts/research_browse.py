#!/usr/bin/env python3
"""Read Crunchbase and Substack through JD's logged-in Chrome.

The sources worth paying for are behind a login, so a browser is the only way
in. Chrome fetches the page; Grok extracts the companies; everything after that
is the same path the search lanes use — qualify, dedup, brief, intake.

    python3 scripts/research_browse.py --dry-run
    python3 scripts/research_browse.py --source cb_nyc_rounds --dry-run
    python3 scripts/research_browse.py --write --yes
    python3 scripts/research_browse.py --write --yes --promote

Mac + Chrome only. Anywhere else it says so and exits cleanly.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from lib import chrome, digest, extract, grok, qualify, state  # noqa: E402
from lib.candidates import Candidate  # noqa: E402
from lib.config import load_config, load_env_key, research_config  # noqa: E402
from lib.sinks import (  # noqa: E402
    SinkError,
    notion_existing_keys,
    promote_via_intake,
    write_evidence,
    write_intake_csv,
)
from research_run import dedupe_within  # noqa: E402


def browse_config() -> dict:
    return load_config("browse")


def collect(source_filter: str | None) -> tuple[list[Candidate], list[dict]]:
    """Walk each enabled source. One bad page never takes the run down."""
    cfg = browse_config()
    sources = [s for s in cfg["sources"] if s.get("enabled", True)]
    if source_filter:
        sources = [s for s in sources if s["id"] == source_filter]
    sources = sources[: cfg.get("maxPagesPerRun", 12)]

    found: list[Candidate] = []
    reports: list[dict] = []

    with chrome.lease():
        for index, source in enumerate(sources):
            report = {"source": source["id"], "outcome": "success", "found": 0, "note": ""}
            try:
                page = chrome.fetch(source["url"], settle=cfg.get("settleSeconds", 3.0))
                cands = extract.from_page(
                    page,
                    source=source["label"],
                    lane=source["id"],
                    mode=source.get("mode", "funding"),
                    limit=source.get("limit", 15),
                    multi_mode=source.get("multiMode", False),
                )
                found.extend(cands)
                report["found"] = len(cands)
                if not cands:
                    report["note"] = "page read fine, no companies on it"

            except chrome.ChromeBlocked as exc:
                # Auth wall: the session is the scarce resource. Stop the lane
                # rather than hammering a logged-out browser.
                report.update(outcome="blocked", note=str(exc))
                reports.append(report)
                print(f"  {source['id']}: BLOCKED — {exc}", flush=True)
                break
            except chrome.PageNotReady as exc:
                report.update(outcome="retry", note=str(exc))
            except chrome.ChromeUnavailable:
                raise
            except grok.GrokError as exc:
                report.update(outcome="retry", note=f"extraction failed: {exc}")

            reports.append(report)
            note = f" — {report['note']}" if report["note"] else ""
            print(
                f"  {source['id']}: {report['outcome']} ({report['found']} found){note}",
                flush=True,
            )

            if index < len(sources) - 1:
                time.sleep(cfg.get("restBetweenPagesSeconds", 6.0))

    return found, reports


def main() -> int:
    parser = argparse.ArgumentParser(description="Browse logged-in sources for companies")
    parser.add_argument("--source", default=None, help="run one source id only")
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--promote", action="store_true",
                        help="hand the CSV straight to crm-core's crm_intake.py")
    parser.add_argument("--show-rejects", action="store_true")
    parser.add_argument("--no-notion-check", action="store_true")
    args = parser.parse_args()

    if args.write and not args.yes:
        raise SystemExit("--write requires --yes")
    if args.write and args.dry_run:
        raise SystemExit("use either --write or --dry-run")
    if args.promote and not args.write:
        raise SystemExit("--promote requires --write --yes (it creates CRM rows)")

    key_env = research_config()["grok"].get("apiKeyEnv", "XAI_API_KEY")
    if not load_env_key(key_env):
        raise SystemExit(f"{key_env} is not set — extraction needs it.")

    print("== browse pass ==", flush=True)
    try:
        found, reports = collect(args.source)
    except chrome.ChromeUnavailable as exc:
        print(f"\nSkipped: {exc}", flush=True)
        print("This lane needs a Mac with Chrome logged into Crunchbase and Substack.", flush=True)
        return 0

    print(f"raw candidates: {len(found)}", flush=True)

    merged = dedupe_within(found)
    passed, rejected = qualify.apply(merged)
    by_mode = Counter(c.mode for c in passed)
    print(
        f"qualified: {len(passed)} of {len(merged)} "
        f"({', '.join(f'{m}={n}' for m, n in sorted(by_mode.items())) or 'none'})",
        flush=True,
    )
    if rejected and args.show_rejects:
        for cand in rejected[:25]:
            print(f"  - {cand.company}: {cand.qualify_reason}", flush=True)

    known = notion_existing_keys() if not args.no_notion_check else set()
    fresh: list[Candidate] = []
    on_board = already_sent = 0

    with state.connect() as conn:
        run_id = state.start_run(conn, f"browse:{args.source or 'all'}")
        for cand in passed:
            state.record_seen(conn, cand)
            if not cand.key:
                continue
            if cand.key in known:
                on_board += 1
            elif state.is_emitted(conn, cand.key):
                already_sent += 1
            else:
                fresh.append(cand)

        order = {"office_expansion": 0, "founder_language": 1, "hiring_growth": 2, "funding": 3}
        fresh.sort(key=lambda c: (order.get(c.mode, 9), c.company.lower()))

        counts = {
            "raw": len(found),
            "merged": len(merged),
            "rejected": len(rejected),
            "already_on_board": on_board,
            "already_emitted": already_sent,
            "emitted": 0,
        }
        outcome = {
            "ran_at": datetime.now(timezone.utc).isoformat(),
            "mode": "write" if args.write else "dry_run",
            "sources": reports,
            "counts": counts,
            "candidates": [c.as_dict() for c in fresh],
        }

        if args.write and fresh:
            csv_path = write_intake_csv(
                fresh, ROOT / "out" / f"browse-intake-{date.today().isoformat()}.csv"
            )
            write_evidence(
                fresh, ROOT / "out" / f"browse-evidence-{date.today().isoformat()}.json"
            )
            state.mark_emitted(conn, [c.key for c in fresh])
            counts["emitted"] = len(fresh)
            outcome["csv"] = str(csv_path)
            print(f"\nwrote {len(fresh)} → {csv_path}", flush=True)

            if args.promote:
                try:
                    summary = promote_via_intake(csv_path)
                    outcome["promote"] = summary["counts"]
                    print(f"promoted → crm_intake.py: {json.dumps(summary['counts'])}", flush=True)
                except SinkError as exc:
                    outcome["promote_error"] = str(exc)
                    print(f"! promotion failed: {exc}", flush=True)

        brief = digest.render(fresh, counts)
        brief_path = ROOT / "out" / f"brief-browse-{date.today().isoformat()}.md"
        brief_path.parent.mkdir(parents=True, exist_ok=True)
        brief_path.write_text(brief)
        print("\n" + "─" * 60, flush=True)
        print(brief, flush=True)
        print("─" * 60, flush=True)
        print(f"brief → {brief_path}", flush=True)

        state.finish_run(conn, run_id, len(found), counts["emitted"])

    receipt = ROOT / "state" / "browse_latest.json"
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(json.dumps(outcome, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
