"""The daily brief — the one thing JD actually reads.

Everything else this repo produces is machine food: a CSV for intake, a JSON
blob for auditing. This renders the same run as something worth opening with
coffee. Markdown so it works in a terminal, an email, or pasted into Notion.

Ordered by intent, not by volume: a company that just signed a lease matters
more than ten that raised money.
"""

from __future__ import annotations

from datetime import date

# Highest intent first. Funding is last because it's a timing signal — lots of
# rows, least likely to mean "needs space this quarter".
SECTIONS = [
    ("office_expansion", "Office Expansion"),
    ("founder_language", "Founder Language"),
    ("hiring_growth", "Hiring & Team Growth"),
    ("funding", "Funding & Valuation"),
]


def _domain(cand) -> str:
    return cand.domain or cand.website or ""


def _facts(cand) -> str:
    """One line of context: money, stage, sector — whatever we actually have."""
    bits: list[str] = []
    if cand.last_funding_type:
        amount = ""
        if cand.last_funding_usd:
            millions = cand.last_funding_usd / 1_000_000
            amount = f" ${millions:.0f}M" if millions >= 1 else ""
        bits.append(f"{cand.last_funding_type}{amount}")
    elif cand.total_funding_usd:
        bits.append(f"${cand.total_funding_usd / 1_000_000:.0f}M raised")
    if cand.industries:
        bits.append(cand.industries)
    if cand.hq and cand.hq != "NYC":
        bits.append(f"HQ {cand.hq}")
    if cand.nyc_open_roles_estimate:
        bits.append(f"{cand.nyc_open_roles_estimate} NYC roles")
    return " · ".join(bits)


def _entry(cand) -> list[str]:
    lines = [f"**{cand.company}**" + (f" · {_domain(cand)}" if _domain(cand) else "")]

    evidence = cand.nyc_evidence.strip()
    if evidence:
        lines.append(f"> {evidence}")
    elif cand.keyword_hits:
        lines.append(f"> {cand.keyword_hits[0]}")

    facts = _facts(cand)
    if facts:
        lines.append(facts)

    if cand.source_urls:
        lines.append(f"[source]({cand.source_urls[0]})")

    return lines


def render(candidates: list, counts: dict, mode_filter: str | None = None) -> str:
    """Return the brief as markdown. Empty days say so plainly."""
    today = date.today()
    out = [f"# NYC Research — {today.strftime('%a %d %b %Y')}", ""]

    if not candidates:
        out += [
            "**Nothing new today.**",
            "",
            f"Looked at {counts.get('raw', 0)} companies · "
            f"{counts.get('already_on_board', 0)} already on the board · "
            f"{counts.get('rejected', 0)} didn't qualify.",
            "",
        ]
        return "\n".join(out)

    summary = [f"**{len(candidates)} new**"]
    if counts.get("already_on_board"):
        summary.append(f"{counts['already_on_board']} already on the board")
    if counts.get("already_emitted"):
        summary.append(f"{counts['already_emitted']} sent previously")
    if counts.get("rejected"):
        summary.append(f"{counts['rejected']} didn't qualify")
    out += [" · ".join(summary), ""]

    for key, label in SECTIONS:
        if mode_filter and key != mode_filter:
            continue
        group = [c for c in candidates if c.mode == key]
        if not group:
            continue
        out.append(f"## {label} — {len(group)}")
        out.append("")
        for cand in group:
            out += _entry(cand)
            out.append("")

    return "\n".join(out)
