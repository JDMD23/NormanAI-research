#!/usr/bin/env python3
"""NormanAI-research — discovery run.

Finds companies that are growing, raising, hiring in NYC, or under office-space
pressure, and hands them to NormanAI-crm-core for intake and scoring.

    python3 scripts/research_run.py --dry-run
    python3 scripts/research_run.py --mode funding --dry-run
    python3 scripts/research_run.py --write --yes
    python3 scripts/research_run.py --write --yes --promote
    python3 scripts/research_run.py --write --yes --promote --loop

Research qualifies; it does not score. Rows land at Status=Research and
crm-core's lanes enrich them before its score agent scores them.

This script NEVER writes Notion. crm_intake.py is the only writer.
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

from lib import digest, qualify, state  # noqa: E402
from lib.candidates import Candidate  # noqa: E402
from lib.config import load_env_key, research_config  # noqa: E402
from lib.discover import run_lane, web_lanes, x_lanes  # noqa: E402
from lib.identity import hard_match  # noqa: E402
from lib.sinks import (  # noqa: E402
    SinkError,
    notion_existing_keys,
    promote_via_intake,
    write_evidence,
    write_intake_csv,
)


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


def dedupe_within(candidates: list[Candidate]) -> list[Candidate]:
    """Collapse the same company appearing in several lanes.

    Evidence is unioned rather than discarded: a company seen by both the
    funding sweep and the office-expansion lane keeps both receipts, and the
    tighter mode wins because it is the more specific claim.
    """
    tightness = {"founder_language": 3, "office_expansion": 3, "hiring_growth": 2, "funding": 1}
    kept: list[Candidate] = []
    for cand in sorted(candidates, key=lambda c: tightness.get(c.mode, 0), reverse=True):
        match = None
        for existing in kept:
            if hard_match(cand.as_dict(), existing.as_dict()):
                match = existing
                break
        if match is None:
            kept.append(cand)
            continue
        for url in cand.source_urls:
            if url not in match.source_urls:
                match.source_urls.append(url)
        for hit in cand.keyword_hits:
            if hit not in match.keyword_hits:
                match.keyword_hits.append(hit)
        # Keep the strongest NYC evidence we saw anywhere.
        rank = {"strong": 3, "moderate": 2, "weak": 1, "none": 0}
        if rank.get(cand.nyc_angle, 0) > rank.get(match.nyc_angle, 0):
            match.nyc_angle = cand.nyc_angle
            match.nyc_evidence = cand.nyc_evidence or match.nyc_evidence
        elif not match.nyc_evidence:
            match.nyc_evidence = cand.nyc_evidence
        # Fill blanks from the duplicate rather than losing the fact.
        for attr in ("website", "linkedin", "crunchbase", "x_handle", "one_liner",
                     "founders", "founded", "hq", "industries", "investors",
                     "last_funding_type", "last_funding_date"):
            if not getattr(match, attr) and getattr(cand, attr):
                setattr(match, attr, getattr(cand, attr))
        for attr in ("last_funding_usd", "total_funding_usd", "num_rounds",
                     "nyc_open_roles_estimate"):
            if getattr(match, attr) is None and getattr(cand, attr) is not None:
                setattr(match, attr, getattr(cand, attr))
        if cand.lane not in match.lane:
            match.lane = f"{match.lane}+{cand.lane}"
    return kept


def one_pass(args: argparse.Namespace) -> dict:
    cfg = research_config()
    caps = cfg["caps"]

    print(f"== discovery pass (lane={args.lane} mode={args.mode or 'all'}) ==", flush=True)
    found, reports = collect(args.lane, args.mode)
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
        print("rejected:", flush=True)
        for cand in rejected[:25]:
            print(f"  - {cand.company}: {cand.qualify_reason}", flush=True)

    known = notion_existing_keys() if not args.no_notion_check else set()
    if known:
        print(f"CRM Core pre-filter: {len(known)} companies already on the board", flush=True)

    fresh: list[Candidate] = []
    on_board = already_sent = no_identity = 0
    with state.connect() as conn:
        run_id = state.start_run(conn, f"{args.lane}:{args.mode or 'all'}")
        for cand in passed:
            state.record_seen(conn, cand)
            if not cand.key:
                no_identity += 1
            elif cand.key in known:
                on_board += 1
            elif state.is_emitted(conn, cand.key):
                already_sent += 1
            else:
                fresh.append(cand)

        # Tight-mode finds are the scarce, high-intent ones — never let a flood
        # of funding rows push them out of the batch.
        order = {"office_expansion": 0, "founder_language": 1, "hiring_growth": 2, "funding": 3}
        fresh.sort(key=lambda c: (order.get(c.mode, 9), c.company.lower()))
        over_cap = max(0, len(fresh) - caps["maxCandidatesPerRun"])
        fresh = fresh[: caps["maxCandidatesPerRun"]]
        if over_cap:
            print(
                f"! {over_cap} held back by maxCandidatesPerRun="
                f"{caps['maxCandidatesPerRun']} — they surface next pass",
                flush=True,
            )

        outcome: dict = {
            "ran_at": datetime.now(timezone.utc).isoformat(),
            "mode": "write" if args.write else "dry_run",
            "lane": args.lane,
            "mode_filter": args.mode,
            "counts": {
                "raw": len(found),
                "merged": len(merged),
                "rejected": len(rejected),
                "already_on_board": on_board,
                "already_emitted": already_sent,
                "no_identity": no_identity,
                "held_by_cap": over_cap,
                "emitted": 0,
                "by_mode": dict(by_mode),
            },
            "lanes": reports,
            "candidates": [c.as_dict() for c in fresh],
            "rejected_sample": [
                {"company": c.company, "mode": c.mode, "reason": c.qualify_reason}
                for c in rejected[:50]
            ],
        }

        if args.write and fresh:
            csv_path = write_intake_csv(fresh)
            evidence_path = write_evidence(fresh)
            state.mark_emitted(conn, [c.key for c in fresh])
            outcome["counts"]["emitted"] = len(fresh)
            outcome["csv"] = str(csv_path)
            outcome["evidence"] = str(evidence_path)
            print(f"\nwrote {len(fresh)} candidates → {csv_path}", flush=True)
            print(f"evidence → {evidence_path}", flush=True)

            if args.promote or cfg["promote"].get("enabled"):
                cap = cfg["promote"].get("maxPerRun", 25)
                promotable = fresh[:cap]
                if len(fresh) > cap:
                    print(f"promote: capping {len(fresh)} → {cap}", flush=True)
                promote_csv = write_intake_csv(
                    promotable, csv_path.with_name(csv_path.stem + "-promote.csv")
                )
                try:
                    summary = promote_via_intake(promote_csv)
                    outcome["promote"] = summary["counts"]
                    print(
                        f"promoted {len(promotable)} → crm_intake.py: "
                        f"{json.dumps(summary['counts'])}",
                        flush=True,
                    )
                except SinkError as exc:
                    outcome["promote_error"] = str(exc)
                    print(f"! promotion failed: {exc}", flush=True)
            else:
                print(
                    "\nnext (in NormanAI-crm-core):\n"
                    f"  python3 scripts/crm_intake.py --csv {csv_path} --dry-run\n"
                    f"  python3 scripts/crm_intake.py --csv {csv_path} --write --yes",
                    flush=True,
                )
        # The brief is the point of the whole run — always write it, dry or live.
        brief = digest.render(fresh, outcome["counts"], args.mode)
        brief_path = ROOT / "out" / f"brief-{date.today().isoformat()}.md"
        brief_path.parent.mkdir(parents=True, exist_ok=True)
        brief_path.write_text(brief)
        outcome["brief"] = str(brief_path)
        print("\n" + "─" * 60, flush=True)
        print(brief, flush=True)
        print("─" * 60, flush=True)
        print(f"brief → {brief_path}", flush=True)

        state.finish_run(conn, run_id, len(found), outcome["counts"]["emitted"])
        outcome["store"] = state.stats(conn)

    receipt = ROOT / "state" / "research_latest.json"
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(json.dumps(outcome, indent=2, default=str))
    c = outcome["counts"]
    print(
        f"\nDONE raw={c['raw']} merged={c['merged']} rejected={c['rejected']} "
        f"emitted={c['emitted']} receipt={receipt}",
        flush=True,
    )
    return outcome


def run_loop(args: argparse.Namespace) -> int:
    """Batch → rest → repeat, with the anti-spin guards sales-nav learned."""
    caps = research_config()["caps"]
    rest = args.batch_rest if args.batch_rest is not None else caps["loopRestSeconds"]
    max_batches = args.max_batches if args.max_batches is not None else caps["loopMaxBatches"]

    empty_streak = 0
    for batch in range(1, max_batches + 1):
        print(f"\n───────── batch {batch}/{max_batches} ─────────", flush=True)
        outcome = one_pass(args)

        if any(r["outcome"] == "blocked" for r in outcome["lanes"]):
            print("! a lane reported blocked — stopping the loop", flush=True)
            return 1

        if outcome["counts"]["emitted"] == 0:
            empty_streak += 1
            if empty_streak >= 2:
                print("! two empty batches in a row — stopping (anti-spin)", flush=True)
                return 0
        else:
            empty_streak = 0

        if batch < max_batches:
            print(f"resting {rest}s…", flush=True)
            time.sleep(rest)
    return 0


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
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--batch-rest", type=int, default=None)
    parser.add_argument("--max-batches", type=int, default=None)
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

    if args.loop:
        if not args.write:
            raise SystemExit("--loop only makes sense with --write --yes")
        return run_loop(args)

    one_pass(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
