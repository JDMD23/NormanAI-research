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
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from lib import chrome, extract, grok, pipeline  # noqa: E402
from lib.candidates import Candidate  # noqa: E402
from lib.config import load_config, load_env_key, research_config  # noqa: E402


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
    parser.add_argument("--show-rejects", action="store_true")
    parser.add_argument("--no-notion-check", action="store_true")
    args = parser.parse_args()
    args.mode = None

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
        raise SystemExit(f"{key_env} is not set — extraction needs it.")

    print("== browse pass ==", flush=True)
    try:
        found, reports = collect(args.source)
    except chrome.ChromeUnavailable as exc:
        print(f"\nSkipped: {exc}", flush=True)
        print("This lane needs a Mac with Chrome logged into Crunchbase and Substack.",
              flush=True)
        return 0

    print(f"raw candidates: {len(found)}", flush=True)
    pipeline.run(found, reports, args, label=f"browse:{args.source or 'all'}", slug="browse")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
