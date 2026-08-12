# xAI Agent Tools — what this repo assumes

## The migration that matters

The Live Search API — search configured via a `search_parameters` block on a
chat-completions request — **was retired on 2026-01-12** and now returns
HTTP 410. Any integration still using `search_parameters` is dead, including
older Make/n8n/LangChain nodes.

The replacement is the **Agent Tools API**: you attach `x_search` and/or
`web_search` as *tools*, and xAI runs the search loop server-side — analysing
the question, issuing searches, reading results, following up, and returning a
composed answer with citations. No retrieval pipeline, rate-limit handling, or
scraping on our side.

## What this repo sends

Built in `scripts/lib/grok.build_request()`:

```jsonc
{
  "model": "grok-4.6",
  "messages": [{ "role": "user", "content": "…sourcing prompt…" }],
  "tools": [
    {
      "type": "x_search",
      "allowed_x_handles": ["unionsquareventures", "…"],   // max 20
      "from_date": "2026-07-19",
      "to_date": "2026-07-26"
    },
    { "type": "web_search", "from_date": "…", "to_date": "…" }
  ],
  "temperature": 0,
  "response_format": { "type": "json_schema", "json_schema": { … } }
}
```

Known parameter facts:

| Parameter | Notes |
|-----------|-------|
| `allowed_x_handles` | Max **20** handles. Cannot be combined with `excluded_x_handles` in the same request. |
| `excluded_x_handles` | Mutually exclusive with the above. |
| `from_date` / `to_date` | ISO dates bounding the search window. |
| Pricing | ~**$5 per 1,000** search calls for each of `x_search` and `web_search`, on top of model tokens. |

`config/sources.json` splits handles into lanes partly for search focus and
partly to stay under the 20-handle cap; `build_request()` truncates as a
backstop rather than letting an oversized list fail the call.

## Honest caveat about this document

`docs.x.ai` returns **403 to every automated fetcher**, so the request shape
above was assembled from xAI's migration notices and secondary documentation —
not read off the official reference. It has not been executed against the live
API from this environment, because that needs JD's key.

This is why `scripts/research_probe.py` exists. It makes one cheap real call and
prints the request, the raw response, the extracted citations, and a verdict:

```bash
python3 scripts/research_probe.py          # both tools
python3 scripts/research_probe.py --raw    # full payload
```

Everything shape-specific is confined to `build_request()`, `message_text()`,
and `citations()` in `scripts/lib/grok.py`. If the probe fails, those three
functions are the only places to fix — the rest of the pipeline is shape-blind.

Failure modes the probe distinguishes:

| Symptom | Meaning |
|---------|---------|
| `410 Gone` | Still hitting the retired Live Search shape. Fix `build_request()`. |
| `4xx` about unknown fields | Tool spec keys are wrong. Fix `build_request()`. |
| Text but no citations | Call works; `citations()` is looking in the wrong place. Re-run `--raw`. |
| Empty text | Response envelope differs. Fix `message_text()`. |

## Structured output

Candidates come back through a strict JSON schema (`candidates.response_schema()`)
rather than prose parsing. Every field is nullable so an unknown arrives as
`null` and stays unknown — see the operating contract, §4.

`grok.extract_json()` tolerates ``` fencing and stray prose as a fallback, since
structured-output enforcement varies by model.

## Model choice

`config/research.json` → `grok.model`, default `grok-4.6` (xAI API model id).
The search tool does the retrieval; the model extracts and filters. Changing
models is a one-line config change.
