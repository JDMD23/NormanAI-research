# NormanAI Research

Discovery arm of NormanAI. Finds companies that are growing, raising, hiring in
NYC, or under office-space pressure, and hands them to `NormanAI-crm-core`.

```
Grok x_search + web_search  →  qualify  →  dedup  →  intake CSV  →  crm_intake.py  →  Norman CRM Core
        (this repo)                                                 (crm-core)          (Notion)
```

**Research finds and qualifies. It does not score.** Rows land at
`Status=Research`; crm-core's Crunchbase, Careers, and LinkedIn lanes enrich
them, and its score agent scores them. This repo never writes Notion —
`crm_intake.py` is the single writer and owns hard dedup.

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

```bash
export XAI_API_KEY=...          # required
export NOTION_TOKEN=...         # optional: read-only pre-filter against the board
```

Or drop them in `.env` — same loader ladder as crm-core, so one file serves both.

Verify the API before trusting a batch:

```bash
python3 scripts/research_probe.py
```

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
python3 scripts/research_browse.py --source cb_nyc_rounds --dry-run # browser only
python3 scripts/daily.py --dry-run --show-rejects                   # why things were filtered
```

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
| `scripts/lib/qualify.py` | The broad/tight rules |
| `scripts/lib/discover.py` | Lane execution and the two prompts |
| `scripts/lib/grok.py` | Agent Tools client (stdlib only) |
| `scripts/lib/identity.py` | Dedup keys, mirroring crm-core's normalisation |

## Scheduling

The full run needs this repo, the crm-core checkout, `NOTION_TOKEN`, and your
logged-in Chrome — all on the Mac. One Codex registry entry:

```
research-daily   30 6 * * 1-5   python3 scripts/daily.py --write --yes --promote
```

CI runs the search half twice a weekday **without** `--promote` and uploads the
brief as an artifact — a free canary that can't touch the board.

Full detail, including exit codes and how this orders against the enrichment
lanes: **`docs/scheduling.md`**.

## Rules

- Research qualifies; crm-core scores. Never write Fit Score, Status, or any
  JD-owned field.
- Never write Notion from here.
- Unknown ≠ 0.
- No source URL, no candidate. No keyword hit, no candidate.
- Tight modes stay tight — if one starts returning 25 companies, it isn't tight
  any more.
- Biotech and therapeutics are excluded outright; crypto is flagged, not dropped.
