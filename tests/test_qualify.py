"""Qualification rules — the two postures, and the evidence discipline.

The asymmetry under test: funding is BROAD (coverage, NYC not required) while
the other three are TIGHT (concrete NYC demand only). A test that lets a vague
signal through a tight mode is the expensive failure — it sends JD to call a
company with no office need.
"""

import pytest

from lib.candidates import Candidate
from lib.qualify import VERDICT_QUALIFY, VERDICT_REJECT, apply, qualify


def funding(**kw) -> Candidate:
    base = {
        "company": "Acme",
        "website": "https://acme.com",
        "mode": "funding",
        "industries": "AI",
        "keyword_hits": ["Series B"],
        "source_urls": ["https://tc.example/acme"],
    }
    base.update(kw)
    return Candidate(**base)


def tight(mode="office_expansion", **kw) -> Candidate:
    base = {
        "company": "Acme",
        "website": "https://acme.com",
        "mode": mode,
        "keyword_hits": ["signed a lease"],
        "nyc_evidence": "Signed a 12,000 sf lease in the Flatiron District",
        "nyc_angle": "strong",
        "source_urls": ["https://re.example/acme"],
    }
    base.update(kw)
    return Candidate(**base)


# ---------------------------------------------------------------- broad mode

def test_funding_qualifies_without_any_nyc_connection():
    """The whole point of broad mode: NYC is reported, never required."""
    cand = funding(nyc_evidence="", nyc_angle="none")
    result = qualify(cand)
    assert result["verdict"] == VERDICT_QUALIFY
    assert result["nyc_angle"] == "none"


def test_funding_qualifies_from_a_watched_account_without_keywords():
    cand = funding(keyword_hits=[], source_handle="@dealwire_")
    assert qualify(cand)["verdict"] == VERDICT_QUALIFY


def test_watched_account_matching_ignores_at_sign_and_case():
    assert qualify(funding(keyword_hits=[], source_handle="ArfurRock"))["verdict"] == VERDICT_QUALIFY
    assert qualify(funding(keyword_hits=[], source_handle="@ArfurRock"))["verdict"] == VERDICT_QUALIFY


def test_funding_rejected_without_keyword_or_watched_account():
    cand = funding(keyword_hits=[], source_handle="@some_random_person")
    result = qualify(cand)
    assert result["verdict"] == VERDICT_REJECT
    assert "watched account" in result["reason"]


def test_funding_rejected_for_irrelevant_sector():
    cand = funding(industries="Restaurant chain", one_liner="A burrito franchise")
    assert qualify(cand)["verdict"] == VERDICT_REJECT


def test_funding_requires_identifiability():
    cand = funding(website="", x_handle="", linkedin="")
    result = qualify(cand)
    assert result["verdict"] == VERDICT_REJECT
    assert "identifiable" in result["reason"]


def test_x_handle_alone_counts_as_identifiable():
    assert qualify(funding(website="", x_handle="@acmehq"))["verdict"] == VERDICT_QUALIFY


# ---------------------------------------------------------------- tight modes

def test_tight_mode_qualifies_on_concrete_nyc_signal():
    assert qualify(tight())["verdict"] == VERDICT_QUALIFY


@pytest.mark.parametrize("angle", ["none", "weak"])
def test_tight_mode_rejects_thin_nyc_angles(angle):
    """A NYC-based investor is not an office need."""
    cand = tight(nyc_angle=angle, nyc_evidence="Backed by a NYC-based fund")
    result = qualify(cand)
    assert result["verdict"] == VERDICT_REJECT
    assert "not concrete enough" in result["reason"]


def test_tight_mode_rejects_when_no_phrase_matched():
    cand = tight(keyword_hits=[])
    result = qualify(cand)
    assert result["verdict"] == VERDICT_REJECT
    assert "no concrete signal phrase" in result["reason"]


def test_hiring_needs_scale_seniority_or_a_stated_build_out():
    thin = tight("hiring_growth", keyword_hits=["hiring in NYC"],
                 nyc_evidence="Two open roles in New York", nyc_angle="moderate")
    assert qualify(thin)["verdict"] == VERDICT_REJECT

    by_volume = tight("hiring_growth", keyword_hits=["hiring in NYC"],
                      nyc_open_roles_estimate=9,
                      nyc_evidence="Nine open NYC roles", nyc_angle="strong")
    assert qualify(by_volume)["verdict"] == VERDICT_QUALIFY

    by_seniority = tight("hiring_growth", keyword_hits=["Head of Workplace"],
                         nyc_evidence="Hired a Head of Workplace in NYC", nyc_angle="strong")
    assert qualify(by_seniority)["verdict"] == VERDICT_QUALIFY

    by_build_out = tight("hiring_growth", keyword_hits=["building our NYC team"],
                         nyc_evidence="Building our NYC team", nyc_angle="strong")
    assert qualify(by_build_out)["verdict"] == VERDICT_QUALIFY


# ---------------------------------------------------- evidence discipline

def test_claimed_angle_is_clamped_when_evidence_is_missing():
    """The model does not get to assert a NYC angle it cannot show."""
    cand = tight(nyc_angle="strong", nyc_evidence="")
    result = qualify(cand)
    assert result["nyc_angle"] == "none"
    assert result["verdict"] == VERDICT_REJECT


def test_garbage_angle_value_falls_back_to_none():
    cand = funding(nyc_angle="extremely strong", nyc_evidence="NYC HQ")
    assert qualify(cand)["nyc_angle"] == "none"


def test_no_source_url_is_always_a_reject():
    assert qualify(funding(source_urls=[]))["verdict"] == VERDICT_REJECT
    assert qualify(tight(source_urls=[]))["verdict"] == VERDICT_REJECT


def test_biotech_is_excluded_in_every_mode():
    assert qualify(funding(industries="Biotech"))["verdict"] == VERDICT_REJECT
    assert qualify(tight(industries="Therapeutics"))["verdict"] == VERDICT_REJECT


def test_unknown_mode_rejects_rather_than_defaulting_open():
    assert qualify(funding(mode="nonsense"))["verdict"] == VERDICT_REJECT


def test_apply_splits_and_annotates():
    good, bad = funding(), funding(source_urls=[])
    qualified, rejected = apply([good, bad])
    assert qualified == [good]
    assert rejected == [bad]
    assert good.fit_hint == good.nyc_angle
    assert bad.qualify_reason
