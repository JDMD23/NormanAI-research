from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_research_ci_requires_private_core_acceptance_checkout() -> None:
    workflow = ROOT / ".github" / "workflows" / "ci.yml"
    text = workflow.read_text(encoding="utf-8")
    assert "JDMD23/NormanAI-crm-core" in text
    assert "CRM_CORE_READONLY_DEPLOY_KEY" in text
    assert "NORMAN_REQUIRE_CROSS_REPO: \"1\"" in text
    assert "NORMAN_CRM_CORE_ACCEPTANCE_PATH:" in text
    assert "python3 -m pytest tests/ -q" in text
    assert "cross-repository-acceptance:" in text
    assert "config/core-compatibility.json" in text
    assert "git merge-base --is-ancestor" in text
    assert "skipped" in text
