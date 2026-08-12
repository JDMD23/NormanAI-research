#!/usr/bin/env python3
"""The daily run. One command, one brief, one intake call.

This is the thing that gets scheduled. It walks both lanes — Grok search and
the logged-in browser — merges everything, and produces a single morning page
plus a single handoff into NormanAI-CRMx intake.

    python3 scripts/daily.py --dry-run          # look first
    python3 scripts/daily.py --write --yes      # brief + CSV + evidence
    python3 scripts/daily.py --write --yes --promote   # ...into CRMx ingest

    python3 scripts/daily.py --no-browser --write --yes   # search only (CI)

The browser lane is skipped automatically off a Mac, so the same command works
on the cron host and in CI — CI just gets the search half. Research never
writes Notion as SoR.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from lib import chrome, pipeline  # noqa: E402
from lib.config import load_env_key, research_config  # noqa: E402
from research_browse import collect as browse_collect  # noqa: E402
from research_run import collect as search_collect  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="NormanAI daily research run")
    parser.add_argument("--write", action="store_true", help="emit the intake CSV")
    parser.add_argument("--yes", action="store_true", help="required with --write")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--promote",
        action="store_true",
        help="hand the CRMx CSV to NormanAI-CRMx ingest_csv (off unless set)",
    )
    parser.add_argument(
        "--promote-legacy-crm-core",
        action="store_true",
        help="explicit legacy shim: invoke crm-core crm_intake.py instead of CRMx",
    )
    parser.add_argument("--no-browser", action="store_true",
                        help="skip Crunchbase/Substack (use in CI)")
    parser.add_argument("--no-search", action="store_true",
                        help="skip the Grok search lanes")
    parser.add_argument("--show-rejects", action="store_true")
    parser.add_argument("--no-notion-check", action="store_true")
    args = parser.parse_args()
    args.mode = None  # the daily brief always shows all four sections

    if args.write and not args.yes:
        raise SystemExit("--write requires --yes")
    if args.write and args.dry_run:
        raise SystemExit("use either --write or --dry-run")
    if args.promote and args.promote_legacy_crm_core:
        raise SystemExit("use either --promote or --promote-legacy-crm-core")
    if (args.promote or args.promote_legacy_crm_core) and not args.write:
        raise SystemExit("--promote requires --write --yes (it creates CRM rows)")

    key_env = research_config()["grok"].get("apiKeyEnv", "XAI_API_KEY")
    if not load_env_key(key_env):
        raise SystemExit(
            f"{key_env} is not set — nothing to run.\n"
            "Set it, then verify with: python3 scripts/research_probe.py"
        )

    found: list = []
    reports: list[dict] = []

    if not args.no_search:
        print("== search lanes ==", flush=True)
        cands, reps = search_collect("all", None)
        found += cands
        reports += reps

    if not args.no_browser:
        print("\n== browser lanes ==", flush=True)
        try:
            cands, reps = browse_collect(None)
            found += cands
            reports += reps
        except chrome.ChromeUnavailable as exc:
            # Not a failure. CI and any non-Mac host simply run the search half.
            print(f"  skipped — {exc}", flush=True)
            reports.append({"source": "browser", "outcome": "skipped", "found": 0,
                            "note": str(exc)})

    print(f"\nraw candidates: {len(found)}", flush=True)

    outcome = pipeline.run(found, reports, args, label="daily")

    # A lane that reported blocked is worth an exit code — cron can alert on it,
    # and a logged-out browser looks exactly like a quiet day otherwise.
    if any(r.get("outcome") == "blocked" for r in reports):
        print("\n! a source was blocked — check the session and re-run", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
