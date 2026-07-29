"""Pin the handoff to NormanAI-crm-core.

The CSV this repo writes is consumed by crm-core's `scripts/crm_intake.py`,
which matches columns case-insensitively against its own `CSV_ALIASES`. A column
name that falls outside those aliases is not an error over there — the field is
silently dropped and the company lands on JD's board missing data.

`CRM_CORE_CSV_ALIASES` below is copied verbatim from crm_intake.py at
commit 4fc6ef0. If intake's aliases change, update this constant and let the
test tell you what broke.
"""

from pathlib import Path

from lib.candidates import CSV_COLUMNS, Candidate

CRM_CORE_CSV_ALIASES = {
    "company": ("company", "company name", "name", "account", "organization name", "organization"),
    "website": ("website", "url", "domain", "company website"),
    "linkedin": ("company linkedin", "linkedin", "linkedin url", "company linkedin url"),
    "crunchbase": ("crunchbase", "crunchbase url", "cb url", "organization name url", "organization url"),
    "founders": ("founders", "founder", "ceo/founder", "ceo founder"),
    "one_liner": ("one-liner", "one liner", "description", "about"),
    "x_handle": ("x handle", "twitter", "x", "twitter url", "x (twitter)"),
    "founded": ("founded date", "founded", "founded year", "year founded"),
    "hq": ("headquarters location", "hq", "headquarters", "location"),
    "industries": ("industries", "industry", "categories"),
    "last_funding_date": ("last funding date",),
    "last_funding_usd": ("last funding amount (in usd)", "last funding amount", "last funding usd"),
    "last_funding_type": ("last funding type", "funding type", "stage"),
    "total_funding_usd": ("total funding amount (in usd)", "total funding amount", "total funding usd"),
    "num_rounds": ("number of funding rounds", "funding rounds"),
    "investors": ("top 5 investors", "investors", "lead investors"),
}

ALL_ALIASES = {alias for group in CRM_CORE_CSV_ALIASES.values() for alias in group}


def test_every_emitted_column_is_recognised_by_intake():
    unknown = [c for c in CSV_COLUMNS if c.strip().casefold() not in ALL_ALIASES]
    assert not unknown, f"crm_intake.py would silently drop these columns: {unknown}"


def test_every_intake_field_is_populated_by_us():
    """We must emit a column for each field intake knows how to hydrate."""
    emitted = {c.strip().casefold() for c in CSV_COLUMNS}
    missing = [
        field
        for field, aliases in CRM_CORE_CSV_ALIASES.items()
        if not emitted.intersection(aliases)
    ]
    assert not missing, f"no CSV column maps to intake fields: {missing}"


def test_unknown_numbers_are_blank_not_zero():
    """Unknown ≠ 0 is a crm-core rule; it has to hold at the point of emission."""
    row = Candidate(company="Acme").csv_row()
    for col in (
        "Last Funding Amount (in USD)",
        "Total Funding Amount (in USD)",
        "Number of Funding Rounds",
    ):
        assert row[col] == "", f"{col} must be blank when unknown, got {row[col]!r}"


def test_x_handle_is_canonicalised_to_url():
    row = Candidate(company="Acme", x_handle="@acmehq").csv_row()
    assert row["X Handle"] == "https://x.com/acmehq"

    row = Candidate(company="Acme", x_handle="https://twitter.com/AcmeHQ").csv_row()
    assert row["X Handle"] == "https://x.com/acmehq"

    assert Candidate(company="Acme").csv_row()["X Handle"] == ""


def test_csv_row_keys_match_header_exactly():
    assert list(Candidate(company="Acme").csv_row().keys()) == CSV_COLUMNS


def test_research_scripts_have_no_notion_write_authority():
    """Research may query the database, but mutation belongs to CRM Core."""
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    violations = []
    for path in scripts.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        lowered = text.casefold()
        if (
            "notion_client" in lowered
            or "api.notion.com/v1/pages" in lowered
            or 'method="patch"' in lowered
            or "method='patch'" in lowered
        ):
            violations.append(str(path.relative_to(scripts.parent)))
    assert not violations, (
        "Research scripts must hand off to CRM Core instead of writing Notion: "
        f"{violations}"
    )
