# NormanAI Research

Discovery arm of NormanAI. Finds companies that may soon need New York City
office space, and hands them to `NormanAI-crm-core` for intake.

```
Grok x_search + web_search  →  score  →  dedup  →  intake CSV  →  crm_intake.py  →  Norman CRM Core
        (this repo)                                                (crm-core)         (Notion)
```

**This repo never writes Notion.** `NormanAI-crm-core/scripts/crm_intake.py` is
the single writer to Norman CRM Core and owns hard dedup. Research emits a CSV
in that script's exact column schema; intake decides what becomes a row.

## Why Grok and not a scraper

X data comes from xAI's **Agent Tools API** (`x_search`), using JD's paid Grok
account. No headless browser, no scraped session, nothing that violates X's
terms or risks the account. It is also the only NormanAI lane that needs no
logged-in Chrome — which is why it can run in CI rather than on the Mac.

The legacy Live Search API (`search_parameters`) was retired 2026-01-12 and now
returns HTTP 410. See `docs/grok-agent-tools.md`.

## Setup

```bash
export XAI_API_KEY=...          # required
export NOTION_TOKEN=...         # optional: read-only pre-filter against the board
```

Or drop them in `.env` — the loader uses the same ladder as crm-core, so one
file on the cron host serves both repos.

Verify the API before trusting a batch:

```bash
python3 scripts/research_probe.py
```

## Run

```bash
python3 scripts/research_run.py --dry-run              # see what it would emit
python3 scripts/research_run.py --lane x --dry-run     # X only
python3 scripts/research_run.py --write --yes          # emit the intake CSV
python3 scripts/research_run.py --write --yes --loop   # batch → rest → repeat
```

Then hand it over:

```bash
cd ../NormanAI-crm-core
python3 scripts/crm_intake.py --csv ../NormanAI-research/out/research-intake-2026-07-26.csv --dry-run
python3 scripts/crm_intake.py --csv ../NormanAI-research/out/research-intake-2026-07-26.csv --write --yes
```

Intake creates rows at `Status=Research` with the Need-\* markers ON, and the
existing CB → Careers → LinkedIn → Score lanes take it from there.

## What counts as a lead

`config/signals.json`. The strongest tells, in order: a stated NYC office
opening, an explicit "we've outgrown this space", a priced round of $5M+, and a
**workplace leadership hire** — someone whose actual job is the office.

Each candidate needs `nyc_proof`: concrete evidence of NYC presence or intent.
No proof is a −40 penalty, which alone drops most candidates below the emit
threshold. Guessing is worse than missing.

**Signal Strength is not Fit Score.** Fit Score belongs to crm-core's scoring
lane. This repo must never write it — see `docs/research-operating-contract.md`.

## Layout

| Path | What |
|------|------|
| `config/research.json` | Model, caps, sink, dedup |
| `config/signals.json` | Signal taxonomy and weights |
| `config/sources.json` | X handle lanes and web query lanes |
| `scripts/research_run.py` | The run: discover → score → dedup → emit |
| `scripts/research_probe.py` | One live call to verify the xAI request shape |
| `scripts/lib/grok.py` | Agent Tools client (stdlib only) |
| `scripts/lib/candidates.py` | Candidate model, response schema, scoring |
| `scripts/lib/identity.py` | Dedup keys, mirroring crm-core's normalisation |
| `scripts/lib/sinks.py` | CSV + Supabase. Deliberately no Notion writer. |
| `sql/001_research_candidates.sql` | Supabase staging table |

## Scheduling

`.github/workflows/discover.yml` runs twice each weekday and uploads the CSV as
an artifact. Set `XAI_API_KEY` (and optionally `NOTION_TOKEN`,
`SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`) in repo secrets.

Promotion into the CRM stays manual by design — a discovery run that goes wrong
should cost you a discarded CSV, not a polluted board.

## Rules

- Unknown ≠ 0. A blank field is correct; an invented one corrupts the pipeline.
- Never write Notion from here.
- Never write Fit Score, Status, or any JD-owned field.
- No NYC proof, no candidate.
- Biotech and therapeutics are excluded outright; crypto is flagged, not dropped
  — the score agent owns that judgment.
