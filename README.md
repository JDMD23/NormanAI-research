# NormanAI Research

Discovery arm of NormanAI. Finds companies that are growing, raising, hiring in
NYC, or under office-space pressure, and hands them to **NormanAI-CRMx** intake.

```text
ordinary discovery → CRMx CSV + evidence JSON → norman.tools.ingest_csv ──┐
approved CB list  → typed funding JSON → CSV+evidence → ingest_csv (*) ───┤
                                                                           ↓
                                                                  NormanAI-CRMx
```

`(*)` Funding watcher still emits typed `funding_handoff.v1` for the ledger,
then adapts to the same CRMx `ingest_csv` + evidence path. Legacy crm-core is
explicit-only (`--handoff-legacy-crm-core`).

**Research finds and qualifies. It does not score Fit.** CRMx is the sole
system of truth. This repo never writes Notion as SoR. Ordinary promote emits a
Crunchbase-shaped CSV plus a versioned evidence sidecar
(`nyc_evidence`, `source_urls`, `keyword_hits`) and invokes CRMx
`ingest_csv`. See `docs/crmx-handoff-migration.md`.

## Two postures, running at once

| Mode | Posture | NYC required? | Goal |
|---|---|---|---|
| **Funding & Valuation** | broad | **no** | High coverage of legitimate funding events |
| **Office Expansion** | tight | yes | Explicit, concrete NYC space demand |
| **Hiring & Team Growth** | tight | yes | NYC headcount growth that triggers real estate needs |
| **Founder Language** | tight | yes | Founder statements of genuine space pressure |

The postures get genuinely different prompts. Asking one prompt to be both
"catch everything" and "only the certain stuff" produces a model that splits the
difference and does neither.

**Broad** qualifies on provenance *or* keyword, plus a relevant sector, plus
identifiability. NYC is reported via `nycEvidence` and `fitHint` — never
required. **Tight** needs a concrete phrase *and* a strong or moderate NYC
angle; `weak` and `none` are rejected. A false positive in a tight mode sends
you to call a company with no office need, which costs more than a miss.

The 10 watched accounts (`config/modes.json` → `watchedAccounts`) qualify a
funding candidate on their own, with no keyword needed.

## Evidence discipline

- `keywordHits` carries the exact qualifying phrases found. No phrase, no candidate.
- `nycEvidence` is quoted or closely paraphrased. `fitHint` is judged **only**
  from it — a claimed angle with no evidence is clamped to `none`, whatever the
  model asserted.
- No source URL is always a reject.
- Unknown means blank, never `0`. A blank funding amount means "not found" and
  the Crunchbase lane fills it. A `0` is a claim, and a wrong one.

## Setup

Two things, both covered in **`docs/setup.md`**:

1. An **xAI API key** from `console.x.ai` — *not* an X developer account, and
   not the same as a Grok subscription. `x_search` reads X on xAI's side, which
   is why no X API is needed.
2. Access to the approved Crunchbase saved list in JD's logged-in Chrome.

```bash
echo 'XAI_API_KEY=xai-...' >> ~/.normanai/.env   # same .env ladder as CRMx
python3 scripts/research_probe.py                # confirms the API, ~5 seconds
```

While you're waiting on either, **`docs/grok-automations.md`** has the same four
searches written as Grok Automations — paste-ready, scheduled, emailed to you.
No API key, no code. It tests whether the searches find good companies before
you spend anything on the pipeline that hands them to CRMx.

## Run

One command does the day:

```bash
python3 scripts/daily.py --dry-run                    # look first
export NORMAN_CRMX_PATH=/absolute/path/to/NormanAI-CRMx
export NORMAN_CRMX_DB=/absolute/path/to/norman.sqlite
python3 scripts/daily.py --write --yes --promote      # brief + CRMx ingest
```

It walks both lanes, merges them, and writes **one** brief and **one** intake
handoff. The browser half is skipped automatically off a Mac, so the same
command works on the cron host and in CI. Promote is fail-closed / off unless
`--promote` (or `promote.enabled`) is set and CRMx path + DB are configured.

Single lanes, when you're tuning one:

```bash
python3 scripts/research_run.py --mode office_expansion --dry-run   # search only
python3 scripts/research_browse.py --source substack_inbox --dry-run  # broad browser lane
python3 scripts/daily.py --dry-run --show-rejects                   # why things were filtered
```

The strict funding detector is separate from the generic browser run:

```bash
python3 scripts/funding_watcher.py migrate-legacy-state
python3 scripts/funding_watcher.py check --dry-run
python3 scripts/funding_watcher.py check --write --yes
python3 scripts/install_funding_watcher_launch_agent.py status
```

It reads only the approved `Main Funding` saved list, fingerprints each funding
event, writes typed JSON plus CRMx CSV/evidence, and invokes
`norman.tools.ingest_csv` (`NORMAN_CRMX_PATH` / `NORMAN_CRMX_DB`). Research
never imports a Notion writer. A terminal result closes the event; a retryable
result remains open for the next run. Legacy Core remains only behind
`--handoff-legacy-crm-core`.

The default state root is
`~/Library/Application Support/NormanAI/Research/crunchbase-funding-watcher/`.
Immutable detector receipts are `receipts/<runId>.json`; handoff artifacts
are `handoffs/<runId>.request.json` and `handoffs/<runId>.result.json`.
`latest.json` is the current run summary and `migration-receipt.json` records
the read-only legacy import.

`--promote` invokes CRMx `norman.tools.ingest_csv` on the Crunchbase-shaped
CSV and always passes `out/evidence-*.json` via `--evidence`. Bounded by
`promote.maxPerRun` and CRMx identity/dedup. Legacy crm-core intake remains
only behind `--promote-legacy-crm-core`.

Without `--promote` it stops at CSV + evidence and prints the CRMx command.

## Layout

| Path | What |
|------|------|
| `config/modes.json` | The four modes, their postures, keywords, and the watchlist |
| `config/sources.json` | Lanes — each declares its mode; cadence |
| `config/research.json` | Grok settings, caps, promotion, dedup |
| `scripts/daily.py` | **The scheduled run** — both lanes, one brief |
| `scripts/research_run.py` | Grok search lanes (X + web) |
| `scripts/research_browse.py` | Browser lanes (Crunchbase + Substack) |
| `scripts/lib/pipeline.py` | The shared tail: qualify → dedup → emit → brief |
| `scripts/research_probe.py` | One live call to verify the xAI request shape |
| `scripts/funding_watcher.py` | Strict saved-list detector and CRM handoff |
| `config/funding-watcher.json` | Exact source, five slots, caps, and state roots |
| `scripts/lib/qualify.py` | The broad/tight rules |
| `scripts/lib/discover.py` | Lane execution and the two prompts |
| `scripts/lib/grok.py` | Agent Tools client (stdlib only) |
| `scripts/lib/identity.py` | Dedup keys (aligned with CRMx / legacy Core normalisation) |
| `scripts/lib/sinks.py` | CSV + evidence sidecar + CRMx promote (legacy shim) |
| `docs/crmx-handoff-migration.md` | Promote cutover notes and example commands |
| `config/crmx-compatibility.json` | Pinned CRMx public intake surface |

## Scheduling

The strict watcher runs through the single approved Codex runtime dispatcher at
**06:00, 10:00, 13:00, 16:00, and 19:00 ET**. The legacy Research LaunchAgent
must remain uninstalled. The broader browser run is offset to 07:15 and 14:15.

The shared browser lock is
`~/Library/Application Support/NormanAI/shared/browser.lock`; all Crunchbase
lanes share the New York-day work-item ledger at
`~/Library/Application Support/NormanAI/shared/crunchbase-budget.json`.
It enforces 40 Core company sessions and 15 Research watcher checks (saved-list
page checks); each watcher slot reserves exactly three pages and each Core
session has its own five-navigation local ceiling. The watcher proves the live
Chrome target and AppleScript JavaScript before making that reservation.

CI runs the search half twice a weekday **without** `--promote` and uploads the
brief as an artifact — a free canary that can't touch the board.

Full detail, including exit codes and how this orders against the enrichment
lanes: **`docs/scheduling.md`**.

## Rules

- Research qualifies; CRMx scores. Never write Fit Score, Status, or any
  JD-owned field.
- Never write Notion from here as SoR.
- Ordinary promote and funding-watcher handoff go through CRMx `ingest_csv` +
  evidence sidecar.
- Legacy crm-core only behind `--promote-legacy-crm-core` /
  `--handoff-legacy-crm-core`.
- Unknown ≠ 0.
- No source URL, no candidate. No keyword hit, no candidate.
- Tight modes stay tight — if one starts returning 25 companies, it isn't tight
  any more.
- Biotech and therapeutics are excluded outright; crypto is flagged, not dropped.
