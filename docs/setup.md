# Setup — the two things you have to do

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

## 2. Crunchbase saved search

The repo ships with a generic Crunchbase discover URL as a placeholder. Your own
saved search will be far better than anything guessable — the filters are the
entire value of this lane.

### Build the search

On `crunchbase.com`, filter for what you actually want to see. A good starting
point for tenant rep:

- **Location** — New York, New York (or the NY metro)
- **Announced date** — last 30 days
- **Funding type** — Series A, Series B, Series C
- **Sort** — most recent first

Then **Save** it and give it a name — `NormanAI-research` works.

### Get the URL into the repo

Open the saved search, copy the URL from the address bar, and paste it into
`config/browse.json`:

```jsonc
{
  "id": "cb_nyc_rounds",
  "enabled": true,
  "label": "Crunchbase — recent NYC funding rounds",
  "url": "https://www.crunchbase.com/lists/…"   // ← paste yours here
}
```

That's the only change. No code, no restart.

### Two searches are better than one

The `cb_nyc_companies` entry is in the config already, disabled. A second saved
search with different filters — a wider date range, or Seed and Series A only —
gives the lane a second angle. Paste its URL, flip `enabled` to `true`.

### Why this lane exists

**Discovery only.** It finds companies that are not yet on your board.

Filling in funding details on companies already there stays with crm-core's
`crm_crunchbase_agent.py`. The two never write the same field, and they take
turns at the one Chrome window through a shared file lock.

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
python3 scripts/daily.py --write --yes --promote
```

Then schedule it — see `docs/scheduling.md`.
