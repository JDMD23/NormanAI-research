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


@pytest.mark.parametrize("filter_text", [
    "Last Funding Date is before Jul 1, 2026 Last Funding Amount is less than $5M",
    "Last Funding Date is not after Jul 1, 2026 Last Funding Amount is not >= $5M",
])
def test_reversed_filter_operators_fail_closed(payload: dict, filter_text: str) -> None:
    with pytest.raises(RuntimeError, match="filters"):
        parse_saved_list_snapshot({**payload, "filterText": filter_text}, SOURCE, OBSERVED_AT)


def test_title_suffix_and_input_backed_filters_are_valid(payload: dict) -> None:
    result = parse_saved_list_snapshot({**payload, "title": "Main Funding - July 2026 (12 new)", "filterText": "Last Funding Date after Last Funding Amount greater than or equal to", "filterInputValues": ["07/01/2026", "5,000,000"]}, SOURCE, OBSERVED_AT)
    assert result.title == SOURCE.name


class FakeTransport:
    def __init__(self, states: list[dict]) -> None:
        self.states, self.opened, self.reset_urls, self.restored = list(states), [], [], False
    def open_dedicated_tab(self, url: str) -> str:
        self.opened.append(url); return "window-1:tab-2"
    def evaluate(self, tab_ref: str, javascript: str) -> str:
        return json.dumps(self.states.pop(0))
    def advance_to_next_page(self, tab_ref: str) -> bool:
        return bool(self.states)
    def reset_to_source(self, tab_ref: str, url: str) -> None:
        self.reset_urls.append(url)
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


def test_browser_returns_complete_snapshot_resets_source_and_restores_tab(payload: dict) -> None:
    complete = {**payload, "resultCount": len(payload["rows"]), "gridRowCount": len(payload["rows"]), "hasNext": False}
    transport = FakeTransport([complete])
    result = CrunchbaseSavedListBrowser(transport=transport).read_source(SOURCE, observed_at=OBSERVED_AT, max_pages=2)
    assert [row.company for row in result.observations] == ["Weave", "Plend"]
    assert transport.opened == [SOURCE.url]
    assert transport.reset_urls == [SOURCE.url]
    assert transport.restored is True


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
