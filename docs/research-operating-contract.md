# NormanAI Research — Operating Contract

**Role:** discovery only. Find companies, evidence them, hand them over.
**Never:** write Notion, write Fit Score, write Status, touch a JD-owned field.

Companion to `NormanAI-crm-core/docs/crm-core-operating-contract.md`. Where the
two disagree, crm-core wins — it owns the board.

---

## 1. Where research sits

```
research  →  intake CSV  →  crm_intake.py  →  Status=Research, Need-* ON
                                                       ↓
                                     Crunchbase → Careers → LinkedIn → Score
```

Research is upstream of intake and nothing else. It has no opinion about what
happens after a row is created, and it never revisits a company it has handed
over — that is the enrichment lanes' job.

## 2. The one-writer rule

`crm_intake.py` is the only thing that writes Norman CRM Core. This is not a
style preference:

- Intake holds the hard-dedup index (domain · LinkedIn · Crunchbase · normalised
  name) built from the live board. A second writer cannot see that index and
  will create duplicates.
- Intake seeds the Need-\* markers in the exact combination the lanes expect. A
  row created any other way is invisible to the daily loop or thrashes it.
- The strangler blueprint's failure mode is two boards fighting. Two writers to
  one board is the same bug, one level down.

If research ever needs the CRM to change, the answer is to emit a CSV or extend
`crm_intake.py` — never to open a Notion client here. `scripts/lib/sinks.py` has
no Notion write path, and that absence is deliberate.

## 3. Signal Strength ≠ Fit Score

| | Signal Strength | Fit Score |
|---|---|---|
| Owner | this repo | crm-core scoring lane |
| Question | "is this worth handing over at all?" | "how good a prospect is it?" |
| Inputs | search-time signals + NYC proof | headcount, jobs, funding, investors, industry |
| Written to Notion | **never** | yes, with FS\_\* components |

Signal Strength dies at the repo boundary. It appears in the evidence sidecar
and the Supabase staging table, and nowhere else. Emitting it into the CSV
would put a discovery guess in front of the scoring lane's real work.

## 4. Unknown ≠ 0

Inherited from crm-core, enforced at the point of capture:

- The Grok response schema makes every fact nullable. There is no default.
- The prompt says so explicitly, twice.
- `csv_row()` writes `""` for a null number, never `0`.
- Tests pin it (`test_unknown_numbers_are_blank_not_zero`).

A blank funding amount means "we didn't find one" and the Crunchbase lane will
fill it. A `0` means "this company has raised nothing", which is a claim, and a
wrong one. The second is far more expensive than the first.

## 5. NYC proof is mandatory

Every candidate carries `nyc_proof` — a concrete statement of NYC presence or
NYC intent from a source actually read. Missing proof is −40, which alone drops
a candidate below the emit threshold of 35.

This exists because the failure mode of an LLM sourcing agent is confident
plausibility: it will happily return well-known companies that have nothing to
do with New York. The penalty makes the absence of evidence expensive rather
than invisible.

## 6. Lane outcomes

Mirrors crm-core's three outcomes:

| Outcome | Meaning | What happens |
|---------|---------|--------------|
| `success` | Lane ran, returned candidates (possibly zero) | Continue |
| `retry` | Transient — timeout, 429, unparseable output | Next run picks it up |
| `blocked` | API contract broken (410 / auth) | **Loop stops immediately** |

A blocked lane stops the loop rather than spinning, because the failure is
structural and every retry costs a Grok search call.

## 7. Anti-spin

Learned from sales-nav §10.6, where a loop wrote the same 6 rows 29 times:

- The SQLite seen-store marks a company `emitted_at` the moment it lands in a
  CSV. It is never handed over twice.
- Two consecutive batches emitting nothing stops the loop.
- `maxSearchesPerRun` caps lane count; `maxCandidatesPerRun` caps the batch.
- When the cap holds candidates back, the run says so out loud rather than
  silently truncating.

## 8. Costs

X search and web search bill per call (~$5 per 1,000) on top of tokens. A full
run is 8 lanes = 8 search calls plus tokens. At the default twice-daily weekday
schedule that is ~350 search calls a month — a couple of dollars. `maxSearchesPerRun`
is the ceiling that keeps a runaway loop from becoming a bill.

## 9. Targeting is delegated, not duplicated

Research predicts a candidate's Fit Score by running crm-core's real
`fit_score.py` as a subprocess against the real `fit-score-weights.json`. It
does not reimplement the bands.

This matters more than it looks. The alternative — a second copy of the scoring
logic living here — would drift the first time JD retunes a weight, and the
drift would be silent: research would keep sourcing against last month's
definition of a good company while the board scored against this month's. A
prediction that is confidently wrong is worse than no prediction.

Consequences to keep in mind:

- Research supplies **estimates** for NYC heads and NYC jobs. They exist to
  triage and are never written to Notion — the LinkedIn and Careers lanes own
  those fields and measure them properly.
- With two of (heads, jobs, funding date) missing, the engine applies its
  `dataBlindCap` of 70. Most research-stage predictions are therefore ceilings,
  not verdicts. Gate at 55 rather than the 60 Prospect threshold to leave room
  for the lanes to revise upward.
- If crm-core is unreachable, `fit_bridge.apply()` degrades to signal strength
  and **says so in the receipt and on stdout**. A silent gate switch would be
  the worst possible failure here.

## 10. Automatic promotion and where it runs

`--promote` invokes `crm_intake.py`. Research calls the writer; it does not
become one. Section 2 still holds in full.

Promotion needs three things co-located: this repo, the crm-core checkout, and
`NOTION_TOKEN`. That is JD's Mac, where the Codex scheduler already runs the
other lanes — so the full discover→promote loop belongs in the Codex registry,
alongside `crm-core-crunchbase` and friends:

| Job id | argv |
|--------|------|
| `research-discover` | `python3 scripts/research_run.py --write --yes --promote` |

CI keeps running discovery **without** `--promote`, as a dry sweep that uploads
a CSV artifact. That gives a free canary: if the Action's candidates look wrong,
the board hasn't been touched.

Guards on promotion, in order:

1. predicted fit ≥ `gate.minPredictedFit`
2. `promote.requireNycEstimate` — no NYC numbers, no auto-add
3. `promote.maxPerRun` — a bad batch is a small mess
4. `crm_intake.py`'s hard dedup — the final authority
5. everything lands at `Status=Research`, a machine status, so the score agent
   can exit it without touching anything JD owns

## 11. Hard forbidden

- Writing Notion from this repo
- Writing Fit Score, Status, Top Pursuit, Priority, or any field crm-core lists
  as JD-owned
- Emitting a candidate with no source URL
- Emitting a candidate with no NYC proof
- Filling an unknown with `0`, `"N/A"`, or a guess
- Scraping x.com directly — the Grok API is the sanctioned path and keeps JD's
  account out of it
