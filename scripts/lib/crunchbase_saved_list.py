"""Strict, read-only Crunchbase saved-list reader for the funding watcher.

The saved list is an authenticated browser source, not an API.  Every page is
therefore treated as untrusted until its URL, visible controls, row count, and
pagination evidence match the configured contract.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from lib import chrome


SCHEMA_VERSION = "norman.research.crunchbase_funding_watcher.v1"
SAVED_LIST_PAGE_SIZE = 50
SAVED_LIST_PATH = re.compile(
    r"/discover/saved/[a-z0-9]+(?:-[a-z0-9]+)*/"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)
ORGANIZATION_PATH = re.compile(r"/organization/[a-z0-9]+(?:-[a-z0-9]+)*")
PAGE_ID = re.compile(
    r"\d+_[a-z]_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}", re.I
)
CURRENCIES = {"$": ("USD", 100), "£": ("GBP", 100), "€": ("EUR", 100)}
AMOUNT_SUFFIXES = {"": Decimal(1), "K": Decimal(1_000), "M": Decimal(1_000_000), "B": Decimal(1_000_000_000)}
DISPLAY_DATE_FORMATS = (
    "%Y-%m-%d",
    "%b %d, %Y",
    "%B %d, %Y",
    "%m/%d/%Y",
    "%m/%d/%y",
)
EXCLUDED_INDUSTRY_TERMS = ("biotech", "biotechnology", "therapeutics", "pharma", "pharmaceutical", "drug discovery")


@dataclass(frozen=True)
class SavedListDefinition:
    name: str
    url: str
    expected_result_type: str
    expected_sort: str
    expected_funding_after: str
    expected_minimum_amount: int


@dataclass(frozen=True)
class FundingObservation:
    source_name: str
    source_url: str
    observed_at: str
    company: str
    crunchbase_url: str
    funding_date: str
    funding_type: str
    funding_amount_raw: str
    funding_amount_minor: int
    funding_currency: str
    total_funding_raw: str
    total_funding_amount_minor: int | None
    total_funding_currency: str
    number_of_funding_rounds: int | None
    website: str
    linkedin: str
    headquarters: str
    founded: str
    description: str
    industries: tuple[str, ...]
    founders: tuple[str, ...]
    investors: tuple[str, ...]


@dataclass(frozen=True)
class SavedListSnapshot:
    source: SavedListDefinition
    title: str
    result_type: str
    new_at_top: bool
    filter_text: str
    result_count: int
    page_count: int
    top_funding_date: str | None
    observations: tuple[FundingObservation, ...]
    rejections: tuple[dict[str, str], ...]


class CrunchbaseSavedListBlocked(RuntimeError):
    def __init__(self, reason: str, evidence: str):
        self.reason, self.evidence = reason, evidence
        super().__init__(f"{reason}: {evidence}")


class CrunchbaseSavedListDrift(RuntimeError):
    """The source was not the complete configured saved list."""


def _spaces(value: Any) -> str:
    return " ".join(str(value or "").split())


def validate_saved_list_url(url: str) -> str:
    candidate = (url or "").strip()
    try:
        parsed = urlparse(candidate)
    except ValueError as exc:
        raise ValueError("invalid Crunchbase saved-list URL") from exc
    if (parsed.scheme != "https" or (parsed.hostname or "").casefold() != "www.crunchbase.com"
            or parsed.username or parsed.password or parsed.port or parsed.query or parsed.fragment
            or parsed.params or not SAVED_LIST_PATH.fullmatch(parsed.path)):
        raise ValueError("Crunchbase source must be an exact https://www.crunchbase.com/discover/saved/<slug>/<uuid> URL")
    return candidate


def _positive_int(payload: dict[str, Any], key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{key} must be a positive integer")
    return value


def _source_definition(raw: Any) -> SavedListDefinition:
    if not isinstance(raw, dict):
        raise ValueError("each watcher source must be an object")
    fields = ("name", "url", "expectedResultType", "expectedSort", "expectedFundingAfter")
    for field in fields:
        if not isinstance(raw.get(field), str) or not raw[field].strip():
            raise ValueError(f"watcher source {field} must be a non-empty string")
    try:
        datetime.strptime(raw["expectedFundingAfter"], "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError("watcher source expectedFundingAfter must be YYYY-MM-DD") from exc
    minimum = raw.get("expectedMinimumAmount")
    if not isinstance(minimum, int) or isinstance(minimum, bool) or minimum <= 0:
        raise ValueError("watcher source expectedMinimumAmount must be a positive integer")
    return SavedListDefinition(raw["name"].strip(), validate_saved_list_url(raw["url"]), raw["expectedResultType"].strip(), raw["expectedSort"].strip(), raw["expectedFundingAfter"], minimum)


def load_watcher_config(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("watcher config must be readable JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("watcher config must be a JSON object")
    if payload.get("schemaVersion") != SCHEMA_VERSION:
        raise ValueError(f"schemaVersion must equal {SCHEMA_VERSION}")
    if not isinstance(payload.get("enabled"), bool):
        raise ValueError("enabled must be boolean")
    if payload.get("timeZone") != "America/New_York":
        raise ValueError("timeZone must equal America/New_York")
    hours = payload.get("scheduleHours")
    if (not isinstance(hours, list) or not hours or any(not isinstance(hour, int) or isinstance(hour, bool) or not 0 <= hour <= 23 for hour in hours) or hours != sorted(hours) or len(hours) != len(set(hours))):
        raise ValueError("scheduleHours must be a sorted unique list of integer hours")
    for key in (
        "scheduledMaxPagesPerSource",
        "bootstrapMaxPagesPerSource",
        "bootstrapSeedTop",
        "sharedWorkItemCeiling",
        "researchDailyCheckLimit",
    ):
        _positive_int(payload, key)
    if payload["scheduledMaxPagesPerSource"] > 3:
        raise ValueError("scheduledMaxPagesPerSource must not exceed 3")
    if payload["sharedWorkItemCeiling"] != 55:
        raise ValueError("sharedWorkItemCeiling must equal 55")
    if payload["researchDailyCheckLimit"] != 15:
        raise ValueError("researchDailyCheckLimit must equal 15")
    for key in ("stateDirectory", "legacyStateDirectory", "crmResultSchemaVersion"):
        if not isinstance(payload.get(key), str) or not payload[key].strip():
            raise ValueError(f"{key} must be a non-empty string")
    sources = payload.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ValueError("sources must contain at least one saved list")
    definitions = tuple(_source_definition(source) for source in sources)
    if len({source.url for source in definitions}) != len(definitions):
        raise ValueError("sources must not contain duplicate URLs")
    loaded = dict(payload)
    loaded["sourceDefinitions"] = definitions
    return loaded


def _parse_iso_datetime(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ValueError("observed_at must be an ISO-8601 datetime") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("observed_at must include a timezone")
    return result


def _canonical_crunchbase_url(value: Any) -> str:
    raw = _spaces(value)
    try:
        parsed = urlparse(raw)
    except ValueError:
        return ""
    if (parsed.scheme != "https" or (parsed.hostname or "").casefold() != "www.crunchbase.com"
            or parsed.query or parsed.fragment or parsed.params or not ORGANIZATION_PATH.fullmatch(parsed.path)):
        return ""
    return f"https://www.crunchbase.com{parsed.path.casefold()}"


def _parse_date(value: Any) -> str:
    for fmt in DISPLAY_DATE_FORMATS:
        try:
            return datetime.strptime(_spaces(value), fmt).date().isoformat()
        except ValueError:
            pass
    raise ValueError("invalid_funding_date")


def _parse_amount(value: Any) -> tuple[int, str]:
    raw = _spaces(value)
    match = re.fullmatch(r"(?P<symbol>[\$£€])\s*(?P<number>\d+(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)\s*(?P<suffix>[KMB]?)", raw, flags=re.I)
    if not match:
        raise ValueError("invalid_funding_amount")
    try:
        number = Decimal(match.group("number").replace(",", ""))
        minor = number * AMOUNT_SUFFIXES[match.group("suffix").upper()] * CURRENCIES[match.group("symbol")][1]
    except (InvalidOperation, KeyError) as exc:
        raise ValueError("invalid_funding_amount") from exc
    if number <= 0 or minor != minor.to_integral_value():
        raise ValueError("invalid_funding_amount")
    return int(minor), CURRENCIES[match.group("symbol")][0]


def _string_tuple(value: Any) -> tuple[str, ...]:
    values = value.split(",") if isinstance(value, str) else value if isinstance(value, list) else []
    result: list[str] = []
    seen: set[str] = set()
    for candidate in values:
        text = _spaces(candidate)
        if text and text.casefold() not in seen:
            seen.add(text.casefold())
            result.append(text)
    return tuple(result)


def _filter_contract_matches(filters: Any, source: SavedListDefinition) -> bool:
    """Match configured values only against their own visible filter control."""
    if not isinstance(filters, list):
        return False
    date_labels = {"funding date", "last funding date"}
    amount_labels = {"funding amount", "last funding amount"}
    date_controls: list[dict[str, Any]] = []
    amount_controls: list[dict[str, Any]] = []
    for control in filters:
        if not isinstance(control, dict):
            return False
        label = _spaces(control.get("label")).casefold()
        if label in date_labels:
            date_controls.append(control)
        elif label in amount_labels:
            amount_controls.append(control)
    if len(date_controls) != 1 or len(amount_controls) != 1:
        return False
    date = date_controls[0]
    if _spaces(date.get("operator")).casefold() != "after":
        return False
    try:
        date_matched = _parse_date(_spaces(date.get("value"))) == source.expected_funding_after
    except ValueError:
        return False
    amount = amount_controls[0]
    if _spaces(amount.get("operator")).casefold() not in {"greater than or equal to", "at least", ">="}:
        return False
    try:
        amount_minor, currency = _parse_amount(_spaces(amount.get("value")))
    except ValueError:
        return False
    return date_matched and currency == "USD" and amount_minor == source.expected_minimum_amount * 100


def _source_title(value: Any) -> str:
    return re.sub(r"\s*\(\d+\s+new\)\s*$", "", _spaces(value), flags=re.I).strip()


def _is_source_page_url(actual_url: Any, source_url: str) -> bool:
    actual = str(actual_url or "")
    if actual == source_url:
        return True
    try:
        actual_parts, source_parts = urlparse(actual), urlparse(source_url)
        query = parse_qs(actual_parts.query, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        return False
    return (actual_parts.scheme == source_parts.scheme and actual_parts.netloc == source_parts.netloc and actual_parts.path == source_parts.path and not actual_parts.params and not actual_parts.fragment and set(query) == {"pageId"} and len(query["pageId"]) == 1 and bool(PAGE_ID.fullmatch(query["pageId"][0])))


def _is_expected_source_page_url(actual_url: Any, source_url: str, page_number: int) -> bool:
    """Verify page zero is the base URL and later pages progress sequentially."""
    actual = str(actual_url or "")
    if page_number == 0:
        return actual == source_url
    try:
        actual_parts, source_parts = urlparse(actual), urlparse(source_url)
        query = parse_qs(actual_parts.query, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        return False
    page_id = query.get("pageId", [])
    return (actual_parts.scheme == source_parts.scheme and actual_parts.netloc == source_parts.netloc and actual_parts.path == source_parts.path and not actual_parts.params and not actual_parts.fragment and set(query) == {"pageId"} and len(page_id) == 1 and bool(PAGE_ID.fullmatch(page_id[0])) and page_id[0].split("_", 1)[0] == str(page_number + 1))


def _drift_reasons(payload: dict[str, Any], source: SavedListDefinition) -> list[str]:
    reasons = []
    if not _is_source_page_url(payload.get("pageUrl"), source.url): reasons.append("source_url")
    if _source_title(payload.get("title")) != source.name: reasons.append("title")
    if _spaces(payload.get("resultType")) != source.expected_result_type: reasons.append("result_type")
    if source.expected_sort.casefold() == "new at top" and not payload.get("newAtTop"): reasons.append("sort")
    if not _filter_contract_matches(payload.get("filters"), source): reasons.append("filters")
    return reasons


def _excluded_industry(industries: tuple[str, ...]) -> str | None:
    for industry in industries:
        lowered = industry.casefold()
        if any(term in lowered for term in EXCLUDED_INDUSTRY_TERMS):
            return lowered
    return None


def _parse_row(row: Any, source: SavedListDefinition, observed_at: str) -> FundingObservation:
    if not isinstance(row, dict): raise ValueError("invalid_row")
    company = _spaces(row.get("company"))
    if not company: raise ValueError("missing_company")
    raw_url = _spaces(row.get("crunchbaseUrl")); crunchbase_url = _canonical_crunchbase_url(raw_url)
    if not crunchbase_url: raise ValueError("missing_crunchbase_url" if not raw_url else "invalid_crunchbase_url")
    funding_date = _parse_date(row.get("fundingDate"))
    if funding_date <= source.expected_funding_after: raise ValueError("funding_date_outside_source_contract")
    if funding_date > _parse_iso_datetime(observed_at).date().isoformat(): raise ValueError("future_funding_date")
    funding_type = _spaces(row.get("fundingType"))
    if not funding_type: raise ValueError("missing_funding_type")
    amount_minor, currency = _parse_amount(row.get("fundingAmount"))
    if amount_minor < source.expected_minimum_amount * 100: raise ValueError("funding_amount_outside_source_contract")
    industries = _string_tuple(row.get("industries")); excluded = _excluded_industry(industries)
    if excluded: raise ValueError(f"excluded_industry:{excluded}")
    total_raw = _spaces(row.get("totalFunding")); total_minor: int | None = None; total_currency = ""
    if total_raw:
        try: total_minor, total_currency = _parse_amount(total_raw)
        except ValueError: pass
    raw_rounds = row.get("numberOfFundingRounds")
    rounds = raw_rounds if isinstance(raw_rounds, int) and not isinstance(raw_rounds, bool) and raw_rounds >= 0 else int(raw_rounds.strip()) if isinstance(raw_rounds, str) and raw_rounds.strip().isdigit() else None
    return FundingObservation(source.name, source.url, observed_at, company, crunchbase_url, funding_date, funding_type, _spaces(row.get("fundingAmount")), amount_minor, currency, total_raw, total_minor, total_currency, rounds, _spaces(row.get("website")), _spaces(row.get("linkedin")), _spaces(row.get("headquarters")), _spaces(row.get("founded")), _spaces(row.get("description")), industries, _string_tuple(row.get("founders")), _string_tuple(row.get("investors")))


def parse_saved_list_snapshot(payload: Any, source: SavedListDefinition, observed_at: str) -> SavedListSnapshot:
    _parse_iso_datetime(observed_at)
    if not isinstance(payload, dict): raise RuntimeError("saved-list contract drift: payload")
    drift = _drift_reasons(payload, source)
    if drift: raise RuntimeError("saved-list contract drift: " + ", ".join(sorted(drift)))
    result_count, page_count, rows = payload.get("resultCount"), payload.get("pageCount", 1), payload.get("rows")
    if not isinstance(result_count, int) or isinstance(result_count, bool) or result_count < 0: raise RuntimeError("saved-list contract drift: result_count")
    if not isinstance(page_count, int) or isinstance(page_count, bool) or page_count <= 0: raise RuntimeError("saved-list contract drift: page_count")
    if not isinstance(rows, list): raise RuntimeError("saved-list contract drift: rows")
    observations: list[FundingObservation] = []; rejections: list[dict[str, str]] = []; identities: set[tuple[str, str, str, int, str]] = set()
    for row in rows:
        company = _spaces(row.get("company")) if isinstance(row, dict) else ""
        try: observation = _parse_row(row, source, observed_at)
        except ValueError as exc:
            rejections.append({"company": company, "reason": str(exc)}); continue
        identity = (observation.crunchbase_url, observation.funding_date, " ".join(observation.funding_type.casefold().split()), observation.funding_amount_minor, observation.funding_currency)
        if identity not in identities:
            identities.add(identity); observations.append(observation)
    return SavedListSnapshot(source, _source_title(payload.get("title")), _spaces(payload.get("resultType")), bool(payload.get("newAtTop")), _spaces(payload.get("filterText")), result_count, page_count, observations[0].funding_date if observations else None, tuple(observations), tuple(rejections))


def browser_snapshot_javascript(source: SavedListDefinition) -> str:
    validate_saved_list_url(source.url)
    return r'''(() => {
const text=e=>((e&&(e.innerText||e.textContent))||"").trim(); const bodyText=document.body?.innerText||"";
const cells=row=>Array.from(row.querySelectorAll("grid-cell,[role='gridcell'],mat-cell")).map(cell=>({text:text(cell),key:[cell.getAttribute("data-column-id")||"",cell.getAttribute("data-field")||"",cell.getAttribute("aria-label")||"",...Array.from(cell.querySelectorAll("a")).map(a=>a.href||"")].join(" ").toLowerCase(),links:Array.from(cell.querySelectorAll("a")).map(a=>({text:text(a),href:a.href||""}))}));
const rowElements=Array.from(document.querySelectorAll(".results-container grid-row,[role='row'],mat-row"));
const rows=rowElements.map(row=>{const all=cells(row), org=row.querySelector("a[href*='/organization/']"); if(!org)return null; const find=p=>all.find(c=>p.test(c.key)); const date=find(/last_funding_at|last funding date/)||all.find(c=>/^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) \d{1,2}, \d{4}$/.test(c.text)); const type=find(/last_funding_type|last funding type/); const money=all.filter(c=>/^[\$£€][\d,.]+(?:[KMB])?$/i.test(c.text)); const amount=find(/last_funding_total|last funding amount/)||money.find(c=>all.indexOf(c)>all.indexOf(date)); const total=all.find(c=>/funding_total|total funding/.test(c.key)&&!/last_funding_total|last funding amount/.test(c.key))||money.find(c=>c!==amount); const links=all.flatMap(c=>c.links); return {company:text(org),crunchbaseUrl:org.href,website:links.find(a=>/^https?:\/\//.test(a.href)&&!a.href.includes("crunchbase.com")&&!a.href.includes("linkedin.com"))?.href||"",linkedin:links.find(a=>a.href.includes("linkedin.com"))?.href||"",headquarters:find(/location_identifiers|headquarters/)?.text||all[4]?.text||"",founded:find(/founded_on|founded/)?.text||all[9]?.text||"",description:find(/short_description|description/)?.text||all[5]?.text||"",industries:links.filter(a=>a.href.includes("/categories/")).map(a=>a.text),founders:links.filter(a=>a.href.includes("/person/")).map(a=>a.text),investors:(find(/investor_identifiers|top investors/)?.links||all[18]?.links||[]).filter(a=>a.href.includes("/organization/")).map(a=>a.text),fundingDate:date?.text||"",fundingType:type?.text||"",fundingAmount:amount?.text||"",totalFunding:total?.text||"",numberOfFundingRounds:Number(find(/num_funding_rounds|number of funding rounds/)?.text||all[14]?.text||"")||null}; }).filter(Boolean);
const livePredicates=Array.from(document.querySelectorAll("predicate")); const filterContainers=livePredicates.length?livePredicates:Array.from(document.querySelectorAll("[data-test*='filter' i],[data-testid*='filter' i],.filter-item,.filter-group"));
const filters=filterContainers.map(container=>{const field=container.querySelector(".search-field,[aria-label='Last Funding Date'],[aria-label='Last Funding Amount'],label,[data-test*='label' i],[data-testid*='label' i],.filter-label"); const label=field?.getAttribute("aria-label")||text(field); const controls=Array.from(container.querySelectorAll("input,button,[role='combobox'],mat-select")); const input=controls.find(control=>control.tagName==="INPUT"); const operator=text(container.querySelector(".mat-mdc-select-min-line"))||controls.map(control=>text(control)||control.getAttribute("aria-label")||"").find(value=>/after|before|greater than|at least|>=|</i.test(value))||""; let value=input?.value||""; if(label==="Last Funding Amount"&&/^[\d,.]+$/.test(value))value="$"+value; return {label,operator,value};}).filter(filter=>filter.label);
const resultTypeControl=document.querySelector("[data-test='result-type'],[data-testid='result-type'],[aria-label='Result type'],[aria-label='Search type'],[role='tablist'][aria-label*='result' i] [role='tab'][aria-selected='true']");
const sortControl=document.querySelector("[data-test='sort-order'],[data-testid='sort-order'],[aria-label='Sort order'],[aria-label='Sort'],[data-test*='sort' i] [aria-selected='true'],[data-testid*='sort' i] [aria-selected='true']");
const controlText=control=>(text(control)||control?.getAttribute("aria-label")||"").replace(/\s+/g," ").trim();
const next=document.querySelector(".page-button-next"); return JSON.stringify({pageUrl:location.href,pageTitle:document.title,readyState:document.readyState,gridRowCount:rowElements.length,pageText:bodyText.slice(0,12000),captchaDetected:Boolean(document.querySelector('#px-captcha,iframe[src*="captcha" i],iframe[src*="recaptcha" i],iframe[src*="hcaptcha" i],[class*="captcha" i],[id*="captcha" i],[data-sitekey]')),securityChallengeDetected:Boolean(document.querySelector('#challenge-form,#cf-challenge-running,[id^="cf-chl-"],[class*="cf-chl-"],script[src*="/cdn-cgi/challenge-platform/"],script[src*="perimeterx"]')),title:text(document.querySelector("[data-test='saved-search-name'],[data-testid='saved-search-name'],h1")),resultType:controlText(resultTypeControl),newAtTop:controlText(sortControl).toUpperCase()==="NEW AT TOP",filters,resultCount:Number(bodyText.match(/(?:of\s+)?([\d,]+)\s+results/i)?.[1]?.replace(/,/g,"")||0),hasNext:Boolean(next&&!next.className.includes("disabled")&&!next.className.includes("no-events")),rows}); })()'''


def _tab_ref(value: str) -> tuple[int, str]:
    match = re.fullmatch(r"window-(\d+):tab-([A-Za-z0-9_-]+)", value or "")
    if not match: raise ValueError(f"invalid Chrome tab reference: {value}")
    return int(match.group(1)), match.group(2)


class ChromeSavedListTransport:
    def __init__(self, *, timeout_seconds: int = 60, sleeper: Callable[[float], None] = time.sleep):
        self.timeout_seconds, self.sleeper = timeout_seconds, sleeper
        self._previous_tab: tuple[int, str] | None = None
        self._owned_tab: tuple[int, str, str] | None = None
    def _osascript(self, script: str) -> str: return chrome.run_osascript(script, timeout=self.timeout_seconds)
    def open_dedicated_tab(self, url: str) -> str:
        source_url = validate_saved_list_url(url); chrome.ensure_chrome_running(); literal = chrome.applescript_string_literal(source_url)
        script = 'tell application "Google Chrome"\n' + f' set sourceUrl to {literal}\n set previousWindowId to id of front window\n set previousTabId to id of active tab of front window\n set targetWindowId to id of front window\n make new tab at end of tabs of front window\n set targetTabIndex to count of tabs of front window\n set URL of tab targetTabIndex of window id targetWindowId to sourceUrl\n set active tab index of window id targetWindowId to targetTabIndex\n set index of window id targetWindowId to 1\n set targetTabId to id of tab targetTabIndex of window id targetWindowId\n return (previousWindowId as text) & "|" & (previousTabId as text) & "|" & (targetWindowId as text) & "|" & (targetTabId as text)\nend tell'
        parts = self._osascript(script).split("|")
        if len(parts) != 4 or not parts[0].isdigit() or not parts[2].isdigit() or not parts[1] or not parts[3]: raise RuntimeError("Chrome returned an invalid saved-list tab reference")
        previous_window, previous_tab, target_window, target_tab = int(parts[0]), parts[1], int(parts[2]), parts[3]; self._previous_tab = (previous_window, previous_tab); self._owned_tab = (target_window, target_tab, source_url)
        return f"window-{target_window}:tab-{target_tab}"
    def evaluate(self, tab_ref: str, javascript: str) -> str:
        window_id, tab_id = _tab_ref(tab_ref); literal = chrome.applescript_string_literal(javascript); tab_literal = chrome.applescript_string_literal(tab_id)
        return self._osascript(f'tell application "Google Chrome"\n tell tab id {tab_literal} of window id {window_id}\n  set jsResult to execute javascript {literal}\n end tell\nend tell\nreturn jsResult')
    def advance_to_next_page(self, tab_ref: str) -> bool:
        raw = self.evaluate(tab_ref, 'JSON.stringify({currentUrl:location.href,nextUrl:(document.querySelector(".page-button-next")?.href||""),first:document.querySelector(".results-container grid-row a[href*=\'/organization/\']")?.href||""})')
        try: state = json.loads(raw)
        except json.JSONDecodeError as exc: raise RuntimeError("Chrome returned invalid pagination state") from exc
        next_url = str(state.get("nextUrl") or "")
        if not next_url: return False
        source_url = urlparse(str(state.get("currentUrl") or ""))._replace(query="", fragment="").geturl(); validate_saved_list_url(source_url)
        if not _is_source_page_url(next_url, source_url): raise RuntimeError("Crunchbase pagination returned a non-allowlisted URL")
        self.evaluate(tab_ref, f"window.location.href = {json.dumps(next_url)}; 'ok';")
        deadline = time.monotonic() + min(self.timeout_seconds, 30)
        while time.monotonic() < deadline:
            self.sleeper(.5)
            try: current = json.loads(self.evaluate(tab_ref, 'JSON.stringify({url:location.href,first:document.querySelector(".results-container grid-row a[href*=\'/organization/\']")?.href||"",rows:document.querySelectorAll(".results-container grid-row").length})'))
            except json.JSONDecodeError: continue
            if current.get("rows") and current.get("url") == next_url and current.get("first") != state.get("first"): return True
        raise RuntimeError("Crunchbase pagination did not finish loading")
    def reset_to_source(self, tab_ref: str, url: str) -> None:
        source_url = validate_saved_list_url(url); self.evaluate(tab_ref, f"if (window.location.href !== {json.dumps(source_url)}) {{ window.location.href = {json.dumps(source_url)}; }} 'ok';")
    def close_dedicated_tab(self, tab_ref: str) -> None:
        window_id, tab_id = _tab_ref(tab_ref)
        if self._owned_tab is None or self._owned_tab[:2] != (window_id, tab_id):
            raise RuntimeError("refusing to close a Chrome tab not owned by the saved-list reader")
        tab_literal = chrome.applescript_string_literal(tab_id)
        try:
            self._osascript(f'tell application "Google Chrome"\n if exists tab id {tab_literal} of window id {window_id} then\n  close tab id {tab_literal} of window id {window_id}\n  return "closed"\n end if\nend tell\nreturn "owned_tab_missing"')
        finally:
            self._owned_tab = None
    def restore_previous_tab(self) -> None:
        if self._previous_tab is None: return
        window_id, tab_id = self._previous_tab; tab_literal = chrome.applescript_string_literal(tab_id)
        try:
            result = self._osascript(f'tell application "Google Chrome"\n set tabCount to count of tabs of window id {window_id}\n repeat with candidateIndex from 1 to tabCount\n  if ((id of tab candidateIndex of window id {window_id}) as text) is {tab_literal} then\n   set active tab index of window id {window_id} to candidateIndex\n   set index of window id {window_id} to 1\n   return "restored"\n  end if\n end repeat\nend tell\nreturn "previous_tab_missing"')
            if result != "restored":
                raise RuntimeError("previous Chrome tab no longer exists")
        finally:
            self._previous_tab = None


def _browser_block(payload: dict[str, Any]) -> tuple[str, str] | None:
    url, title, text = str(payload.get("pageUrl") or ""), _spaces(payload.get("pageTitle")), _spaces(payload.get("pageText")); evidence = _spaces(f"{url} {title} {text}")[:300]; lowered = evidence.casefold()
    if payload.get("captchaDetected"): return "captcha", evidence
    if payload.get("securityChallengeDetected") or any(marker in lowered for marker in ("checking your browser", "security challenge", "verify you are human", "access denied")): return "security_challenge", evidence
    if re.search(r"/(?:login|signin)(?:/|$)", urlparse(url).path.casefold()) or "log in to crunchbase" in lowered or "sign in to crunchbase" in lowered: return "auth_wall", evidence
    return None


class CrunchbaseSavedListBrowser:
    def __init__(self, *, transport: Any | None = None, sleeper: Callable[[float], None] = time.sleep, wait_timeout: float = 60):
        if wait_timeout <= 0: raise ValueError("wait_timeout must be positive")
        self.transport, self.sleeper, self.wait_timeout = transport or ChromeSavedListTransport(), sleeper, wait_timeout
    def read_source(self, source: SavedListDefinition, *, observed_at: str, max_pages: int) -> SavedListSnapshot:
        validate_saved_list_url(source.url)
        if not isinstance(max_pages, int) or isinstance(max_pages, bool) or not 1 <= max_pages <= 20: raise ValueError("max_pages must be between 1 and 20")
        tab_ref: str | None = None; snapshots: list[SavedListSnapshot] = []; result_count: int | None = None; failed = True
        try:
            tab_ref = self.transport.open_dedicated_tab(source.url); javascript = browser_snapshot_javascript(source)
            for page_number in range(max_pages):
                deadline = time.monotonic() + self.wait_timeout
                while True:
                    try: payload = json.loads(self.transport.evaluate(tab_ref, javascript))
                    except (TypeError, json.JSONDecodeError) as exc: raise CrunchbaseSavedListDrift("saved-list browser returned invalid JSON") from exc
                    if not isinstance(payload, dict): raise CrunchbaseSavedListDrift("saved-list browser returned a non-object snapshot")
                    blocked = _browser_block(payload)
                    if blocked: raise CrunchbaseSavedListBlocked(*blocked)
                    rows, current_count, grid_count = payload.get("rows"), payload.get("resultCount"), payload.get("gridRowCount")
                    page_url = payload.get("pageUrl")
                    if _is_source_page_url(page_url, source.url) and not _is_expected_source_page_url(page_url, source.url, page_number):
                        raise CrunchbaseSavedListDrift("saved-list page position drift")
                    ready = _is_expected_source_page_url(page_url, source.url, page_number) and payload.get("readyState") in (None, "interactive", "complete") and isinstance(rows, list) and len(rows) > 0 and isinstance(payload.get("filters"), list) and len(payload["filters"]) >= 2 and (grid_count is None or isinstance(grid_count, int) and grid_count > 0) and isinstance(current_count, int) and not isinstance(current_count, bool) and current_count > 0
                    if ready and result_count is None: result_count = current_count
                    elif ready and current_count != result_count: raise CrunchbaseSavedListDrift("saved-list result count changed during pagination")
                    expected = min(SAVED_LIST_PAGE_SIZE, max(int(result_count or 0) - page_number * SAVED_LIST_PAGE_SIZE, 0)); has_more = int(result_count or 0) > (page_number + 1) * SAVED_LIST_PAGE_SIZE
                    ready = ready and len(rows) == expected and (grid_count is None or grid_count >= expected) and bool(payload.get("hasNext")) == has_more
                    if ready: break
                    if time.monotonic() >= deadline: raise CrunchbaseSavedListDrift("saved-list navigation or results grid timed out")
                    self.sleeper(.5)
                try: snapshots.append(parse_saved_list_snapshot(payload, source, observed_at))
                except RuntimeError as exc: raise CrunchbaseSavedListDrift(str(exc)) from exc
                if not payload.get("hasNext") or page_number + 1 >= max_pages: break
                if not self.transport.advance_to_next_page(tab_ref):
                    raise CrunchbaseSavedListDrift(
                        "saved-list promised a next page but pagination was unavailable"
                    )
            failed = False
        finally:
            if tab_ref is not None:
                cleanup: Exception | None = None
                try:
                    close = getattr(self.transport, "close_dedicated_tab", None)
                    if callable(close):
                        close(tab_ref)
                    else:
                        reset = getattr(self.transport, "reset_to_source", None)
                        if callable(reset): reset(tab_ref, source.url)
                except Exception as exc: cleanup = exc
                try: self.transport.restore_previous_tab()
                except Exception as exc:
                    if cleanup is None: cleanup = exc
                if cleanup is not None and not failed: raise cleanup
        if not snapshots: raise CrunchbaseSavedListDrift("saved-list returned no snapshots")
        observations: list[FundingObservation] = []; rejections: list[dict[str, str]] = []; seen: set[tuple[str, str, str, int, str]] = set()
        for snapshot in snapshots:
            rejections.extend(snapshot.rejections)
            for row in snapshot.observations:
                identity = (row.crunchbase_url, row.funding_date, " ".join(row.funding_type.casefold().split()), row.funding_amount_minor, row.funding_currency)
                if identity not in seen: seen.add(identity); observations.append(row)
        first = snapshots[0]
        return SavedListSnapshot(source, first.title, first.result_type, first.new_at_top, first.filter_text, first.result_count, len(snapshots), observations[0].funding_date if observations else None, tuple(observations), tuple(rejections))
