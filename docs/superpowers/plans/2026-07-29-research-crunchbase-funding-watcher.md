# Research Crunchbase Funding Watcher Implementation Plan

> Historical implementation plan. Its original 25-page protocol was
> superseded by the implemented v3 shared ledger: 40 total work items, 30
> Core company sessions, and ten Research checks across five two-check slots.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make NormanAI-research read the approved Crunchbase saved list five times daily, detect only new funding events, and hand them to CRM Core without writing Notion.

**Architecture:** Research owns the strict authenticated saved-list reader, event fingerprint ledger, legacy bootstrap migration, schedule, receipts, and retrying a versioned JSON handoff. It holds the shared browser lease and reserves from the shared 25-page daily Crunchbase budget, then invokes CRM Core’s public handoff CLI and terminalizes events only after validating Core’s immutable result.

**Tech Stack:** Python 3.11+, standard library (`argparse`, `dataclasses`, `fcntl`, `hashlib`, `json`, `pathlib`, `subprocess`, `tempfile`, `zoneinfo`), macOS AppleScript/Chrome, `pytest`, launchd.

## Global Constraints

- Research is discovery-only and contains no Notion mutation client.
- Request schema is exactly `norman.research.funding_handoff.v1`.
- Result schema is exactly `norman.crm_core.funding_handoff_result.v1`.
- The first allowlisted source is exactly `https://www.crunchbase.com/discover/saved/main-funding-july-2026/730c458b-149c-4a0a-9684-7146e7258993`.
- Source sort must visibly remain `NEW AT TOP`; configured filter and result-type drift fail closed.
- Funding event keys hash exact source URL, canonical Crunchbase organization URL, ISO funding date, normalized funding type, integer minor units, and ISO currency.
- Schedule is 06:00, 10:00, 13:00, 16:00, and 19:00 in `America/New_York`.
- Scheduled detector capacity is at most two saved-list pages per slot.
- Shared browser lock is `~/Library/Application Support/NormanAI/shared/browser.lock`.
- Shared Crunchbase budget is `~/Library/Application Support/NormanAI/shared/crunchbase-budget.json`, ceiling 25 reservations per New York day.
- Research marks no event terminal before a validated CRM result is durably present.
- The old 189-event Core ledger is imported read-only and must verify 179 baseline, 10 created, and one bootstrap source.
- The Research LaunchAgent is installed only after both repositories pass cross-repository acceptance from permanent checkouts.

---

## File Map

### Create

- `config/funding-watcher.json` — exact saved-list contract, schedule, page limits, state root, and CRM CLI path.
- `scripts/lib/browser_coordination.py` — Research-side implementation of the versioned shared lock/budget protocol.
- `scripts/lib/crunchbase_saved_list.py` — strict DOM snapshot parser and dedicated-tab browser transport.
- `scripts/lib/funding_watcher_state.py` — event keys, atomic ledger, immutable receipts, migration, and run-slot checks.
- `scripts/lib/funding_handoff.py` — request serializer, Core CLI invocation, and result validator.
- `scripts/funding_watcher.py` — check/bootstrap orchestration and macOS notification.
- `scripts/install_funding_watcher_launch_agent.py` — install/status/uninstall for the Research-owned five-slot service.
- `tests/fixtures/crunchbase_saved_list_snapshot.json` — strict saved-list snapshot fixture.
- `tests/fixtures/funding_handoff_result_v1.json` — canonical Core result fixture.
- `tests/test_browser_coordination.py`
- `tests/test_crunchbase_saved_list.py`
- `tests/test_funding_watcher_state.py`
- `tests/test_funding_handoff.py`
- `tests/test_funding_watcher.py`
- `tests/test_install_funding_watcher_launch_agent.py`

### Modify

- `scripts/lib/chrome.py` — expose safe AppleScript helpers and use the shared lease.
- `config/browse.json` — disable the generic Crunchbase source entry and offset broader browser cadence.
- `config/research.json` — point `crmCore.path` at the permanent sibling Core checkout used on this Mac.
- `docs/research-operating-contract.md` — define the funding detector as discovery plus handoff, never Notion.
- `docs/setup.md` — replace generic saved-search setup with the exact approved list and watcher commands.
- `docs/scheduling.md` — add the five-slot LaunchAgent and move broader browser runs away from those slots.
- `README.md` — add watcher entry points, state, and result behavior.
- `.gitignore` — ignore local handoff artifacts if the external state root is overridden into the repository during tests.

---

### Task 1: Strict saved-list source and browser reader

**Files:**
- Create: `config/funding-watcher.json`
- Create: `scripts/lib/crunchbase_saved_list.py`
- Create: `tests/fixtures/crunchbase_saved_list_snapshot.json`
- Create: `tests/test_crunchbase_saved_list.py`
- Modify: `scripts/lib/chrome.py`
- Modify: `config/browse.json`

**Interfaces:**
- Produces: `SavedListDefinition`
- Produces: `FundingObservation`
- Produces: `SavedListSnapshot`
- Produces: `load_watcher_config(path: Path) -> dict[str, Any]`
- Produces: `validate_saved_list_url(url: str) -> str`
- Produces: `parse_saved_list_snapshot(payload: Any, source: SavedListDefinition, observed_at: str) -> SavedListSnapshot`
- Produces: `CrunchbaseSavedListBrowser.read_source(source, *, observed_at: str, max_pages: int) -> SavedListSnapshot`

- [ ] **Step 1: Port the fixture and write failing source-contract tests**

Move the strict snapshot fixture and saved-list tests from PR #32 before Core
Task 5 deletes them. Adjust imports to `lib.crunchbase_saved_list` and config to
`config/funding-watcher.json`.

Pin:

- exact saved-list URL shape;
- title suffix normalization;
- visible result type, `NEW AT TOP`, funding-after, and minimum-amount filters;
- USD, GBP, and EUR minor-unit parsing without conversion;
- canonical Crunchbase organization URLs;
- rejection of missing identity and undisclosed amount without dropping valid rows;
- stable result count and exact page coverage;
- auth wall, CAPTCHA, security challenge, source drift, stale page, wrong tab,
  truncated page, and incomplete pagination fail closed;
- user’s previously active Chrome tab is restored.

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
python3 -m pytest tests/test_crunchbase_saved_list.py -q
```

Expected: module and config are absent.

- [ ] **Step 3: Add the exact Research watcher config**

Use:

```json
{
  "schemaVersion": "norman.research.crunchbase_funding_watcher.v1",
  "enabled": true,
  "timeZone": "America/New_York",
  "scheduleHours": [6, 10, 13, 16, 19],
  "scheduledMaxPagesPerSource": 2,
  "bootstrapMaxPagesPerSource": 6,
  "bootstrapSeedTop": 10,
  "dailyPageLoadCeiling": 25,
  "stateDirectory": "~/Library/Application Support/NormanAI/Research/crunchbase-funding-watcher",
  "legacyStateDirectory": "~/Library/Application Support/NormanAI/CRM Core/crunchbase-funding-watcher",
  "crmResultSchemaVersion": "norman.crm_core.funding_handoff_result.v1",
  "sources": [
    {
      "name": "Main Funding - July 2026",
      "url": "https://www.crunchbase.com/discover/saved/main-funding-july-2026/730c458b-149c-4a0a-9684-7146e7258993",
      "expectedResultType": "Companies",
      "expectedSort": "NEW AT TOP",
      "expectedFundingAfter": "2026-07-01",
      "expectedMinimumAmount": 5000000
    }
  ]
}
```

- [ ] **Step 4: Port and decouple the strict reader**

Adapt PR #32’s `scripts/crm_crunchbase_saved_list.py` into
`scripts/lib/crunchbase_saved_list.py`:

- change the schema constant to the Research schema above;
- implement canonical organization URL normalization locally;
- call safe helpers exposed by `lib.chrome` instead of importing any CRM Core
  module;
- retain the dedicated restorable tab, exact DOM selectors, readiness loop,
  stable result-count check, and pagination completeness checks.

Expose `run_osascript`, `ensure_chrome_running`, and
`applescript_string_literal` from `lib.chrome`; keep HTTPS-only navigation.

- [ ] **Step 5: Disable the generic Crunchbase lane**

In `config/browse.json`, set `cb_nyc_rounds.enabled` to `false`, retain
Substack, and change the broader browser cadence to `07:15` and `14:15`. Its
note must say the strict funding watcher owns the approved saved list.

- [ ] **Step 6: Run focused and existing browser tests**

Run:

```bash
python3 -m pytest tests/test_crunchbase_saved_list.py tests/test_browse.py -q
```

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add config/funding-watcher.json config/browse.json scripts/lib/chrome.py scripts/lib/crunchbase_saved_list.py tests/fixtures/crunchbase_saved_list_snapshot.json tests/test_crunchbase_saved_list.py
git commit -m "feat: read approved Crunchbase funding list"
```

---

### Task 2: Shared coordination and durable detector state

**Files:**
- Create: `scripts/lib/browser_coordination.py`
- Create: `scripts/lib/funding_watcher_state.py`
- Create: `tests/test_browser_coordination.py`
- Create: `tests/test_funding_watcher_state.py`
- Modify: `scripts/lib/chrome.py`

**Interfaces:**
- Produces the same shared lease/budget file protocol as CRM Core.
- Produces: `funding_event_key(observation: FundingObservation) -> str`
- Produces: `FundingWatcherLedger(path: Path)`
- Produces: `write_immutable_receipt(root: Path, payload: dict[str, Any]) -> Path`
- Produces: `new_york_slot(now: datetime, schedule_hours: Sequence[int]) -> str | None`
- Produces: `migrate_legacy_state(legacy_root: Path, research_root: Path, *, expected_source_url: str) -> dict[str, Any]`

- [ ] **Step 1: Write failing coordination, event-key, and migration tests**

Tests must prove:

- Research default shared paths exactly equal Core’s documented paths;
- concurrent claims never exceed 25;
- the detector can reserve at most two pages for one scheduled source;
- event keys are stable across irrelevant descriptive-field changes;
- source URL, canonical organization URL, date, normalized type, amount minor,
  and currency each change the key;
- event lifecycle is `observed → handoff_pending → terminal` or
  `observed → handoff_pending → retryable`;
- retryable is not terminal;
- immutable receipts cannot overwrite;
- migration accepts only legacy schema
  `norman.crm_core.crunchbase_funding_ledger.v1`;
- migration verifies exactly 189 events, 179 baseline, 10 created, one
  bootstrap, and the configured source URL;
- migration preserves every event key, leaves the legacy files untouched, and
  is idempotent;
- any count, schema, source, or key mismatch aborts before writing Research
  state.

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
python3 -m pytest tests/test_browser_coordination.py tests/test_funding_watcher_state.py -q
```

Expected: both modules are absent.

- [ ] **Step 3: Implement the versioned shared coordination protocol**

Match Core’s `norman.shared.crunchbase_budget.v1` JSON shape and lock
algorithm byte-for-byte at the contract level. Change `lib.chrome.lease()` to
hold `SharedBrowserLease`, translating contention to `ChromeUnavailable`.

- [ ] **Step 4: Implement event state and receipts**

Use Research schema
`norman.research.crunchbase_funding_ledger.v1`. Store events by SHA-256 key,
bootstraps by exact source URL, and immutable run receipts under
`receipts/<runId>.json`. Use atomic JSON replacement with file and parent
directory `fsync`.

- [ ] **Step 5: Implement read-only legacy migration**

Copy only validated semantic records into a new Research ledger. Write a
`migration-receipt.json` containing old/new paths, counts, source URL, and a
digest of sorted event keys. Never rename, delete, or edit the Core ledger.

- [ ] **Step 6: Run focused tests**

Run:

```bash
python3 -m pytest tests/test_browser_coordination.py tests/test_funding_watcher_state.py tests/test_browse.py -q
```

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add scripts/lib/browser_coordination.py scripts/lib/funding_watcher_state.py scripts/lib/chrome.py tests/test_browser_coordination.py tests/test_funding_watcher_state.py
git commit -m "feat: add durable funding detector state"
```

---

### Task 3: Versioned CRM handoff client

**Files:**
- Create: `scripts/lib/funding_handoff.py`
- Create: `tests/fixtures/funding_handoff_result_v1.json`
- Create: `tests/test_funding_handoff.py`
- Modify: `config/research.json`

**Interfaces:**
- Produces: `build_handoff(run_id: str, generated_at: str, events: Sequence[tuple[str, FundingObservation]]) -> dict[str, Any]`
- Produces: `write_handoff(path: Path, payload: dict[str, Any]) -> None`
- Produces: `validate_result(payload: Any, *, request: dict[str, Any]) -> dict[str, Any]`
- Produces: `invoke_crm_handoff(request_path: Path, result_path: Path, *, write: bool, timeout_seconds: int = 900) -> dict[str, Any]`

- [ ] **Step 1: Write failing serialization and invocation tests**

Pin:

- exact request schema and field names from the approved design;
- founders and industries serialize as arrays;
- raw funding facts remain in the handoff receipt;
- result schema, `runId`, request digest, event count, event keys, terminal
  states, and page IDs are validated;
- missing, extra, duplicated, or reordered-to-different-key results fail;
- a nonzero retryable Core exit leaves the event retryable;
- command arguments use an absolute Core path and never a private module import;
- dry-run uses `--dry-run`; write uses `--write --yes`;
- no environment value or token is copied into a receipt.

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
python3 -m pytest tests/test_funding_handoff.py -q
```

Expected: `lib.funding_handoff` is absent.

- [ ] **Step 3: Configure the permanent Core checkout**

Set `crmCore.path` in `config/research.json` to the stable sibling checkout
used for deployment:

```json
{
  "path": "../Core CRM"
}
```

At runtime resolve it to an absolute path and require
`scripts/crm_funding_handoff.py` to exist.

- [ ] **Step 4: Implement strict request and result handling**

Write the request atomically before invoking Core. Run:

```text
python3 <core>/scripts/crm_funding_handoff.py --handoff <request> --result <result> --dry-run
```

or the write variant. Read only the result file, not human stdout. Validate
every event result against its request key before returning it.

- [ ] **Step 5: Run focused tests**

Run:

```bash
python3 -m pytest tests/test_funding_handoff.py tests/test_intake_contract.py -q
```

Expected: all tests pass and the existing CSV intake contract remains intact
for non-watcher research lanes.

- [ ] **Step 6: Commit**

```bash
git add config/research.json scripts/lib/funding_handoff.py tests/fixtures/funding_handoff_result_v1.json tests/test_funding_handoff.py
git commit -m "feat: hand funding events to CRM Core"
```

---

### Task 4: Funding watcher orchestration

**Files:**
- Create: `scripts/funding_watcher.py`
- Create: `tests/test_funding_watcher.py`

**Interfaces:**
- Produces: `WatcherDependencies`
- Produces: `run_check(config: dict[str, Any], dependencies: WatcherDependencies, *, write: bool, now: datetime, enforce_schedule: bool) -> dict[str, Any]`
- Produces: `run_bootstrap(config: dict[str, Any], dependencies: WatcherDependencies, *, seed_top: int, write: bool, now: datetime) -> dict[str, Any]`
- CLI: `python3 scripts/funding_watcher.py check --dry-run`
- CLI: `python3 scripts/funding_watcher.py check --write --yes --enforce-schedule`
- CLI: `python3 scripts/funding_watcher.py bootstrap --dry-run --seed-top 10`
- CLI: `python3 scripts/funding_watcher.py migrate-legacy-state`

- [ ] **Step 1: Write failing orchestration tests**

Cover:

- outside-slot invocation exits without browser or budget use;
- a second run in the same hour slot returns `already_checked_slot`;
- disabled, busy, budget exhausted, auth wall, CAPTCHA, source drift, partial
  pagination, and CRM retryable failure all produce distinct receipts;
- first bootstrap sends only the top ten candidates and baselines the remaining
  179 after complete coverage;
- imported legacy state produces zero repeated top-ten creates;
- a normal check sends only nonterminal event keys;
- dry-run previews through Core but never changes the Research ledger;
- live run records `handoff_pending` before invoking Core;
- only validated Core terminal results change Research events to terminal;
- interrupted or missing result stays retryable;
- one new company and one existing company produce `created` and
  `queued_existing` respectively;
- notification lists new company names and Crunchbase profile URLs;
- no observation results in a complete zero-change receipt.

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
python3 -m pytest tests/test_funding_watcher.py -q
```

Expected: `scripts.funding_watcher` is absent.

- [ ] **Step 3: Implement check flow**

The exact order is:

```text
run lock
→ enabled/schedule/slot-idempotency check
→ reserve at most two pages per source
→ shared browser lease
→ complete strict snapshot
→ event-key diff
→ immutable detector receipt + atomic handoff request
→ CRM CLI
→ validate durable CRM result
→ terminalize matching event keys
→ latest receipt + macOS notification
```

Never reserve pages or touch Chrome after the run is known to be outside its
slot or already completed.

- [ ] **Step 4: Implement bootstrap and migration commands**

`bootstrap` requires full page coverage before baselining. In live mode it
hands the first ten to Core and baselines the remainder only after every top-ten
result is terminal. `migrate-legacy-state` runs the strict migration from Task
2 and prints the receipt; it performs no browser or CRM call.

- [ ] **Step 5: Implement CLI exit contract**

Use:

- `0` for complete, no-change, outside schedule, already checked, or already
  bootstrapped;
- `75` for lease contention, browser retry, budget exhaustion, or CRM retry;
- `78` for source drift, migration mismatch, auth/CAPTCHA, or schema mismatch;
- `64` for command misuse.

- [ ] **Step 6: Run focused tests**

Run:

```bash
python3 -m pytest tests/test_funding_watcher.py tests/test_funding_handoff.py tests/test_funding_watcher_state.py tests/test_crunchbase_saved_list.py -q
```

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add scripts/funding_watcher.py tests/test_funding_watcher.py
git commit -m "feat: orchestrate funding event detection"
```

---

### Task 5: Research-owned five-slot LaunchAgent

**Files:**
- Create: `scripts/install_funding_watcher_launch_agent.py`
- Create: `tests/test_install_funding_watcher_launch_agent.py`

**Interfaces:**
- Produces LaunchAgent label `com.normanai.research.crunchbase-funding-watcher`.
- Produces subcommands `install`, `status`, and `uninstall`.
- Installed program invokes Research `scripts/funding_watcher.py check --write --yes --enforce-schedule`.

- [ ] **Step 1: Port and write failing installer tests**

Adapt PR #32’s installer tests to assert:

- Research label and permanent Research checkout path;
- `StartCalendarInterval` contains exactly hours 6, 10, 13, 16, and 19;
- no Core watcher path or label appears;
- `WorkingDirectory` is the Research repository;
- program arguments contain `check --write --yes --enforce-schedule`;
- environment is sanitized and contains no token values;
- logs live under
  `~/Library/Logs/NormanAI/Research/crunchbase-funding-watcher/`;
- install is atomic and idempotent;
- status checks both plist tracking and loaded service;
- uninstall bootouts before removing the plist.

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
python3 -m pytest tests/test_install_funding_watcher_launch_agent.py -q
```

Expected: installer module is absent.

- [ ] **Step 3: Implement the Research installer**

Use `/bin/zsh -lc` only to load the configured environment ladder and execute
the absolute Python/script paths. Set a conservative `PATH`, no raw secrets,
`ProcessType=Background`, `RunAtLoad=false`, and the five local-time calendar
entries.

Installation must first require:

- migrated or supervised bootstrap-complete Research ledger;
- Core handoff CLI exists in the permanent checkout;
- Core watcher plist is absent;
- Research and Core source/result schema versions match.

- [ ] **Step 4: Run installer tests**

Run:

```bash
python3 -m pytest tests/test_install_funding_watcher_launch_agent.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add scripts/install_funding_watcher_launch_agent.py tests/test_install_funding_watcher_launch_agent.py
git commit -m "feat: install research funding watcher"
```

---

### Task 6: Research contract and scheduling documentation

**Files:**
- Modify: `README.md`
- Modify: `docs/research-operating-contract.md`
- Modify: `docs/setup.md`
- Modify: `docs/scheduling.md`
- Modify: `.gitignore`
- Test: `tests/test_intake_contract.py`

**Interfaces:**
- Preserves the CSV/`crm_intake.py` path for ordinary research candidates.
- Adds the versioned funding handoff as the only event-specific path.

- [ ] **Step 1: Add a failing “no Notion writer” boundary test**

Walk `scripts/` and fail if Research imports CRM Core’s Notion client, calls
`api.notion.com/v1/pages`, or defines a function that PATCHes Notion.
Allow the existing read-only board prefilter in `scripts/lib/sinks.py`.

- [ ] **Step 2: Run boundary tests and record the baseline**

Run:

```bash
python3 -m pytest tests/test_intake_contract.py -q
```

Expected: existing tests pass; the new boundary test also passes before and
after watcher implementation.

- [ ] **Step 3: Update the operating contract**

Document two upstream handoff forms:

1. candidate CSV → `crm_intake.py` for ordinary discovery;
2. typed funding-event JSON → `crm_funding_handoff.py` for the strict watcher.

State explicitly that neither form gives Research Notion write authority.

- [ ] **Step 4: Update setup and scheduling**

Replace all generic saved-search instructions with the exact approved URL.
Document migration, dry-run, supervised write, install/status/uninstall, the
five slots, shared lock/budget paths, and broader browser cadence at 07:15 and
14:15.

- [ ] **Step 5: Update README and ignores**

List request/result/receipt locations and terminal versus retryable semantics.
Ignore local test overrides under `state/funding-watcher/` and
`out/funding-handoffs/`.

- [ ] **Step 6: Run docs/config tests**

Run:

```bash
python3 -m json.tool config/funding-watcher.json >/dev/null
python3 -m json.tool config/browse.json >/dev/null
python3 -m json.tool config/research.json >/dev/null
python3 -m pytest tests/test_intake_contract.py tests/test_browse.py -q
```

Expected: all commands pass.

- [ ] **Step 7: Commit**

```bash
git add .gitignore README.md docs/research-operating-contract.md docs/setup.md docs/scheduling.md tests/test_intake_contract.py
git commit -m "docs: define funding watcher operations"
```

---

### Task 7: Cross-repository acceptance and supervised activation

**Files:**
- Verify local state and both repositories

**Interfaces:**
- Produces evidence that Research detects and Core alone mutates.
- Produces the only active watcher label:
  `com.normanai.research.crunchbase-funding-watcher`.

- [ ] **Step 1: Run both complete suites**

Run:

```bash
python3 -m pytest -q
python3 -m pytest -q --rootdir="/Users/normanai/Documents/Core CRM/.worktrees/crunchbase-funding-watcher" "/Users/normanai/Documents/Core CRM/.worktrees/crunchbase-funding-watcher/tests"
```

Expected: both suites pass.

- [ ] **Step 2: Verify the shared fixture contract**

Generate a request from Research’s canonical observation fixture and validate
it with:

```bash
mkdir -p /tmp/norman-funding-handoff-acceptance
python3 "/Users/normanai/Documents/Core CRM/.worktrees/crunchbase-funding-watcher/scripts/crm_funding_handoff.py" \
  --handoff /tmp/norman-funding-handoff-acceptance/request.json \
  --result /tmp/norman-funding-handoff-acceptance/result.json \
  --dry-run
```

Expected: result schema is `norman.crm_core.funding_handoff_result.v1`, every
event key matches, and no Notion mutation call occurs in the mocked acceptance
test.

- [ ] **Step 3: Migrate the real preserved ledger**

Run:

```bash
python3 scripts/funding_watcher.py migrate-legacy-state
```

Expected receipt: 189 total, 179 baseline, 10 created, one bootstrap, exact
source URL, and identical sorted-key digest. Confirm the old Core state files’
hashes and mtimes are unchanged.

- [ ] **Step 4: Run a live read-only source check**

Run:

```bash
python3 scripts/funding_watcher.py check --dry-run
```

Expected: exact source, `NEW AT TOP`, current visible filter contract, complete
pagination within two pages, and zero repeated top-ten candidates.

- [ ] **Step 5: Preview through real CRM Core**

Run the watcher’s dry-run with Core invocation enabled and confirm:

- the ten imported companies classify as already terminal before handoff;
- any unseen event is classified by the live full-board index;
- the result contains no writes;
- canonical Crunchbase profile URLs appear in the receipt.

- [ ] **Step 6: Perform one supervised fixture-only write**

Use a temporary mocked Notion writer with one synthetic new event and one exact
existing match. Prove one standard Research create, one exact five-property
queue patch, zero funding-fact writes, and replay creates zero duplicates.

- [ ] **Step 7: Perform one supervised real check**

Run:

```bash
python3 scripts/funding_watcher.py check --write --yes
```

Verify every mutation by canonical Crunchbase identity and compare Research
event receipts to Core result receipts by `runId` and `eventKey`.

- [ ] **Step 8: Install only from permanent merged checkouts**

After both PRs merge and permanent checkouts are updated:

```bash
python3 scripts/install_funding_watcher_launch_agent.py install
python3 scripts/install_funding_watcher_launch_agent.py status
```

Confirm:

```bash
launchctl print "gui/$(id -u)/com.normanai.research.crunchbase-funding-watcher"
test ! -e "$HOME/Library/LaunchAgents/com.normanai.crm-core.crunchbase-funding-watcher.plist"
```

Expected: Research service loaded; Core watcher absent.

- [ ] **Step 9: Audit receipts after the next scheduled slot**

Confirm a new immutable Research receipt exists for the correct New York slot,
page reservations stay at or below two, the shared daily total is at or below
25, and either zero changes or validated Core terminal results are present.

- [ ] **Step 10: Commit verification-only corrections if needed**

If acceptance exposed a concrete defect, add its regression test first, make
the smallest correction, rerun both complete suites, and commit with a message
naming the corrected boundary. If no files changed, do not create an empty
commit.
