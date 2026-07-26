"""Candidate model: what Grok returns, and the intake CSV row.

There is deliberately no scoring here. Research finds and qualifies; crm-core's
score agent scores after intake, with enriched inputs it can actually trust.

The CSV column names below are not cosmetic — they are matched against
`CSV_ALIASES` in NormanAI-crm-core's `scripts/crm_intake.py`. Renaming a column
here silently drops that field on intake. Test coverage pins them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from lib.config import load_config
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

HQ_OPTIONS = [
    "NYC", "SF Bay", "LA", "Boston", "Austin", "Seattle", "Chicago",
    "Remote", "Other US", "International", "Unknown",
]

FUNDING_TYPES = [
    "Pre-Seed", "Seed", "Series A", "Series B", "Series C", "Series D",
    "Series E+", "Growth", "Debt Financing", "Corporate Round", "Grant",
    "Private Equity", "Other", "Unknown",
]


def response_schema() -> dict[str, Any]:
    """JSON schema handed to Grok so candidates come back structured.

    Every fact is nullable rather than defaulted: an unknown must arrive as null
    so it can stay unknown. Unknown ≠ 0 is a crm-core rule and it starts here,
    at the point of capture.
    """
    angles = load_config("modes")["nycAngle"]["values"]
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
                        "company", "website", "nyc_evidence", "nyc_angle",
                        "keyword_hits", "signal_notes", "source_urls",
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
                        "nyc_open_roles_estimate": {
                            "type": ["integer", "null"],
                            "description": "Open roles located in NYC, if a source states or clearly implies a count.",
                        },
                        "nyc_evidence": {
                            "type": ["string", "null"],
                            "description": "Quoted or closely paraphrased evidence of the NYC connection. Null when there is none — never guess.",
                        },
                        "nyc_angle": {
                            "type": "string",
                            "enum": angles,
                            "description": "How strong the NYC angle is, judged only from nyc_evidence.",
                        },
                        "keyword_hits": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "The exact qualifying phrases found in the source. This is the receipt for why the company was included.",
                        },
                        "source_handle": {
                            "type": ["string", "null"],
                            "description": "X handle that surfaced this, when it came from a post.",
                        },
                        "mode": {
                            "type": ["string", "null"],
                            "enum": [
                                "funding", "office_expansion",
                                "hiring_growth", "founder_language", None,
                            ],
                            "description": "Which category this signal belongs to. Leave null unless the source covers several — search lanes set it from the lane itself.",
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
    nyc_open_roles_estimate: int | None = None

    # Why it was included.
    nyc_evidence: str = ""
    nyc_angle: str = "none"
    fit_hint: str = "none"
    keyword_hits: list[str] = field(default_factory=list)
    source_handle: str = ""
    signal_notes: str = ""
    source_urls: list[str] = field(default_factory=list)

    # Bookkeeping.
    mode: str = "funding"
    lane: str = ""
    qualify_reason: str = ""

    @classmethod
    def from_model(cls, raw: dict[str, Any], lane: str = "", mode: str = "funding") -> "Candidate":
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
            val = n(key)
            return None if val is None else int(val)

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
            num_rounds=i("num_rounds"),
            investors=s("investors"),
            nyc_open_roles_estimate=i("nyc_open_roles_estimate"),
            nyc_evidence=s("nyc_evidence"),
            nyc_angle=s("nyc_angle") or "none",
            keyword_hits=[str(k) for k in (raw.get("keyword_hits") or []) if str(k).strip()],
            source_handle=s("source_handle"),
            signal_notes=s("signal_notes"),
            source_urls=[
                str(u) for u in (raw.get("source_urls") or []) if str(u).startswith("http")
            ],
            lane=lane,
            # A multi-topic source (a newsletter) can tell us which bucket a
            # company belongs in; a single-intent search lane cannot be
            # overridden by the model.
            mode=s("mode") or mode,
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
            "nyc_open_roles_estimate": self.nyc_open_roles_estimate,
            "nyc_evidence": self.nyc_evidence,
            "nyc_angle": self.nyc_angle,
            "fit_hint": self.fit_hint,
            "keyword_hits": self.keyword_hits,
            "source_handle": self.source_handle,
            "signal_notes": self.signal_notes,
            "source_urls": self.source_urls,
            "mode": self.mode,
            "lane": self.lane,
            "qualify_reason": self.qualify_reason,
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
