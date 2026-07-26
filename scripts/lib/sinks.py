"""Where candidates go when research is done with them.

Two sinks ship today:

  csv       — writes the hydrated intake CSV that NormanAI-crm-core's
              `crm_intake.py` consumes. This is the contract of record.
  promote   — invokes `crm_intake.py` on that CSV so rows land on the board
              with no human step.

Deliberately absent: a Notion writer. `crm_intake.py` is the single writer to
Norman CRM Core and it does the hard dedup. Adding a second writer here would
create exactly the two-boards-fighting problem the operating contract warns
about. The workspace D1 queue is keyed on a Notion page id, so it cannot
accept a company that has no row yet — creation must go through intake first.
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
from lib.config import ROOT, crm_core_path, load_env_key, research_config


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


def promote_via_intake(csv_path: Path, dry_run: bool = False) -> dict[str, Any]:
    """Hand the CSV to crm-core's crm_intake.py and let it create the rows.

    This is how research "automatically adds to CRM Core" without becoming a
    second Notion writer: we invoke the one writer rather than reimplementing
    it. Intake keeps its hard dedup, its Need-* seeding, and its receipt.
    """
    import subprocess

    core = crm_core_path()
    script = core / "scripts" / "crm_intake.py"
    if not script.exists():
        raise SinkError(
            f"crm_intake.py not found at {script}. Promotion needs the "
            "NormanAI-crm-core checkout — set crmCore.path in config/research.json."
        )

    cmd = ["python3", str(script), "--csv", str(csv_path)]
    cmd += ["--dry-run"] if dry_run else ["--write", "--yes"]

    proc = subprocess.run(
        cmd, cwd=str(core), capture_output=True, text=True, timeout=900
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        raise SinkError(f"crm_intake.py failed ({proc.returncode}): {out[-1500:]}")

    # Intake writes its own receipt; surface it rather than reparsing stdout.
    receipt = core / "state" / "intake_latest.json"
    summary: dict[str, Any] = {"stdout": out[-4000:], "receipt": str(receipt)}
    try:
        summary["counts"] = json.loads(receipt.read_text()).get("counts")
    except (OSError, json.JSONDecodeError):
        summary["counts"] = None
    return summary


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
