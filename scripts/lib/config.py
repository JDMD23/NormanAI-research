"""Config + env loading for NormanAI-research.

Shares the same `.env` ladder as NormanAI-CRMx / legacy crm-core so one file on
the cron host serves the discovery arm and the system of truth.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# Production Mac CRMx checkout. Research may live under ~/Documents/; a relative
# sibling ../NormanAI-CRMx does not exist there. CI/tests override via env.
DEFAULT_CRMX_PATH = "/Users/normanai/Projects/NormanAI-CRMx"

# CRMx mac-paths.json crunchbase drop. Research writes CSV here; Pipeline/CRMx
# funding-drop LaunchAgent (com.normanai.crmx.funding-drop) owns ingest/score.
DEFAULT_FUNDING_DROP_DIR = "/Users/normanai/Drops/crunchbase"


def _env_file_candidates() -> list[Path]:
    """Same ladder crm-core uses: explicit override, repo-local, then host homes."""
    if os.environ.get("NORMANAI_DISABLE_DOTENV") == "1":
        return []
    candidates: list[Path] = []
    override = os.environ.get("NORMAN_ENV_FILE")
    if override:
        candidates.append(Path(override))
    candidates.append(ROOT / ".env.local")
    candidates.append(ROOT / ".env")
    candidates.append(Path.home() / ".openclaw" / ".env")
    candidates.append(Path.home() / ".normanai" / ".env")
    candidates.append(Path.home() / "Projects" / "NormansHub" / ".env.local")
    return candidates


def load_env_key(key: str) -> str:
    """Return an env var, falling back to the dotenv ladder. Never raises."""
    val = os.environ.get(key, "")
    if val:
        return val
    for path in _env_file_candidates():
        try:
            if not path.exists():
                continue
            for line in path.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k = k.strip()
                v = v.strip()
                if len(v) >= 2 and v[0] in ('"', "'") and v[-1] == v[0]:
                    v = v[1:-1]
                if k == key and v:
                    return v
        except OSError:
            continue
    return ""


def require_env_key(key: str) -> str:
    val = load_env_key(key)
    if not val:
        raise SystemExit(
            f"missing {key} — set it in the environment or one of: "
            + ", ".join(str(p) for p in _env_file_candidates())
        )
    return val


@lru_cache(maxsize=None)
def load_config(name: str) -> dict:
    """Load and cache a JSON file from config/."""
    path = ROOT / "config" / f"{name}.json"
    if not path.exists():
        raise SystemExit(f"missing config file: {path}")
    return json.loads(path.read_text())


def research_config() -> dict:
    return load_config("research")


def modes_config() -> dict:
    return load_config("modes")


def sources_config() -> dict:
    return load_config("sources")


def crm_core_path() -> Path:
    """Where the legacy NormanAI-crm-core checkout lives (explicit shim only)."""
    override = os.environ.get("NORMAN_CRM_CORE_PATH")
    if override:
        path = Path(override).expanduser()
        if not path.is_absolute():
            raise SystemExit("NORMAN_CRM_CORE_PATH must be absolute")
        return path.resolve()
    raw = (research_config().get("crmCore") or {}).get("path") or "../NormanAI-crm-core"
    path = Path(raw)
    return path if path.is_absolute() else (ROOT / path).resolve()


def crmx_path() -> Path:
    """Where the NormanAI-CRMx checkout lives, for promotion.

    Prefer NORMAN_CRMX_PATH (must be absolute). Otherwise use crmx.path from
    research.json, defaulting to the production Mac Projects checkout.
    """
    cfg = research_config().get("crmx") or {}
    env_name = cfg.get("pathEnv") or "NORMAN_CRMX_PATH"
    override = os.environ.get(env_name)
    if override:
        path = Path(override).expanduser()
        if not path.is_absolute():
            raise SystemExit(f"{env_name} must be absolute")
        return path.resolve()
    raw = cfg.get("path") or DEFAULT_CRMX_PATH
    path = Path(raw).expanduser()
    return path if path.is_absolute() else (ROOT / path).resolve()


def funding_drop_dir() -> Path:
    """Directory where the funding watcher drops CRMx-shaped CSVs.

    Prefer NORMAN_CRMX_FUNDING_DROP (must be absolute). Otherwise use
    crmx.fundingDropDir from research.json, defaulting to the Mac Drops path
    aligned with CRMx config/mac-paths.json.
    """
    cfg = research_config().get("crmx") or {}
    env_name = cfg.get("fundingDropDirEnv") or "NORMAN_CRMX_FUNDING_DROP"
    override = os.environ.get(env_name, "").strip()
    if override:
        path = Path(override).expanduser()
        if not path.is_absolute():
            raise SystemExit(f"{env_name} must be absolute")
        return path.resolve()
    raw = cfg.get("fundingDropDir") or DEFAULT_FUNDING_DROP_DIR
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise SystemExit("crmx.fundingDropDir must be absolute")
    return path


def crmx_db_path() -> Path | None:
    """SQLite SoR path for CRMx ingest. None when unset (promote must fail closed)."""
    cfg = research_config().get("crmx") or {}
    env_name = cfg.get("dbPathEnv") or "NORMAN_CRMX_DB"
    override = os.environ.get(env_name, "").strip()
    if override:
        path = Path(override).expanduser()
        if not path.is_absolute():
            raise SystemExit(f"{env_name} must be absolute")
        return path.resolve()
    raw = (cfg.get("dbPath") or "").strip()
    if not raw:
        return None
    path = Path(raw)
    return path if path.is_absolute() else (ROOT / path).resolve()


def promote_target(explicit: str | None = None) -> str:
    """Resolve promote target. Unknown values fail closed at the caller."""
    if explicit:
        return explicit.strip()
    raw = (research_config().get("promote") or {}).get("target") or "crmx"
    return str(raw).strip()


def state_path(rel: str) -> Path:
    path = ROOT / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    return path

