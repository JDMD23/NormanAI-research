from __future__ import annotations

from pathlib import Path

import pytest

from lib import config


def test_test_runtime_disables_dotenv_fallback() -> None:
    assert config._env_file_candidates() == []
    assert config.load_env_key("NORMAN_TEST_MISSING_KEY") == ""


def test_crmx_path_defaults_to_mac_projects_absolute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NORMAN_CRMX_PATH", raising=False)
    cfg = config.research_config()["crmx"]
    assert cfg["path"] == config.DEFAULT_CRMX_PATH
    assert cfg["path"] == "/Users/normanai/Projects/NormanAI-CRMx"
    assert Path(cfg["path"]).is_absolute()
    assert cfg["pathEnv"] == "NORMAN_CRMX_PATH"
    assert config.crmx_path() == Path(config.DEFAULT_CRMX_PATH)


def test_funding_drop_dir_defaults_to_mac_drops_absolute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NORMAN_CRMX_FUNDING_DROP", raising=False)
    cfg = config.research_config()["crmx"]
    assert cfg["fundingDropDir"] == config.DEFAULT_FUNDING_DROP_DIR
    assert cfg["fundingDropDir"] == "/Users/normanai/Drops/crunchbase"
    assert Path(cfg["fundingDropDir"]).is_absolute()
    assert cfg["fundingDropDirEnv"] == "NORMAN_CRMX_FUNDING_DROP"
    assert config.funding_drop_dir() == Path(config.DEFAULT_FUNDING_DROP_DIR)


def test_funding_drop_dir_prefers_env_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    override = tmp_path / "Drops" / "crunchbase"
    monkeypatch.setenv("NORMAN_CRMX_FUNDING_DROP", str(override))
    assert config.funding_drop_dir() == override.resolve()
