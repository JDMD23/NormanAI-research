# NormanAI Research — Operating Contract

**Role:** discovery only. Find companies, evidence them, hand them over.
**Never:** write Notion as SoR, write Fit Score, write Status, touch a JD-owned field.

**System of truth:** NormanAI-CRMx (SQLite). This repo is a discovery/qualify
supplement only. Where Research and CRMx disagree, CRMx wins.

Companion migration notes: `docs/crmx-handoff-migration.md` and
`config/crmx-compatibility.json`.

---

## 1. Where research sits

```text
ordinary research → CRMx CSV + evidence JSON → norman.tools.ingest_csv ──┐
strict CB watcher → typed JSON v1 → CSV drop → Pipeline/CRMx ingest ─────┤
                                                                          ↓
                                                         NormanAI-CRMx SoR
                                                                          ↓
                                              enrich → score → operator view
```

`(*)` Funding watcher keeps typed `funding_handoff.v1` for ledger/audit, then
writes a Crunchbase-shaped CSV to `/Users/normanai/Drops/crunchbase` and stops.
Pipeline / CRMx owns ingest, score, and project. Legacy `crm_funding_handoff.py`
is explicit-only (`--handoff-legacy-crm-core`).

Research is upstream of CRMx. Ordinary candidates use the Crunchbase-shaped CSV
plus a versioned evidence sidecar. Research has no Notion mutation client and no
Fit-score authority.

## 2. The one-writer rule

Only CRMx owns writes to the system of truth. Research has two public upstream
handoffs:

1. **Ordinary promote** — CRMx CSV → `uv run python -m norman.tools.ingest_csv`
   (plus evidence sidecar `norman.research.crmx_evidence.v1`).
2. **Strict funding watcher** — typed JSON → Crunchbase-shaped CSV drop at
   `/Users/normanai/Drops/crunchbase`. Research does not ingest, score, or
   reconcile.

Neither handoff makes Research a writer or grants it Notion property authority:

- CRMx holds identity, dedup, and SoR mutation.
- Research must not create a second CRM or a second Notion writer.
- `scripts/lib/sinks.py` has only a read-only Notion prefilter. Read-only
  database queries and page `GET` requests do not grant write authority.

If research ever needs the CRM to change, it emits the documented CSV/evidence
(or typed funding JSON) and invokes the matching **CRMx** (or explicitly legacy)
CLI. It never opens a Notion write client here.

## 3. Research qualifies; CRMx scores

There is no Fit score in this repo. Qualification is binary — did we find enough
to justify creating a row — and it is answered by rules in
`scripts/lib/qualify.py`, not by a number.

| | Qualification | Fit Score |
|---|---|---|
| Owner | this repo | NormanAI-CRMx |
| Question | "does this belong in the CRM at all?" | "how good a prospect is it?" |
| Inputs | keyword hits, NYC evidence, sector, identifiability | headcount, jobs, funding, investors, industry |
| Written to Notion / SoR as Fit | **never** | CRMx only, after enrichment |

An earlier version of this repo predicted Fit Score to rank candidates before
emission. That was removed deliberately. Ranking at discovery time competes with
the scoring lane using worse inputs. Find, qualify, hand over.

## 3a. The two postures

| Mode | Posture | NYC evidence required |
|---|---|---|
| `funding` | broad | no |
| `office_expansion` | tight | yes — strong or moderate |
| `hiring_growth` | tight | yes — strong or moderate |
| `founder_language` | tight | yes — strong or moderate |

Broad exists because funding events are cheap to over-collect and expensive to
miss: an extra row costs one enrichment pass, a missed round costs a deal.
Tight exists because the opposite is true for space signals — a company wrongly
flagged as needing an office wastes a call and JD's credibility.

`hiring_growth` additionally needs one of: `minNycRoles` open NYC roles, a
senior NYC hire, or a stated team build-out. "Hiring in NYC" with two roles is
not a real estate event.

## 4. Unknown ≠ 0

Enforced at the point of capture:

- The Grok response schema makes every fact nullable. There is no default.
- The prompt says so explicitly, twice.
- `csv_row()` / `crmx_csv_row()` write `""` for a null number, never `0`.
- Tests pin it (`test_unknown_numbers_are_blank_not_zero`).

A blank funding amount means "we didn't find one". A `0` means "this company has
raised nothing", which is a claim, and a wrong one.

## 5. Evidence discipline

Three receipts, each enforced in `qualify.py`:

- **`keyword_hits`** — the exact qualifying phrases found in a source. No
  phrase, no candidate, in either posture.
- **`source_urls`** — links actually used. Empty is always a reject.
- **`nyc_evidence`** — quoted or closely paraphrased. `nyc_angle` (surfaced as
  `fitHint`) is judged **only** from it. A model claiming "strong" with no
  evidence is clamped to "none".

These fields travel in the evidence sidecar because CRMx `ingest_csv` is
CSV-only today. Silently dropping them into a thin CSV is forbidden.

## 6. Lane outcomes

| Outcome | Meaning | What happens |
|---------|---------|--------------|
| `success` | Lane ran, returned candidates (possibly zero) | Continue |
| `retry` | Transient — timeout, 429, unparseable output | Next run picks it up |
| `blocked` | API contract broken (410 / auth) | **Loop stops immediately** |

A blocked lane stops the loop rather than spinning, because the failure is
structural and every retry costs a Grok search call.

## 7. Anti-spin

- The SQLite seen-store marks a company `emitted_at` the moment it lands in a
  CSV. It is never handed over twice.
- Two consecutive batches emitting nothing stops the loop.
- `maxSearchesPerRun` caps lane count; `maxCandidatesPerRun` caps the batch.
- When the cap holds candidates back, the run says so out loud rather than
  silently truncating.

## 8. Costs

X search and web search bill per call (~$5 per 1,000) on top of tokens. A full
run is 9 lanes = 9 search calls. At the 5×/weekday schedule that is roughly 950
search calls a month — under $5, plus tokens. `maxSearchesPerRun` is the ceiling
that keeps a runaway loop from becoming a bill.

## 9. Merging duplicates keeps the more specific claim

One company routinely appears in several lanes — the funding sweep sees the
round, the office lane sees the lease. `dedupe_within()` collapses them on
identity keys and keeps the **tighter mode**, because that is the more
actionable fact, while unioning both sets of `keyword_hits` and `source_urls`
and taking the strongest NYC angle seen anywhere.

Blank fields are filled from the duplicate rather than lost. A merge that
discards a fact one lane found is a silent data loss, and the merged row is the
only one that reaches intake.

## 10. Automatic promotion and where it runs

`--promote` invokes CRMx `norman.tools.ingest_csv` via `NORMAN_CRMX_PATH`.
Research calls the writer; it does not become one. Section 2 still holds in
full. Promotion is **off by default** (`promote.enabled: false`).

Promotion needs: this repo, a NormanAI-CRMx checkout (`NORMAN_CRMX_PATH`),
`NORMAN_CRMX_DB`, and `uv` on PATH. Fail closed if any are missing — never fall
back to Notion writes.

| Job id | argv |
|--------|------|
| `research-discover` | `python3 scripts/research_run.py --write --yes --promote` |

CI keeps running discovery **without** `--promote`, as a dry sweep that uploads
CSV + evidence artifacts. That gives a free canary: if the Action's candidates
look wrong, the SoR hasn't been touched.

Guards on promotion, in order:

1. qualification — broad or tight rules, per §3a
2. the seen-store — a company is never handed over twice
3. the read-only board pre-filter — skip what is already there
4. `promote.maxPerRun` — a bad batch is a small mess
5. CRMx ingest / identity — the final authority
6. Research never invents Fit scores or Status transitions

### Legacy shim

`--promote-legacy-crm-core` (or `promote.target: legacy_crm_core`) keeps the old
`crm_intake.py` path for temporary cutover. It is never the default.

## 10a. Strict funding-event watcher

The only allowlisted source is:

`https://www.crunchbase.com/discover/saved/main-funding-august-2026/730c458b-149c-4a0a-9684-7146e7258993`

Research owns source validation, browser reading, the event-key ledger,
immutable receipts, scheduling, and retry. Mutation of the SoR is Pipeline /
CRMx work after the CSV lands in `/Users/normanai/Drops/crunchbase`
(`NORMAN_CRMX_FUNDING_DROP` overrides). Research does not call `funding_ingest`,
`reconcile_sweep --apply`, or `score_batch`. Checks run weekdays at
09/12/15/18 ET and hand off only companies funded today (America/New_York).
Research never dual-writes Notion MACHINE fields. The event key hashes the
exact source URL, canonical Crunchbase organization URL, funding date,
normalized funding type, integer minor units, and ISO currency.

The preserved Core ledger is migrated read-only only when it proves 189 events:
179 baseline and 10 created under one bootstrap source. Research never marks an
event terminal until it has a validated durable result.

A source must complete supervised bootstrap before `check` can reserve budget
or open Chrome. The check returns `bootstrap_required` with exit 78 otherwise;
it does not complete the slot. Bootstrap remains the only path allowed to send
the top `seedTop` rows and baseline the rest.

For every browser-backed check or bootstrap, Research first acquires the shared
browser lease and proves that exactly one live Google Chrome application
process exists, has a window, and accepts a bounded AppleScript JavaScript
probe. Only then may it atomically reserve saved-list page allowance and read
the source. A checked Chrome preference is not treated as runtime proof;
multiple Chrome instances, lease contention, or a live JavaScript refusal stop
with `pagesReserved = 0`. Recovery never quits or restarts JD's browser.

For a truncated two-page snapshot, a terminal row proves only that the window
reached known territory. It does not truncate candidate selection: every
nonterminal valid row in the fetched window is compared and handed off once.
An actionable parser rejection fails closed before handoff or baseline.
Intentional `excluded_industry:*` rows remain excluded, but their company and
reason stay visible in the receipt.

Every watcher outcome atomically replaces `heartbeat.json` beside
`latest.json`. Configuration failures alert on their first status/reason
transition; retryable failures alert on the second consecutive occurrence;
success clears the failure counter. Immutable receipt, latest summary, and
heartbeat are durable before best-effort notification, and notification
failure cannot change event state.

Research CI declares an exact compatible Core commit in
`config/core-compatibility.json` for the funding-handoff cross-repo job. Its
mandatory cross-repository job runs ten behavior cases against that checkout
with `NORMAN_REQUIRE_CROSS_REPO=1` and fails on any skip. A Core-only,
read-only SSH deploy key is used only to check out that pinned Core commit;
checkout does not persist the credential and test code receives only the local
Core path.

The scheduler is still an activation step, not a consequence of merging this
code. The active scheduler is the Codex automation
`research-crunchbase-funding-watcher`; the legacy LaunchAgent stays
uninstalled. Activate or change the Codex schedule only after both repositories
are merged into permanent checkouts, their required checks pass, and one
supervised bootstrap/dry-run/write sequence succeeds.

## 10b. Deliberately deferred funding intelligence

The immediate v1 release has one list, filesystem receipts, and deterministic
event keys. It does not add a second source, evergreen search, reconciliation
enforcement, canonical-round identity, a journal database, amendment
heuristics, or automated rule tuning.

## 11. Hard forbidden

- Writing Notion from this repo as SoR / mutation client
- Importing a Notion write client or mutating `/v1/pages` directly through
  an SDK page create/update or HTTP `POST`/`PATCH`
- Writing Fit Score, Status, Top Pursuit, Priority, or any JD-owned field
- Emitting a candidate with no source URL
- Emitting a candidate with no NYC proof (tight modes)
- Filling an unknown with `0`, `"N/A"`, or a guess
- Silently dropping `nyc_evidence` / `source_urls` / `keyword_hits` on promote
- Scraping x.com directly — the Grok API is the sanctioned path
- Merging NormansBrain into this repo, or inventing Fit scores here
