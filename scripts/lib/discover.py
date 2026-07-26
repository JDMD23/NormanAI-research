"""Discovery lanes: ask Grok for companies, get back scored candidates.

One lane = one search intent (a set of X handles, or a web query) run against
the Agent Tools API with a structured-output schema. Lanes are independent so a
blocked or empty lane never takes the run down with it.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from lib import grok
from lib.candidates import Candidate, response_schema, score
from lib.config import research_config, signals_config, sources_config

PROMPT = """\
You are a sourcing analyst for a Manhattan commercial real estate tenant-rep broker.
Your job is to find COMPANIES that may soon need New York City office space.

Search task: {query}

Look at the last {window_days} days.

What counts as a lead — the signals you may report:
{signal_list}

Hard rules, in priority order:
1. Report COMPANIES, not people, funds, products, or news outlets.
2. Every company needs `nyc_proof`: a concrete statement of NYC presence or NYC
   intent, quoted or closely paraphrased from a source you actually read. If you
   cannot find one, set nyc_proof to null. Do NOT guess, and do NOT report a
   company just because it is well known.
3. Unknown means null. Never fill a field with 0, "N/A", "unknown", or a guess.
   A blank field is correct and useful; an invented one corrupts the pipeline.
4. Skip biotech and therapeutics companies entirely.
5. Skip companies whose only NYC connection is an investor being NYC-based.
6. `source_urls` must be links you actually used. No links, no candidate.
7. Prefer companies between roughly 20 and 1000 employees. Enormous public
   companies and 3-person pre-seed teams are both out of scope.

Return at most {limit} companies, strongest signal first.
"""


def _signal_list() -> str:
    lines = []
    for name, spec in signals_config()["signals"].items():
        lines.append(f"  - {name}: {spec['meaning']}")
    return "\n".join(lines)


def _window(days: int) -> tuple[str, str]:
    today = date.today()
    return (today - timedelta(days=days)).isoformat(), today.isoformat()


def build_prompt(query: str, window_days: int, limit: int) -> str:
    return PROMPT.format(
        query=query,
        window_days=window_days,
        signal_list=_signal_list(),
        limit=limit,
    )


def run_lane(lane: dict[str, Any], kind: str, limit: int | None = None) -> dict[str, Any]:
    """Execute one lane. Returns a result dict; never raises for lane-local failure.

    A lane that fails is reported as an outcome, not an exception — one blocked
    source must not cost the whole run, same as crm-core's per-lane outcomes.
    """
    cfg = research_config()
    cap = limit or cfg["caps"]["maxCandidatesPerLane"]
    window_days = int(lane.get("windowDays", 7))
    from_date, to_date = _window(window_days)

    tools = ["x_search"] if kind == "x" else ["web_search"]
    allowed = lane.get("allowedHandles") or None
    excluded = None if allowed else (sources_config().get("excludedHandles") or None)

    body = grok.build_request(
        build_prompt(lane["query"], window_days, cap),
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
        cand = Candidate.from_model(raw, lane=lane["id"])
        if not cand.source_urls:
            # Fall back to the lane's own citations rather than dropping a
            # candidate that is otherwise well-evidenced.
            cand.source_urls = lane_citations[:3]
        candidates.append(score(cand))

    result["candidates"] = candidates
    if not candidates:
        result["note"] = "lane ran clean but found nothing"
    return result


def x_lanes() -> list[dict[str, Any]]:
    return sources_config()["xLanes"]


def web_lanes() -> list[dict[str, Any]]:
    return sources_config()["webLanes"]
