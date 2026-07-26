#!/usr/bin/env python3
"""NormanAI-research — discovery run.

Finds companies that are growing, raising, hiring in NYC, or under office-space
pressure, and hands them to NormanAI-crm-core for intake and scoring.

    python3 scripts/research_run.py --dry-run
    python3 scripts/research_run.py --mode funding --dry-run
    python3 scripts/research_run.py --write --yes
    python3 scripts/research_run.py --write --yes --promote

For the scheduled daily run that walks both lanes, use scripts/daily.py.

Research qualifies; it does not score. Rows land at Status=Research and
crm-core's lanes enrich them before its score agent scores them.

This script NEVER writes Notion. crm_intake.py is the only writer.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from lib import pipeline  # noqa: E402
from lib.candidates import Candidate  # noqa: E402
from lib.config import load_env_key, research_config  # noqa: E402
from lib.discover import run_lane, web_lanes, x_lanes  # noqa: E402

# Re-exported: the daily runner and the tests both reach for it here.
dedupe_within = pipeline.dedupe_within


def collect(lane_kind: str, mode_filter: str | None, verbose: bool = True):
    """Run every matching lane. Returns (candidates, lane reports)."""
    plan: list[tuple[dict, str]] = []
    if lane_kind in {"x", "all"}:
        plan += [(lane, "x") for lane in x_lanes()]
    if lane_kind in {"web", "all"}:
        plan += [(lane, "web") for lane in web_lanes()]
    if mode_filter:
        plan = [(lane, kind) for lane, kind in plan if lane.get("mode") == mode_filter]

    cap = research_config()["caps"]["maxSearchesPerRun"]
    if len(plan) > cap:
        if verbose:
            print(f"! capping {len(plan)} lanes to maxSearchesPerRun={cap}", flush=True)
        plan = plan[:cap]

    found: list[Candidate] = []
    reports: list[dict] = []
    for lane, kind in plan:
        result = run_lane(lane, kind)
        reports.append({
            "lane": result["lane"],
            "kind": result["kind"],
            "mode": result["mode"],
            "posture": result["posture"],
            "outcome": result["outcome"],
            "found": len(result["candidates"]),
            "note": result["note"],
            "usage": result["usage"],
        })
        if verbose:
            note = f" — {result['note']}" if result["note"] else ""
            print(
                f"  {result['kind']}/{result['lane']} [{result['posture']}]: "
                f"{result['outcome']} ({len(result['candidates'])} found){note}",
                flush=True,
            )
        found.extend(result["candidates"])
    return found, reports


def one_pass(args: argparse.Namespace) -> dict:
    print(f"== discovery pass (lane={args.lane} mode={args.mode or 'all'}) ==", flush=True)
    found, reports = collect(args.lane, args.mode)
    print(f"raw candidates: {len(found)}", flush=True)
    return pipeline.run(
        found, reports, args, label=f"{args.lane}:{args.mode or 'all'}", slug="search"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="NormanAI research discovery run")
    parser.add_argument("--lane", choices=["x", "web", "all"], default="all")
    parser.add_argument(
        "--mode",
        choices=["funding", "office_expansion", "hiring_growth", "founder_language"],
        default=None,
        help="run only lanes of this mode (default: all four)",
    )
    parser.add_argument("--write", action="store_true", help="emit the intake CSV")
    parser.add_argument("--yes", action="store_true", help="required with --write")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--promote", action="store_true",
                        help="hand the CSV straight to crm-core's crm_intake.py")
    parser.add_argument("--show-rejects", action="store_true",
                        help="print why candidates were rejected")
    parser.add_argument("--no-notion-check", action="store_true",
                        help="skip the read-only CRM Core pre-filter")
    args = parser.parse_args()

    if args.write and not args.yes:
        raise SystemExit("--write requires --yes")
    if args.write and args.dry_run:
        raise SystemExit("use either --write or --dry-run")
    if args.promote and not args.write:
        raise SystemExit("--promote requires --write --yes (it creates CRM rows)")

    key_env = research_config()["grok"].get("apiKeyEnv", "XAI_API_KEY")
    if not load_env_key(key_env):
        raise SystemExit(
            f"{key_env} is not set — nothing to run.\n"
            "Set it in the environment or a .env file, then verify with:\n"
            "  python3 scripts/research_probe.py"
        )

    one_pass(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
