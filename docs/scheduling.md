# Scheduling — Research discovery and the strict funding watcher

## Strict funding watcher

The Research-owned Codex automation is
`research-crunchbase-funding-watcher`. It runs at these local New York times
every day:

| Slot | Command |
|---:|---|
| 06:00, 10:00, 13:00, 16:00, 19:00 ET | `python3 scripts/funding_watcher.py check --write --yes --enforce-schedule` |

Each scheduled source may reserve at most three pages. All Core and Research
Crunchbase work shares one 55-work-item ledger per New York day. The ledger
enforces separate allocations: 40 Core company sessions and 15 Research
saved-list page checks. The five scheduled three-page watcher runs therefore
fit exactly without Core being able to consume their allocation:

- browser lease: `~/Library/Application Support/NormanAI/shared/browser.lock`
- work-item ledger: `~/Library/Application Support/NormanAI/shared/crunchbase-budget.json`

State and immutable receipts live under
`~/Library/Application Support/NormanAI/Research/crunchbase-funding-watcher/`.
The exact durable artifacts are:

- `ledger.json` — event lifecycle and completed slots
- `receipts/<runId>.json` — immutable detector receipt
- `handoffs/<runId>.request.json` — typed request handed to Core
- `handoffs/<runId>.result.json` — durable Core result
- `latest.json` — replaceable summary of the latest run
- `migration-receipt.json` — proof of the read-only legacy import

An event moves `observed → handoff_pending → terminal` only after a validated
Core result is durable. `retryable` remains nonterminal and is eligible for a
later handoff. Core terminal results are `created`, `queued_existing`,
`duplicate_event`, `rejected_identity`, or `ambiguous_review`.

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

The automation runs from the permanent Research checkout only after both
repositories match GitHub and cross-repository acceptance passes. The former
macOS LaunchAgent is intentionally uninstalled: macOS privacy controls blocked
it from reading the checkout under `Documents`. Its state and logs remain
preserved. Do not install a second scheduler beside the Codex automation.

## Broader daily discovery

One command does the whole day:

```bash
python3 scripts/daily.py --write --yes --promote
```

It walks both lanes, merges everything, writes **one** brief, and creates the
Notion rows through `crm_intake.py`. That's the thing to schedule.

---

## Where it has to run

The two lanes have different requirements, and that decides the host.

| Lane | Needs | Runs on |
|------|-------|---------|
| Grok search (9 lanes) | `XAI_API_KEY` | Anywhere — Mac, CI, a server |
| Crunchbase + Substack | JD's logged-in Chrome | The Mac, only |
| Promotion to Notion | crm-core checkout + `NOTION_TOKEN` | The Mac, only |

So the **full** run — both lanes, writing to Notion — lives on the Mac,
alongside the Codex jobs that already drive the enrichment lanes. `daily.py`
skips the browser half automatically when Chrome isn't reachable, which is why
the same command works in CI without a separate script.

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

Working directory is the `NormanAI-research` checkout. It needs `XAI_API_KEY`
and `NOTION_TOKEN` in the environment — the loader reads the same `.env` ladder
crm-core uses, so one file on the host serves both repos.

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
