# Scheduling — Research discovery and the strict funding watcher

## Strict funding watcher

Live Mac LaunchAgent: `com.normanai.research.crunchbase-funding-watcher`
(Documents checkout). Weekdays only at these America/New_York hours:

| Slot | Command |
|---:|---|
| 09:00, 12:00, 15:00, 18:00 ET (Mon–Fri) | `python3 scripts/funding_watcher.py check --write --yes --enforce-schedule` |

Selection: last funding date **= today (ET)**. Handoff: temp CSV → CRMx
`funding_ingest` (SQLite SoR, `added_from=crunchbase-watcher:YYYY-MM-DD`) →
`reconcile_sweep --apply` → narrow cohort `score_batch --apply`. Research does
not dual-write Notion. CSV drop LaunchAgent remains fallback only.

Each scheduled source may reserve at most three pages. All Crunchbase work
shares one 55-work-item ledger per New York day (15 Research saved-list page
checks). Four weekday three-page watcher runs fit within that allocation:

- browser lease: `~/Library/Application Support/NormanAI/shared/browser.lock`
- work-item ledger: `~/Library/Application Support/NormanAI/shared/crunchbase-budget.json`

The runtime order is strict: acquire the browser lease, prove exactly one live
Chrome target and working AppleScript JavaScript, reserve the exact allowance,
then read the saved list. Browser contention or readiness failure therefore
uses zero allowance and returns a retryable receipt with `pagesReserved = 0`.
The watcher reports the live process state; it does not infer readiness from
Chrome's saved menu preference and never restarts the user's browser.

State and immutable receipts live under
`~/Library/Application Support/NormanAI/Research/crunchbase-funding-watcher/`.
The exact durable artifacts are:

- `ledger.json` — event lifecycle and completed slots
- `receipts/<runId>.json` — immutable detector receipt
- `handoffs/<runId>.request.json` — typed request for CRMx ingest
- `handoffs/<runId>.crmx.csv` — Crunchbase-shaped CSV fed to funding_ingest
- `handoffs/<runId>.result.json` — durable CRMx result (entity ids in pageId)
- `latest.json` — replaceable summary of the latest run
- `migration-receipt.json` — proof of the read-only legacy import

An event moves `observed → handoff_pending → terminal` only after a validated
CRMx result is durable. `retryable` remains nonterminal and is eligible for a
later handoff. Terminal results are `created`, `queued_existing`,
`duplicate_event`, `rejected_identity`, or `ambiguous_review`. Default path:
`funding_ingest` → `reconcile_sweep` → narrow `score_batch`. Legacy crm-core
requires `--handoff-legacy-crm-core`.

Watcher exit `0` covers clean completion, no change, disabled, outside
schedule, and already-checked/bootstrap-complete runs. Exit `75` means retry
(`busy`, budget exhaustion, browser retry, or CRM retry); exit `78` means a
source, auth/CAPTCHA, budget-state, migration, or schema contract failure; exit
`64` is command misuse. Check and bootstrap orchestration outcomes write a
status-bearing run receipt. CLI argument/config failures and migration failures
report through stderr and their exit code; successful migration writes the
separate `migration-receipt.json`, which has no run `status`.

The generic Crunchbase source is disabled. Broader Research browser work is
offset to **07:15 and 14:15** so it does not collide with the strict watcher.

The live LaunchAgent points at the permanent Documents Research checkout and
requires a permanent CRMx checkout + `data/norman.db` + `uv`. Do not reload
retired plists or `com.normanai.scheduler`. Do not dual-write Notion from
Research.

## Broader daily discovery

One command does the whole day:

```bash
python3 scripts/daily.py --write --yes --promote
```

It walks both lanes, merges everything, writes **one** brief, and hands rows to
NormanAI-CRMx via `norman.tools.ingest_csv` when `--promote` is set. That's the
thing to schedule.

---

## Where it has to run

The two lanes have different requirements, and that decides the host.

| Lane | Needs | Runs on |
|------|-------|---------|
| Grok search (9 lanes) | `XAI_API_KEY` | Anywhere — Mac, CI, a server |
| Crunchbase + Substack | JD's logged-in Chrome | The Mac, only |
| Promotion to CRMx | `NORMAN_CRMX_PATH` + `NORMAN_CRMX_DB` + `uv` | The Mac, only |

So the **full** run — both lanes, promoting into CRMx — lives on the Mac,
alongside the jobs that already drive enrichment. `daily.py` skips the browser
half automatically when Chrome isn't reachable, which is why the same command
works in CI without a separate script. Research never writes Notion as SoR.

---

## On the Mac (the real daily run)

If broader daily discovery is activated later, keep it separate from the strict
watcher:

| Job id | Schedule (ET) | argv |
|--------|---------------|------|
| `research-daily-am` | `15 7 * * 1-5` | `python3 scripts/daily.py --write --yes --promote` |
| `research-daily-pm` | `15 14 * * 1-5` | `python3 scripts/daily.py --write --yes --promote` |

Two runs a weekday, both lanes. The morning one matters most — it lands before
the enrichment lanes wake up. The afternoon one catches anything that broke
during the day.

Working directory is the `NormanAI-research` checkout. It needs `XAI_API_KEY`,
`NORMAN_CRMX_PATH`, and `NORMAN_CRMX_DB` for promote — the loader reads the same
`.env` ladder CRMx uses, so one file on the host can serve both repos. Notion
token is optional and read-only (prefilter).

Plain cron works too:

```cron
15 7  * * 1-5  cd ~/Projects/NormanAI-research && /usr/bin/python3 scripts/daily.py --write --yes --promote >> ~/Library/Logs/norman-research.log 2>&1
15 14 * * 1-5  cd ~/Projects/NormanAI-research && /usr/bin/python3 scripts/daily.py --write --yes --promote >> ~/Library/Logs/norman-research.log 2>&1
```

**Exit codes:** `0` is a clean run — including a run that found nothing.
`1` means a source was blocked (logged out, captcha). That distinction exists
so a wrapper can alert on it; a logged-out browser otherwise looks exactly
like a quiet news day.

### Ordering against the enrichment lanes

Run broader research before the enrichment lanes wake up. Intake creates rows
at `Status=Research` with the Need-\* markers set, so Crunchbase → Careers →
LinkedIn pick them up on their next supervised cycle. The strict funding
watcher has its own five slots and does not depend on the broader run.

Research and the browser-driven enrichment lanes both want the one Chrome
window. They take turns — `scripts/lib/chrome.py` holds the same kind of
exclusive `flock` lease `linkedin_aggregate` uses — but a research run that
starts while LinkedIn holds the lease will skip its browser half rather than
wait. Give it its own slot.

---

## In CI (the canary)

`.github/workflows/discover.yml` runs `daily.py --no-browser --write --yes`
twice each weekday and uploads the brief and CSV as artifacts. It deliberately
does **not** pass `--promote`, so the board is never touched by a run nobody
looked at. If the Action's candidates look wrong, you've learned that for free.

Set `XAI_API_KEY` in repo secrets (and `NOTION_TOKEN` if you want the
read-only pre-filter that skips companies already on the board).

---

## Adding more search sweeps

The two runs above cover both lanes. If you want the *search* lanes to run more
often than Crunchbase and Substack do, add entries that skip the browser:

```
research-sweep   0 10,16,19 * * 1-5   python3 scripts/daily.py --no-browser --write --yes --promote
```

Three extra sweeps, no Chrome, no contention with the enrichment lanes.

**Cost.** `x_search` and `web_search` bill about $5 per 1,000 calls, plus
tokens. Nine searches a run:

| Schedule | Calls/month | Search cost |
|---|---|---|
| 2 runs/weekday (both lanes) | ~380 | ~$2 |
| 5 runs/weekday (+3 sweeps) | ~950 | ~$5 |

The browser lane adds **no** search calls — extraction runs with tools disabled,
so it only pays tokens. `caps.maxSearchesPerRun` is the ceiling that stops a bug
from becoming a bill.

---

## What lands where

```
out/brief-2026-07-26.md      ← the page you read
out/intake-2026-07-26.csv    ← what went to crm_intake.py
out/evidence-2026-07-26.json ← full receipts, for auditing a bad batch
state/latest-run.json        ← counts, per-source outcomes, API usage
state/research.db            ← the seen-store (never hand a company over twice)
```

`state/` and `out/` are gitignored. The seen-store is the one file worth
keeping across runs — delete it and the next run re-emits everything it has
ever found.
