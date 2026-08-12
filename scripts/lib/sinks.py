"""Where candidates go when research is done with them.

Sinks that ship today:

  csv (legacy) — hydrated intake CSV for the explicit crm-core shim.
  csv (crmx)   — Crunchbase-export-shaped CSV for NormanAI-CRMx ingest_csv.
  evidence     — versioned JSON sidecar (nyc_evidence, source_urls, keyword_hits).
  promote      — invokes CRMx `norman.tools.ingest_csv` (default) or legacy
                 `crm_intake.py` behind an explicit flag.

Deliberately absent: a Notion writer. CRMx is the sole system of truth; Research
proposes into CRMx intake and never writes Notion as SoR. The optional Notion
read in `notion_existing_keys()` is a courtesy pre-filter only.
"""

from __future__ import annotations

import csv
import json
import shutil
import subprocess
import sys
import urllib.request
from datetime import date
from pathlib import Path
from typing import Any

from lib.candidates import CRMX_CSV_COLUMNS, CSV_COLUMNS, Candidate
from lib.config import (
    ROOT,
    crm_core_path,
    crmx_db_path,
    crmx_path,
    load_env_key,
    promote_target,
    research_config,
)

PROMOTE_TARGETS = frozenset({"crmx", "legacy_crm_core"})


class SinkError(RuntimeError):
    pass


def write_intake_csv(candidates: list[Candidate], out_path: Path | None = None) -> Path:
    """Write the legacy crm-core CSV. Kept for the explicit legacy shim."""
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


def write_crmx_intake_csv(
    candidates: list[Candidate], out_path: Path | None = None
) -> Path:
    """Write the Crunchbase-shaped CSV CRMx `ingest_csv` expects."""
    cfg = research_config()["sink"]["csv"]
    if out_path is None:
        pattern = cfg.get("crmxFilenamePattern") or "research-crmx-intake-{date}.csv"
        name = pattern.format(date=date.today().isoformat())
        out_path = ROOT / cfg["outDir"] / name
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with out_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CRMX_CSV_COLUMNS)
        writer.writeheader()
        for cand in candidates:
            writer.writerow(cand.crmx_csv_row())
    return out_path


def write_evidence(candidates: list[Candidate], out_path: Path | None = None) -> Path:
    """Versioned sidecar JSON holding why each company was picked.

    Schema `norman.research.crmx_evidence.v1` carries nyc_evidence /
    source_urls / keyword_hits that the Crunchbase-shaped CSV cannot hold.
    Promote always passes this path to CRMx via `ingest_csv --evidence`.
    """
    evidence_cfg = (research_config().get("sink") or {}).get("evidence") or {}
    schema = evidence_cfg.get("schemaVersion") or "norman.research.crmx_evidence.v1"
    if out_path is None:
        pattern = evidence_cfg.get("filenamePattern") or (
            "research-evidence-{date}.json"
        )
        out_dir = evidence_cfg.get("outDir") or "out"
        out_path = ROOT / out_dir / pattern.format(date=date.today().isoformat())
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schemaVersion": schema,
        "generated_at": date.today().isoformat(),
        "count": len(candidates),
        "candidates": [c.as_dict() for c in candidates],
        "note": (
            "Research qualification evidence for CRMx. Not a Fit score. "
            "CRMx owns SoR writes; this file is a propose-side artifact."
        ),
    }
    out_path.write_text(json.dumps(payload, indent=2, default=str))
    return out_path


def resolve_promote_target(
    *,
    promote: bool = False,
    legacy: bool = False,
    configured: str | None = None,
) -> str | None:
    """Return the promote target, or None when promotion is off.

    Fail-closed: unknown targets raise. Legacy requires an explicit opt-in
    (`legacy=True` or configured target `legacy_crm_core`).
    """
    cfg = research_config().get("promote") or {}
    enabled = bool(promote or cfg.get("enabled"))
    if not enabled and not legacy:
        return None
    if legacy:
        return "legacy_crm_core"
    target = promote_target(configured)
    if target not in PROMOTE_TARGETS:
        raise SinkError(
            f"unknown promote.target={target!r}; expected one of "
            f"{sorted(PROMOTE_TARGETS)}. Fail closed — refusing to guess a writer."
        )
    return target


def promote(
    csv_path: Path,
    *,
    evidence_path: Path | None = None,
    target: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Dispatch promote to CRMx (default) or the explicit legacy crm-core shim."""
    resolved = target or resolve_promote_target(promote=True)
    if resolved is None:
        raise SinkError("promote called but promotion is disabled")
    if resolved == "crmx":
        return promote_via_crmx(
            csv_path, evidence_path=evidence_path, dry_run=dry_run
        )
    if resolved == "legacy_crm_core":
        return promote_via_legacy_crm_core(csv_path, dry_run=dry_run)
    raise SinkError(
        f"unknown promote target {resolved!r}; refuse to invent a Notion writer"
    )


def promote_via_crmx(
    csv_path: Path,
    *,
    evidence_path: Path | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Hand the CSV + evidence sidecar to CRMx's public ingest CLI.

    Documented public surface (CRMx fail-closed evidence ingest):

        uv run python -m norman.tools.ingest_csv <csv> <db> \\
            --added-from <label> --evidence <path.json>

    Fail closed when the CRMx checkout, ingest module, DB path, or evidence
    sidecar is missing. Research does not fall back to Notion writes and does
    not invent Fit scores.
    """
    cfg = research_config().get("crmx") or {}
    root = crmx_path()
    if not root.is_dir():
        raise SinkError(
            f"NormanAI-CRMx checkout not found at {root}. Set NORMAN_CRMX_PATH "
            "or crmx.path in config/research.json. Promote fails closed."
        )

    module = cfg.get("ingestModule") or "norman.tools.ingest_csv"
    if not _crmx_module_present(root, module):
        raise SinkError(
            f"CRMx ingest module {module!r} not found under {root}. "
            "Refusing to guess an alternate writer (including Notion). "
            "See config/crmx-compatibility.json for the adapter contract."
        )

    db = crmx_db_path()
    if db is None:
        raise SinkError(
            "NORMAN_CRMX_DB (or crmx.dbPath) is unset. CRMx ingest requires the "
            "SQLite SoR path. Promote fails closed — Research will not write Notion."
        )

    if dry_run:
        # Public ingest_csv examples do not document a dry-run flag. Fail closed
        # rather than inventing --dry-run or silently writing.
        raise SinkError(
            "CRMx ingest_csv has no verified dry-run flag; refuse to call it "
            "under dry_run. Use --write --yes --promote for a real handoff, or "
            "omit promote and inspect the CSV + evidence sidecar."
        )

    if evidence_path is None:
        raise SinkError(
            "CRMx promote requires an evidence sidecar path "
            "(schema norman.research.crmx_evidence.v1). Fail closed — refusing "
            "to call ingest_csv without --evidence."
        )
    evidence = Path(evidence_path)
    if not evidence.is_file():
        raise SinkError(
            f"evidence sidecar missing at {evidence}. CRMx promote fails "
            "closed when --evidence cannot be passed."
        )

    added_from = (cfg.get("addedFromPattern") or "research:{date}").format(
        date=date.today().isoformat()
    )
    uv_bin = cfg.get("uvBin") or "uv"
    if shutil.which(uv_bin) is None and not Path(uv_bin).exists():
        raise SinkError(
            f"{uv_bin!r} not found on PATH. CRMx promote requires uv to run "
            f"`{uv_bin} run python -m {module}`."
        )

    cmd = [
        uv_bin,
        "run",
        "python",
        "-m",
        module,
        str(csv_path.resolve()),
        str(db),
        "--added-from",
        added_from,
        "--evidence",
        str(evidence.resolve()),
    ]
    proc = subprocess.run(
        cmd, cwd=str(root), capture_output=True, text=True, timeout=900
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        raise SinkError(
            f"CRMx {module} failed ({proc.returncode}): {out[-1500:]}"
        )

    summary: dict[str, Any] = {
        "target": "crmx",
        "command": cmd,
        "stdout": out[-4000:],
        "csv": str(csv_path),
        "evidence": str(evidence),
        "db": str(db),
        "added_from": added_from,
        "counts": _parse_crmx_counts(out),
        "note": (
            "Evidence sidecar passed to ingest_csv via --evidence "
            "(schema norman.research.crmx_evidence.v1). Promote remains "
            "off-by-default; Research never writes Notion or invents Fit."
        ),
    }
    return summary


def promote_via_legacy_crm_core(
    csv_path: Path, dry_run: bool = False
) -> dict[str, Any]:
    """Explicit legacy shim: invoke crm-core's crm_intake.py.

    Ordinary `--promote` does not use this path. Pass
    `--promote-legacy-crm-core` or set promote.target=legacy_crm_core.
    """
    core = crm_core_path()
    script = core / "scripts" / "crm_intake.py"
    if not script.exists():
        raise SinkError(
            f"crm_intake.py not found at {script}. Legacy promotion needs the "
            "NormanAI-crm-core checkout — set crmCore.path or NORMAN_CRM_CORE_PATH."
        )

    cmd = [sys.executable, str(script), "--csv", str(csv_path)]
    cmd += ["--dry-run"] if dry_run else ["--write", "--yes"]

    proc = subprocess.run(
        cmd, cwd=str(core), capture_output=True, text=True, timeout=900
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        raise SinkError(f"crm_intake.py failed ({proc.returncode}): {out[-1500:]}")

    receipt = core / "state" / "intake_latest.json"
    summary: dict[str, Any] = {
        "target": "legacy_crm_core",
        "stdout": out[-4000:],
        "receipt": str(receipt),
    }
    try:
        summary["counts"] = json.loads(receipt.read_text()).get("counts")
    except (OSError, json.JSONDecodeError):
        summary["counts"] = None
    return summary


# Back-compat name used by older call sites / tests.
def promote_via_intake(csv_path: Path, dry_run: bool = False) -> dict[str, Any]:
    """Deprecated alias — dispatches through the configured promote target."""
    return promote(csv_path, dry_run=dry_run)


def _crmx_module_present(root: Path, module: str) -> bool:
    """Best-effort check that the ingest module exists in the CRMx checkout.

    Accepts src-layout, flat package layout, or tools/ingest_csv.py when the
    configured module ends with ingest_csv. Deliberately does **not** treat a
    bare `core/intake.py` as proof: `"intake" in "norman.tools.ingest_csv"` is
    true, so that check was fail-open for any checkout with an unrelated
    intake file. Match install_funding_watcher_launch_agent._crmx_module_present.
    """
    parts = module.split(".")
    # Prefer src-layout, then flat package layout.
    candidates = [
        root.joinpath("src", *parts),
        root.joinpath(*parts),
    ]
    for base in candidates:
        if base.with_suffix(".py").is_file():
            return True
        if (base / "__init__.py").is_file() or (base / "__main__.py").is_file():
            return True
    # Also accept a tools/ingest_csv.py layout mentioned in the task brief.
    if (root / "tools" / "ingest_csv.py").is_file() and module.endswith(
        "ingest_csv"
    ):
        return True
    return False


def _parse_crmx_counts(output: str) -> dict[str, Any] | None:
    """Best-effort parse of ingest stdout; never invents Fit scores."""
    for line in reversed(output.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload.get("counts") or payload
    return None


def notion_existing_keys() -> set[str]:
    """Read-only pre-filter: identity keys already on the operator board.

    Returns an empty set (never raises) when the token is missing or Notion is
    unreachable — a failed pre-filter must degrade to "emit and let intake
    dedup", not to dropping a run on the floor. Never writes.
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
