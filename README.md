# NormanAI Research

Discovery arm of NormanAI. Finds companies that are growing, raising, hiring in
NYC, or under office-space pressure, and hands them to `NormanAI-crm-core`.

```text
ordinary discovery → candidate CSV ───────→ crm_intake.py ───────────────┐
approved CB list  → typed funding event → crm_funding_handoff.py ────────┤
                                                                          ↓
                                                                  Norman CRM Core
```

**Research finds and qualifies. It does not score.** Rows land at
`Status=Research`; crm-core's Crunchbase, Careers, and LinkedIn lanes enrich
them, and its score agent scores them. This repo never writes Notion. Ordinary
candidate CSVs go to Core's `crm_intake.py`; typed funding-event JSON goes to
Core's `crm_funding_handoff.py`. Neither handoff grants Research write
authority; Core owns mutation and hard dedup.

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
echo 'XAI_API_KEY=xai-...' >> ~/.normanai/.env   # same .env ladder as crm-core
python3 scripts/research_probe.py                # confirms the API, ~5 seconds
```

While you're waiting on either, **`docs/grok-automations.md`** has the same four
searches written as Grok Automations — paste-ready, scheduled, emailed to you.
No API key, no code. It tests whether the searches find good companies before
you spend anything on the pipeline that writes them to Notion.

## Run

One command does the day:

```bash
python3 scripts/daily.py --dry-run                    # look first
python3 scripts/daily.py --write --yes --promote      # brief + rows in Notion
```

It walks both lanes, merges them, and writes **one** brief and **one** intake
call. The browser half is skipped automatically off a Mac, so the same command
works on the cron host and in CI.

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

It reads only the approved `Main Funding - July 2026` saved list, fingerprints
each funding event, and sends a typed JSON request to CRM Core. Research never
imports a Notion writer. A terminal Core result closes the event; a retryable
result remains open for the next run.

The default state root is
`~/Library/Application Support/NormanAI/Research/crunchbase-funding-watcher/`.
Immutable detector receipts are `receipts/<runId>.json`; Core handoff artifacts
are `handoffs/<runId>.request.json` and `handoffs/<runId>.result.json`.
`latest.json` is the current run summary and `migration-receipt.json` records
the read-only legacy import.

`--promote` invokes crm-core's `crm_intake.py` on the CSV, so companies reach
the board with no human step. Research calls the writer rather than becoming
one. Bounded by `promote.maxPerRun`, intake's hard dedup, and the fact that
everything lands at `Status=Research` — a machine status the score agent can
exit without touching anything you own.

Without `--promote` it stops at a CSV and prints the two commands to run by hand.

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
| `scripts/lib/identity.py` | Dedup keys, mirroring crm-core's normalisation |

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

- Research qualifies; crm-core scores. Never write Fit Score, Status, or any
  JD-owned field.
- Never write Notion from here.
- Funding-event writes go only through CRM Core's versioned public CLI.
- Unknown ≠ 0.
- No source URL, no candidate. No keyword hit, no candidate.
- Tight modes stay tight — if one starts returning 25 companies, it isn't tight
  any more.
- Biotech and therapeutics are excluded outright; crypto is flagged, not dropped.
