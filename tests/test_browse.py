"""Browser lane: the guards that matter, without a Mac or a network call."""

import json

import pytest

from lib import chrome, extract, grok
from lib.config import load_config


def fake_page(body: str = "x" * 2000) -> dict:
    return {"title": "Page", "url": "https://example.com/p", "body": body, "truncated": False}


# ------------------------------------------------------------------ chrome

def test_js_strips_comments_before_collapsing():
    """sales-nav §10.10: a `//` comment survived the one-line collapse and
    commented out the rest of the payload, killing every scan for a day."""
    collapsed = chrome._js("const a = 1; // grab the body\nreturn a;")
    assert "//" not in collapsed
    assert "\n" not in collapsed
    assert "return a;" in collapsed


def test_read_payload_is_single_line_and_intact():
    assert "\n" not in chrome._READ
    assert "JSON.stringify" in chrome._READ
    assert chrome._READ.rstrip().endswith(")")


def test_non_https_url_is_refused():
    with pytest.raises(ValueError):
        chrome.open_url("http://crunchbase.com/x")


def test_auth_wall_raises_blocked_not_empty(monkeypatch):
    """A logged-out page must never look like 'no companies today'."""
    monkeypatch.setattr(chrome, "open_url", lambda url: None)
    monkeypatch.setattr(chrome, "read_page",
                        lambda: {"body": "Please log in to continue reading.", "length": 40})
    with pytest.raises(chrome.ChromeBlocked):
        chrome.fetch("https://substack.com/inbox", settle=0, attempts=1)


def test_thin_render_retries_then_raises(monkeypatch):
    calls = {"n": 0}

    def thin():
        calls["n"] += 1
        return {"body": "loading", "length": 7}

    monkeypatch.setattr(chrome, "open_url", lambda url: None)
    monkeypatch.setattr(chrome, "read_page", thin)
    with pytest.raises(chrome.PageNotReady):
        chrome.fetch("https://example.com", settle=0, attempts=3)
    assert calls["n"] == 3, "a thin page must be retried, not accepted"


def test_thin_render_recovers_when_page_settles(monkeypatch):
    bodies = iter([{"body": "", "length": 0}, {"body": "y" * 900, "length": 900}])
    monkeypatch.setattr(chrome, "open_url", lambda url: None)
    monkeypatch.setattr(chrome, "read_page", lambda: next(bodies))
    page = chrome.fetch("https://example.com", settle=0, attempts=3)
    assert len(page["body"]) == 900


def test_missing_osascript_is_unavailable_not_a_crash(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError()

    monkeypatch.setattr(chrome.subprocess, "run", boom)
    with pytest.raises(chrome.ChromeUnavailable):
        chrome._osascript("noop")


def test_live_automation_readiness_accepts_one_javascript_capable_chrome(
    monkeypatch,
):
    monkeypatch.setattr(chrome, "_osascript", lambda script, timeout=30: "ready")

    assert chrome.require_automation_ready() is None


@pytest.mark.parametrize(
    ("probe_result", "reason"),
    [
        ("instance_count:0", "chrome_instance_count:0"),
        ("instance_count:2", "chrome_instance_count:2"),
        ("no_window", "chrome_window_unavailable"),
        ("javascript_error:12", "chrome_javascript_apple_events_unavailable"),
    ],
)
def test_live_automation_readiness_returns_only_bounded_failure_codes(
    monkeypatch,
    probe_result,
    reason,
):
    monkeypatch.setattr(
        chrome,
        "_osascript",
        lambda script, timeout=30: probe_result,
    )

    with pytest.raises(chrome.ChromeUnavailable) as caught:
        chrome.require_automation_ready()

    assert str(caught.value) == reason
    assert "Executing JavaScript" not in str(caught.value)


# ----------------------------------------------------------------- extract

def test_extraction_sends_no_search_tools(monkeypatch):
    """Grok must read the page we handed it, not go looking for more."""
    seen = {}
    monkeypatch.setattr(grok, "call", lambda body: seen.update(body) or {
        "choices": [{"message": {"content": json.dumps({"candidates": []})}}]
    })
    extract.from_page(fake_page(), source="Crunchbase", lane="cb")
    assert seen["tools"] == []


def test_page_text_reaches_the_prompt(monkeypatch):
    seen = {}
    monkeypatch.setattr(grok, "call", lambda body: seen.update(body) or {
        "choices": [{"message": {"content": json.dumps({"candidates": []})}}]
    })
    extract.from_page(fake_page("Ledgerline raised $42M"), source="Crunchbase", lane="cb")
    prompt = seen["messages"][0]["content"]
    assert "Ledgerline raised $42M" in prompt
    assert "Use ONLY what is in the text below" in prompt


def test_candidates_inherit_lane_and_source_url(monkeypatch):
    monkeypatch.setattr(grok, "call", lambda body: {
        "choices": [{"message": {"content": json.dumps({"candidates": [
            {"company": "Ledgerline", "website": "https://ledgerline.com",
             "nyc_evidence": "Flatiron office", "nyc_angle": "strong",
             "keyword_hits": ["Series B"], "signal_notes": "", "source_urls": []}
        ]})}}]
    })
    cands = extract.from_page(fake_page(), source="Crunchbase", lane="cb_nyc_rounds")
    assert cands[0].lane == "cb_nyc_rounds"
    assert cands[0].source_urls == ["https://example.com/p"]
    assert cands[0].mode == "funding"


def test_multi_mode_lets_the_model_pick_the_bucket(monkeypatch):
    monkeypatch.setattr(grok, "call", lambda body: {
        "choices": [{"message": {"content": json.dumps({"candidates": [
            {"company": "Northwind", "website": "https://n.dev", "mode": "office_expansion",
             "nyc_evidence": "outgrowing our SoHo office", "nyc_angle": "strong",
             "keyword_hits": ["outgrowing"], "signal_notes": "", "source_urls": []}
        ]})}}]
    })
    cands = extract.from_page(fake_page(), source="Substack", lane="sub",
                              mode="funding", multi_mode=True)
    assert cands[0].mode == "office_expansion", "a newsletter can carry any signal type"


def test_empty_page_returns_nothing_without_calling_the_api(monkeypatch):
    def fail(body):
        raise AssertionError("should not call Grok on an empty page")

    monkeypatch.setattr(grok, "call", fail)
    assert extract.from_page({"body": "  "}, source="s", lane="l") == []


# ------------------------------------------------------------------ config

def test_browse_sources_are_https_and_well_formed():
    for source in load_config("browse")["sources"]:
        assert source["url"].startswith("https://"), source["id"]
        assert source["mode"] in {
            "funding", "office_expansion", "hiring_growth", "founder_language"
        }
        assert source["limit"] > 0
