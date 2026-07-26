"""The fit bridge must degrade, not crash, and must never invent inputs."""

import csv

import pytest

from lib import fit_bridge
from lib.candidates import Candidate, response_schema


def test_score_input_leaves_unknowns_blank():
    row = fit_bridge._score_row_input(Candidate(company="Acme"))
    for col in fit_bridge.SCORE_COLUMNS:
        assert col in row
    assert row["LinkedIn NYC Metro Count"] == ""
    assert row["NYC Open Jobs (# roles)"] == ""
    assert row["Total Funding Amount ($M)"] == ""
    assert row["Company"] == "Acme"


def test_usd_is_converted_to_millions_for_the_scorer():
    """crm-core's columns are ($M); ours are (in USD). Getting this wrong
    inflates funding by 1e6 and every candidate scores like a unicorn."""
    row = fit_bridge._score_row_input(
        Candidate(company="Acme", total_funding_usd=42_000_000, last_funding_usd=25_000_000)
    )
    assert row["Total Funding Amount ($M)"] == "42"
    assert row["Last Funding Amount ($M)"] == "25"


def test_velocity_tag_is_never_fabricated():
    """Velocity comes from full round history, which research never has."""
    row = fit_bridge._score_row_input(
        Candidate(company="Acme", last_funding_type="Series B", last_funding_usd=25_000_000)
    )
    assert row["Funding Velocity Tag"] == ""


def test_estimates_flow_into_the_scored_columns():
    row = fit_bridge._score_row_input(
        Candidate(company="Acme", nyc_headcount_estimate=60, nyc_open_roles_estimate=12)
    )
    assert row["LinkedIn NYC Metro Count"] == "60"
    assert row["NYC Open Jobs (# roles)"] == "12"


def test_apply_degrades_cleanly_when_crm_core_is_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(fit_bridge, "crm_core_path", lambda: tmp_path / "nope")
    cands = [Candidate(company="Acme")]
    out, note = fit_bridge.apply(cands)
    assert out[0].predicted_fit is None
    assert "unavailable" in note


def test_predict_raises_rather_than_guessing_when_unavailable(monkeypatch, tmp_path):
    monkeypatch.setattr(fit_bridge, "crm_core_path", lambda: tmp_path / "nope")
    with pytest.raises(fit_bridge.FitBridgeUnavailable):
        fit_bridge.predict([Candidate(company="Acme")])


def test_empty_input_is_not_a_subprocess_call():
    assert fit_bridge.predict([]) == []


def test_schema_asks_for_the_two_highest_weighted_inputs():
    props = response_schema()["properties"]["candidates"]["items"]["properties"]
    for field in ("nyc_headcount_estimate", "nyc_open_roles_estimate", "nyc_estimate_basis"):
        assert field in props, f"{field} must be requested from the model"
        assert "null" in props[field]["type"], f"{field} must be nullable"


def test_score_columns_are_notion_property_names_not_intake_names():
    """The scorer reads ($M) columns; the intake CSV uses (in USD). These are
    two different shapes and merging them silently breaks one of them."""
    from lib.candidates import CSV_COLUMNS

    assert "Last Funding Amount ($M)" in fit_bridge.SCORE_COLUMNS
    assert "Last Funding Amount (in USD)" in CSV_COLUMNS
    assert "Founded Year" in fit_bridge.SCORE_COLUMNS
    assert "Founded" in CSV_COLUMNS


def test_score_csv_round_trips(tmp_path):
    """The temp CSV the bridge writes must be readable with the same header."""
    path = tmp_path / "s.csv"
    cands = [Candidate(company="Acme", nyc_headcount_estimate=60)]
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fit_bridge.SCORE_COLUMNS)
        w.writeheader()
        for c in cands:
            w.writerow(fit_bridge._score_row_input(c))
    rows = list(csv.DictReader(path.open(newline="", encoding="utf-8")))
    assert rows[0]["LinkedIn NYC Metro Count"] == "60"
