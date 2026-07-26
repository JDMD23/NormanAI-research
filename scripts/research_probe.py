#!/usr/bin/env python3
"""Verify the xAI Agent Tools request shape against the live API.

Run this FIRST, and any time a run starts failing oddly. `docs.x.ai` blocks
automated fetching, so `lib/grok.build_request()` was assembled from xAI's
migration notices rather than read off the official reference. This script makes
one cheap real call and shows exactly what came back, so a shape mismatch is a
30-second diagnosis instead of a confusing empty batch.

    python3 scripts/research_probe.py
    python3 scripts/research_probe.py --tool web_search
    python3 scripts/research_probe.py --raw
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from lib import grok  # noqa: E402
from lib.config import load_env_key, research_config  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe the xAI Agent Tools API")
    parser.add_argument("--tool", choices=["x_search", "web_search", "both"], default="both")
    parser.add_argument("--raw", action="store_true", help="dump the full JSON response")
    parser.add_argument(
        "--prompt",
        default="Name one company that announced a new New York City office in the last 30 days. "
                "Reply with the company name and the source URL, nothing else.",
    )
    args = parser.parse_args()

    cfg = research_config()["grok"]
    key_env = cfg.get("apiKeyEnv", "XAI_API_KEY")
    if not load_env_key(key_env):
        print(f"FAIL: {key_env} is not set. Add it to your environment or .env.")
        return 2

    tools = ["x_search", "web_search"] if args.tool == "both" else [args.tool]
    body = grok.build_request(args.prompt, tools=tools)

    print("=== request ===")
    print(json.dumps(body, indent=2))
    print(f"\nPOST {cfg['baseUrl'].rstrip('/')}{cfg.get('endpoint', '/chat/completions')}\n")

    try:
        response = grok.call(body)
    except grok.GrokDeprecatedAPI as exc:
        print("FAIL: the API says this shape is retired.\n")
        print(exc)
        print(
            "\nFix: update build_request() in scripts/lib/grok.py to the current "
            "Agent Tools shape, then re-run this probe."
        )
        return 1
    except grok.GrokError as exc:
        print(f"FAIL: {exc}\n")
        print(
            "If this is a 4xx about unknown fields, the tool spec shape is wrong — "
            "fix build_request() in scripts/lib/grok.py and re-run."
        )
        return 1

    print("=== response ===")
    if args.raw:
        print(json.dumps(response, indent=2))
    else:
        print("text:", grok.message_text(response)[:1500] or "(empty)")
        print("\ncitations:")
        for url in grok.citations(response) or ["(none returned)"]:
            print("  -", url)
        print("\nusage:", json.dumps(grok.usage(response)))
        print("\ntop-level keys:", sorted(response.keys()))

    text = grok.message_text(response)
    cites = grok.citations(response)
    print("\n=== verdict ===")
    if text and cites:
        print("PASS — model answered and returned citations. Search tools are live.")
        return 0
    if text and not cites:
        print(
            "PARTIAL — got an answer but no citations. The call worked; citation "
            "extraction may need a shape fix in grok.citations(). Re-run with --raw "
            "and look for where the source URLs actually live."
        )
        return 0
    print("FAIL — empty response text. Re-run with --raw and inspect the payload.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
