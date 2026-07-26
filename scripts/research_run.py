#!/usr/bin/env python3
"""NormanAI-research — discovery run.

Finds NYC companies showing office-demand signals via Grok's Agent Tools
(x_search + web_search), dedups against what we've already handed over, and
writes an intake CSV for NormanAI-crm-core.

    python3 scripts/research_run.py --dry-run
    python3 scripts/research_run.py --lane x --dry-run
    python3 scripts/research_run.py --write --yes
    python3 scripts/research_run.py --write --yes --loop --batch-rest 900

Then, in NormanAI-crm-core:

    python3 scripts/crm_intake.py --csv <emitted.csv> --dry-run
    python3 scripts/crm_intake.py --csv <emitted.csv> --write --yes

This script NEVER writes Notion. crm_intake.py is the only writer.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from lib import state  # noqa: E402
from lib.candidates import Candidate  # noqa: E402
from lib.config import load_env_key, research_config  # noqa: E402
from lib.discover import run_lane, web_lanes, x_lanes  # noqa: E402
from lib.identity import hard_match  # noqa: E402
from lib.sinks import (  # noqa: E402
    SinkError,
    notion_existing_keys,
    push_supabase,
    write_evidence,
    write_intake_csv,
)


def collect(lane_kind: str, verbose: bool = True) -> tuple[list[Candidate], list[dict]]:
    """Run every lane of the requested kind. Returns (candidates, lane reports)."""
    plan: list[tuple[dict, str]] = []
    if lane_kind in {"x", "all"}:
        plan += [(lane, "x") for lane in x_lanes()]
    if lane_kind in {"web", "all"}:
        plan += [(lane, "web") for lane in web_lanes()]

    cap = research_config()["caps"]["maxSearchesPerRun"]
    if len(plan) > cap:
        if verbose:
            print(f"! capping {len(plan)} lanes to maxSearchesPerRun={cap}", flush=True)
        plan = plan[:cap]

    found: list[Candidate] = []
    reports: list[dict] = []
    for lane, kind in plan:
        result = run_lane(lane, kind)
        reports.append(
            {
                "lane": result["lane"],
                "kind": result["kind"],
                "outcome": result["outcome"],
                "found": len(result["candidates"]),
                "note": result["note"],
                "usage": result["usage"],
            }
        )
        if verbose:
            note = f" — {result['note']}" if result["note"] else ""
            print(
                f"  {result['kind']}/{result['lane']}: {result['outcome']} "
                f"({len(result['candidates'])} found){note}",
                flush=True,
            )
        found.extend(result["candidates"])
    return found, reports


def dedupe_within(candidates: list[Candidate]) -> list[Candidate]:
    """Collapse the same company appearing in several lanes.

    Keeps the highest-scoring version and unions its evidence, so a company seen
    by both X and the web ends up stronger, not duplicated.
    """
    kept: list[Candidate] = []
    for cand in sorted(candidates, key=lambda c: c.signal_strength, reverse=True):
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
        for sig in cand.signals:
            if sig not in match.signals:
                match.signals.append(sig)
        if cand.lane not in match.lane:
            match.lane = f"{match.lane}+{cand.lane}"
    return kept


def one_pass(args: argparse.Namespace) -> dict:
    cfg = research_config()
    caps = cfg["caps"]
    threshold = args.min_strength if args.min_strength is not None else caps["minSignalStrength"]

    print(f"== discovery pass ({args.lane}) ==", flush=True)
    found, reports = collect(args.lane)
    print(f"raw candidates: {len(found)}", flush=True)

    merged = dedupe_within(found)
    strong = [c for c in merged if c.signal_strength >= threshold]
    weak = len(merged) - len(strong)

    known = notion_existing_keys() if not args.no_notion_check else set()
    if known:
        print(f"CRM Core pre-filter: {len(known)} companies already on the board", flush=True)

    fresh: list[Candidate] = []
    on_board = 0
    already_sent = 0
    no_identity = 0
    with state.connect() as conn:
        run_id = state.start_run(conn, args.lane)
        for cand in strong:
            state.record_seen(conn, cand)
            if not cand.key:
                no_identity += 1
                continue
            if cand.key in known:
                on_board += 1
                continue
            if state.is_emitted(conn, cand.key):
                already_sent += 1
                continue
            fresh.append(cand)

        fresh.sort(key=lambda c: c.signal_strength, reverse=True)
        over_cap = max(0, len(fresh) - caps["maxCandidatesPerRun"])
        fresh = fresh[: caps["maxCandidatesPerRun"]]
        if over_cap:
            print(
                f"! {over_cap} candidates held back by maxCandidatesPerRun="
                f"{caps['maxCandidatesPerRun']} — they stay unemitted and will "
                "surface next pass",
                flush=True,
            )

        outcome: dict = {
            "ran_at": datetime.now(timezone.utc).isoformat(),
            "mode": "write" if args.write else "dry_run",
            "lane": args.lane,
            "threshold": threshold,
            "counts": {
                "raw": len(found),
                "merged": len(merged),
                "below_threshold": weak,
                "already_on_board": on_board,
                "already_emitted": already_sent,
                "no_identity": no_identity,
                "held_by_cap": over_cap,
                "emitted": 0,
            },
            "lanes": reports,
            "candidates": [c.as_dict() for c in fresh],
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

            if cfg["sink"]["supabase"].get("enabled"):
                try:
                    pushed = push_supabase(fresh)
                    outcome["supabase_rows"] = pushed
                    print(f"supabase: upserted {pushed} rows", flush=True)
                except SinkError as exc:
                    outcome["supabase_error"] = str(exc)
                    print(f"! supabase sink failed: {exc}", flush=True)

            print(
                "\nnext (in NormanAI-crm-core):\n"
                f"  python3 scripts/crm_intake.py --csv {csv_path} --dry-run\n"
                f"  python3 scripts/crm_intake.py --csv {csv_path} --write --yes",
                flush=True,
            )
        elif fresh:
            print(f"\nDRY RUN — {len(fresh)} candidates would be emitted:", flush=True)
            for cand in fresh:
                proof = cand.nyc_proof[:70] if cand.nyc_proof else "(no NYC proof)"
                print(
                    f"  [{cand.signal_strength:>3}] {cand.company} — "
                    f"{', '.join(cand.signals) or 'no signals'} — {proof}",
                    flush=True,
                )
        else:
            print("\nnothing new to emit this pass", flush=True)

        state.finish_run(conn, run_id, len(found), outcome["counts"]["emitted"])
        outcome["store"] = state.stats(conn)

    receipt = ROOT / "state" / "research_latest.json"
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(json.dumps(outcome, indent=2, default=str))
    c = outcome["counts"]
    print(
        f"\nDONE raw={c['raw']} merged={c['merged']} weak={c['below_threshold']} "
        f"emitted={c['emitted']} receipt={receipt}",
        flush=True,
    )
    return outcome


def run_loop(args: argparse.Namespace) -> int:
    """Batch → rest → repeat, with the same anti-spin guards sales-nav learned.

    Stops on: max batches, a blocked API (auth/deprecation), or two consecutive
    batches that emit nothing. A loop that emits nothing twice running is not
    quietly working — it is burning Grok search credits.
    """
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
    parser.add_argument("--write", action="store_true", help="emit the intake CSV")
    parser.add_argument("--yes", action="store_true", help="required with --write")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--min-strength", type=int, default=None)
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

    # Preflight: a missing key should fail before any lane runs, not halfway
    # through a pass with partial state written.
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
