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

```bash
python3 scripts/research_run.py --dry-run                     # all four modes
python3 scripts/research_run.py --mode funding --dry-run      # one mode
python3 scripts/research_run.py --dry-run --show-rejects      # see what was filtered and why
python3 scripts/research_run.py --write --yes                 # emit the intake CSV
python3 scripts/research_run.py --write --yes --promote       # ...and create the rows
python3 scripts/research_run.py --write --yes --promote --loop
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
| `scripts/research_run.py` | The run: discover → qualify → dedup → emit → promote |
| `scripts/research_probe.py` | One live call to verify the xAI request shape |
| `scripts/lib/qualify.py` | The broad/tight rules |
| `scripts/lib/discover.py` | Lane execution and the two prompts |
| `scripts/lib/grok.py` | Agent Tools client (stdlib only) |
| `scripts/lib/identity.py` | Dedup keys, mirroring crm-core's normalisation |

## Scheduling

`.github/workflows/discover.yml` runs 5× each weekday and uploads the CSV as an
artifact — discovery only, no `--promote`, so a bad sweep can't touch the board.

The full discover→promote loop needs this repo, the crm-core checkout, and
`NOTION_TOKEN` co-located, which is the Mac. It belongs in the Codex registry
next to the other lanes:

```
research-discover   python3 scripts/research_run.py --write --yes --promote
```

## Rules

- Research qualifies; crm-core scores. Never write Fit Score, Status, or any
  JD-owned field.
- Never write Notion from here.
- Unknown ≠ 0.
- No source URL, no candidate. No keyword hit, no candidate.
- Tight modes stay tight — if one starts returning 25 companies, it isn't tight
  any more.
- Biotech and therapeutics are excluded outright; crypto is flagged, not dropped.
