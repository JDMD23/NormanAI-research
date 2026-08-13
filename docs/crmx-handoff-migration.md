# Research → CRMx handoff migration

**Ruling:** NormanAI-CRMx is the sole system of truth. This repo is a
discovery/qualify supplement only — it never scores Fit and never writes Notion
as SoR.

## What changed

| Path | Before | After |
|------|--------|-------|
| Ordinary `--promote` | `crm_intake.py` in NormanAI-crm-core | `uv run python -m norman.tools.ingest_csv` in NormanAI-CRMx |
| Funding watcher handoff | `crm_funding_handoff.py` in NormanAI-crm-core | CRMx-shaped CSV drop at `/Users/normanai/Drops/crunchbase`; Pipeline/CRMx owns ingest/score |
| Evidence | JSON sidecar (unaudited by Core) | Versioned sidecar `norman.research.crmx_evidence.v1` always passed via `--evidence` |
| Notion | Never written from Research | Still never written; read-only prefilter optional |
| Promote default | off (`promote.enabled: false`) | still off |

## Example commands

Discover only (CSV + evidence, no SoR write):

```bash
python3 scripts/daily.py --write --yes
```

Promote into CRMx (fail-closed until path + DB + evidence sidecar are set):

```bash
export NORMAN_CRMX_PATH=/Users/normanai/Projects/NormanAI-CRMx   # optional; also crmx.path default
export NORMAN_CRMX_DB=/absolute/path/to/norman.sqlite
python3 scripts/daily.py --write --yes --promote
```

Equivalent manual CRMx call printed by Research when `--promote` is omitted:

```bash
cd "$NORMAN_CRMX_PATH"
uv run python -m norman.tools.ingest_csv \
  /path/to/research/out/crmx-intake-YYYY-MM-DD.csv \
  "$NORMAN_CRMX_DB" \
  --added-from research:YYYY-MM-DD \
  --evidence /path/to/research/out/evidence-YYYY-MM-DD.json
```

Legacy crm-core shim (temporary, explicit only):

```bash
export NORMAN_CRM_CORE_PATH=/absolute/path/to/Core\ CRM
python3 scripts/daily.py --write --yes --promote-legacy-crm-core
```

## Evidence contract (do not drop)

Research always writes:

1. `out/crmx-intake-*.csv` — Crunchbase-export headers for ingest_csv
2. `out/evidence-*.json` — `norman.research.crmx_evidence.v1` carrying
   `nyc_evidence`, `source_urls`, `keyword_hits`, `signal_notes`, mode/lane

When `--promote` targets CRMx, Research **always** passes the sidecar:

```text
uv run python -m norman.tools.ingest_csv <csv> <db> --added-from <label> --evidence <path.json>
```

Promote fails closed if the sidecar path is unset or the file is missing.
Research will **not** invent Notion writes or Fit scores to preserve evidence.

Pin of the public surface: `config/crmx-compatibility.json`.

## Funding watcher (CSV drop; Pipeline/CRMx cooks)

Primary path (Mac live / fleet audit 2026-08-12):

```text
Chrome saved-search extract (today ET only)
  → typed funding_handoff.v1 + full Crunchbase CSV
  → /Users/normanai/Drops/crunchbase/crunchbase-watcher-<date>-<runId>.csv
  → CRMx funding-drop LaunchAgent (com.normanai.crmx.funding-drop) / Pipeline
     owns funding_ingest, reconcile, and score
```

Research stops at the CSV. It does not call `funding_ingest`, `score_batch`,
or `reconcile_sweep --apply`. The prior auto-score JD exception is revoked.
Notion is projection only via Pipeline/CRMx. There is one drop directory,
aligned with CRMx `config/mac-paths.json`.

```bash
export NORMAN_CRMX_FUNDING_DROP=/Users/normanai/Drops/crunchbase   # optional; also crmx.fundingDropDir
python3 scripts/funding_watcher.py check --dry-run
python3 scripts/funding_watcher.py check --write --yes
```

Schedule: weekdays **09:00 / 12:00 / 15:00 / 18:00 ET**. Source:
`main-funding-august-2026/730c458b-…`.

Mac live Research may be `~/Documents/NormanAI-research` with CRMx at
`/Users/normanai/Projects/NormanAI-CRMx` (not a Documents sibling). The funding
CSV drop is `/Users/normanai/Drops/crunchbase` (`NORMAN_CRMX_FUNDING_DROP`
overrides). This GitHub repo's `scripts/` + `config/` remain the source of
truth.

## Fail-closed rules

Ordinary `--promote` raises (and does not write Notion) when:

- `NORMAN_CRMX_PATH` / `crmx.path` is missing or not a directory
- `norman.tools.ingest_csv` (or `tools/ingest_csv.py`) is absent in that checkout
- `NORMAN_CRMX_DB` / `crmx.dbPath` is unset
- evidence sidecar path is missing or the file does not exist
- `uv` is not on PATH
- `promote.target` is anything other than `crmx` or `legacy_crm_core`
- dry-run promote is requested against CRMx ingest itself (no verified dry-run flag)

Funding watcher `--write` raises when:

- `NORMAN_CRMX_FUNDING_DROP` / `crmx.fundingDropDir` is missing or not absolute
- the drop CSV for that `runId` already exists (refuse overwrite)
- `handoffTarget` is anything other than `crmx` or `legacy_crm_core`

Funding watcher dry-run writes a preview CSV next to the request and does not
touch the live drop.
