import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


@pytest.fixture(autouse=True)
def isolate_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    shared = tmp_path / "shared-runtime"
    monkeypatch.setenv("NORMANAI_SHARED_STATE_DIR", str(shared))
    monkeypatch.setenv("NORMANAI_DISABLE_DOTENV", "1")
    monkeypatch.setenv("NORMANAI_TEST_MODE", "1")
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    monkeypatch.delenv("NOTION_API_KEY", raising=False)
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    yield
