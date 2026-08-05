# X Funding Intelligence Watcher — Test-Run Design

**Date:** 2026-08-05  
**Repositories:** `NormanAI-research` detector and `NormanAI-crm-core` writer  
**Status:** Approved for specification and implementation planning  

## 1. Outcome

Build a background X funding watcher that checks seven approved accounts five
times per New York day, uses Grok to identify explicit company funding
announcements of at least US$5 million, and hands qualified companies to CRM
Core. Research never writes Notion. CRM Core remains the sole Notion writer and
the sole authority for company identity, duplicate prevention, and intake-safe
mutation.

The first activation is a bounded test run. It progresses through offline
fixtures, a live no-write shadow, a Core dry run, and one supervised write
canary. Unattended writes are not enabled until all gates in this document pass.

## 2. Approved sources

The watcher monitors exactly these X handles in v1:

| Handle | Source type | Accepted evidence |
|---|---|---|
| `fundable_ai` | funding aggregator | explicit company funding announcements |
| `raisingfi` | fundraising platform | explicit company raise announcements |
| `techmeme` | broad technology news | explicit funding announcements only |
| `nextplayso` | startup and hiring curator | explicit funding announcements only; hiring lists alone do not qualify |
| `dealroomco` | startup intelligence | explicit company funding announcements |
| `ideafireconsult` | funding and startup-news curator | explicit company funding announcements |
| `adinonline` | venture and scout network | explicit portfolio-company funding announcements; fund and scout promotion does not qualify |

Handles are stored without `@` and compared case-insensitively. A source-registry
change is a reviewed configuration change. An unconfigured handle cannot create
an observation or a handoff.

## 3. Schedule and isolation

The approved schedule is:

- 06:30 America/New_York
- 10:30 America/New_York
- 14:00 America/New_York
- 17:00 America/New_York
- 20:00 America/New_York

The X watcher is API-only. It does not use Chrome, the shared Crunchbase browser
lease, or the Crunchbase work-item ledger. The stagger avoids routine overlap
with the existing Crunchbase detector. Core's existing intake/dispatcher lock
serializes any coincident Core mutation.

Each slot has a deterministic New York-day slot key. A completed slot cannot run
twice. A retry of an incomplete slot reuses its source checkpoints and immutable
raw observations rather than treating the retry as new work.

## 4. Approaches considered

### A. X API retrieval plus Grok analysis — selected production design

The X user-timeline API retrieves posts deterministically and records a
per-account `since_id`. Grok analyzes only the durably captured posts. This
separates source completeness from model judgment and makes missed-post and
checkpoint behavior testable.

### B. Grok X Search alone — permitted only for the initial shadow smoke test

Grok can search the seven allowlisted handles and return cited evidence without
using the logged-in browser. This requires only an xAI API key and is useful for
proving prompt quality. Search is model-directed rather than a deterministic
enumeration of every timeline post, so this adapter cannot authorize unattended
Notion writes.

### C. Logged-in browser automation — rejected

Browser automation depends on an unlocked active GUI session, saved login
state, page structure, and X security challenges. It is not suitable for a
background watcher.

## 5. Component boundaries

### 5.1 Research source registry

Owns the seven allowlisted handles, source categories, enabled flags, and
schedule. It contains no credentials. Configuration validation is fail-closed:
unknown keys, duplicate handles, invalid schedules, or an empty source list
prevent a run.

### 5.2 X timeline reader

Resolves configured handles to X user IDs and fetches posts newer than that
account's committed `since_id`. It requests post ID, author ID, creation time,
text, edit history, referenced posts, URLs, and available media metadata.

Replies and ordinary reposts are excluded from the primary fetch. Quote posts
remain eligible because the quoting account may add a material announcement.
Referenced posts and threads are fetched only as bounded supporting evidence.

The reader emits raw immutable observations. It does not classify funding and
does not call CRM Core.

### 5.3 Grok analyzer

Receives a bounded batch of raw posts and the strict policy below. It returns a
JSON-schema-constrained analysis. Grok may use native X Search to inspect an
allowed source's relevant thread or supporting company/investor post. Every
qualified or review result must retain at least one direct X post URL.

Grok cannot calculate observation keys, decide terminal ledger state, advance
checkpoints, or call Core. Research deterministically performs those actions
after validating the response.

### 5.4 Research journal

The journal stores:

- immutable raw X observations;
- per-handle retrieval checkpoints;
- Grok analysis attempts and model/schema version;
- normalized funding-event candidates;
- review and rejection outcomes;
- Core handoff requests and validated results;
- per-slot receipts and heartbeat state.

For the bounded v1 test, canonical JSON files and immutable receipts remain the
authoritative store, following the existing Research funding-watcher pattern.
SQLite is not introduced for the test run.

### 5.5 CRM Core handoff

The existing Crunchbase handoff schemas remain unchanged. They require a
canonical Crunchbase organization URL and encode Crunchbase-specific identity,
so X events must not be forced into them.

The implementation adds a separate versioned boundary:

- request: `norman.research.x_funding_handoff.v1`
- result: `norman.crm_core.x_funding_handoff_result.v1`

Core exposes the new CLI and reuses its existing company identity index,
immutable request/result receipts, mutation intents, event index, intake-safe
creation, exact readback, and replay protections. The new CLI contains no X
reader, Grok client, scheduler, or detector ledger.

New companies receive a `Research` intake row with verified identity fields and
appropriate enrichment needs. The funding announcement is evidence that
justifies intake; it is not permission to write unverified funding properties.
Existing companies receive a verification-queue update through the same narrow
governed property posture used by Core's funding path. Funding facts remain
owned by Core's verified enrichment lanes.

## 6. Data flow

1. Scheduler resolves the New York slot and refuses a completed duplicate.
2. Research loads and validates the source registry and local state.
3. The X reader fetches each account independently from its committed
   checkpoint.
4. Research durably stores every raw post before advancing that account's
   retrieval checkpoint.
5. Grok analyzes newly stored posts under a strict JSON schema.
6. Research validates citations, source ownership, amount, currency, company
   identity, event date, confirmation state, and schema shape.
7. Research deterministically classifies each observation as `qualified`,
   `review`, `rejected`, or `analysis_retryable`.
8. Multiple observations about the same round merge into one canonical event
   while retaining every source post.
9. In shadow mode, Research writes a proposed-handoff receipt and stops.
10. In Core dry-run or write mode, Research emits an immutable versioned request
    and invokes only Core's public X-funding handoff CLI.
11. Research validates the result schema, run ID, request digest, event order,
    terminal state, and page ID rules before terminalizing any event.
12. The run writes its receipt and heartbeat. Alerts happen after durable state
    persistence and cannot change the run outcome.

## 7. Qualification policy

An observation is `qualified` only when all conditions are true:

1. The author is one of the seven configured handles.
2. The target is an operating company, not a fund, accelerator, service
   provider, scout, or investor.
3. The post or bounded supporting evidence explicitly confirms completed
   financing. Seeking, targeting, considering, reportedly raising, or rumored
   financing does not qualify.
4. The current round amount is at least US$5,000,000. Total historical funding
   cannot substitute for an unstated current-round amount.
5. The amount, currency, event date, company name, and direct evidence URL are
   structurally valid.
6. Company identity is resolvable to a canonical official domain, canonical X
   handle, or another Core-approved identity packet. Grok does not decide
   whether the company already exists in Notion.

US$5,000,000 qualifies exactly. US$4,999,999 does not.

For foreign currencies, Research uses a captured, dated rate from the approved
ECB reference-rate feed and persists the input rate and computed USD amount in
the receipt. A missing currency, unsupported rate, or conversion too close to
the boundary to verify becomes `review_currency_unverified`, never qualified.

For the test run, confirmed priced equity, SAFE/convertible financing, and
venture debt may qualify. Grants, fund closings, acquisition values, token
sales, crowdfunding targets, and unclosed financing do not qualify.

## 8. Identity and deduplication

Research computes keys; Grok never supplies authoritative hashes.

### Observation key

```text
sha256({
  "platform": "x",
  "post_id": canonical_post_id,
  "author_handle": canonical_casefolded_handle
})
```

Each source post is one observation. Three monitored accounts announcing the
same round produce three observations.

### Canonical X round key

```text
sha256({
  "company_identity": official_domain_else_company_x_handle,
  "funding_date": canonical_yyyy_mm_dd,
  "funding_type": normalized_round_type,
  "currency": iso_4217_currency
})
```

The amount is an attribute rather than part of the round key so corrected
amounts attach as new observations to the same round. Research preserves the
full amendment history. Ambiguous same-company, same-day, same-type collisions
go to review.

Core remains authoritative for company-level deduplication. An X round and a
Crunchbase round may have different source-event keys, but both must resolve to
one company page. A cross-source discovery may trigger verification; it may not
create a second company.

## 9. Checkpoint and retry rules

- Source checkpoints are per handle, never global.
- A checkpoint advances only after all returned raw posts are durably stored.
- A source retrieval failure does not advance that source. Other sources may
  complete and commit normally.
- Once a raw post is durable, its analysis can retry independently even if the
  retrieval checkpoint has advanced.
- Malformed, contradictory, or uncited Grok output produces
  `analysis_retryable`; it cannot call Core.
- `review` and `rejected` are terminal observation outcomes but never Core
  handoffs.
- Only `qualified` canonical events enter a handoff.
- Core timeouts or invalid results leave events retryable. Research never infers
  that a Notion write succeeded.
- Replays use the same observation and event keys. A terminal Core event cannot
  produce a second mutation.

## 10. Acceptance contract

| Test | Research outcome | Core called | Notion effect |
|---|---|---:|---:|
| Confirmed US$5M round | `qualified` | yes | at most one company |
| Confirmed US$20M round | `qualified` | yes | at most one company |
| Confirmed US$4.9M round | `rejected_below_threshold` | no | none |
| "Raised millions" | `review_amount_unverified` | no | none |
| Rumored or seeking US$30M | `rejected_unconfirmed` | no | none |
| US$20M total; current round unstated | `review_round_amount_unverified` | no | none |
| Confirmed EUR8M with verified conversion | `qualified` | yes | at most one company |
| Foreign currency without reliable rate | `review_currency_unverified` | no | none |
| Same round from three handles | three observations, one event | once | at most one company |
| Venture fund closes US$100M fund | `rejected_fund_announcement` | no | none |
| Fund announces portfolio company's US$12M round | company qualifies; fund excluded | yes | at most one company |
| Old repost without new facts | `duplicate_observation` | no | none |
| Corrected amount | new observation on existing round | only if verification is owed | no second company |
| Invalid Grok schema or contradiction | `analysis_retryable` | no | none |
| Grok unavailable after retrieval | raw post remains queued | no | none |
| One source retrieval fails | failed source retryable; others commit | for valid events from completed sources | valid changes only |
| All source retrieval fails | run retryable; no source checkpoint advances | no | none |
| Identical batch replay | existing terminal keys recognized | no new call or safe replay | zero additional pages |
| Core timeout | event remains retryable | retry allowed | never assume success |
| Crash after Core success | Core recognizes replayed event | safe replay | zero additional pages |

## 11. Test-run stages

### Stage 1 — Offline fixtures

Implement the acceptance contract with deterministic X payload and Grok
response fixtures. Tests run without network, credentials, Notion, or
production state. Architecture tests prove Research cannot import or invoke a
Notion writer and Core contains no X reader or Grok client.

### Stage 2 — Grok X Search smoke test

Using a protected `XAI_API_KEY`, run one manual search over the seven handles
for the previous 24 hours. Enable X Search citations and strict structured
output. Write only a local shadow receipt. No Core invocation occurs.

This stage measures whether the prompt returns useful evidence. It does not
claim complete timeline coverage and cannot authorize writes.

### Stage 3 — Deterministic timeline shadow

Using a protected X API bearer token, bootstrap each handle's user ID and
timeline checkpoint. Read the previous 24 hours, preserve all raw observations,
and analyze them with Grok. Initial history remains shadow-only; it is never a
mass import.

Run at least three consecutive scheduled slots. A pass requires:

- all seven handles resolve;
- every requested account reports complete coverage;
- no checkpoint gaps or regressions;
- every qualified candidate has a direct X URL and verified amount;
- cross-account duplicates merge correctly;
- zero Research-to-Notion mutation paths;
- retry tests preserve raw posts and checkpoints as specified.

### Stage 4 — Core dry run

Emit the new handoff schema for reviewed qualified candidates and invoke Core
with `--dry-run`. Validate identity classification, request/result digest
binding, exact event alignment, and replay behavior. No Notion mutation occurs.

### Stage 5 — One supervised write canary

Select one reviewed qualified event. Invoke Core with explicit `--write --yes`
while observing receipts and the CRM result. Then replay the same request and
the same event in a new request. The gate passes only when the first run creates
or safely queues the correct company and both replays perform zero additional
Notion mutations.

### Stage 6 — Scheduled activation

Enable the five slots only after Stages 1–5 pass. The initial unattended period
uses failure alerts and a daily digest. Any schema failure, coverage gap,
checkpoint anomaly, or Core result mismatch disables write progression for the
affected event and surfaces an operator-visible failure.

## 12. Credentials and secret handling

The production adapter requires:

- `XAI_API_KEY` for Grok analysis and bounded X Search verification;
- an X API bearer token for deterministic public user timelines;
- existing Core-managed Notion credentials for Core only.

Secrets are entered by the operator into protected local credential storage and
injected at runtime. They are never placed in configuration JSON, prompts,
receipts, logs, fixtures, commits, or GitHub Actions output. Research never
receives the Notion token used for mutation. Logs redact authorization headers
and provider error bodies before persistence.

## 13. Receipts, heartbeat, and operator visibility

Every run records:

- run and slot IDs;
- source registry digest;
- prompt and JSON-schema digests;
- model name;
- per-source requested checkpoint, returned range, stored observation count,
  committed checkpoint, and status;
- qualified, review, rejected, and retryable counts;
- observation and canonical event keys;
- Core request/result digests and terminal states when invoked;
- elapsed time and provider usage when available.

The heartbeat records the last attempted run, last complete run, last successful
Core handoff, per-source lag, consecutive failures, and next expected slot.
Alerts are best-effort and happen only after receipts and heartbeat state are
durable.

## 14. Non-goals for the test run

- no browser control or use of the personal logged-in X session;
- no likes, follows, reposts, posts, DMs, or other X mutations;
- no monitoring outside the seven configured handles;
- no launches, hiring, people moves, traction, or funding below US$5 million in
  New Intake;
- no direct Research-to-Notion writer;
- no modification of the existing Crunchbase handoff schemas;
- no SQLite/Postgres migration;
- no unattended mass backfill;
- no automated source expansion or self-modifying qualification rules.

## 15. Definition of ready

The test-run implementation is ready for a live smoke test when offline tests
pass, source and prompt schemas are pinned, secret presence can be checked
without exposing values, and the CLI defaults to shadow mode.

It is ready for one supervised Notion canary only when the deterministic X
timeline path and Core dry run pass. It is ready for scheduled writes only when
the canary and replay checks prove exactly-once company mutation behavior and
all seven sources have completed at least three scheduled shadow slots without
a coverage or checkpoint defect.
