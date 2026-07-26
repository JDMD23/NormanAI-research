"""xAI Grok client — Agent Tools API (`x_search`, `web_search`).

Why this module is deliberately thin and loud
---------------------------------------------
The legacy Live Search API (`search_parameters`) was retired 2026-01-12 and now
returns HTTP 410. The replacement is the Agent Tools API: you attach `x_search`
and/or `web_search` as tools and xAI runs the search loop server-side.

`docs.x.ai` blocks automated fetching, so the exact request shape below was
assembled from xAI's migration notices and secondary documentation rather than
read off the official reference. Everything shape-specific is confined to
`build_request()` and driven by `config/research.json`, so correcting it is a
one-function change. Run `scripts/research_probe.py` before trusting a batch —
it makes one live call and prints exactly what came back.

Stdlib only, matching crm-core's zero-dependency posture on the cron host.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any

from lib.config import require_env_key, research_config


class GrokError(RuntimeError):
    """Any non-recoverable failure talking to the xAI API."""


class GrokDeprecatedAPI(GrokError):
    """The 410 you get from the retired Live Search endpoint."""


def _cfg() -> dict:
    return research_config()["grok"]


def build_request(
    prompt: str,
    *,
    tools: list[str] | None = None,
    allowed_x_handles: list[str] | None = None,
    excluded_x_handles: list[str] | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    response_schema: dict | None = None,
) -> dict[str, Any]:
    """Assemble the Agent Tools request body.

    THIS is the function to fix if xAI's shape differs from what's here.
    `allowed_x_handles` is capped at 20 by the API; we truncate rather than let
    the whole call fail.
    """
    cfg = _cfg()
    want = tools if tools is not None else cfg.get("tools", ["x_search", "web_search"])

    tool_specs: list[dict[str, Any]] = []
    for name in want:
        spec: dict[str, Any] = {"type": name}
        if name == "x_search":
            if allowed_x_handles:
                spec["allowed_x_handles"] = list(allowed_x_handles)[:20]
            elif excluded_x_handles:
                # The API rejects allowed_* and excluded_* together.
                spec["excluded_x_handles"] = list(excluded_x_handles)[:20]
            if from_date:
                spec["from_date"] = from_date
            if to_date:
                spec["to_date"] = to_date
        elif name == "web_search":
            if from_date:
                spec["from_date"] = from_date
            if to_date:
                spec["to_date"] = to_date
        tool_specs.append(spec)

    body: dict[str, Any] = {
        "model": cfg["model"],
        "messages": [{"role": "user", "content": prompt}],
        "tools": tool_specs,
        "temperature": cfg.get("temperature", 0),
    }
    if response_schema is not None:
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": "research_candidates",
                "strict": True,
                "schema": response_schema,
            },
        }
    return body


def call(body: dict[str, Any]) -> dict[str, Any]:
    """POST to xAI and return the parsed response. Retries transient failures."""
    cfg = _cfg()
    key = require_env_key(cfg.get("apiKeyEnv", "XAI_API_KEY"))
    url = cfg["baseUrl"].rstrip("/") + cfg.get("endpoint", "/chat/completions")
    payload = json.dumps(body).encode("utf-8")
    timeout = cfg.get("requestTimeoutSeconds", 240)
    attempts = max(1, int(cfg.get("maxRetries", 3)))

    last_err: Exception | None = None
    for attempt in range(attempts):
        req = urllib.request.Request(
            url,
            data=payload,
            method="POST",
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8")[:2000]
            except Exception:  # noqa: BLE001 - diagnostics only
                pass
            if exc.code == 410:
                raise GrokDeprecatedAPI(
                    "xAI returned 410 Gone. The Live Search `search_parameters` API was "
                    "retired 2026-01-12; this client must use the Agent Tools shape. "
                    f"Server said: {detail}"
                ) from exc
            # 4xx other than 429 is our bug — retrying just burns quota.
            if exc.code != 429 and 400 <= exc.code < 500:
                raise GrokError(f"xAI HTTP {exc.code}: {detail}") from exc
            last_err = GrokError(f"xAI HTTP {exc.code}: {detail}")
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_err = GrokError(f"xAI transport error: {exc}")

        if attempt < attempts - 1:
            time.sleep(2 ** (attempt + 1))

    raise last_err or GrokError("xAI call failed for an unknown reason")


def message_text(response: dict[str, Any]) -> str:
    """Pull assistant text out of a chat-completions response."""
    for choice in response.get("choices") or []:
        content = (choice.get("message") or {}).get("content")
        if isinstance(content, str) and content.strip():
            return content
        # Some tool-augmented responses return content as a list of parts.
        if isinstance(content, list):
            parts = [
                p.get("text", "")
                for p in content
                if isinstance(p, dict) and p.get("type") in {"text", "output_text"}
            ]
            joined = "".join(parts).strip()
            if joined:
                return joined
    return ""


def citations(response: dict[str, Any]) -> list[str]:
    """Best-effort source URL extraction across known response shapes."""
    found: list[str] = []

    def _add(value: Any) -> None:
        if isinstance(value, str) and value.startswith("http"):
            found.append(value)
        elif isinstance(value, dict):
            for k in ("url", "link", "source_url"):
                if isinstance(value.get(k), str) and value[k].startswith("http"):
                    found.append(value[k])

    for key in ("citations", "sources", "search_results"):
        for item in response.get(key) or []:
            _add(item)
    for choice in response.get("choices") or []:
        msg = choice.get("message") or {}
        for key in ("citations", "sources"):
            for item in msg.get(key) or []:
                _add(item)

    seen: set[str] = set()
    ordered: list[str] = []
    for url in found:
        if url not in seen:
            seen.add(url)
            ordered.append(url)
    return ordered


def usage(response: dict[str, Any]) -> dict[str, Any]:
    """Token + search-call usage, for cost receipts. X/web search bill separately."""
    raw = response.get("usage") or {}
    sources = raw.get("num_sources_used")
    if sources is None:
        sources = len(citations(response))
    return {
        "prompt_tokens": raw.get("prompt_tokens"),
        "completion_tokens": raw.get("completion_tokens"),
        "sources_used": sources,
    }


def extract_json(text: str) -> Any:
    """Parse a JSON payload out of model text, tolerating ``` fencing."""
    raw = (text or "").strip()
    if not raw:
        raise GrokError("model returned empty text")
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1]
        if raw.rstrip().endswith("```"):
            raw = raw.rstrip()[: -3]
    raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    # Last resort: slice from the first brace/bracket to its matching tail.
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = raw.find(opener), raw.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(raw[start : end + 1])
            except json.JSONDecodeError:
                continue
    raise GrokError(f"could not parse JSON from model output: {raw[:400]}")
