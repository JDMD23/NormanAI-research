"""Predict a candidate's Fit Score using crm-core's REAL scoring engine.

Why a subprocess and not an import
----------------------------------
`fit_score.py` does `from lib.parsing import ...`, and this repo also has a
`lib` package on sys.path. Importing it in-process would collide. It already
ships a CLI (`--csv --today --out`) built for exactly this, so we shell out.
Process isolation, real engine, real `config/fit-score-weights.json` — when JD
retunes a weight, research retargets on the next run with no code change here.

Never reimplement the bands. A second copy of the scorer that drifts from the
first is worse than no prediction at all.

What research can and cannot see
--------------------------------
55 of the 100 points are NYC Heads (30) + NYC Jobs (25), and both come from
lanes that run after intake. Research supplies *estimates* for them, clearly
labelled, and the lanes overwrite with measured values later. The prediction is
therefore a triage ceiling, not a verdict — which is exactly what it is used
for: deciding whether a company is worth a row at all.
"""

from __future__ import annotations

import csv
import json
import subprocess
import tempfile
from datetime import date
from pathlib import Path

from lib.config import ROOT, research_config

# Column names fit_score.py reads. These are Notion property names and differ
# from the intake CSV's column names — do not merge the two.
SCORE_COLUMNS = [
    "Company",
    "LinkedIn NYC Metro Count",
    "NYC Open Jobs (# roles)",
    "Founded Year",
    "HQ",
    "Industry",
    "Last Funding Date",
    "Last Funding Type",
    "Last Funding Amount ($M)",
    "Total Funding Amount ($M)",
    "Funding Velocity Tag",
    "Top 5 Investors",
]


class FitBridgeUnavailable(RuntimeError):
    """crm-core isn't reachable from here — caller should degrade, not crash."""


def crm_core_path() -> Path:
    cfg = research_config().get("crmCore") or {}
    raw = cfg.get("path") or "../NormanAI-crm-core"
    path = Path(raw)
    if not path.is_absolute():
        path = (ROOT / path).resolve()
    return path


def scorer_path() -> Path:
    return crm_core_path() / "scripts" / "fit_score.py"


def available() -> bool:
    return scorer_path().exists()


def _score_row_input(cand) -> dict[str, str]:
    """Map a candidate onto the columns fit_score.py expects.

    Blank means unknown and the engine renormalizes around it — never write a
    0 to fill a gap, or the score silently becomes a claim we can't defend.
    """

    def num(value) -> str:
        return "" if value is None else f"{value:g}"

    total_m = None if cand.total_funding_usd is None else cand.total_funding_usd / 1_000_000
    last_m = None if cand.last_funding_usd is None else cand.last_funding_usd / 1_000_000

    return {
        "Company": cand.company,
        "LinkedIn NYC Metro Count": num(cand.nyc_headcount_estimate),
        "NYC Open Jobs (# roles)": num(cand.nyc_open_roles_estimate),
        "Founded Year": cand.founded or "",
        "HQ": cand.hq or "",
        "Industry": cand.industries or "",
        "Last Funding Date": cand.last_funding_date or "",
        "Last Funding Type": cand.last_funding_type or "",
        "Last Funding Amount ($M)": num(last_m),
        "Total Funding Amount ($M)": num(total_m),
        # Velocity is derived by crm-core's funding_velocity lane from full
        # round history. Research has one round at best, so it stays blank.
        "Funding Velocity Tag": "",
        "Top 5 Investors": cand.investors or "",
    }


def predict(candidates: list, today: date | None = None) -> list[dict]:
    """Return one prediction dict per candidate, in the same order.

    Raises FitBridgeUnavailable when crm-core can't be found so the caller can
    fall back to signal strength rather than dropping the run.
    """
    if not candidates:
        return []
    script = scorer_path()
    if not script.exists():
        raise FitBridgeUnavailable(
            f"crm-core scorer not found at {script}. Set crmCore.path in "
            "config/research.json to the NormanAI-crm-core checkout."
        )

    today = today or date.today()
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        in_csv, out_jsonl = tmp_dir / "score-in.csv", tmp_dir / "score-out.jsonl"

        with in_csv.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=SCORE_COLUMNS)
            writer.writeheader()
            for cand in candidates:
                writer.writerow(_score_row_input(cand))

        proc = subprocess.run(
            [
                "python3", str(script),
                "--csv", str(in_csv),
                "--today", today.isoformat(),
                "--out", str(out_jsonl),
            ],
            cwd=str(crm_core_path()),
            capture_output=True,
            text=True,
            timeout=120,
        )
        if proc.returncode != 0:
            raise FitBridgeUnavailable(
                f"crm-core fit_score.py failed ({proc.returncode}): "
                f"{(proc.stderr or proc.stdout)[:600]}"
            )

        rows = [json.loads(line) for line in out_jsonl.read_text().splitlines() if line.strip()]

    if len(rows) != len(candidates):
        raise FitBridgeUnavailable(
            f"scorer returned {len(rows)} rows for {len(candidates)} candidates"
        )
    return rows


def apply(candidates: list, today: date | None = None) -> tuple[list, str]:
    """Attach predictions in place. Returns (candidates, note).

    The note explains what happened, so a run that degraded says so out loud
    instead of silently switching gates.
    """
    try:
        rows = predict(candidates, today)
    except FitBridgeUnavailable as exc:
        for cand in candidates:
            cand.predicted_fit = None
            cand.predicted_fit_reason = ""
            cand.predicted_fit_flags = []
        return candidates, f"fit prediction unavailable — {exc}"
    except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError) as exc:
        for cand in candidates:
            cand.predicted_fit = None
            cand.predicted_fit_reason = ""
            cand.predicted_fit_flags = []
        return candidates, f"fit prediction failed — {exc}"

    for cand, row in zip(candidates, rows):
        cand.predicted_fit = int(row.get("fit_score") or 0)
        cand.predicted_fit_reason = str(row.get("reason") or "")
        cand.predicted_fit_flags = list(row.get("flags") or [])
        cand.predicted_fit_components = dict(row.get("components") or {})
    version = rows[0].get("formula_version", "?") if rows else "?"
    return candidates, f"predicted with crm-core {version}"
