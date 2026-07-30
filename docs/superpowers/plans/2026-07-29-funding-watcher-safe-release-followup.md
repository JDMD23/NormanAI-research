# Funding Watcher Safe-Release Follow-up Implementation Plan

> Historical v1/25-page plan. Superseded by the implemented v3 30/10/40
> contract in `docs/scheduling.md`.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Research's existing v1 funding watcher complete within its
fetched window, bootstrap-safe, rejection-visible, observable, and
cross-repository-CI enforced without changing production contracts or state.

**Architecture:** Keep Research as the saved-list detector and Core as the only
Notion writer. Correct the detector's trust gate, add local operational state
around the existing receipt boundary, and make the existing behavior-based
Core acceptance suite mandatory in CI.

**Tech Stack:** CPython 3.11, pytest, JSON receipts, `fcntl.flock`, atomic
temp-file publication, GitHub Actions.

## Global Constraints

- Preserve `norman.research.funding_handoff.v1` and
  `norman.crm_core.funding_handoff_result.v1`.
- Preserve one `norman.shared.crunchbase_budget.v1` ledger with ceiling `25`.
- Preserve schedule hours `[6, 10, 13, 16, 19]` in `America/New_York`.
- Research may reserve exactly two pages per scheduled slot.
- Research may not import or call a Notion writer.
- Core remains the only Notion mutation owner.
- Tests use isolated temporary state and make no live browser or Notion calls.
- Do not install or activate a LaunchAgent.

---

### Task 1: Correct high-water candidate selection

**Files:**
- Modify: `tests/test_funding_watcher.py:659-725`
- Modify: `scripts/funding_watcher.py:181-214`

**Interfaces:**
- Consumes: `FundingWatcherLedger.is_terminal(event_key) -> bool`
- Produces: truncated snapshots use the anchor only as a trust gate and diff
  all `snapshot.observations`

- [ ] **Step 1: Replace the old prefix-preserving tests with failing safety tests**

Rename the first test to
`test_truncated_snapshot_hands_off_all_unseen_rows_around_anchor_once`. Mark
`rows[3]` terminal, then assert the first request contains every event key
except `rows[3]`, including `rows[4]`, and the second run creates no request.

Change the first-row-anchor test to assert rows `1..99` are handed off rather
than asserting zero change.

Add an amended-event test using `dataclasses.replace`:

```python
known = observation(0)
amended = replace(
    known,
    funding_amount_raw="$11M",
    funding_amount_minor=1_100_000_000,
)
deps = dependencies(tmp_path, FakeBrowser([known, amended], result_count=195))
_mark_terminal(deps.ledger, known)
receipt = run_check(
    config(tmp_path), deps, write=True, now=NOW, enforce_schedule=False
)
assert receipt["counts"]["new_events"] == 1
assert deps.ledger.is_terminal(funding_event_key(amended))
```

- [ ] **Step 2: Run the focused tests and capture the red state**

Run:

```bash
python3 -m pytest \
  tests/test_funding_watcher.py::test_truncated_snapshot_hands_off_all_unseen_rows_around_anchor_once \
  tests/test_funding_watcher.py::test_truncated_new_at_top_snapshot_with_first_row_anchor_is_zero_change \
  tests/test_funding_watcher.py::test_truncated_snapshot_hands_off_amended_event_below_anchor \
  -q
```

Expected: the old slice omits every below-anchor event.

- [ ] **Step 3: Make the minimal implementation change**

Keep the terminal-anchor search and missing-anchor failure unchanged. Replace:

```python
rows_to_diff = snapshot.observations[:anchor_index]
```

with:

```python
rows_to_diff = snapshot.observations
```

- [ ] **Step 4: Run the focused tests and full watcher module**

Run:

```bash
python3 -m pytest tests/test_funding_watcher.py -q
```

Expected: all tests pass after updating only assertions that encoded the unsafe
prefix rule. Do not change bootstrap's valid assertion that baseline rows are
not handed off.

- [ ] **Step 5: Commit the isolated correction**

```bash
git add scripts/funding_watcher.py tests/test_funding_watcher.py
git commit -m "fix: diff all trusted funding window rows"
```

### Task 2: Require bootstrap before scheduled checking

**Files:**
- Modify: `tests/test_funding_watcher.py`
- Modify: `scripts/funding_watcher.py:52-60,81-110`

**Interfaces:**
- Consumes: `FundingWatcherLedger.bootstrap_complete(source_url) -> bool`
- Produces: `bootstrap_required/source_not_bootstrapped` before budget/browser

- [ ] **Step 1: Make check-oriented test dependencies bootstrapped explicitly**

Extend the test helper:

```python
def dependencies(..., bootstrapped: bool = True) -> WatcherDependencies:
    ledger = FundingWatcherLedger(tmp_path / "state" / "ledger.json")
    if bootstrapped:
        ledger.mark_bootstrap_complete(SOURCE, NOW.isoformat())
    ...
```

Pass `bootstrapped=False` from every test whose subject is `run_bootstrap` or
unbootstrapped behavior.

- [ ] **Step 2: Add the F9 regression test**

```python
def test_unbootstrapped_full_snapshot_fails_before_budget_browser_or_core(tmp_path):
    browser = FakeBrowser([observation(i) for i in range(40)])
    budget = FakeBudget()
    calls = []
    deps = dependencies(
        tmp_path,
        browser,
        budget=budget,
        invoke=lambda request, write: calls.append(request),
        bootstrapped=False,
    )
    receipt = run_check(
        config(tmp_path), deps, write=True, now=NOW, enforce_schedule=False
    )
    assert receipt["status"] == "bootstrap_required"
    assert receipt["stopReason"] == "source_not_bootstrapped"
    assert receipt["pagesReserved"] == 0
    assert budget.claims == []
    assert browser.calls == []
    assert calls == []
    assert deps.ledger.events == {}
```

Also assert `_exit_code` maps `bootstrap_required` to `78`.

- [ ] **Step 3: Run the regression and capture the red state**

Run:

```bash
python3 -m pytest \
  tests/test_funding_watcher.py::test_unbootstrapped_full_snapshot_fails_before_budget_browser_or_core \
  tests/test_funding_watcher.py::test_cli_exit_contract \
  -q
```

Expected: the current check reserves pages and hands off all 40 events.

- [ ] **Step 4: Implement the gate**

Add `bootstrap_required` to `CONFIG_ERROR_STATUSES`. After the completed-slot
check and before `max_pages`:

```python
sources = config["sourceDefinitions"]
if not all(
    dependencies.ledger.bootstrap_complete(source.url)
    for source in sources
):
    return finish("bootstrap_required", "source_not_bootstrapped")
```

Reuse `sources` in the browser loop.

- [ ] **Step 5: Run the watcher tests and commit**

```bash
python3 -m pytest tests/test_funding_watcher.py -q
git add scripts/funding_watcher.py tests/test_funding_watcher.py
git commit -m "fix: require source bootstrap before checks"
```

### Task 3: Fail closed on actionable row rejections

**Files:**
- Modify: `scripts/funding_watcher.py:159-214,403-411,600-609`
- Modify: `tests/test_funding_watcher.py`

**Interfaces:**
- Produces:
  - `_partition_rejections(rejections) -> tuple[list[dict], list[dict]]`
  - source summaries with `rejectionDetails`
  - `source_drift/source_rows_rejected` for actionable rows

- [ ] **Step 1: Add rejection-policy tests**

Add tests proving:

1. a fully covered check with `missing_company` rejection fails before Core;
2. bootstrap with an actionable rejection fails before handoff/baseline;
3. a truncated snapshot containing only `excluded_industry:biotechnology`
   remains eligible for anchor processing;
4. the latest receipt includes bounded rejection details and separate
   `rejected_parse`/`excluded_industry` counts.

Extend `FakeBrowser` to accept explicit rejection dictionaries while retaining
its integer convenience argument.

- [ ] **Step 2: Run the four focused tests and capture the red state**

Run the four node IDs with `python3 -m pytest ... -q`.

Expected: complete snapshots currently proceed, bootstrap completes, and
receipts expose only the rejection count.

- [ ] **Step 3: Implement deterministic rejection classification**

```python
def _partition_rejections(
    rejections: tuple[dict[str, str], ...],
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    excluded, actionable = [], []
    for rejection in rejections:
        target = (
            excluded
            if str(rejection.get("reason", "")).startswith("excluded_industry:")
            else actionable
        )
        target.append(dict(rejection))
    return excluded, actionable
```

For each snapshot, update the counts from the partition. Fail with
`source_drift/source_rows_rejected` when `actionable` is non-empty before
coverage/anchor diff or bootstrap mutation. Retain the invalid coverage checks.

Add to `_snapshot_summary`:

```python
"rejectionDetails": [
    {"company": str(item.get("company", ""))[:200],
     "reason": str(item.get("reason", ""))[:500]}
    for item in snapshot.rejections[:100]
],
```

Add `excluded_industry: 0` to base receipt counts.

- [ ] **Step 4: Run parser, watcher, and receipt tests**

```bash
python3 -m pytest \
  tests/test_crunchbase_saved_list.py \
  tests/test_funding_watcher.py \
  -q
```

- [ ] **Step 5: Commit**

```bash
git add scripts/funding_watcher.py tests/test_funding_watcher.py
git commit -m "fix: surface and block rejected funding rows"
```

### Task 4: Add durable heartbeat and deduplicated alerts

**Files:**
- Modify: `scripts/funding_watcher.py`
- Modify: `tests/test_funding_watcher.py`

**Interfaces:**
- Produces:
  - `heartbeat.json` under the configured watcher state root
  - `_finish(..., notify=callable)` durability and alert ordering

- [ ] **Step 1: Add heartbeat and alert tests**

Add tests asserting:

- every `complete`, config-error, and retryable finish writes the v1 heartbeat;
- `latest.json` and heartbeat exist before the notifier runs;
- first config failure alerts once and the unchanged failure is deduplicated;
- a retryable status alerts on its second consecutive run;
- success clears `consecutiveFailures` and the alert key;
- corrupt prior heartbeat is replaced and causes one alert;
- notifier exceptions do not change the returned receipt or terminal ledger.

- [ ] **Step 2: Run the focused tests and capture the red state**

Expected: no heartbeat exists and fail-closed runs never call the notifier.

- [ ] **Step 3: Implement heartbeat state**

Add:

```python
HEARTBEAT_SCHEMA = "norman.research.crunchbase_funding_heartbeat.v1"
SUCCESS_STATUSES = {"complete", "already_checked_slot", "already_bootstrapped"}
```

Implement `_write_heartbeat(state_root, receipt) -> dict` using
`_atomic_write_json`. It reads a valid prior v1 heartbeat when possible,
updates `lastSuccessAt`, increments or resets `consecutiveFailures`, preserves
or clears the alert key, and sets `recoveredInvalidHeartbeat`.

- [ ] **Step 4: Add best-effort alert routing after durability**

Extend `_finish` with `notify: Callable[[list[str]], None] | None = None`.
Write immutable receipt, `latest.json`, and heartbeat in that order. Alert when:

- status is in `CONFIG_ERROR_STATUSES` and the status/reason key differs from
  `lastAlertKey`;
- status is in `RETRYABLE_STATUSES`, `consecutiveFailures >= 2`, and the key
  differs from `lastAlertKey`;
- prior heartbeat was invalid.

Record the alert attempt in heartbeat before notification. Wrap only the
notification dependency boundary so its exception cannot escape `_finish`.

Create a local `finish` closure in `run_check` and `run_bootstrap` that always
passes `dependencies.notify`; replace their direct `_finish` calls.

- [ ] **Step 5: Run focused and complete Research tests**

```bash
python3 -m pytest tests/test_funding_watcher.py -q
python3 -m pytest -q
```

- [ ] **Step 6: Commit**

```bash
git add scripts/funding_watcher.py tests/test_funding_watcher.py
git commit -m "feat: persist watcher heartbeat and failure alerts"
```

### Task 5: Make cross-repository acceptance mandatory in CI

**Files:**
- Create: `config/core-compatibility.json`
- Modify: `tests/test_cross_repo_coordination.py`
- Modify: `.github/workflows/ci.yml`
- Modify: `docs/setup.md`

**Interfaces:**
- Consumes: GitHub App secrets `NORMANAI_CROSS_REPO_APP_ID` and
  `NORMANAI_CROSS_REPO_APP_PRIVATE_KEY`
- Produces: required-capable `cross-repository-acceptance` workflow job

- [ ] **Step 1: Add the compatibility declaration**

Create:

```json
{
  "schemaVersion": "norman.research.crm_core_compatibility.v1",
  "repository": "JDMD23/NormanAI-crm-core",
  "commit": "54822dade20cb258620ca9177140a6fae55a69f6",
  "requestSchemaVersion": "norman.research.funding_handoff.v1",
  "resultSchemaVersion": "norman.crm_core.funding_handoff_result.v1"
}
```

- [ ] **Step 2: Add hard-fail and pin tests**

When `NORMAN_REQUIRE_CROSS_REPO=1`, replace the missing-Core `pytest.skip`
with `pytest.fail`. Add one acceptance test that validates the compatibility
file's exact shape, confirms the declared commit exists in the supplied Core
checkout, and confirms it is an ancestor of `HEAD`.

- [ ] **Step 3: Run the acceptance module against the local Core worktree**

```bash
NORMAN_REQUIRE_CROSS_REPO=1 \
NORMAN_CRM_CORE_ACCEPTANCE_PATH="/Users/normanai/Documents/Core CRM/.worktrees/crunchbase-funding-watcher" \
python3 -m pytest tests/test_cross_repo_coordination.py -q
```

Expected after the new pin test: `10 passed`.

- [ ] **Step 4: Add the private cross-repository workflow job**

The job must:

1. mint a short-lived GitHub App installation token scoped only to Core;
2. parse the repository and commit from `core-compatibility.json`;
3. check out Core at that commit into `${{ github.workspace }}/core`;
4. install pytest;
5. run the acceptance module with the required env variables and JUnit XML;
6. parse XML and require exactly 10 tests, zero errors, failures, or skips.

- [ ] **Step 5: Document the one external administration requirement**

Document the organization-owned GitHub App, its Core-only `Contents: Read`
permission, the two Research Actions secrets, and that branch protection must
mark `cross-repository-acceptance` required before merge.

- [ ] **Step 6: Run config and complete local verification**

```bash
python3 -m pytest -q
python3 -c "import json; json.load(open('config/core-compatibility.json'))"
```

- [ ] **Step 7: Commit**

```bash
git add \
  .github/workflows/ci.yml \
  config/core-compatibility.json \
  tests/test_cross_repo_coordination.py \
  docs/setup.md
git commit -m "ci: require pinned Core acceptance"
```

### Task 6: Record the approved staged architecture and verify release safety

**Files:**
- Create: `docs/superpowers/plans/2026-07-29-funding-watcher-safe-release-followup.md`
- Modify: `docs/research-operating-contract.md`
- Modify: `docs/scheduling.md`

**Interfaces:**
- Consumes: the authoritative Core-home-base design at
  `NormanAI-crm-core/docs/superpowers/specs/2026-07-29-funding-watcher-safe-release-followup-design.md`
- Produces: Research operating guidance for the immediate scope and explicit
  deferred roadmap

- [ ] **Step 1: Update operating documentation**

Document bootstrap enforcement, actionable rejection behavior, heartbeat path,
alert semantics, pinned cross-repository CI, disabled scheduler state, and the
deferred multi-list/v2 roadmap.

- [ ] **Step 2: Run spec self-review**

Search this plan and the Research operating documents for placeholders,
contradictions, stale rejected 40-page capacity language, or claims that the
scheduler is active. Correct every hit.

- [ ] **Step 3: Run final Research and cross-repository suites**

```bash
python3 -m pytest -q
NORMAN_REQUIRE_CROSS_REPO=1 \
NORMAN_CRM_CORE_ACCEPTANCE_PATH="/Users/normanai/Documents/Core CRM/.worktrees/crunchbase-funding-watcher" \
python3 -m pytest tests/test_cross_repo_coordination.py -q
```

- [ ] **Step 4: Verify production state was not touched**

Confirm watcher plists are absent, no watcher launch service is loaded, and
both worktree diffs contain only intended source/test/docs/CI changes.

- [ ] **Step 5: Commit documentation**

```bash
git add docs
git commit -m "docs: define funding watcher safe release"
```
