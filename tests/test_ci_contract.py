from __future__ import annotations

import pytest

import test_cross_repo_coordination as acceptance


def test_required_cross_repo_checkout_fails_instead_of_skipping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NORMAN_REQUIRE_CROSS_REPO", "1")

    with pytest.raises(
        pytest.fail.Exception,
        match="cross-repository acceptance requires",
    ):
        acceptance._handle_missing_core_checkout()
