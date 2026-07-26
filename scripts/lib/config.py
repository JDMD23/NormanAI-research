"""Config + env loading for NormanAI-research.

Mirrors NormanAI-crm-core's `scripts/lib/config.py` env ladder so a single
`.env` on the cron host serves both repos.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _env_file_candidates() -> list[Path]:
    """Same ladder crm-core uses: explicit override, repo-local, then host homes."""
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
    """Where the NormanAI-crm-core checkout lives, for promotion."""
    raw = (research_config().get("crmCore") or {}).get("path") or "../NormanAI-crm-core"
    path = Path(raw)
    return path if path.is_absolute() else (ROOT / path).resolve()


def state_path(rel: str) -> Path:
    path = ROOT / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    return path
