"""Everything that happens after candidates are found.

Both lanes — Grok search and logged-in browser — converge here: qualify,
merge duplicates, drop what we've already handed over, write one brief, one
CSV, one intake call. Keeping this in one place is what lets the daily run
produce a single page instead of one per lane.

Collectors differ. The tail never should.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path

from lib import digest, qualify, state
from lib.config import ROOT, research_config
from lib.identity import hard_match
from lib.sinks import (
    SinkError,
    notion_existing_keys,
    promote,
    resolve_promote_target,
    write_crmx_intake_csv,
    write_evidence,
    write_intake_csv,
)

# Highest-intent first. A signed lease outranks a funding round, so tight-mode
# finds are never pushed out of a batch by a flood of raises.
MODE_ORDER = {"office_expansion": 0, "founder_language": 1, "hiring_growth": 2, "funding": 3}
TIGHTNESS = {"founder_language": 3, "office_expansion": 3, "hiring_growth": 2, "funding": 1}

_FILL_TEXT = (
    "website", "linkedin", "crunchbase", "x_handle", "one_liner", "founders",
    "founded", "hq", "industries", "investors", "last_funding_type", "last_funding_date",
)
_FILL_NUM = ("last_funding_usd", "total_funding_usd", "num_rounds", "nyc_open_roles_estimate")


def dedupe_within(candidates: list) -> list:
    """Collapse the same company found by several sources.

    The tighter mode wins because it is the more specific claim, and evidence
    is unioned rather than discarded — a company seen by both the funding sweep
    and Crunchbase keeps both receipts. Blanks are filled from the duplicate;
    the merged row is the only one that reaches intake, so a lossy merge is
    silent data loss.
    """
    kept: list = []
    for cand in sorted(candidates, key=lambda c: TIGHTNESS.get(c.mode, 0), reverse=True):
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

        rank = {"strong": 3, "moderate": 2, "weak": 1, "none": 0}
        if rank.get(cand.nyc_angle, 0) > rank.get(match.nyc_angle, 0):
            match.nyc_angle = cand.nyc_angle
            match.nyc_evidence = cand.nyc_evidence or match.nyc_evidence
        elif not match.nyc_evidence:
            match.nyc_evidence = cand.nyc_evidence

        for attr in _FILL_TEXT:
            if not getattr(match, attr) and getattr(cand, attr):
                setattr(match, attr, getattr(cand, attr))
        for attr in _FILL_NUM:
            if getattr(match, attr) is None and getattr(cand, attr) is not None:
                setattr(match, attr, getattr(cand, attr))

        if cand.lane not in match.lane:
            match.lane = f"{match.lane}+{cand.lane}"
    return kept


def run(found: list, reports: list[dict], args, *, label: str, slug: str = "") -> dict:
    """Qualify → dedup → emit → brief. Returns the run receipt."""
    cfg = research_config()
    caps = cfg["caps"]
    today = date.today().isoformat()
    tag = f"-{slug}" if slug else ""

    merged = dedupe_within(found)
    passed, rejected = qualify.apply(merged)

    by_mode = Counter(c.mode for c in passed)
    print(
        f"qualified: {len(passed)} of {len(merged)} "
        f"({', '.join(f'{m}={n}' for m, n in sorted(by_mode.items())) or 'none'})",
        flush=True,
    )
    if rejected and getattr(args, "show_rejects", False):
        print("rejected:", flush=True)
        for cand in rejected[:25]:
            print(f"  - {cand.company}: {cand.qualify_reason}", flush=True)

    known = set() if getattr(args, "no_notion_check", False) else notion_existing_keys()
    if known:
        print(
            f"board pre-filter (Notion read-only): {len(known)} companies already present",
            flush=True,
        )

    fresh: list = []
    on_board = already_sent = no_identity = 0

    with state.connect() as conn:
        run_id = state.start_run(conn, label)
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

        fresh.sort(key=lambda c: (MODE_ORDER.get(c.mode, 9), c.company.lower()))
        over_cap = max(0, len(fresh) - caps["maxCandidatesPerRun"])
        fresh = fresh[: caps["maxCandidatesPerRun"]]
        if over_cap:
            print(
                f"! {over_cap} held back by maxCandidatesPerRun="
                f"{caps['maxCandidatesPerRun']} — they surface next run",
                flush=True,
            )

        counts = {
            "raw": len(found),
            "merged": len(merged),
            "rejected": len(rejected),
            "already_on_board": on_board,
            "already_emitted": already_sent,
            "no_identity": no_identity,
            "held_by_cap": over_cap,
            "emitted": 0,
            "by_mode": dict(by_mode),
        }
        outcome: dict = {
            "ran_at": datetime.now(timezone.utc).isoformat(),
            "label": label,
            "mode": "write" if getattr(args, "write", False) else "dry_run",
            "sources": reports,
            "counts": counts,
            "candidates": [c.as_dict() for c in fresh],
            "rejected_sample": [
                {"company": c.company, "mode": c.mode, "reason": c.qualify_reason}
                for c in rejected[:50]
            ],
        }

        if getattr(args, "write", False) and fresh:
            legacy_csv = write_intake_csv(
                fresh, ROOT / "out" / f"intake{tag}-{today}.csv"
            )
            crmx_csv = write_crmx_intake_csv(
                fresh, ROOT / "out" / f"crmx-intake{tag}-{today}.csv"
            )
            evidence_path = write_evidence(
                fresh, ROOT / "out" / f"evidence{tag}-{today}.json"
            )
            state.mark_emitted(conn, [c.key for c in fresh])
            counts["emitted"] = len(fresh)
            outcome["csv"] = str(legacy_csv)
            outcome["crmx_csv"] = str(crmx_csv)
            outcome["evidence"] = str(evidence_path)
            print(f"\nwrote {len(fresh)} → {crmx_csv}", flush=True)
            print(f"evidence sidecar → {evidence_path}", flush=True)

            try:
                target = resolve_promote_target(
                    promote=bool(getattr(args, "promote", False)),
                    legacy=bool(getattr(args, "promote_legacy_crm_core", False)),
                )
            except SinkError as exc:
                outcome["promote_error"] = str(exc)
                print(f"! promotion failed: {exc}", flush=True)
                target = None

            if target:
                cap = cfg["promote"].get("maxPerRun", 25)
                promotable = fresh[:cap]
                handoff_csv = crmx_csv if target == "crmx" else legacy_csv
                handoff_evidence = evidence_path
                if len(fresh) > cap:
                    print(f"promote: capping {len(fresh)} → {cap}", flush=True)
                    if target == "crmx":
                        handoff_csv = write_crmx_intake_csv(
                            promotable,
                            crmx_csv.with_name(crmx_csv.stem + "-promote.csv"),
                        )
                    else:
                        handoff_csv = write_intake_csv(
                            promotable,
                            legacy_csv.with_name(legacy_csv.stem + "-promote.csv"),
                        )
                    handoff_evidence = write_evidence(
                        promotable,
                        evidence_path.with_name(
                            evidence_path.stem + "-promote.json"
                        ),
                    )
                try:
                    summary = promote(
                        handoff_csv,
                        evidence_path=handoff_evidence,
                        target=target,
                    )
                    outcome["promote"] = summary.get("counts")
                    outcome["promote_target"] = target
                    outcome["promote_detail"] = {
                        k: summary.get(k)
                        for k in (
                            "target",
                            "csv",
                            "evidence",
                            "db",
                            "added_from",
                            "note",
                        )
                        if k in summary
                    }
                    print(
                        f"promoted {len(promotable)} → {target}: "
                        f"{json.dumps(summary.get('counts'))}",
                        flush=True,
                    )
                    if target == "crmx" and handoff_evidence:
                        print(
                            f"  evidence sidecar for CRMx ingest (not yet consumed "
                            f"by ingest_csv): {handoff_evidence}",
                            flush=True,
                        )
                except SinkError as exc:
                    outcome["promote_error"] = str(exc)
                    print(f"! promotion failed: {exc}", flush=True)
            else:
                print(
                    "\nnext (NormanAI-CRMx intake):\n"
                    f"  export NORMAN_CRMX_PATH=/path/to/NormanAI-CRMx\n"
                    f"  export NORMAN_CRMX_DB=/path/to/norman.sqlite\n"
                    f"  (cd \"$NORMAN_CRMX_PATH\" && uv run python -m "
                    f"norman.tools.ingest_csv {crmx_csv} \"$NORMAN_CRMX_DB\" "
                    f"--added-from research:{today})\n"
                    f"  # evidence sidecar (CRMx CSV-only today — do not drop): "
                    f"{evidence_path}",
                    flush=True,
                )

        brief = digest.render(fresh, counts, getattr(args, "mode", None))
        brief_path = ROOT / "out" / f"brief{tag}-{today}.md"
        brief_path.parent.mkdir(parents=True, exist_ok=True)
        brief_path.write_text(brief)
        outcome["brief"] = str(brief_path)
        print("\n" + "─" * 60, flush=True)
        print(brief, flush=True)
        print("─" * 60, flush=True)
        print(f"brief → {brief_path}", flush=True)

        state.finish_run(conn, run_id, len(found), counts["emitted"])
        outcome["store"] = state.stats(conn)

    receipt = ROOT / "state" / f"latest{tag or '-run'}.json"
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(json.dumps(outcome, indent=2, default=str))
    c = counts
    print(
        f"\nDONE raw={c['raw']} merged={c['merged']} rejected={c['rejected']} "
        f"emitted={c['emitted']} receipt={receipt}",
        flush=True,
    )
    return outcome
