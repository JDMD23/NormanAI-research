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

## 3. Research qualifies; crm-core scores

There is no score in this repo. Qualification is binary — did we find enough to
justify creating a row — and it is answered by rules in `scripts/lib/qualify.py`,
not by a number.

| | Qualification | Fit Score |
|---|---|---|
| Owner | this repo | crm-core scoring lane |
| Question | "does this belong in the CRM at all?" | "how good a prospect is it?" |
| Inputs | keyword hits, NYC evidence, sector, identifiability | headcount, jobs, funding, investors, industry |
| Written to Notion | **never** | yes, with FS\_\* components |

An earlier version of this repo predicted Fit Score to rank candidates before
emission. That was removed deliberately. Ranking at discovery time competes with
the scoring lane using worse inputs — research sees a press mention, the lanes
see measured NYC headcount — and any threshold it applies silently withholds
companies the real scorer never got to judge. Find, qualify, hand over.

## 3a. The two postures

| Mode | Posture | NYC evidence required |
|---|---|---|
| `funding` | broad | no |
| `office_expansion` | tight | yes — strong or moderate |
| `hiring_growth` | tight | yes — strong or moderate |
| `founder_language` | tight | yes — strong or moderate |

Broad exists because funding events are cheap to over-collect and expensive to
miss: an extra row costs one enrichment pass, a missed round costs a deal.
Tight exists because the opposite is true for space signals — a company wrongly
flagged as needing an office wastes a call and JD's credibility.

`hiring_growth` additionally needs one of: `minNycRoles` open NYC roles, a
senior NYC hire, or a stated team build-out. "Hiring in NYC" with two roles is
not a real estate event.

## 4. Unknown ≠ 0

Inherited from crm-core, enforced at the point of capture:

- The Grok response schema makes every fact nullable. There is no default.
- The prompt says so explicitly, twice.
- `csv_row()` writes `""` for a null number, never `0`.
- Tests pin it (`test_unknown_numbers_are_blank_not_zero`).

A blank funding amount means "we didn't find one" and the Crunchbase lane will
fill it. A `0` means "this company has raised nothing", which is a claim, and a
wrong one. The second is far more expensive than the first.

## 5. Evidence discipline

Three receipts, each enforced in `qualify.py`:

- **`keyword_hits`** — the exact qualifying phrases found in a source. No
  phrase, no candidate, in either posture.
- **`source_urls`** — links actually used. Empty is always a reject.
- **`nyc_evidence`** — quoted or closely paraphrased. `nyc_angle` (surfaced as
  `fitHint`) is judged **only** from it. A model claiming "strong" with no
  evidence is clamped to "none".

This exists because the failure mode of an LLM sourcing agent is confident
plausibility: it will happily return well-known companies that have nothing to
do with New York, and assert a NYC angle it cannot show. Clamping makes an
unsupported claim structurally impossible rather than merely discouraged.

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
run is 9 lanes = 9 search calls. At the 5×/weekday schedule that is roughly 950
search calls a month — under $5, plus tokens. `maxSearchesPerRun` is the ceiling
that keeps a runaway loop from becoming a bill.

## 9. Merging duplicates keeps the more specific claim

One company routinely appears in several lanes — the funding sweep sees the
round, the office lane sees the lease. `dedupe_within()` collapses them on
crm-core's identity keys and keeps the **tighter mode**, because that is the
more actionable fact, while unioning both sets of `keyword_hits` and
`source_urls` and taking the strongest NYC angle seen anywhere.

Blank fields are filled from the duplicate rather than lost. A merge that
discards a fact one lane found is a silent data loss, and the merged row is the
only one that reaches intake.

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

1. qualification — broad or tight rules, per §3a
2. the seen-store — a company is never handed over twice
3. the read-only board pre-filter — skip what is already there
4. `promote.maxPerRun` — a bad batch is a small mess
5. `crm_intake.py`'s hard dedup — the final authority
6. everything lands at `Status=Research`, a machine status, so the score agent
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
