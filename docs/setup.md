# Setup — the external things you have to do

Everything else is built. These are the two blockers.

---

## 1. xAI API key

**You do not need an X (Twitter) developer account.** Those are different products:

| | What | Cost | Needed? |
|---|---|---|---|
| X API — `developer.x.com` | Direct tweet access | ~$200/mo for search | **No** |
| xAI API — `console.x.ai` | Grok, incl. `x_search` | Pay-as-you-go | **Yes** |

`x_search` runs on xAI's infrastructure and reads X on your behalf. That is the
whole reason this repo uses Grok rather than scraping X — no developer account,
nothing that risks your X login.

### The part that trips people up

**A Grok subscription is not API access.** Paying for Grok on `grok.com`, or
getting it through X Premium, gives you the chat product. The API is billed
separately, through a different console. Having one does not give you the other.

### Steps

1. Go to **`console.x.ai`** and sign in (same account is fine).
2. **Add credits** under billing. There is no free tier for the search tools.
3. Create an **API key**. Copy it — it is shown once.
4. Put it where the repo will find it:

   ```bash
   echo 'XAI_API_KEY=xai-...' >> ~/.normanai/.env
   ```

   The loader reads the same `.env` ladder crm-core uses, so one file serves
   both repos. `export XAI_API_KEY=...` in your shell works too.

5. Confirm it works — one cheap call, about five seconds:

   ```bash
   python3 scripts/research_probe.py
   ```

   `PASS` means the search tools are live. Anything else prints what to fix and
   which function to fix it in.

### What it costs

`x_search` and `web_search` bill about **$5 per 1,000 calls**, plus tokens.

Nine searches per run, twice a weekday, is roughly **380 calls a month — about
$2**, plus a few dollars of tokens. The browser lane adds no search calls at
all; it only pays tokens, because Grok is reading a page we already fetched.

`caps.maxSearchesPerRun` in `config/research.json` is the ceiling that stops a
bug from becoming a bill.

---

## 2. Approved Crunchbase funding list

The strict watcher is pinned to one source; do not substitute a generic list:

`https://www.crunchbase.com/discover/saved/main-funding-august-2026/730c458b-149c-4a0a-9684-7146e7258993`

Open it in JD's Chrome and confirm it visibly says `Companies`, `NEW AT TOP`
(last funding date newest first), funding after `2026-08-01`, and minimum
amount `$5M`. Scheduled checks hand off only companies funded **today**
(America/New_York). The watcher fails closed on login walls, CAPTCHA, changed
filters, incomplete pages, or result-count drift.

Before the first supervised run:

```bash
export NORMAN_CRMX_PATH=/absolute/path/to/NormanAI-CRMx
export NORMAN_CRMX_DB=/absolute/path/to/norman.sqlite
python3 scripts/funding_watcher.py migrate-legacy-state
python3 scripts/funding_watcher.py check --dry-run
python3 scripts/funding_watcher.py check --write --yes
```

Migration copies the preserved 189-event Core ledger without changing it. The
first command is idempotent and refuses any count, source, schema, or key
mismatch. Default handoff targets CRMx `funding_ingest` → `reconcile_sweep` →
narrow `score_batch` with `--added-from crunchbase-watcher:<date>`.

After Research and CRMx are merged into permanent checkouts and acceptance is
clean, activate the Codex automation `research-crunchbase-funding-watcher` for
09:00, 12:00, 15:00, and 18:00 New York time on weekdays. It must run only the
guarded `check --write --yes --enforce-schedule` command and must verify the
CRMx checkout + DB before browser work. The legacy LaunchAgent remains
uninstalled so there is exactly one scheduler. Mac live checkouts may live
under `~/Documents/NormanAI-research` — this repo remains the source of truth.

---

## 3. Required cross-repository CI

Research's release-safety suite checks the real Core CLI, shared browser lock,
shared v4 55-work-item ledger, and schema constants. CI checks out the exact Core
commit declared in `config/core-compatibility.json`; when
`NORMAN_REQUIRE_CROSS_REPO=1`, a missing Core checkout is a test failure rather
than a skip.

Create an SSH deploy key for `JDMD23/NormanAI-crm-core` and leave **Allow write
access** disabled. Store the private key as this Research Actions secret:

- `CRM_CORE_READONLY_DEPLOY_KEY`

The workflow uses the key only for the pinned Core checkout, disables
credential persistence, and never passes it to test code. The test process
receives only the local Core checkout path. Rotation is manual because deploy
keys are long-lived: replace the Core public key and Research secret together,
then rerun the mandatory job before removing the old key.

In Research branch protection, make the
`cross-repository-acceptance` job a required status check. This repository
change cannot create the deploy key, set the secret, or change branch
protection; an administrator must complete those actions before relying on the
gate. A compatible Core change requires a reviewed update to the pinned
40-character commit and the two declared schema versions.

---

## Then

```bash
python3 scripts/daily.py --dry-run
```

Reads everything, writes nothing, prints the brief. That first run is where you
find out whether the searches return companies you'd actually call — and tuning
that is `config/sources.json` and `config/modes.json`, not code.

When the brief looks right:

```bash
export NORMAN_CRMX_PATH=/absolute/path/to/NormanAI-CRMx
export NORMAN_CRMX_DB=/absolute/path/to/norman.sqlite
python3 scripts/daily.py --write --yes --promote
```

Promote fails closed until CRMx path + DB are set. See
`docs/crmx-handoff-migration.md`. Then schedule it — see `docs/scheduling.md`.
