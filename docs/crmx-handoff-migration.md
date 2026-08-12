# Research → CRMx handoff migration

**Ruling:** NormanAI-CRMx is the sole system of truth. This repo is a
discovery/qualify supplement only — it never scores Fit and never writes Notion
as SoR.

## What changed

| Path | Before | After |
|------|--------|-------|
| Ordinary `--promote` | `crm_intake.py` in NormanAI-crm-core | `uv run python -m norman.tools.ingest_csv` in NormanAI-CRMx |
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
- evidence sidecar path is missing or the file does not exist
- `uv` is not on PATH
- `promote.target` is anything other than `crmx` or `legacy_crm_core`
- dry-run promote is requested against CRMx (no verified dry-run flag)
