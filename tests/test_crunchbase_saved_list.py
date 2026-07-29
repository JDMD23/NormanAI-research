"""Approved Crunchbase saved-list contract: strict and read-only."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from lib.crunchbase_saved_list import (
    ChromeSavedListTransport,
    CrunchbaseSavedListBlocked,
    CrunchbaseSavedListBrowser,
    CrunchbaseSavedListDrift,
    SavedListDefinition,
    load_watcher_config,
    parse_saved_list_snapshot,
    validate_saved_list_url,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "crunchbase_saved_list_snapshot.json"
CONFIG = ROOT / "config" / "funding-watcher.json"
SOURCE = SavedListDefinition(
    name="Main Funding - July 2026",
    url="https://www.crunchbase.com/discover/saved/main-funding-july-2026/730c458b-149c-4a0a-9684-7146e7258993",
    expected_result_type="Companies",
    expected_sort="NEW AT TOP",
    expected_funding_after="2026-07-01",
    expected_minimum_amount=5_000_000,
)
OBSERVED_AT = "2026-07-29T14:00:00+00:00"


@pytest.fixture
def payload() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_approved_config_has_only_the_exact_source_contract() -> None:
    config = load_watcher_config(CONFIG)
    assert config["enabled"] is True
    assert config["scheduleHours"] == [6, 10, 13, 16, 19]
    assert config["dailyPageLoadCeiling"] == 25
    assert config["sourceDefinitions"] == (SOURCE,)


@pytest.mark.parametrize("url", [
    "https://www.crunchbase.com/organization/weave-f27a",
    "https://example.com/discover/saved/list/id",
    SOURCE.url + "?unexpected=1",
    "http://www.crunchbase.com/discover/saved/main-funding-july-2026/730c458b-149c-4a0a-9684-7146e7258993",
])
def test_only_exact_https_saved_list_urls_are_allowed(url: str) -> None:
    with pytest.raises(ValueError, match="exact https"):
        validate_saved_list_url(url)


def test_snapshot_preserves_currency_minor_units_and_canonical_identity(payload: dict) -> None:
    result = parse_saved_list_snapshot(payload, SOURCE, OBSERVED_AT)
    assert [(row.company, row.funding_amount_minor, row.funding_currency) for row in result.observations] == [("Weave", 1_350_000_000, "USD"), ("Plend", 2_000_000_000, "GBP")]
    assert result.observations[0].crunchbase_url == "https://www.crunchbase.com/organization/weave-f27a"
    assert result.result_count == 187
    assert result.top_funding_date == "2026-07-29"


def test_snapshot_parses_eur_without_conversion(payload: dict) -> None:
    row = {**payload["rows"][0], "company": "Euro Co", "crunchbaseUrl": "https://www.crunchbase.com/organization/euro-co", "fundingAmount": "€5M"}
    result = parse_saved_list_snapshot({**payload, "rows": [row]}, SOURCE, OBSERVED_AT)
    assert (result.observations[0].funding_amount_minor, result.observations[0].funding_currency) == (500_000_000, "EUR")


@pytest.mark.parametrize(("field", "value", "reason"), [
    ("crunchbaseUrl", "", "missing_crunchbase_url"),
    ("fundingAmount", "Undisclosed", "invalid_funding_amount"),
    ("fundingDate", "Jul 1, 2026", "funding_date_outside_source_contract"),
    ("fundingDate", "Jul 30, 2026", "future_funding_date"),
    ("fundingAmount", "$4.9M", "funding_amount_outside_source_contract"),
    ("industries", ["Pharmaceuticals"], "excluded_industry:pharmaceuticals"),
])
def test_invalid_rows_are_rejected_without_dropping_other_valid_rows(payload: dict, field: str, value: object, reason: str) -> None:
    bad = {**payload["rows"][0], field: value}
    result = parse_saved_list_snapshot({**payload, "rows": [bad, payload["rows"][1]]}, SOURCE, OBSERVED_AT)
    assert [row.company for row in result.observations] == ["Plend"]
    assert result.rejections == ({"company": "Weave", "reason": reason},)


@pytest.mark.parametrize("filters", [
    [
        {"label": "Last Funding Date", "operator": "before", "value": "Jul 1, 2026"},
        {"label": "Last Funding Amount", "operator": "less than", "value": "$5M"},
    ],
    [
        {"label": "Last Funding Date", "operator": "not after", "value": "Jul 1, 2026"},
        {"label": "Last Funding Amount", "operator": "not >=", "value": "$5M"},
    ],
])
def test_reversed_filter_operators_fail_closed(payload: dict, filters: list[dict]) -> None:
    with pytest.raises(RuntimeError, match="filters"):
        parse_saved_list_snapshot({**payload, "filters": filters}, SOURCE, OBSERVED_AT)


def test_title_suffix_and_labeled_filters_are_valid(payload: dict) -> None:
    result = parse_saved_list_snapshot({**payload, "title": "Main Funding - July 2026 (12 new)", "filterText": "Last Funding Date after Last Funding Amount greater than or equal to", "filterInputValues": ["07/01/2026", "5,000,000"]}, SOURCE, OBSERVED_AT)
    assert result.title == SOURCE.name


def test_unrelated_body_or_input_text_cannot_mask_changed_filter_controls(payload: dict) -> None:
    changed_controls = {
        "filters": [
            {"label": "Last Funding Date", "operator": "after", "value": "Jul 2, 2026"},
            {"label": "Last Funding Amount", "operator": "greater than or equal to", "value": "$6M"},
        ]
    }
    with pytest.raises(RuntimeError, match="filters"):
        parse_saved_list_snapshot(
            {**payload, **changed_controls, "filterText": payload["filterText"], "filterInputValues": ["07/01/2026", "5,000,000"]},
            SOURCE,
            OBSERVED_AT,
        )


def test_filter_values_require_the_matching_visible_filter_label(payload: dict) -> None:
    controls = {
        "filters": [
            {"label": "Founded Date", "operator": "after", "value": "Jul 1, 2026"},
            {"label": "Total Funding", "operator": "greater than or equal to", "value": "$5M"},
        ]
    }
    with pytest.raises(RuntimeError, match="filters"):
        parse_saved_list_snapshot({**payload, **controls}, SOURCE, OBSERVED_AT)


def test_duplicate_filter_controls_fail_closed_even_if_one_value_matches(payload: dict) -> None:
    controls = [
        {"label": "Funding Date", "operator": "after", "value": "Jul 2, 2026"},
        {"label": "Funding Date", "operator": "after", "value": "Jul 1, 2026"},
        {"label": "Last Funding Amount", "operator": "greater than or equal to", "value": "$5M"},
    ]
    with pytest.raises(RuntimeError, match="filters"):
        parse_saved_list_snapshot({**payload, "filters": controls}, SOURCE, OBSERVED_AT)


class FakeTransport:
    def __init__(self, states: list[dict]) -> None:
        self.states, self.opened, self.reset_urls, self.closed, self.restored = list(states), [], [], [], False
    def open_dedicated_tab(self, url: str) -> str:
        self.opened.append(url); return "window-1:tab-2"
    def evaluate(self, tab_ref: str, javascript: str) -> str:
        return json.dumps(self.states.pop(0))
    def advance_to_next_page(self, tab_ref: str) -> bool:
        return bool(self.states)
    def reset_to_source(self, tab_ref: str, url: str) -> None:
        self.reset_urls.append(url)
    def close_dedicated_tab(self, tab_ref: str) -> None:
        self.closed.append(tab_ref)
    def restore_previous_tab(self) -> None:
        self.restored = True


def test_browser_auth_wall_fails_closed_and_restores_active_tab() -> None:
    transport = FakeTransport([{"pageUrl": "https://www.crunchbase.com/login", "pageTitle": "Log in", "pageText": "Log in to Crunchbase"}])
    with pytest.raises(CrunchbaseSavedListBlocked, match="auth_wall"):
        CrunchbaseSavedListBrowser(transport=transport).read_source(SOURCE, observed_at=OBSERVED_AT, max_pages=2)
    assert transport.restored is True


def test_browser_requires_complete_pages_and_stable_pagination(payload: dict) -> None:
    incomplete = {**payload, "resultCount": 51, "rows": [payload["rows"][0]], "gridRowCount": 1, "hasNext": True}
    transport = FakeTransport([incomplete] * 10_000)
    with pytest.raises(CrunchbaseSavedListDrift, match="timed out"):
        CrunchbaseSavedListBrowser(transport=transport, sleeper=lambda _: None, wait_timeout=0.001).read_source(SOURCE, observed_at=OBSERVED_AT, max_pages=2)
    assert transport.restored is True


def test_browser_returns_complete_snapshot_closes_temporary_tab_and_restores_tab(payload: dict) -> None:
    complete = {**payload, "resultCount": len(payload["rows"]), "gridRowCount": len(payload["rows"]), "hasNext": False}
    transport = FakeTransport([complete])
    result = CrunchbaseSavedListBrowser(transport=transport).read_source(SOURCE, observed_at=OBSERVED_AT, max_pages=2)
    assert [row.company for row in result.observations] == ["Weave", "Plend"]
    assert transport.opened == [SOURCE.url]
    assert transport.closed == ["window-1:tab-2"]
    assert transport.restored is True


def test_browser_rejects_page_id_on_the_initial_source_page(payload: dict) -> None:
    stale = {
        **payload,
        "pageUrl": SOURCE.url + "?pageId=2_a_769350d3-c7ec-440d-a1c8-76b4dc0c93cd",
        "resultCount": len(payload["rows"]),
        "gridRowCount": len(payload["rows"]),
        "hasNext": False,
    }
    with pytest.raises(CrunchbaseSavedListDrift, match="page position"):
        CrunchbaseSavedListBrowser(transport=FakeTransport([stale])).read_source(SOURCE, observed_at=OBSERVED_AT, max_pages=2)


def test_browser_rejects_skipped_or_stale_page_id_during_pagination(payload: dict) -> None:
    def row(index: int) -> dict:
        return {**payload["rows"][0], "company": f"Company {index}", "crunchbaseUrl": f"https://www.crunchbase.com/organization/company-{index}"}

    first = {**payload, "resultCount": 51, "rows": [row(index) for index in range(50)], "gridRowCount": 50, "hasNext": True}
    skipped = {**payload, "pageUrl": SOURCE.url + "?pageId=3_a_769350d3-c7ec-440d-a1c8-76b4dc0c93cd", "resultCount": 51, "rows": [row(50)], "gridRowCount": 1, "hasNext": False}
    with pytest.raises(CrunchbaseSavedListDrift, match="page position"):
        CrunchbaseSavedListBrowser(transport=FakeTransport([first, skipped])).read_source(SOURCE, observed_at=OBSERVED_AT, max_pages=2)


def test_browser_refuses_non_saved_list_navigation() -> None:
    source = replace(SOURCE, url="https://www.crunchbase.com/organization/weave-f27a")
    transport = FakeTransport([])
    with pytest.raises(ValueError):
        CrunchbaseSavedListBrowser(transport=transport).read_source(source, observed_at=OBSERVED_AT, max_pages=1)
    assert transport.opened == []


def test_chrome_transport_uses_safe_chrome_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    from lib import chrome
    monkeypatch.setattr(chrome, "ensure_chrome_running", lambda: None)
    monkeypatch.setattr(chrome, "run_osascript", lambda script, timeout=60: "10|1|10|3")
    assert ChromeSavedListTransport().open_dedicated_tab(SOURCE.url) == "window-10:tab-3"


def test_chrome_transport_owns_a_new_temporary_tab_and_closes_it_before_restore(monkeypatch: pytest.MonkeyPatch) -> None:
    from lib import chrome

    calls: list[str] = []
    monkeypatch.setattr(chrome, "ensure_chrome_running", lambda: None)
    monkeypatch.setattr(chrome, "run_osascript", lambda script, timeout=60: calls.append(script) or "10|1|10|3")
    transport = ChromeSavedListTransport()
    tab_ref = transport.open_dedicated_tab(SOURCE.url)
    transport.close_dedicated_tab(tab_ref)
    transport.restore_previous_tab()

    assert "make new tab at end of tabs of front window" in calls[0]
    assert "repeat with candidateWindow" not in calls[0]
    assert "close tab 3 of window id 10" in calls[1]
    assert "set active tab index of window id 10 to 1" in calls[2]
