# Browser Readiness and Budget Guard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent an ambiguous or unusable Chrome process from consuming Research Crunchbase allowance, and report the real browser fault before any paid page reservation.

**Architecture:** Add one bounded, read-only live Chrome readiness probe that requires exactly one Chrome application process and successful AppleScript JavaScript execution. Run it while holding the existing shared browser lease and before the existing atomic budget claim; only a ready browser may reserve pages and read the saved list.

**Tech Stack:** Python 3.11+, AppleScript via `osascript`, existing shared browser lease and v4 capacity ledger, pytest.

## Global Constraints

- Research remains detection-only and never writes Notion.
- Keep the shared 55/40/15 work-item contract and exact three-page watcher slot unchanged.
- Do not automatically quit or restart JD's Chrome.
- Do not inspect or persist page content, account identity, cookies, or credentials in the readiness probe.
- Browser refusal must leave `pagesReserved = 0` and the capacity ledger unchanged.
- Preserve the existing watcher schedule, saved-list source, handoff schema, and retry exit code.

---

### Task 1: Bounded live Chrome readiness probe

**Files:**
- Modify: `scripts/lib/chrome.py`
- Test: `tests/test_browse.py`

**Interfaces:**
- Produces: `require_automation_ready() -> None`; raises `ChromeUnavailable` with a bounded reason code when Chrome instance count, window availability, or AppleScript JavaScript execution is unsafe.

- [ ] **Step 1: Write failing readiness tests**

Add tests that inject literal probe results for `ready`, `instance_count:2`, `no_window`, and `javascript_error:12`. Assert only `ready` returns and every refusal raises `ChromeUnavailable` without raw Chrome error text.

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `python3 -m pytest -q tests/test_browse.py`

Expected: FAIL because `require_automation_ready` does not exist.

- [ ] **Step 3: Implement the minimal probe**

Run a single bounded AppleScript that counts `Google Chrome` application processes through System Events, refuses counts other than one, requires an existing window, executes the constant JavaScript expression `'norman-ready'`, catches Chrome errors inside AppleScript, and returns only a closed result token.

- [ ] **Step 4: Run the focused tests and verify GREEN**

Run: `python3 -m pytest -q tests/test_browse.py`

Expected: all tests pass.

### Task 2: Lease and readiness before allowance

**Files:**
- Modify: `scripts/funding_watcher.py`
- Test: `tests/test_funding_watcher.py`

**Interfaces:**
- Consumes: `require_automation_ready() -> None` from Task 1.
- Produces: watcher ordering `run lock -> schedule/bootstrap checks -> browser lease -> live readiness -> exact capacity claim -> source read` for both scheduled checks and bootstrap.

- [ ] **Step 1: Write failing ordering and refusal tests**

Add tests proving: browser-lease refusal never calls the budget; readiness refusal never calls the budget or source reader and receipts `pagesReserved = 0`; success calls readiness before capacity and capacity before source read. Cover both `check` and `bootstrap` refusal paths.

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `python3 -m pytest -q tests/test_funding_watcher.py`

Expected: FAIL because the current code claims capacity before acquiring the lease and has no readiness dependency.

- [ ] **Step 3: Implement the minimal ordering change**

Add the readiness callable to `WatcherDependencies`, wire production to `require_automation_ready`, and move each exact claim inside the existing shared browser lease after readiness but before `read_source`. Preserve all existing status and exit-code mappings.

- [ ] **Step 4: Run focused and cross-repository tests**

Run:

```bash
python3 -m pytest -q tests/test_browse.py tests/test_funding_watcher.py tests/test_browser_coordination.py
NORMAN_CRM_CORE_ACCEPTANCE_PATH='/Users/normanai/Documents/Core CRM' NORMAN_REQUIRE_CROSS_REPO=1 python3 -m pytest -q tests/test_cross_repo_coordination.py
```

Expected: all pass; cross-repository suite runs rather than skips.

- [ ] **Step 5: Run the complete Research suite**

Run: `python3 -m pytest -q`

Expected: all Research tests pass with only the documented standalone Core-absent skips.

### Task 3: Live non-consuming verification and governance

**Files:**
- Modify: `docs/runbooks/crunchbase-funding-watcher.md` if the existing runbook path is present; otherwise update the current watcher operations document discovered by `rg`.

**Interfaces:**
- Consumes: Tasks 1-2 production behavior.
- Produces: one clear operator diagnosis and a repeatable, zero-capacity live probe.

- [ ] **Step 1: Verify the direct live probe without running the watcher**

Import and call `require_automation_ready()` from the worktree. Confirm it succeeds with one normal Chrome process and does not change the budget ledger.

- [ ] **Step 2: Verify the failure mode hermetically**

Use the unit fixture for `instance_count:2`; do not launch a second real Chrome. Confirm the watcher returns `browser_retryable`, `pagesReserved = 0`, and no budget mutation.

- [ ] **Step 3: Document the exact operational meaning**

Document: a checked Chrome menu item is saved preference, not live readiness; the runtime trusts the live probe; multiple Chrome app instances and JavaScript refusal stop before allowance; recovery never kills or restarts JD's browser automatically.

- [ ] **Step 4: Run release verification**

Run the full Research suite, mandatory cross-repo suite, `git diff --check`, and confirm the permanent Research checkout remains on its production branch and matches `origin`.

## Self-Review

- Spec coverage: live-process ambiguity, stale runtime setting, lease contention, allowance preservation, check/bootstrap parity, bounded receipts, and no automatic browser restart are each assigned above.
- Placeholder scan: no deferred implementation steps or unspecified error handling remain.
- Type consistency: `require_automation_ready() -> None` is the single new production interface throughout.
