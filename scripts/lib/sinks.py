"""Where candidates go when research is done with them.

Two sinks ship today:

  csv       — writes the hydrated intake CSV that NormanAI-crm-core's
              `crm_intake.py` consumes. This is the contract of record.
  supabase  — mirrors candidates into this repo's own `research_candidates`
              staging table (sql/001_research_candidates.sql).

Deliberately absent: a Notion writer. `crm_intake.py` is the single writer to
Norman CRM Core and it does the hard dedup. Adding a second writer here would
create exactly the two-boards-fighting problem the operating contract warns
about. To enqueue into the workspace D1 job queue instead, add an adapter here
against that schema — do not reach into Notion.
"""

from __future__ import annotations

import csv
import json
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path
from typing import Any

from lib.candidates import CSV_COLUMNS, Candidate
from lib.config import ROOT, load_env_key, research_config


class SinkError(RuntimeError):
    pass


def write_intake_csv(candidates: list[Candidate], out_path: Path | None = None) -> Path:
    """Write the CSV `crm_intake.py --csv` expects. Returns the path written."""
    cfg = research_config()["sink"]["csv"]
    if out_path is None:
        name = cfg["filenamePattern"].format(date=date.today().isoformat())
        out_path = ROOT / cfg["outDir"] / name
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with out_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for cand in candidates:
            writer.writerow(cand.csv_row())
    return out_path


def write_evidence(candidates: list[Candidate], out_path: Path | None = None) -> Path:
    """Sidecar JSON holding why each company was picked.

    The CSV carries only fields intake understands. Signals, NYC proof, and
    source URLs would be dropped on the floor otherwise, and those are the part
    a human needs to audit a bad batch.
    """
    if out_path is None:
        out_path = ROOT / "out" / f"research-evidence-{date.today().isoformat()}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": date.today().isoformat(),
        "count": len(candidates),
        "candidates": [c.as_dict() for c in candidates],
    }
    out_path.write_text(json.dumps(payload, indent=2, default=str))
    return out_path


def push_supabase(candidates: list[Candidate]) -> int:
    """Upsert candidates into the research_candidates staging table.

    Uses PostgREST directly so the repo stays dependency-free. Returns the
    number of rows sent.
    """
    cfg = research_config()["sink"]["supabase"]
    if not cfg.get("enabled"):
        raise SinkError("supabase sink is disabled in config/research.json")
    url = load_env_key(cfg["urlEnv"])
    key = load_env_key(cfg["keyEnv"])
    if not url or not key:
        raise SinkError(f"set {cfg['urlEnv']} and {cfg['keyEnv']} to use the supabase sink")
    if not candidates:
        return 0

    rows: list[dict[str, Any]] = []
    for cand in candidates:
        row = cand.as_dict()
        row["signals"] = cand.signals
        row["source_urls"] = cand.source_urls
        row["score_notes"] = cand.score_notes
        rows.append(row)

    endpoint = f"{url.rstrip('/')}/rest/v1/{cfg['table']}?on_conflict=identity_key"
    req = urllib.request.Request(
        endpoint,
        data=json.dumps(rows, default=str).encode("utf-8"),
        method="POST",
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            if resp.status >= 300:
                raise SinkError(f"supabase returned HTTP {resp.status}")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8")[:1000]
        except Exception:  # noqa: BLE001 - diagnostics only
            pass
        raise SinkError(f"supabase HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise SinkError(f"supabase transport error: {exc}") from exc
    return len(rows)


def notion_existing_keys() -> set[str]:
    """Read-only pre-filter: identity keys already on the CRM Core board.

    Returns an empty set (never raises) when the token is missing or Notion is
    unreachable — a failed pre-filter must degrade to "emit and let intake
    dedup", not to dropping a run on the floor.
    """
    from lib.identity import identity_key  # local import keeps module import cheap

    cfg = research_config()["dedup"]
    if not cfg.get("checkNotionReadOnly"):
        return set()
    token = load_env_key(cfg["notionTokenEnv"])
    if not token:
        return set()

    keys: set[str] = set()
    cursor: str | None = None
    db_id = cfg["notionDatabaseId"]
    while True:
        body: dict[str, Any] = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        req = urllib.request.Request(
            f"https://api.notion.com/v1/databases/{db_id}/query",
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Notion-Version": "2022-06-28",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except Exception:  # noqa: BLE001 - pre-filter is best-effort by design
            return keys

        for page in payload.get("results") or []:
            props = page.get("properties") or {}
            title = ""
            for part in (props.get("Company") or {}).get("title") or []:
                title += part.get("plain_text", "")
            website = (props.get("Website") or {}).get("url") or ""
            linkedin = (props.get("Company Linkedin") or {}).get("url") or ""
            key = identity_key(title, website, linkedin)
            if key:
                keys.add(key)

        if not payload.get("has_more"):
            break
        cursor = payload.get("next_cursor")
    return keys
