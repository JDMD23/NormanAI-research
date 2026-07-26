from lib.candidates import Candidate, response_schema, score


def make(**kw) -> Candidate:
    base = {"company": "Acme", "website": "https://acme.com", "nyc_proof": "Opening a SoHo office"}
    base.update(kw)
    return Candidate(**base)


def test_signals_accumulate():
    cand = score(make(signals=["nyc_office_opening", "hiring_surge"]))
    assert cand.signal_strength == 48  # 30 + 18
    assert any("nyc_office_opening" in n for n in cand.score_notes)


def test_missing_nyc_proof_is_heavily_penalised():
    with_proof = score(make(signals=["funding_round"], last_funding_usd=20_000_000))
    without = score(make(signals=["funding_round"], last_funding_usd=20_000_000, nyc_proof=""))
    assert without.signal_strength < with_proof.signal_strength
    assert without.signal_strength == 0  # 25 - 40, clamped


def test_small_funding_round_does_not_count():
    small = score(make(signals=["funding_round"], last_funding_usd=1_000_000))
    big = score(make(signals=["funding_round"], last_funding_usd=20_000_000))
    assert small.signal_strength == 0
    assert big.signal_strength == 25
    assert any("threshold" in n for n in small.score_notes)


def test_funding_with_unknown_amount_still_counts():
    """Unknown ≠ 0: a round we can't size shouldn't be treated as a tiny round."""
    cand = score(make(signals=["funding_round"], last_funding_usd=None))
    assert cand.signal_strength == 25


def test_excluded_industry_is_zeroed():
    cand = score(make(signals=["nyc_office_opening", "lease_signal"], industries="Biotech"))
    assert cand.signal_strength == 0


def test_crypto_is_flagged_not_dropped():
    cand = score(make(signals=["nyc_office_opening"], one_liner="A web3 payments company"))
    assert cand.signal_strength > 0
    assert any("flag:" in n for n in cand.score_notes)


def test_score_is_clamped_to_100():
    cand = score(make(signals=list(response_schema()["properties"]["candidates"]["items"]
                                   ["properties"]["signals"]["items"]["enum"])))
    assert 0 <= cand.signal_strength <= 100


def test_unknown_signal_is_ignored_not_fatal():
    cand = score(make(signals=["nyc_office_opening", "made_up_signal"]))
    assert cand.signal_strength == 30
    assert any("unknown signal" in n for n in cand.score_notes)


def test_response_schema_forbids_extra_fields():
    schema = response_schema()
    item = schema["properties"]["candidates"]["items"]
    assert item["additionalProperties"] is False
    assert "nyc_proof" in item["required"]
    assert "source_urls" in item["required"]


def test_response_schema_allows_null_for_unknowns():
    props = response_schema()["properties"]["candidates"]["items"]["properties"]
    for field in ("website", "last_funding_usd", "num_rounds", "hq"):
        assert "null" in props[field]["type"], f"{field} must be nullable so unknown stays unknown"
