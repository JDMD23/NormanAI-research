"""Discovery lanes: ask Grok for companies, get back qualified candidates.

Two postures run at the same time, per JD's operating rules:

  BROAD (funding)  — high coverage, NYC not required.
  TIGHT (the rest) — concrete NYC office demand or real headcount growth only.

The postures need genuinely different prompts. Asking one prompt to be both
"catch everything" and "only the certain stuff" produces a model that splits
the difference and does neither.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from lib import grok
from lib.candidates import Candidate, response_schema
from lib.config import research_config, sources_config
from lib.qualify import modes_config

_COMMON_RULES = """\
Hard rules:
1. Report COMPANIES, not people, funds, products, or news outlets.
2. `keyword_hits` must contain the exact qualifying phrases you actually found
   in a source. This is the receipt for why the company is here. No phrase, no
   candidate.
3. `source_urls` must be links you actually used. No links, no candidate.
4. Unknown means null. Never fill a field with 0, "N/A", "unknown", or a guess.
   A blank field is correct and useful; an invented one corrupts the pipeline.
5. `nyc_evidence` is quoted or closely paraphrased from a source. If you have
   none, set it to null and set nyc_angle to "none". Judge nyc_angle ONLY from
   that evidence — never from what you happen to know about the company.
6. The company must be identifiable: a website, X handle, or LinkedIn URL.
7. Skip biotech and therapeutics companies entirely.
"""

BROAD_PROMPT = """\
You are a sourcing analyst for a Manhattan commercial real estate tenant-rep broker.

MODE: Funding & Valuation — BROAD. Your goal is HIGH COVERAGE.
Capture as many legitimate funding and valuation events as you reasonably can.

Search task: {query}
Window: the last {window_days} days.

Qualifying phrases to look for:
{keywords}

Relevant sectors (the company should plausibly be one of these):
{sectors}

IMPORTANT: do NOT require a New York connection in this mode. A funding event
qualifies on its own. Report the NYC angle in `nyc_evidence` and `nyc_angle` so
the broker can see how strong it is — "none" is a perfectly acceptable answer
and does not disqualify the company.

Prefer recall over precision here. A legitimate funding event you are unsure
about is worth reporting; a company you invented is not.

{common}
Return at most {limit} companies.
"""

TIGHT_PROMPT = """\
You are a sourcing analyst for a Manhattan commercial real estate tenant-rep broker.

MODE: {label} — TIGHT. Quality over coverage.
{goal}

Search task: {query}
Window: the last {window_days} days.

ONLY include a company when you find an explicit, concrete statement. The
qualifying phrases are:
{keywords}

This mode exists to find companies that need New York office space NOW or in
the near term. A vague growth mention is NOT enough. "Company is doing well",
"raised a round", or "is based in NYC" do not qualify here — those belong to
the funding mode. What qualifies is a specific, current statement about space,
about a New York office, or about New York headcount growth of the kind that
forces a real estate decision.

If you are not sure, leave the company out. A false positive here sends the
broker to call a company that has no office need, which costs more than a miss.

{common}
Return at most {limit} companies — only the ones that genuinely qualify. Returning
two solid companies is a better answer than ten speculative ones.
"""


def _fmt_list(items: list[str]) -> str:
    return "\n".join(f"  - {i}" for i in items)


def _window(days: int) -> tuple[str, str]:
    today = date.today()
    return (today - timedelta(days=days)).isoformat(), today.isoformat()


def build_prompt(lane: dict[str, Any], limit: int) -> str:
    mode_key = lane.get("mode", "funding")
    mode = modes_config()["modes"][mode_key]
    window_days = int(lane.get("windowDays", 7))

    if mode["posture"] == "broad":
        return BROAD_PROMPT.format(
            query=lane["query"],
            window_days=window_days,
            keywords=_fmt_list(mode["keywords"]),
            sectors=_fmt_list(mode["sectors"]),
            common=_COMMON_RULES,
            limit=limit,
        )
    return TIGHT_PROMPT.format(
        label=mode["label"],
        goal=mode["goal"],
        query=lane["query"],
        window_days=window_days,
        keywords=_fmt_list(mode["keywords"]),
        common=_COMMON_RULES,
        limit=limit,
    )


def run_lane(lane: dict[str, Any], kind: str, limit: int | None = None) -> dict[str, Any]:
    """Execute one lane. Returns a result dict; never raises for lane-local failure.

    A lane that fails is reported as an outcome, not an exception — one blocked
    source must not cost the whole run, same as crm-core's per-lane outcomes.
    """
    cfg = research_config()
    mode_key = lane.get("mode", "funding")
    posture = modes_config()["modes"][mode_key]["posture"]

    # Broad mode is allowed a bigger haul; tight modes should stay small.
    default_cap = cfg["caps"]["maxCandidatesPerLane"]
    cap = limit or (default_cap if posture == "broad" else cfg["caps"]["maxTightPerLane"])

    from_date, to_date = _window(int(lane.get("windowDays", 7)))
    tools = ["x_search"] if kind == "x" else ["web_search"]
    allowed = lane.get("allowedHandles") or None
    excluded = None if allowed else (sources_config().get("excludedHandles") or None)

    body = grok.build_request(
        build_prompt(lane, cap),
        tools=tools,
        allowed_x_handles=allowed if kind == "x" else None,
        excluded_x_handles=excluded if kind == "x" else None,
        from_date=from_date,
        to_date=to_date,
        response_schema=response_schema(),
    )

    result: dict[str, Any] = {
        "lane": lane["id"],
        "kind": kind,
        "mode": mode_key,
        "posture": posture,
        "outcome": "success",
        "candidates": [],
        "note": "",
        "usage": {},
    }

    try:
        response = grok.call(body)
    except grok.GrokDeprecatedAPI as exc:
        result.update(outcome="blocked", note=str(exc))
        return result
    except grok.GrokError as exc:
        result.update(outcome="retry", note=str(exc))
        return result

    result["usage"] = grok.usage(response)

    try:
        parsed = grok.extract_json(grok.message_text(response))
    except grok.GrokError as exc:
        result.update(outcome="retry", note=f"unparseable model output: {exc}")
        return result

    raw_list = parsed.get("candidates") if isinstance(parsed, dict) else parsed
    if not isinstance(raw_list, list):
        result.update(outcome="retry", note="model returned no candidates array")
        return result

    lane_citations = grok.citations(response)
    candidates: list[Candidate] = []
    for raw in raw_list[:cap]:
        if not isinstance(raw, dict) or not (raw.get("company") or "").strip():
            continue
        cand = Candidate.from_model(raw, lane=lane["id"], mode=mode_key)
        if not cand.source_urls:
            cand.source_urls = lane_citations[:3]
        if not cand.source_handle and allowed and len(allowed) == 1:
            cand.source_handle = allowed[0]
        candidates.append(cand)

    result["candidates"] = candidates
    if not candidates:
        result["note"] = "lane ran clean but found nothing"
    return result


def x_lanes() -> list[dict[str, Any]]:
    return sources_config()["xLanes"]


def web_lanes() -> list[dict[str, Any]]:
    return sources_config()["webLanes"]
