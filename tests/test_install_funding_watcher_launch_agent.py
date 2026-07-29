from __future__ import annotations

import json
import plistlib
import shlex
from pathlib import Path
from types import SimpleNamespace

import pytest

import install_funding_watcher_launch_agent as installer
from install_funding_watcher_launch_agent import (
    CORE_LABEL,
    LABEL,
    main,
    render_plist,
)


SOURCE = (
    "https://www.crunchbase.com/discover/saved/"
    "main-funding-july-2026/730c458b-149c-4a0a-9684-7146e7258993"
)


class FakeRunner:
    def __init__(
        self,
        *,
        loaded: bool = False,
        bootout_code: int = 0,
        bootstrap_code: int = 0,
        tracked: bool = True,
        core_tracked: bool = True,
    ):
        self.loaded = loaded
        self.bootout_code = bootout_code
        self.bootstrap_code = bootstrap_code
        self.tracked = tracked
        self.core_tracked = core_tracked
        self.calls: list[list[str]] = []
        self.bootout_plist_existed: list[bool] = []

    def __call__(self, command, **kwargs):
        command = [str(part) for part in command]
        self.calls.append(command)
        if command[:2] == ["launchctl", "print"]:
            return SimpleNamespace(
                returncode=0 if self.loaded else 1, stdout="", stderr=""
            )
        if command[:2] == ["launchctl", "bootout"]:
            self.bootout_plist_existed.append(Path(command[-1]).exists())
            return SimpleNamespace(
                returncode=self.bootout_code, stdout="", stderr="bootout failed"
            )
        if command[:2] == ["launchctl", "bootstrap"]:
            return SimpleNamespace(
                returncode=self.bootstrap_code, stdout="", stderr="bootstrap failed"
            )
        if command and command[0] == "git":
            tracked = (
                self.core_tracked
                if command[-1] == "scripts/crm_funding_handoff.py"
                else self.tracked
            )
            return SimpleNamespace(
                returncode=0 if tracked else 1, stdout="", stderr=""
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")


def setup_checkout(tmp_path: Path) -> tuple[Path, Path, Path]:
    repo = tmp_path / "NormanAI-research"
    home = tmp_path / "home"
    core = tmp_path / "Core CRM"
    (repo / "scripts").mkdir(parents=True, exist_ok=True)
    (repo / "config").mkdir(exist_ok=True)
    (core / "scripts").mkdir(parents=True, exist_ok=True)
    (repo / "scripts" / "funding_watcher.py").write_text("# watcher\n")
    (core / "scripts" / "crm_funding_handoff.py").write_text(
        'REQUEST_SCHEMA_VERSION = "norman.research.funding_handoff.v1"\n'
        'RESULT_SCHEMA_VERSION = "norman.crm_core.funding_handoff_result.v1"\n'
    )
    (repo / "config" / "research.json").write_text(
        json.dumps(
            {
                "crmCore": {
                    "path": str(core),
                    "fundingHandoff": "scripts/crm_funding_handoff.py",
                }
            }
        )
    )
    (repo / "config" / "funding-watcher.json").write_text(
        json.dumps(
            {
                "crmResultSchemaVersion": "norman.crm_core.funding_handoff_result.v1",
                "stateDirectory": str(
                    home
                    / "Library/Application Support/NormanAI/Research/"
                    "crunchbase-funding-watcher"
                ),
                "sources": [{"url": SOURCE}],
            }
        )
    )
    state = (
        home
        / "Library/Application Support/NormanAI/Research/"
        "crunchbase-funding-watcher"
    )
    state.mkdir(parents=True, exist_ok=True)
    (state / "ledger.json").write_text(
        json.dumps(
            {
                "schemaVersion": "norman.research.crunchbase_funding_ledger.v1",
                "events": {},
                "bootstraps": {
                    SOURCE: {
                        "completedAt": "2026-07-29T12:06:04+00:00",
                        "migrated": True,
                    }
                },
                "completedSlots": {},
            }
        )
    )
    return repo, home, core


def test_rendered_plist_is_research_owned_sanitized_and_exact(
    monkeypatch,
) -> None:
    monkeypatch.setenv("NOTION_TOKEN", "notion-secret-must-not-appear")
    monkeypatch.setenv("XAI_API_KEY", "xai-secret-must-not-appear")
    payload = plistlib.loads(
        render_plist(
            Path("/Users/test/NormanAI Research"),
            Path("/Users/test"),
            Path("/usr/bin/python3"),
            Path("/logs/out.log"),
            Path("/logs/err.log"),
        )
    )
    assert payload["Label"] == "com.normanai.research.crunchbase-funding-watcher"
    assert payload["StartCalendarInterval"] == [
        {"Hour": 6, "Minute": 0},
        {"Hour": 10, "Minute": 0},
        {"Hour": 13, "Minute": 0},
        {"Hour": 16, "Minute": 0},
        {"Hour": 19, "Minute": 0},
    ]
    assert payload["WorkingDirectory"] == "/Users/test/NormanAI Research"
    args = payload["ProgramArguments"]
    assert len(args) == 7
    assert args[:7] == [
        "/usr/bin/env",
        "-i",
        "HOME=/Users/test",
        "PATH=/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "/bin/zsh",
        "-lc",
        args[-1],
    ]
    assert shlex.split(args[-1]) == [
        "exec",
        "/usr/bin/python3",
        "/Users/test/NormanAI Research/scripts/funding_watcher.py",
        "check",
        "--write",
        "--yes",
        "--enforce-schedule",
    ]
    rendered = plistlib.dumps(payload).decode("utf-8")
    assert CORE_LABEL not in rendered
    assert "Core CRM" not in rendered
    assert "NOTION_TOKEN" not in rendered
    assert "XAI_API_KEY" not in rendered
    assert "notion-secret-must-not-appear" not in rendered
    assert "xai-secret-must-not-appear" not in rendered
    assert payload["StandardOutPath"] == "/logs/out.log"
    assert payload["StandardErrorPath"] == "/logs/err.log"
    assert payload["RunAtLoad"] is False
    assert payload["ProcessType"] == "Background"


def test_install_requires_yes_and_refuses_feature_worktree(tmp_path: Path) -> None:
    repo, home, _ = setup_checkout(tmp_path)
    assert main(["install"], repo_root=repo, home=home) == 64
    feature = tmp_path / ".worktrees" / "feature"
    feature.mkdir(parents=True)
    assert main(
        ["install", "--yes"], repo_root=feature, home=home
    ) == 69


def test_install_requires_bootstrap_core_cli_schema_and_no_core_plist(
    tmp_path: Path,
) -> None:
    repo, home, core = setup_checkout(tmp_path)
    runner = FakeRunner()
    ledger = next(home.rglob("ledger.json"))
    ledger.unlink()
    assert main(
        ["install", "--yes"],
        repo_root=repo,
        home=home,
        runner=runner,
        uid=501,
    ) == 69
    setup_checkout(tmp_path)
    legacy = (
        home
        / "Library/LaunchAgents/"
        "com.normanai.crm-core.crunchbase-funding-watcher.plist"
    )
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text("legacy")
    assert main(
        ["install", "--yes"],
        repo_root=repo,
        home=home,
        runner=runner,
        uid=501,
    ) == 69
    legacy.unlink()
    (core / "scripts" / "crm_funding_handoff.py").write_text(
        'REQUEST_SCHEMA_VERSION = "wrong"\n'
        'RESULT_SCHEMA_VERSION = "wrong"\n'
    )
    assert main(
        ["install", "--yes"],
        repo_root=repo,
        home=home,
        runner=runner,
        uid=501,
    ) == 69
    assert not any(call[0] == "launchctl" for call in runner.calls)


def test_install_resolves_exact_tilde_state_directory_against_selected_home(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo, home, _ = setup_checkout(tmp_path)
    ambient_home = tmp_path / "ambient-home"
    ambient_home.mkdir()
    monkeypatch.setenv("HOME", str(ambient_home))
    watcher_config = repo / "config/funding-watcher.json"
    payload = json.loads(watcher_config.read_text())
    payload["stateDirectory"] = (
        "~/Library/Application Support/NormanAI/Research/"
        "crunchbase-funding-watcher"
    )
    watcher_config.write_text(json.dumps(payload))
    runner = FakeRunner()

    assert main(
        ["install", "--yes"],
        repo_root=repo,
        home=home,
        python_path=Path("/usr/bin/python3"),
        runner=runner,
        uid=501,
    ) == 0
    assert not (
        ambient_home
        / "Library/Application Support/NormanAI/Research/"
        "crunchbase-funding-watcher/ledger.json"
    ).exists()


def test_install_rejects_malformed_bootstrap_completion_marker(
    tmp_path: Path,
) -> None:
    repo, home, _ = setup_checkout(tmp_path)
    ledger = next(home.rglob("ledger.json"))
    payload = json.loads(ledger.read_text())
    payload["bootstraps"][SOURCE] = {"migrated": True}
    ledger.write_text(json.dumps(payload))
    runner = FakeRunner()

    assert main(
        ["install", "--yes"],
        repo_root=repo,
        home=home,
        runner=runner,
        uid=501,
    ) == 69
    assert not any(call[0] == "launchctl" for call in runner.calls)
    assert not (
        home / "Library/LaunchAgents" / f"{LABEL}.plist"
    ).exists()


def test_install_accepts_supervised_bootstrap_completion_marker(
    tmp_path: Path,
) -> None:
    repo, home, _ = setup_checkout(tmp_path)
    ledger = next(home.rglob("ledger.json"))
    payload = json.loads(ledger.read_text())
    payload["bootstraps"][SOURCE].pop("migrated")
    ledger.write_text(json.dumps(payload))

    assert main(
        ["install", "--yes"],
        repo_root=repo,
        home=home,
        python_path=Path("/usr/bin/python3"),
        runner=FakeRunner(),
        uid=501,
    ) == 0


def test_install_requires_tracked_permanent_core_handoff_cli(
    tmp_path: Path,
) -> None:
    repo, home, _ = setup_checkout(tmp_path)
    runner = FakeRunner(core_tracked=False)

    assert main(
        ["install", "--yes"],
        repo_root=repo,
        home=home,
        runner=runner,
        uid=501,
    ) == 69
    assert not any(call[0] == "launchctl" for call in runner.calls)
    assert not (
        home / "Library/LaunchAgents" / f"{LABEL}.plist"
    ).exists()


def test_install_requires_tracked_permanent_research_watcher(
    tmp_path: Path,
) -> None:
    repo, home, _ = setup_checkout(tmp_path)
    runner = FakeRunner(tracked=False)

    assert main(
        ["install", "--yes"],
        repo_root=repo,
        home=home,
        runner=runner,
        uid=501,
    ) == 69
    assert not any(call[0] == "launchctl" for call in runner.calls)


def test_install_requires_exact_configured_core_handoff_cli_path(
    tmp_path: Path,
) -> None:
    repo, home, _ = setup_checkout(tmp_path)
    research_config = repo / "config/research.json"
    payload = json.loads(research_config.read_text())
    payload["crmCore"]["fundingHandoff"] = "scripts/private_handoff.py"
    research_config.write_text(json.dumps(payload))
    runner = FakeRunner()

    assert main(
        ["install", "--yes"],
        repo_root=repo,
        home=home,
        runner=runner,
        uid=501,
    ) == 69
    assert not any(call[0] == "launchctl" for call in runner.calls)


def test_install_rejects_core_feature_worktree(tmp_path: Path) -> None:
    repo, home, core = setup_checkout(tmp_path)
    worktree_core = tmp_path / ".worktrees" / "core"
    worktree_core.parent.mkdir()
    core.rename(worktree_core)
    research_config = repo / "config/research.json"
    payload = json.loads(research_config.read_text())
    payload["crmCore"]["path"] = str(worktree_core)
    research_config.write_text(json.dumps(payload))
    runner = FakeRunner()

    assert main(
        ["install", "--yes"],
        repo_root=repo,
        home=home,
        runner=runner,
        uid=501,
    ) == 69
    assert not any(call[0] == "launchctl" for call in runner.calls)


@pytest.mark.parametrize(
    ("request_schema", "result_schema", "configured_result_schema"),
    [
        (
            "wrong.request.v1",
            "norman.crm_core.funding_handoff_result.v1",
            "norman.crm_core.funding_handoff_result.v1",
        ),
        (
            "norman.research.funding_handoff.v1",
            "wrong.result.v1",
            "norman.crm_core.funding_handoff_result.v1",
        ),
        (
            "norman.research.funding_handoff.v1",
            "norman.crm_core.funding_handoff_result.v1",
            "wrong.configured-result.v1",
        ),
    ],
)
def test_install_requires_each_handoff_schema_boundary_to_match(
    tmp_path: Path,
    request_schema: str,
    result_schema: str,
    configured_result_schema: str,
) -> None:
    repo, home, core = setup_checkout(tmp_path)
    (core / "scripts/crm_funding_handoff.py").write_text(
        f'REQUEST_SCHEMA_VERSION = "{request_schema}"\n'
        f'RESULT_SCHEMA_VERSION = "{result_schema}"\n'
    )
    watcher_config = repo / "config/funding-watcher.json"
    payload = json.loads(watcher_config.read_text())
    payload["crmResultSchemaVersion"] = configured_result_schema
    watcher_config.write_text(json.dumps(payload))
    runner = FakeRunner()

    assert main(
        ["install", "--yes"],
        repo_root=repo,
        home=home,
        runner=runner,
        uid=501,
    ) == 69
    assert not any(call[0] == "launchctl" for call in runner.calls)


def test_successful_install_is_atomic_loaded_and_idempotent(
    tmp_path: Path,
) -> None:
    repo, home, _ = setup_checkout(tmp_path)
    runner = FakeRunner()
    result = main(
        ["install", "--yes"],
        repo_root=repo,
        home=home,
        python_path=Path("/usr/bin/python3"),
        runner=runner,
        uid=501,
    )
    plist = home / "Library/LaunchAgents" / f"{LABEL}.plist"
    assert result == 0
    assert plist.exists()
    payload = plistlib.loads(plist.read_bytes())
    assert payload["WorkingDirectory"] == str(repo.resolve())
    assert payload["StandardOutPath"].startswith(
        str(home / "Library/Logs/NormanAI/Research/crunchbase-funding-watcher")
    )
    log_root = (
        home / "Library/Logs/NormanAI/Research/crunchbase-funding-watcher"
    ).resolve()
    assert payload["StandardOutPath"] == str(log_root / "stdout.log")
    assert payload["StandardErrorPath"] == str(log_root / "stderr.log")
    assert ["launchctl", "bootstrap", "gui/501", str(plist.resolve())] in runner.calls
    assert list(plist.parent.glob(f".{plist.name}.*.tmp")) == []
    assert plist.stat().st_mode & 0o777 == 0o644

    runner.loaded = True
    before = plist.read_bytes()
    calls_before = len(runner.calls)
    assert main(
        ["install", "--yes"],
        repo_root=repo,
        home=home,
        python_path=Path("/usr/bin/python3"),
        runner=runner,
        uid=501,
    ) == 0
    assert plist.read_bytes() == before
    assert not any(
        call[0] == "plutil"
        or call[:2] in (
            ["launchctl", "bootout"],
            ["launchctl", "bootstrap"],
        )
        for call in runner.calls[calls_before:]
    )


def test_atomic_install_preserves_existing_plist_when_replace_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo, home, _ = setup_checkout(tmp_path)
    plist = home / "Library/LaunchAgents" / f"{LABEL}.plist"
    plist.parent.mkdir(parents=True)
    plist.write_bytes(b"previous-complete-plist")
    runner = FakeRunner()

    def fail_replace(source, destination) -> None:
        assert Path(source).parent == plist.parent
        assert Path(destination) == plist
        raise OSError("injected atomic replace failure")

    monkeypatch.setattr(installer.os, "replace", fail_replace)
    with pytest.raises(OSError, match="injected atomic replace failure"):
        main(
            ["install", "--yes"],
            repo_root=repo,
            home=home,
            python_path=Path("/usr/bin/python3"),
            runner=runner,
            uid=501,
        )

    assert plist.read_bytes() == b"previous-complete-plist"
    assert list(plist.parent.glob(f".{plist.name}.*.tmp")) == []
    assert not any(
        call[:2] == ["launchctl", "bootstrap"] for call in runner.calls
    )


def test_status_checks_both_plist_and_loaded_service(tmp_path: Path) -> None:
    repo, home, _ = setup_checkout(tmp_path)
    runner = FakeRunner(loaded=True)
    assert main(
        ["status"], repo_root=repo, home=home, runner=runner, uid=501
    ) == 69
    plist = home / "Library/LaunchAgents" / f"{LABEL}.plist"
    plist.parent.mkdir(parents=True)
    plist.write_bytes(b"plist")
    assert main(
        ["status"], repo_root=repo, home=home, runner=runner, uid=501
    ) == 0
    runner.loaded = False
    assert main(
        ["status"], repo_root=repo, home=home, runner=runner, uid=501
    ) == 69


def test_uninstall_boots_out_before_removing_and_preserves_on_failure(
    tmp_path: Path,
) -> None:
    _, home, _ = setup_checkout(tmp_path)
    plist = home / "Library/LaunchAgents" / f"{LABEL}.plist"
    plist.parent.mkdir(parents=True)
    plist.write_bytes(b"plist")
    runner = FakeRunner(loaded=True, bootout_code=5)
    assert main(
        ["uninstall", "--yes"], home=home, runner=runner, uid=501
    ) == 70
    assert plist.exists()

    runner.bootout_code = 0
    assert main(
        ["uninstall", "--yes"], home=home, runner=runner, uid=501
    ) == 0
    assert not plist.exists()
    assert runner.bootout_plist_existed == [True, True]
    bootout_index = next(
        index
        for index, call in enumerate(runner.calls)
        if call[:2] == ["launchctl", "bootout"] and index > 0
    )
    assert runner.calls[bootout_index][0:2] == ["launchctl", "bootout"]
