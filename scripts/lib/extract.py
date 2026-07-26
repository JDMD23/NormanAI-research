"""Pull companies out of a page Chrome already fetched.

The browser gets past the login; this does the reading. No search tools are
attached — Grok is working purely from text we handed it, so it cannot wander
off and invent a company it half-remembers.

Same response schema and same qualification rules as the search lanes, so a
Crunchbase find and an X find are indistinguishable downstream.
"""

from __future__ import annotations

from typing import Any

from lib import grok
from lib.candidates import Candidate, response_schema
from lib.qualify import modes_config

PROMPT = """\
You are a sourcing analyst for a Manhattan commercial real estate tenant-rep broker.

Below is the text of a page from {source}. Extract the COMPANIES worth tracking.

{focus}

Hard rules:
1. Use ONLY what is in the text below. If it is not on this page, it does not
   exist — do not add companies, funding amounts, or details from memory.
2. `keyword_hits` must quote the exact phrases from this page that made you
   include the company. No phrase, no candidate.
3. `nyc_evidence` is quoted or closely paraphrased FROM THIS PAGE. If the page
   does not establish a New York connection, set it to null and nyc_angle to
   "none" — that is a fine answer, not a failure.
4. Unknown means null. Never fill a field with 0, "N/A", or a guess.
5. Set `source_urls` to the page URL: {url}
6. Report COMPANIES, not people, funds, or publications. Skip the newsletter or
   database itself, and skip investors unless they are also the subject.
7. Skip biotech and therapeutics companies entirely.
{mode_hint}
Return at most {limit} companies.

--- PAGE: {title} ---
{body}
--- END PAGE ---
"""

MODE_HINT = """\
8. Set `mode` per company to whichever fits the signal you found:
   - office_expansion — a concrete NYC office, lease, move, or space statement
   - hiring_growth — multiple NYC roles, a senior NYC hire, a team build-out
   - founder_language — a founder/CEO quote showing real space pressure
   - funding — a raise, valuation, or ARR milestone (the default)
"""


def _mode_focus(mode: str) -> str:
    spec = modes_config()["modes"].get(mode)
    if not spec:
        return ""
    keywords = "\n".join(f"  - {k}" for k in spec["keywords"][:24])
    return f"What qualifies ({spec['label']}):\n{keywords}"


def from_page(
    page: dict,
    *,
    source: str,
    lane: str,
    mode: str = "funding",
    limit: int = 15,
    multi_mode: bool = False,
) -> list[Candidate]:
    """Extract candidates from a fetched page. Raises GrokError on API failure."""
    body = str(page.get("body") or "")
    if not body.strip():
        return []

    prompt = PROMPT.format(
        source=source,
        focus=_mode_focus(mode),
        url=page.get("url", ""),
        title=page.get("title", ""),
        body=body,
        limit=limit,
        mode_hint=MODE_HINT if multi_mode else "",
    )

    # No tools: extraction only, from the text we supplied.
    body_req = grok.build_request(prompt, tools=[], response_schema=response_schema())
    response = grok.call(body_req)
    parsed = grok.extract_json(grok.message_text(response))

    raw_list = parsed.get("candidates") if isinstance(parsed, dict) else parsed
    if not isinstance(raw_list, list):
        return []

    out: list[Candidate] = []
    for raw in raw_list[:limit]:
        if not isinstance(raw, dict) or not (raw.get("company") or "").strip():
            continue
        cand = Candidate.from_model(raw, lane=lane, mode=mode)
        if not cand.source_urls and page.get("url"):
            cand.source_urls = [str(page["url"])]
        out.append(cand)
    return out


def usage_of(response: dict[str, Any]) -> dict[str, Any]:
    return grok.usage(response)
