from __future__ import annotations

from lib import config


def test_test_runtime_disables_dotenv_fallback() -> None:
    assert config._env_file_candidates() == []
    assert config.load_env_key("NORMAN_TEST_MISSING_KEY") == ""
