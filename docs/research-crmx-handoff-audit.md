# Research → CRMx handoff honesty audit

**Scope:** NormanAI-research as a **supplement** to NormanAI-CRMx (never a
parallel CRM). Thin honesty pass only — no reweighting of qualify rules.

**Ruling confirmed in code:** CRMx is the sole SoR. Ordinary `--promote` and
funding-watcher handoff default to `norman.tools.ingest_csv` + evidence
sidecar, fail closed on `NORMAN_CRMX_PATH` / `NORMAN_CRMX_DB` / missing
evidence, and never write Notion Fit/Status.

Companion docs: `docs/crmx-handoff-migration.md`,
`docs/research-operating-contract.md`, `config/crmx-compatibility.json`.

---

## Ranked findings

| Rank | ID | Area | Verdict | Action |
|------|----|------|---------|--------|
| P0 | F1 | Funding result synthesis | Adapter invents terminal `created` + synthetic `pageId` after ingest_csv | **Report only** — needs CRMx typed funding CLI / JD |
| P1 | F2 | Promote preflight | `_crmx_module_present` treated `core/intake.py` as proof of `ingest_csv` (substring `"intake"`) | **Fixed this PR** + test |
| P2 | F3 | Dead config | `crmCore.intake` / `crmCore.fundingHandoff` declared, never read | Report only |
| P2 | F4 | Stale narrative | Docs/comments still say Notion write, `Status=Research`, or crm-core scores Fit | Report only (no JD) |
| P3 | F5 | Seen-store vs promote | `mark_emitted` runs at CSV write, before promote success | Intentional anti-spin; report tradeoff |
| OK | F6 | Notion Fit/Status writes | No mutation client; AST contract tests pin it | Clean |
| OK | F7 | Unknown ≠ 0 on qualify/emit | Null money/counts stay blank in CSV + funding adapter | Clean |
| OK | F8 | Promote / funding default target | `crmx`; legacy only behind explicit flags | Clean |

---

## 1. Promote / funding-watcher → CRMx ingest_csv + evidence

### Ordinary promote (`scripts/lib/sinks.py`, `scripts/lib/pipeline.py`)

| Check | Status |
|-------|--------|
| Default target `crmx` (`promote.target`, off unless `--promote`) | Pass |
| Invokes `uv run python -m norman.tools.ingest_csv <csv> <db> --added-from … --evidence …` | Pass |
| Requires `NORMAN_CRMX_PATH` / `crmx.path` directory | Pass (fail closed) |
| Requires `NORMAN_CRMX_DB` / `crmx.dbPath` | Pass (fail closed) |
| Requires evidence sidecar path + file | Pass (fail closed) |
| Refuses dry-run against ingest (no verified flag) | Pass |
| Unknown `promote.target` fails closed | Pass |
| Legacy only via `--promote-legacy-crm-core` / `legacy_crm_core` | Pass |
| Pipeline always writes `crmx-intake-*.csv` + `evidence-*.json` on `--write` | Pass |
| Cap path rewrites promote CSV **and** evidence before call | Pass |

### Funding watcher (`scripts/lib/funding_handoff.py`)

| Check | Status |
|-------|--------|
| `handoffTarget` / env default `crmx` | Pass |
| Typed `norman.research.funding_handoff.v1` still written for ledger | Pass |
| Adapter → CRMx CSV + `norman.research.crmx_evidence.v1` → `promote_via_crmx` | Pass |
| Dry-run preflights CRMx config then synthesizes preview (no ingest call) | Pass |
| Legacy only via `--handoff-legacy-crm-core` | Pass |
| Non-USD amounts blank in CSV (no invented FX) | Pass |

### F1 — Result synthesis (P0, JD / CRMx follow-up)

After successful `ingest_csv`, Research synthesizes
`norman.crm_core.funding_handoff_result.v1` with every event
`state=created` and `pageId=crmx:ingest:<eventKey>`. Documented in
`config/crmx-compatibility.json`. This is **not** a second SoR write, but it
**is** an honesty gap: ingest_csv does not return per-event terminal states
(`queued_existing`, `duplicate_event`, …). Closing the ledger as `created`
can over-claim.

**Do not “fix” from Research without a typed CRMx funding CLI.** Preferred
shape already noted in `docs/crmx-handoff-migration.md`.

### F2 — Module preflight was fail-open (fixed)

`_crmx_module_present` accepted `(root / "core" / "intake.py")` when
`"intake" in module`. The configured module is `norman.tools.ingest_csv`, so
the substring matched and any decoy `core/intake.py` satisfied preflight.
The LaunchAgent helper already omitted that branch.

**Fix:** remove the decoy branch; align with
`install_funding_watcher_launch_agent._crmx_module_present`. Covered by
`test_promote_via_crmx_does_not_treat_core_intake_as_crmx_module`.

---

## 2. Notion Fit/Status writes and crm-core-as-default

### Runtime (clean)

- No Notion page create/update/PATCH client in `scripts/`.
- `notion_existing_keys()` is read-only DB query; fails open to empty set.
- No Fit score field emitted; evidence note says “Not a Fit score.”
- `qualify.apply` sets `fit_hint` = clamped `nyc_angle` only (hint, not Fit).
- Promote defaults and funding `handoffTarget` default to `crmx`.

Pinned by `tests/test_intake_contract.py` (AST no-write) and
`tests/test_promote_crmx.py`.

### F4 — Stale narrative (P2, report-only)

These still **say** Research or crm-core owns SoR / Fit / Status even though
code does not:

| Location | Stale claim |
|----------|-------------|
| `docs/grok-automations.md` | “`daily.py` … writes the results into Notion” |
| `docs/scheduling.md` | Intake creates rows at `Status=Research`; artifact table points only at legacy `crm_intake.py` CSV |
| `config/modes.json` note | “Fit Score belongs to crm-core's scoring lane” |
| `scripts/lib/qualify.py` module doc | “crm-core's score agent does that after intake” |
| `scripts/lib/state.py` module doc | “`crm_intake.py` is [the dedup authority]” |
| `tests/test_intake_contract.py` header | “Pin the handoff to NormanAI-crm-core” (legacy CSV pins remain useful) |

**Not fixed here** (wording / ownership narrative — no behavior bug). Prefer a
follow-up doc sync once JD confirms CRMx naming in operator docs.

CI `cross-repository-acceptance` still checks out pinned
`NormanAI-crm-core` via `config/core-compatibility.json` for the funding
ledger contract. That is an explicit legacy pin, not a promote default.

---

## 3. Unknown ≠ 0 / inventing zeros on qualify

| Path | Status |
|------|--------|
| Grok `response_schema()` — money/counts nullable, no default `0` | Pass |
| `Candidate.from_model` — missing → `None` | Pass |
| `csv_row` / `crmx_csv_row` — `None` → `""` | Pass (`test_unknown_numbers_are_blank_not_zero`, promote tests) |
| Funding adapter non-USD / missing minor → blank CSV cells | Pass |
| `qualify()` — no numeric score; rejects on missing evidence, never fills `0` | Pass |
| Hiring `nyc_open_roles_estimate is None` rejects volume path (does not treat as 0 roles for a pass) | Pass |

No Unknown≠0 violation found on the qualify → emit → CRMx handoff path.

Note: a **source-asserted** USD `amountMinor == 0` becomes `"0"` in the CSV.
That is a claim of $0, not unknown — consistent with the operating contract.

---

## 4. Dead config / declared-but-inert paths

### F3 — `crmCore.intake` / `crmCore.fundingHandoff` (P2)

`config/research.json` declares:

```json
"crmCore": {
  "path": "../Core CRM",
  "intake": "scripts/crm_intake.py",
  "fundingHandoff": "scripts/crm_funding_handoff.py",
  ...
}
```

Code only reads `crmCore.path` (plus `NORMAN_CRM_CORE_PATH`). Legacy CLIs are
**hardcoded** to `scripts/crm_intake.py` and `scripts/crm_funding_handoff.py`.
Changing the JSON keys alone does nothing.

**Report only** — removing keys is harmless for runtime but may surprise
operators who treat the JSON as the contract surface. Prefer annotating
`note` or reading the keys in a follow-up.

### Live (not dead)

| Key | Consumer |
|-----|----------|
| `crmx.*` (`path`, `pathEnv`, `dbPath`, `dbPathEnv`, `ingestModule`, `addedFromPattern`, `uvBin`) | `config.py`, `sinks.py`, LaunchAgent installer |
| `promote.target` / `enabled` / `maxPerRun` | `sinks.py`, `pipeline.py` |
| `sink.csv` / `sink.evidence` | `sinks.py` |
| `funding-watcher.json` → `handoffTarget`, `crmResultSchemaVersion`, ceilings | watcher + handoff |
| `crmx-compatibility.json` | contract pin (docs / human) |

### F5 — Emitted-before-promote (P3 tradeoff)

`pipeline.run` calls `state.mark_emitted` when the CSV is written, then may
call promote. If promote fails, the company will not be re-emitted next run
even though CRMx may not have ingested it. Operating contract §7 treats this
as anti-spin (“marks … the moment it lands in a CSV”). Changing order risks
duplicate promote after partial success — **JD call**, not a silent fix.

---

## Constraints respected

- No Sales Nav work
- No careers apply path
- No NormansBrain clone into this repo
- No qualify reweighting

---

## Code change in this PR

1. **`scripts/lib/sinks.py`** — remove fail-open `core/intake.py` branch from
   `_crmx_module_present`.
2. **`tests/test_promote_crmx.py`** — pin that a decoy `core/intake.py` fails
   closed without the real `norman.tools.ingest_csv` module.

Everything else above is report-only.
