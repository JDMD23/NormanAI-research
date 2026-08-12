# Research → CRMx handoff migration

**Ruling:** NormanAI-CRMx is the sole system of truth. This repo is a
discovery/qualify supplement only — it never scores Fit and never writes Notion
as SoR.

## What changed

| Path | Before | After |
|------|--------|-------|
| Ordinary `--promote` | `crm_intake.py` in NormanAI-crm-core | `uv run python -m norman.tools.ingest_csv` in NormanAI-CRMx |
| Evidence | JSON sidecar (unaudited by Core) | Versioned sidecar `norman.research.crmx_evidence.v1` (still not consumed by CSV ingest) |
| Notion | Never written from Research | Still never written; read-only prefilter optional |
| Promote default | off (`promote.enabled: false`) | still off |

## Example commands

Discover only (CSV + evidence, no SoR write):

```bash
python3 scripts/daily.py --write --yes
```

Promote into CRMx (fail-closed until path + DB are set):

```bash
export NORMAN_CRMX_PATH=/absolute/path/to/NormanAI-CRMx
export NORMAN_CRMX_DB=/absolute/path/to/norman.sqlite
python3 scripts/daily.py --write --yes --promote
```

Equivalent manual CRMx call printed by Research when `--promote` is omitted:

```bash
cd "$NORMAN_CRMX_PATH"
uv run python -m norman.tools.ingest_csv \
  /path/to/research/out/crmx-intake-YYYY-MM-DD.csv \
  "$NORMAN_CRMX_DB" \
  --added-from research:YYYY-MM-DD
# Keep out/evidence-YYYY-MM-DD.json — ingest_csv does not read it today.
```

Legacy crm-core shim (temporary, explicit only):

```bash
export NORMAN_CRM_CORE_PATH=/absolute/path/to/Core\ CRM
python3 scripts/daily.py --write --yes --promote-legacy-crm-core
```

## Evidence contract (do not drop)

CRMx's verified public intake is **CSV-only** today
(`norman.tools.ingest_csv`). Research therefore always writes:

1. `out/crmx-intake-*.csv` — Crunchbase-export headers for ingest_csv
2. `out/evidence-*.json` — `norman.research.crmx_evidence.v1` carrying
   `nyc_evidence`, `source_urls`, `keyword_hits`, `signal_notes`, mode/lane

CRMx must grow an adapter (or extend ingest) to load that sidecar into the
SQLite SoR. Research will **not** invent Notion writes to preserve evidence.

Pin of the public surface: `config/crmx-compatibility.json`.

## Funding watcher (not retargeted)

The strict Crunchbase funding watcher still uses
`norman.research.funding_handoff.v1` → legacy `crm_funding_handoff.py`. No public
CRMx funding-handoff CLI was verified from this agent (CRMx repo returned 404
to the token). That path stays on the legacy shim until CRMx publishes a
replacement; do not guess a Notion writer.

## Fail-closed rules

Promote raises (and does not write Notion) when:

- `NORMAN_CRMX_PATH` / `crmx.path` is missing or not a directory
- `norman.tools.ingest_csv` (or `tools/ingest_csv.py`) is absent in that checkout
- `NORMAN_CRMX_DB` / `crmx.dbPath` is unset
- `uv` is not on PATH
- `promote.target` is anything other than `crmx` or `legacy_crm_core`
- dry-run promote is requested against CRMx (no verified dry-run flag)
