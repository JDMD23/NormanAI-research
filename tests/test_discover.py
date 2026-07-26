"""End-to-end lane behaviour with a stubbed xAI API.

Proves discover → score → CSV works without a key or a network call, and pins
the failure semantics: a lane that breaks reports an outcome, it does not raise.
"""

import json

import pytest

from lib import discover, grok


def fake_response(candidates: list[dict]) -> dict:
    return {
        "choices": [{"message": {"content": json.dumps({"candidates": candidates})}}],
        "citations": ["https://source.example/article"],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50, "num_sources_used": 3},
    }


LANE = {"id": "test_lane", "mode": "funding", "query": "test query",
        "windowDays": 7, "allowedHandles": ["a", "b"]}


def test_lane_returns_scored_candidates(monkeypatch):
    monkeypatch.setattr(grok, "call", lambda body: fake_response([
        {
            "company": "Acme",
            "website": "https://acme.com",
            "nyc_evidence": "Signed a Flatiron lease",
            "nyc_angle": "strong",
            "keyword_hits": ["signed a lease"],
            "signal_notes": "announced 2026-07-20",
            "source_urls": ["https://acme.example/news"],
            "last_funding_usd": 30_000_000,
        }
    ]))

    result = discover.run_lane(LANE, "x")

    assert result["outcome"] == "success"
    assert len(result["candidates"]) == 1
    cand = result["candidates"][0]
    assert cand.company == "Acme"
    assert cand.lane == "test_lane"
    assert cand.mode == "funding"
    assert cand.nyc_angle == "strong"
    assert cand.keyword_hits == ["signed a lease"]
    assert result["usage"]["sources_used"] == 3


def test_candidate_without_own_sources_inherits_lane_citations(monkeypatch):
    monkeypatch.setattr(grok, "call", lambda body: fake_response([
        {"company": "Acme", "website": "https://acme.com", "nyc_evidence": "NYC HQ",
         "nyc_angle": "moderate", "keyword_hits": ["hiring in NYC"],
         "signal_notes": "", "source_urls": []}
    ]))

    result = discover.run_lane(LANE, "x")
    assert result["candidates"][0].source_urls == ["https://source.example/article"]


def test_nameless_candidates_are_dropped(monkeypatch):
    monkeypatch.setattr(grok, "call", lambda body: fake_response([
        {"company": "", "nyc_evidence": "x", "nyc_angle": "weak", "keyword_hits": [], "signal_notes": "", "source_urls": []},
        {"company": "  ", "nyc_evidence": "x", "nyc_angle": "weak", "keyword_hits": [], "signal_notes": "", "source_urls": []},
        {"company": "Real Co", "nyc_evidence": "x", "nyc_angle": "weak", "keyword_hits": [], "signal_notes": "", "source_urls": []},
    ]))

    result = discover.run_lane(LANE, "x")
    assert [c.company for c in result["candidates"]] == ["Real Co"]


def test_deprecated_api_reports_blocked_not_raise(monkeypatch):
    def boom(body):
        raise grok.GrokDeprecatedAPI("410 Gone")

    monkeypatch.setattr(grok, "call", boom)
    result = discover.run_lane(LANE, "x")

    assert result["outcome"] == "blocked"
    assert result["candidates"] == []
    assert "410" in result["note"]


def test_transient_error_reports_retry(monkeypatch):
    def boom(body):
        raise grok.GrokError("timeout")

    monkeypatch.setattr(grok, "call", boom)
    assert discover.run_lane(LANE, "x")["outcome"] == "retry"


def test_unparseable_output_reports_retry(monkeypatch):
    monkeypatch.setattr(grok, "call", lambda body: {
        "choices": [{"message": {"content": "I could not find anything useful."}}]
    })
    result = discover.run_lane(LANE, "x")
    assert result["outcome"] == "retry"
    assert "unparseable" in result["note"]


def test_lane_respects_its_cap(monkeypatch):
    many = [
        {"company": f"Co{i}", "website": f"https://co{i}.com", "nyc_evidence": "NYC",
         "nyc_angle": "moderate", "keyword_hits": ["hiring in NYC"],
         "signal_notes": "", "source_urls": ["https://x.example"]}
        for i in range(50)
    ]
    monkeypatch.setattr(grok, "call", lambda body: fake_response(many))
    assert len(discover.run_lane(LANE, "x", limit=5)["candidates"]) == 5


def test_x_lane_sends_handles_and_web_lane_does_not(monkeypatch):
    seen = {}
    monkeypatch.setattr(grok, "call", lambda body: seen.update(body) or fake_response([]))

    discover.run_lane(LANE, "x")
    x_tools = {t["type"]: t for t in seen["tools"]}
    assert "x_search" in x_tools
    assert x_tools["x_search"]["allowed_x_handles"] == ["a", "b"]
    assert "from_date" in x_tools["x_search"]

    seen.clear()
    discover.run_lane({"id": "w", "mode": "funding", "query": "q", "windowDays": 30}, "web")
    web_tools = {t["type"]: t for t in seen["tools"]}
    assert set(web_tools) == {"web_search"}


def test_handle_list_is_truncated_to_api_cap():
    body = grok.build_request("p", tools=["x_search"], allowed_x_handles=[f"h{i}" for i in range(40)])
    spec = next(t for t in body["tools"] if t["type"] == "x_search")
    assert len(spec["allowed_x_handles"]) == 20


def test_allowed_and_excluded_handles_are_never_both_sent():
    body = grok.build_request(
        "p", tools=["x_search"], allowed_x_handles=["a"], excluded_x_handles=["b"]
    )
    spec = next(t for t in body["tools"] if t["type"] == "x_search")
    assert "allowed_x_handles" in spec
    assert "excluded_x_handles" not in spec


@pytest.mark.parametrize(
    "text,expected",
    [
        ('{"a": 1}', {"a": 1}),
        ('```json\n{"a": 1}\n```', {"a": 1}),
        ('Here you go:\n{"a": 1}\nhope that helps', {"a": 1}),
    ],
)
def test_extract_json_tolerates_wrapping(text, expected):
    assert grok.extract_json(text) == expected


def test_extract_json_raises_on_garbage():
    with pytest.raises(grok.GrokError):
        grok.extract_json("no json here at all")
