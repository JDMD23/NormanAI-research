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


def test_crmx_path_prefers_env_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    override = tmp_path / "mock-NormanAI-CRMx"
    monkeypatch.setenv("NORMAN_CRMX_PATH", str(override))
    assert config.crmx_path() == override.resolve()
