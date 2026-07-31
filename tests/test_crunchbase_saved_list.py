"""Approved Crunchbase saved-list contract: strict and read-only."""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from lib.crunchbase_saved_list import (
    ChromeSavedListTransport,
    CrunchbaseSavedListBlocked,
    CrunchbaseSavedListBrowser,
    CrunchbaseSavedListDrift,
    SavedListDefinition,
    browser_snapshot_javascript,
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
    assert config["scheduledMaxPagesPerSource"] == 3
    assert config["sharedWorkItemCeiling"] == 55
    assert config["researchDailyCheckLimit"] == 15
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


def test_live_crunchbase_filter_control_formats_match_contract(
    payload: dict,
) -> None:
    controls = [
        {
            "label": "Last Funding Date",
            "operator": "after",
            "value": "07/01/2026",
        },
        {
            "label": "Last Funding Amount",
            "operator": "greater than or equal to",
            "value": "$5,000,000",
        },
    ]
    result = parse_saved_list_snapshot(
        {**payload, "filters": controls}, SOURCE, OBSERVED_AT
    )
    assert result.result_count == payload["resultCount"]


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"resultType": "People"}, "result_type"),
        ({"newAtTop": False}, "sort"),
    ],
)
def test_global_page_text_cannot_mask_visible_result_or_sort_drift(
    payload: dict,
    changes: dict,
    reason: str,
) -> None:
    with pytest.raises(RuntimeError, match=reason):
        parse_saved_list_snapshot(
            {
                **payload,
                **changes,
                "pageText": (
                    "Companies NEW AT TOP "
                    "Last Funding Date after Jul 1, 2026 "
                    "Last Funding Amount greater than or equal to $5M"
                ),
            },
            SOURCE,
            OBSERVED_AT,
        )


def test_browser_snapshot_reads_result_type_and_sort_from_visible_controls() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required to execute the generated browser snapshot")
    harness = r"""
const fs = require("fs");
const javascript = fs.readFileSync(0, "utf8");
const element = (value, attributes = {}) => ({
  innerText: value,
  textContent: value,
  tagName: attributes.tagName || "DIV",
  value: attributes.value || "",
  className: "",
  getAttribute: name => attributes[name] || null,
  querySelector: () => null,
  querySelectorAll: () => [],
});
const predicate = (label, operator, value) => {
  const field = element(label, {"aria-label": label});
  const input = element("", {tagName: "INPUT", value});
  return {
    querySelector: selector =>
      selector.includes(".mat-mdc-select-min-line") ? element(operator) : field,
    querySelectorAll: () => [input],
  };
};
const predicates = [
  predicate("Last Funding Date", "after", "07/01/2026"),
  predicate("Last Funding Amount", "greater than or equal to", "5,000,000"),
];
global.location = {href: process.argv[1]};
global.document = {
  title: "Main Funding - July 2026 - Crunchbase",
  readyState: "complete",
  body: {
    innerText:
      "Companies NEW AT TOP Last Funding Date after Jul 1, 2026 " +
      "Last Funding Amount greater than or equal to $5M 0 results",
  },
  querySelectorAll: selector => selector === "predicate" ? predicates : [],
  querySelector: selector => {
    if (selector.includes("saved-search-name")) {
      return element("Main Funding - July 2026");
    }
    if (selector.includes("result-type")) {
      return element("People");
    }
    if (selector.includes("sort-order")) {
      return element("OLDEST AT TOP");
    }
    return null;
  },
};
process.stdout.write(eval(javascript));
"""
    completed = subprocess.run(
        [node, "-e", harness, SOURCE.url],
        input=browser_snapshot_javascript(SOURCE),
        text=True,
        capture_output=True,
        check=True,
    )
    snapshot = json.loads(completed.stdout)

    assert snapshot["resultType"] == "People"
    assert snapshot["newAtTop"] is False


def test_browser_snapshot_reads_current_crunchbase_control_markup() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required to execute the generated browser snapshot")
    harness = r"""
const fs = require("fs");
const javascript = fs.readFileSync(0, "utf8");
const element = (value, attributes = {}, querySelector = () => null) => ({
  innerText: value,
  textContent: value,
  tagName: attributes.tagName || "DIV",
  value: attributes.value || "",
  className: attributes.class || "",
  getAttribute: name => attributes[name] || null,
  querySelector,
  querySelectorAll: () => [],
});
const activeCompanies = element("Companies", {
  tagName: "BUTTON",
  class: "visible-item visible-item-active",
});
const switchButton = element("", {
  tagName: "BUTTON",
  role: "switch",
  "aria-checked": "true",
});
const newAtTop = element(
  "New at top",
  {tagName: "MAT-SLIDE-TOGGLE"},
  selector => selector.includes("role='switch'") ? switchButton : null,
);
global.location = {href: process.argv[1]};
global.document = {
  title: "Main Funding - July 2026 - Crunchbase",
  readyState: "complete",
  body: {innerText: "Companies New at top 0 results"},
  querySelectorAll: selector => {
    if (selector === "button.visible-item.visible-item-active") {
      return [activeCompanies];
    }
    if (selector === "mat-slide-toggle") {
      return [newAtTop];
    }
    return [];
  },
  querySelector: selector => {
    if (selector.includes("saved-search-name")) {
      return element("Main Funding - July 2026");
    }
    return null;
  },
};
process.stdout.write(eval(javascript));
"""
    completed = subprocess.run(
        [node, "-e", harness, SOURCE.url],
        input=browser_snapshot_javascript(SOURCE),
        text=True,
        capture_output=True,
        check=True,
    )
    snapshot = json.loads(completed.stdout)

    assert snapshot["resultType"] == "Companies"
    assert snapshot["newAtTop"] is True


class FakeTransport:
    def __init__(self, states: list[dict]) -> None:
        self.states, self.opened, self.reset_urls, self.closed, self.restored = list(states), [], [], [], False
        self.advance_calls = 0
    def open_dedicated_tab(self, url: str) -> str:
        self.opened.append(url); return "window-1:tab-2"
    def evaluate(self, tab_ref: str, javascript: str) -> str:
        return json.dumps(self.states.pop(0))
    def advance_to_next_page(self, tab_ref: str) -> bool:
        self.advance_calls += 1
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


@pytest.mark.parametrize(
    ("blocker", "reason"),
    [
        ({"captchaDetected": True}, "captcha"),
        ({"securityChallengeDetected": True}, "security_challenge"),
    ],
)
def test_browser_blockers_fail_closed_before_parsing(
    payload: dict,
    blocker: dict,
    reason: str,
) -> None:
    transport = FakeTransport([{**payload, **blocker}])
    with pytest.raises(CrunchbaseSavedListBlocked, match=reason):
        CrunchbaseSavedListBrowser(transport=transport).read_source(
            SOURCE,
            observed_at=OBSERVED_AT,
            max_pages=2,
        )
    assert transport.closed == ["window-1:tab-2"]
    assert transport.restored is True


def test_browser_requires_complete_pages_and_stable_pagination(payload: dict) -> None:
    incomplete = {**payload, "resultCount": 51, "rows": [payload["rows"][0]], "gridRowCount": 1, "hasNext": True}
    transport = FakeTransport([incomplete] * 10_000)
    with pytest.raises(CrunchbaseSavedListDrift, match="timed out"):
        CrunchbaseSavedListBrowser(transport=transport, sleeper=lambda _: None, wait_timeout=0.001).read_source(SOURCE, observed_at=OBSERVED_AT, max_pages=2)
    assert transport.restored is True


def test_browser_rejects_thin_empty_saved_list_page(payload: dict) -> None:
    thin = {
        **payload,
        "rows": [],
        "gridRowCount": 0,
        "resultCount": 0,
        "hasNext": False,
    }
    transport = FakeTransport([thin] * 10_000)
    with pytest.raises(CrunchbaseSavedListDrift, match="timed out"):
        CrunchbaseSavedListBrowser(
            transport=transport,
            sleeper=lambda _: None,
            wait_timeout=0.001,
        ).read_source(SOURCE, observed_at=OBSERVED_AT, max_pages=2)
    assert transport.restored is True


def test_browser_returns_complete_snapshot_closes_temporary_tab_and_restores_tab(payload: dict) -> None:
    complete = {**payload, "resultCount": len(payload["rows"]), "gridRowCount": len(payload["rows"]), "hasNext": False}
    transport = FakeTransport([complete])
    result = CrunchbaseSavedListBrowser(transport=transport).read_source(SOURCE, observed_at=OBSERVED_AT, max_pages=2)
    assert [row.company for row in result.observations] == ["Weave", "Plend"]
    assert transport.opened == [SOURCE.url]
    assert transport.closed == ["window-1:tab-2"]
    assert transport.restored is True


def _funding_row(payload: dict, index: int) -> dict:
    return {
        **payload["rows"][0],
        "company": f"Company {index}",
        "crunchbaseUrl": (
            f"https://www.crunchbase.com/organization/company-{index}"
        ),
    }


def test_browser_returns_complete_multi_page_snapshot(payload: dict) -> None:
    first = {
        **payload,
        "resultCount": 51,
        "rows": [_funding_row(payload, index) for index in range(50)],
        "gridRowCount": 50,
        "hasNext": True,
    }
    second = {
        **payload,
        "pageUrl": (
            SOURCE.url
            + "?pageId=2_a_769350d3-c7ec-440d-a1c8-76b4dc0c93cd"
        ),
        "resultCount": 51,
        "rows": [_funding_row(payload, 50)],
        "gridRowCount": 1,
        "hasNext": False,
    }
    transport = FakeTransport([first, second])

    result = CrunchbaseSavedListBrowser(transport=transport).read_source(
        SOURCE,
        observed_at=OBSERVED_AT,
        max_pages=2,
    )

    assert result.page_count == 2
    assert len(result.observations) == 51
    assert result.observations[-1].company == "Company 50"
    assert transport.advance_calls == 1
    assert transport.restored is True


def test_browser_fails_closed_when_promised_next_page_is_unavailable(
    payload: dict,
) -> None:
    first = {
        **payload,
        "resultCount": 51,
        "rows": [_funding_row(payload, index) for index in range(50)],
        "gridRowCount": 50,
        "hasNext": True,
    }
    transport = FakeTransport([first])

    with pytest.raises(CrunchbaseSavedListDrift, match="pagination"):
        CrunchbaseSavedListBrowser(transport=transport).read_source(
            SOURCE,
            observed_at=OBSERVED_AT,
            max_pages=2,
        )

    assert transport.advance_calls == 1
    assert transport.restored is True


def test_browser_rejects_result_count_change_during_pagination(
    payload: dict,
) -> None:
    first = {
        **payload,
        "resultCount": 51,
        "rows": [_funding_row(payload, index) for index in range(50)],
        "gridRowCount": 50,
        "hasNext": True,
    }
    changed = {
        **payload,
        "pageUrl": (
            SOURCE.url
            + "?pageId=2_a_769350d3-c7ec-440d-a1c8-76b4dc0c93cd"
        ),
        "resultCount": 52,
        "rows": [
            _funding_row(payload, 50),
            _funding_row(payload, 51),
        ],
        "gridRowCount": 2,
        "hasNext": False,
    }
    transport = FakeTransport([first, changed])

    with pytest.raises(CrunchbaseSavedListDrift, match="result count changed"):
        CrunchbaseSavedListBrowser(transport=transport).read_source(
            SOURCE,
            observed_at=OBSERVED_AT,
            max_pages=2,
        )

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
    first = {**payload, "resultCount": 51, "rows": [_funding_row(payload, index) for index in range(50)], "gridRowCount": 50, "hasNext": True}
    skipped = {**payload, "pageUrl": SOURCE.url + "?pageId=3_a_769350d3-c7ec-440d-a1c8-76b4dc0c93cd", "resultCount": 51, "rows": [_funding_row(payload, 50)], "gridRowCount": 1, "hasNext": False}
    with pytest.raises(CrunchbaseSavedListDrift, match="page position"):
        CrunchbaseSavedListBrowser(transport=FakeTransport([first, skipped])).read_source(SOURCE, observed_at=OBSERVED_AT, max_pages=2)


def test_browser_rejects_wrong_live_page(payload: dict) -> None:
    wrong = {
        **payload,
        "pageUrl": "https://www.crunchbase.com/organization/weave-f27a",
    }
    transport = FakeTransport([wrong] * 10_000)

    with pytest.raises(CrunchbaseSavedListDrift, match="timed out"):
        CrunchbaseSavedListBrowser(
            transport=transport,
            sleeper=lambda _: None,
            wait_timeout=0.001,
        ).read_source(SOURCE, observed_at=OBSERVED_AT, max_pages=2)

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
    monkeypatch.setattr(chrome, "run_osascript", lambda script, timeout=60: "10|user-tab|10|watcher-tab")
    assert ChromeSavedListTransport().open_dedicated_tab(SOURCE.url) == "window-10:tab-watcher-tab"


def test_chrome_javascript_literal_preserves_unicode_for_applescript() -> None:
    from lib import chrome

    literal = chrome.applescript_string_literal("^[\\$£€][\\d,.]+$")
    assert "£€" in literal
    assert "\\u00a3" not in literal
    assert "\\u20ac" not in literal


def test_browser_snapshot_reads_live_predicate_controls() -> None:
    javascript = browser_snapshot_javascript(SOURCE)
    assert 'document.querySelectorAll("predicate")' in javascript
    assert 'label==="Last Funding Amount"' in javascript


def test_chrome_transport_owns_a_new_temporary_tab_and_closes_it_before_restore(monkeypatch: pytest.MonkeyPatch) -> None:
    from lib import chrome

    calls: list[str] = []
    monkeypatch.setattr(chrome, "ensure_chrome_running", lambda: None)
    responses = iter(["10|user-tab|10|watcher-tab", "closed", "restored"])
    monkeypatch.setattr(chrome, "run_osascript", lambda script, timeout=60: calls.append(script) or next(responses))
    transport = ChromeSavedListTransport()
    tab_ref = transport.open_dedicated_tab(SOURCE.url)
    transport.close_dedicated_tab(tab_ref)
    transport.restore_previous_tab()

    assert "make new tab at end of tabs of front window" in calls[0]
    assert "repeat with candidateWindow" not in calls[0]
    assert 'close tab id "watcher-tab" of window id 10' in calls[1]
    assert 'is "user-tab"' in calls[2]
    assert "repeat with candidateIndex" in calls[2]


def test_chrome_transport_uses_stable_ids_when_tab_insertion_changes_indexes(monkeypatch: pytest.MonkeyPatch) -> None:
    from lib import chrome

    calls: list[str] = []
    responses = iter(["10|user-tab|10|watcher-tab", "closed", "restored"])
    monkeypatch.setattr(chrome, "ensure_chrome_running", lambda: None)
    monkeypatch.setattr(chrome, "run_osascript", lambda script, timeout=60: calls.append(script) or next(responses))
    transport = ChromeSavedListTransport()
    tab_ref = transport.open_dedicated_tab(SOURCE.url)

    # A user opens or closes tabs while the watcher runs, shifting all indexes.
    transport.close_dedicated_tab(tab_ref)
    transport.restore_previous_tab()

    assert 'close tab id "watcher-tab" of window id 10' in calls[1]
    assert 'is "user-tab"' in calls[2]
    assert "repeat with candidateIndex" in calls[2]
    assert "close tab 3" not in calls[1]
    assert "active tab index of window id 10 to 1" not in calls[2]


def test_chrome_transport_does_not_select_an_unrelated_tab_when_prior_tab_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    from lib import chrome

    calls: list[str] = []
    responses = iter(["10|user-tab|10|watcher-tab", "closed", "previous_tab_missing"])
    monkeypatch.setattr(chrome, "ensure_chrome_running", lambda: None)
    monkeypatch.setattr(chrome, "run_osascript", lambda script, timeout=60: calls.append(script) or next(responses))
    transport = ChromeSavedListTransport()
    tab_ref = transport.open_dedicated_tab(SOURCE.url)
    transport.close_dedicated_tab(tab_ref)

    with pytest.raises(RuntimeError, match="previous Chrome tab no longer exists"):
        transport.restore_previous_tab()

    assert 'is "user-tab"' in calls[2]
    assert "repeat with candidateIndex" in calls[2]
    assert "active tab index of window id 10 to 1" not in calls[2]


def test_chrome_transport_does_not_close_a_replacement_when_watcher_tab_was_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    from lib import chrome

    calls: list[str] = []
    responses = iter(["10|user-tab|10|watcher-tab", "owned_tab_missing", "restored"])
    monkeypatch.setattr(chrome, "ensure_chrome_running", lambda: None)
    monkeypatch.setattr(chrome, "run_osascript", lambda script, timeout=60: calls.append(script) or next(responses))
    transport = ChromeSavedListTransport()
    tab_ref = transport.open_dedicated_tab(SOURCE.url)

    # The user closes the watcher tab and Chrome reuses its old numeric index.
    transport.close_dedicated_tab(tab_ref)
    transport.restore_previous_tab()

    assert 'if exists tab id "watcher-tab" of window id 10' in calls[1]
    assert "close tab 3" not in calls[1]
