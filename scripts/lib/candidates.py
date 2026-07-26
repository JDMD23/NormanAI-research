"""Candidate model: the schema Grok fills, the score, and the intake CSV row.

The CSV column names below are not cosmetic — they are matched against
`CSV_ALIASES` in NormanAI-crm-core's `scripts/crm_intake.py`. Renaming a column
here silently drops that field on intake. Test coverage pins them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from lib.config import research_config, signals_config
from lib.identity import identity_key, normalize_domain, x_handle_url

# Order matters: this is the CSV header, and it mirrors crm_intake.py's aliases.
CSV_COLUMNS = [
    "Company",
    "Website",
    "Company Linkedin",
    "Crunchbase",
    "Founders",
    "One-Liner",
    "X Handle",
    "Founded",
    "HQ",
    "Industries",
    "Last Funding Date",
    "Last Funding Amount (in USD)",
    "Last Funding Type",
    "Total Funding Amount (in USD)",
    "Number of Funding Rounds",
    "Top 5 Investors",
]

# HQ values crm-core's `map_hq` recognises.
HQ_OPTIONS = [
    "NYC", "SF Bay", "LA", "Boston", "Austin", "Seattle", "Chicago",
    "Remote", "Other US", "International", "Unknown",
]

# Funding types crm-core's `map_funding_type` recognises.
FUNDING_TYPES = [
    "Pre-Seed", "Seed", "Series A", "Series B", "Series C", "Series D",
    "Series E+", "Growth", "Debt Financing", "Corporate Round", "Grant",
    "Private Equity", "Other", "Unknown",
]


def response_schema() -> dict[str, Any]:
    """JSON schema handed to Grok so candidates come back structured.

    Every field is nullable rather than defaulted: an unknown must arrive as
    null so it can stay unknown. Unknown ≠ 0 is a crm-core rule and it starts
    here, at the point of capture.
    """
    signal_names = sorted(signals_config()["signals"].keys())
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["candidates"],
        "properties": {
            "candidates": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "company", "website", "nyc_proof", "signals",
                        "signal_notes", "source_urls",
                    ],
                    "properties": {
                        "company": {"type": "string"},
                        "website": {"type": ["string", "null"]},
                        "linkedin": {"type": ["string", "null"]},
                        "crunchbase": {"type": ["string", "null"]},
                        "x_handle": {"type": ["string", "null"]},
                        "one_liner": {"type": ["string", "null"]},
                        "founders": {"type": ["string", "null"]},
                        "founded": {"type": ["string", "null"]},
                        "hq": {"type": ["string", "null"], "enum": HQ_OPTIONS + [None]},
                        "industries": {"type": ["string", "null"]},
                        "last_funding_date": {"type": ["string", "null"]},
                        "last_funding_usd": {"type": ["number", "null"]},
                        "last_funding_type": {
                            "type": ["string", "null"],
                            "enum": FUNDING_TYPES + [None],
                        },
                        "total_funding_usd": {"type": ["number", "null"]},
                        "num_rounds": {"type": ["integer", "null"]},
                        "investors": {"type": ["string", "null"]},
                        "nyc_proof": {
                            "type": ["string", "null"],
                            "description": "Concrete evidence of NYC presence or intent. Null when there is none — never guess.",
                        },
                        "nyc_headcount_estimate": {
                            "type": ["integer", "null"],
                            "description": "Approximate people based in the NYC metro. Null unless a source states or strongly implies it. This is the single most important field — do not guess it.",
                        },
                        "nyc_open_roles_estimate": {
                            "type": ["integer", "null"],
                            "description": "Approximate open roles located in NYC. Null unless a source states or strongly implies it.",
                        },
                        "nyc_estimate_basis": {
                            "type": ["string", "null"],
                            "description": "Where the two NYC numbers came from. Required if either is non-null.",
                        },
                        "signals": {
                            "type": "array",
                            "items": {"type": "string", "enum": signal_names},
                        },
                        "signal_notes": {"type": "string"},
                        "source_urls": {"type": "array", "items": {"type": "string"}},
                    },
                },
            }
        },
    }


@dataclass
class Candidate:
    company: str
    website: str = ""
    linkedin: str = ""
    crunchbase: str = ""
    x_handle: str = ""
    one_liner: str = ""
    founders: str = ""
    founded: str = ""
    hq: str = ""
    industries: str = ""
    last_funding_date: str = ""
    last_funding_usd: float | None = None
    last_funding_type: str = ""
    total_funding_usd: float | None = None
    num_rounds: int | None = None
    investors: str = ""
    nyc_proof: str = ""
    # The two inputs worth 55 of crm-core's 100 fit points. Research can only
    # estimate them; the LinkedIn and Careers lanes measure them properly and
    # overwrite. Estimates exist to triage, never to be written to Notion.
    nyc_headcount_estimate: int | None = None
    nyc_open_roles_estimate: int | None = None
    nyc_estimate_basis: str = ""
    signals: list[str] = field(default_factory=list)
    signal_notes: str = ""
    source_urls: list[str] = field(default_factory=list)
    lane: str = ""
    signal_strength: int = 0
    score_notes: list[str] = field(default_factory=list)
    predicted_fit: int | None = None
    predicted_fit_reason: str = ""
    predicted_fit_flags: list[str] = field(default_factory=list)
    predicted_fit_components: dict = field(default_factory=dict)

    @classmethod
    def from_model(cls, raw: dict[str, Any], lane: str = "") -> "Candidate":
        def s(key: str) -> str:
            val = raw.get(key)
            return str(val).strip() if val not in (None, "") else ""

        def n(key: str) -> float | None:
            val = raw.get(key)
            if val in (None, ""):
                return None
            try:
                return float(val)
            except (TypeError, ValueError):
                return None

        def i(key: str) -> int | None:
            val = raw.get(key)
            if val in (None, ""):
                return None
            try:
                return int(float(val))
            except (TypeError, ValueError):
                return None

        rounds_int = i("num_rounds")

        return cls(
            company=s("company"),
            website=s("website"),
            linkedin=s("linkedin"),
            crunchbase=s("crunchbase"),
            x_handle=s("x_handle"),
            one_liner=s("one_liner"),
            founders=s("founders"),
            founded=s("founded"),
            hq=s("hq"),
            industries=s("industries"),
            last_funding_date=s("last_funding_date"),
            last_funding_usd=n("last_funding_usd"),
            last_funding_type=s("last_funding_type"),
            total_funding_usd=n("total_funding_usd"),
            num_rounds=rounds_int,
            investors=s("investors"),
            nyc_proof=s("nyc_proof"),
            nyc_headcount_estimate=i("nyc_headcount_estimate"),
            nyc_open_roles_estimate=i("nyc_open_roles_estimate"),
            nyc_estimate_basis=s("nyc_estimate_basis"),
            signals=[str(x) for x in (raw.get("signals") or [])],
            signal_notes=s("signal_notes"),
            source_urls=[str(u) for u in (raw.get("source_urls") or []) if str(u).startswith("http")],
            lane=lane,
        )

    @property
    def key(self) -> str:
        return identity_key(self.company, self.website, self.linkedin)

    @property
    def domain(self) -> str:
        return normalize_domain(self.website)

    def as_dict(self) -> dict[str, Any]:
        return {
            "company": self.company,
            "website": self.website,
            "linkedin": self.linkedin,
            "crunchbase": self.crunchbase,
            "x_handle": self.x_handle,
            "one_liner": self.one_liner,
            "founders": self.founders,
            "founded": self.founded,
            "hq": self.hq,
            "industries": self.industries,
            "last_funding_date": self.last_funding_date,
            "last_funding_usd": self.last_funding_usd,
            "last_funding_type": self.last_funding_type,
            "total_funding_usd": self.total_funding_usd,
            "num_rounds": self.num_rounds,
            "investors": self.investors,
            "nyc_proof": self.nyc_proof,
            "nyc_headcount_estimate": self.nyc_headcount_estimate,
            "nyc_open_roles_estimate": self.nyc_open_roles_estimate,
            "nyc_estimate_basis": self.nyc_estimate_basis,
            "signals": self.signals,
            "signal_notes": self.signal_notes,
            "source_urls": self.source_urls,
            "lane": self.lane,
            "signal_strength": self.signal_strength,
            "score_notes": self.score_notes,
            "predicted_fit": self.predicted_fit,
            "predicted_fit_reason": self.predicted_fit_reason,
            "predicted_fit_flags": self.predicted_fit_flags,
            "predicted_fit_components": self.predicted_fit_components,
            "identity_key": self.key,
        }

    def csv_row(self) -> dict[str, str]:
        """Row for the intake CSV. Blank means unknown — never a zero."""

        def money(val: float | None) -> str:
            return "" if val is None else f"{val:.0f}"

        return {
            "Company": self.company,
            "Website": self.website,
            "Company Linkedin": self.linkedin,
            "Crunchbase": self.crunchbase,
            "Founders": self.founders,
            "One-Liner": self.one_liner,
            "X Handle": x_handle_url(self.x_handle),
            "Founded": self.founded,
            "HQ": self.hq,
            "Industries": self.industries,
            "Last Funding Date": self.last_funding_date,
            "Last Funding Amount (in USD)": money(self.last_funding_usd),
            "Last Funding Type": self.last_funding_type,
            "Total Funding Amount (in USD)": money(self.total_funding_usd),
            "Number of Funding Rounds": "" if self.num_rounds is None else str(self.num_rounds),
            "Top 5 Investors": self.investors,
        }


def score(cand: Candidate) -> Candidate:
    """Compute Signal Strength in place and return the candidate.

    Signal Strength answers only "is this worth handing to intake". It is not
    Fit Score — crm-core's scoring lane owns that and must never see this number.
    """
    cfg = signals_config()
    weights = cfg["signals"]
    penalties = cfg["penalties"]
    research = research_config()
    excluded = {i.lower() for i in research["exclusions"]["industries"]}
    flag_terms = [t.lower() for t in research["exclusions"]["flagOnly"]]

    total = 0
    notes: list[str] = []

    for name in cand.signals:
        spec = weights.get(name)
        if not spec:
            notes.append(f"unknown signal '{name}' ignored")
            continue
        weight = int(spec["weight"])
        # A funding signal only counts at a size that actually implies space.
        if name == "funding_round":
            minimum = spec.get("minAmountUsd")
            if minimum and cand.last_funding_usd is not None and cand.last_funding_usd < minimum:
                notes.append(
                    f"funding_round below ${int(minimum):,} threshold — not counted"
                )
                continue
        total += weight
        notes.append(f"+{weight} {name}")

    if not cand.nyc_proof:
        total += penalties["no_nyc_proof"]
        notes.append(f"{penalties['no_nyc_proof']} no NYC proof")

    industries_low = cand.industries.lower()
    if any(bad in industries_low for bad in excluded):
        total += penalties["excluded_industry"]
        notes.append(f"{penalties['excluded_industry']} excluded industry")

    haystack = f"{cand.industries} {cand.one_liner} {cand.signal_notes}".lower()
    hit = [t for t in flag_terms if t in haystack]
    if hit:
        notes.append(f"flag: {', '.join(hit)} — score agent decides, not us")

    cand.signal_strength = max(0, min(100, total))
    cand.score_notes = notes
    return cand
