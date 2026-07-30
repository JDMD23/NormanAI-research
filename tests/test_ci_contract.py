from __future__ import annotations

from pathlib import Path

import pytest

import test_cross_repo_coordination as acceptance

ROOT = Path(__file__).resolve().parents[1]


def test_required_cross_repo_checkout_fails_instead_of_skipping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NORMAN_REQUIRE_CROSS_REPO", "1")

    with pytest.raises(
        pytest.fail.Exception,
        match="cross-repository acceptance requires",
    ):
        acceptance._handle_missing_core_checkout()


def test_cross_repo_ci_uses_core_only_read_only_deploy_key() -> None:
    workflow_path = ROOT / ".github" / "workflows" / "ci.yml"
    workflow_text = workflow_path.read_text(encoding="utf-8")
    core_checkout = workflow_text.split(
        "- name: Check out pinned Core", maxsplit=1
    )[1].split("- name: Prove pinned Core commit is merged", maxsplit=1)[0]

    assert "repository: JDMD23/NormanAI-crm-core" in core_checkout
    assert (
        "ssh-key: ${{ secrets.CRM_CORE_READONLY_DEPLOY_KEY }}"
        in core_checkout
    )
    assert "persist-credentials: false" in core_checkout
    assert "actions/create-github-app-token" not in workflow_text
    assert "NORMANAI_CROSS_REPO_APP_ID" not in workflow_text
    assert "NORMANAI_CROSS_REPO_APP_PRIVATE_KEY" not in workflow_text
