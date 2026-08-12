#!/usr/bin/env python3
"""Install, inspect, or remove the Research funding watcher LaunchAgent."""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import re
import shlex
import stat
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from lib.funding_handoff import (  # noqa: E402
    REQUEST_SCHEMA_VERSION,
    RESULT_SCHEMA_VERSION,
)
from lib.funding_watcher_state import FundingWatcherLedger  # noqa: E402


LABEL = "com.normanai.research.crunchbase-funding-watcher"
CORE_LABEL = "com.normanai.crm-core.crunchbase-funding-watcher"
WATCHER_RELATIVE_PATH = Path("scripts/funding_watcher.py")
CORE_HANDOFF_RELATIVE_PATH = Path("scripts/crm_funding_handoff.py")
SCHEDULE = tuple(
    {"Weekday": weekday, "Hour": hour, "Minute": 0}
    for weekday in (1, 2, 3, 4, 5)  # Mon–Fri (launchd: 0=Sun)
    for hour in (9, 12, 15, 18)
)
Runner = Callable[..., Any]
OK, BAD_ARGS, DEPENDENCY, INTERNAL = 0, 64, 69, 70


def render_plist(
    repo_root: Path,
    home: Path,
    python: Path,
    stdout_path: Path,
    stderr_path: Path,
) -> bytes:
    root = repo_root.resolve()
    command = shlex.join(
        [
            str(python),
            str(root / WATCHER_RELATIVE_PATH),
            "check",
            "--write",
            "--yes",
            "--enforce-schedule",
        ]
    )
    payload = {
        "Label": LABEL,
        "ProgramArguments": [
            "/usr/bin/env",
            "-i",
            f"HOME={home.resolve()}",
            "PATH=/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            "/bin/zsh",
            "-lc",
            f"exec {command}",
        ],
        "WorkingDirectory": str(root),
        "StartCalendarInterval": [dict(item) for item in SCHEDULE],
        "RunAtLoad": False,
        "ProcessType": "Background",
        "StandardOutPath": str(stdout_path),
        "StandardErrorPath": str(stderr_path),
    }
    return plistlib.dumps(payload, sort_keys=True)


def _paths(home: Path) -> dict[str, Path]:
    logs = (
        home
        / "Library/Logs/NormanAI/Research/crunchbase-funding-watcher"
    )
    return {
        "plist": home / "Library/LaunchAgents" / f"{LABEL}.plist",
        "corePlist": home / "Library/LaunchAgents" / f"{CORE_LABEL}.plist",
        "stdout": logs / "stdout.log",
        "stderr": logs / "stderr.log",
    }


def _run(runner: Runner, command: list[str]) -> Any:
    return runner(command, capture_output=True, text=True)


def _loaded(runner: Runner, uid: int) -> bool:
    return (
        _run(runner, ["launchctl", "print", f"gui/{uid}/{LABEL}"]).returncode
        == 0
    )


def _tracked(
    runner: Runner,
    root: Path,
    relative_path: Path,
) -> bool:
    result = _run(
        runner,
        [
            "git",
            "-C",
            str(root),
            "ls-files",
            "--error-unmatch",
            relative_path.as_posix(),
        ],
    )
    return result.returncode == 0


def _load_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"configuration is unreadable: {path}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"configuration must be an object: {path}")
    return payload


def _permanent(path: Path) -> bool:
    return ".worktrees" not in path.resolve().parts


def _path_from_selected_home(value: Any, home: Path) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError("Research funding state directory is not configured")
    if value == "~":
        return home.resolve()
    if value.startswith("~/"):
        return (home / value[2:]).resolve()
    path = Path(value)
    if not path.is_absolute() or value.startswith("~"):
        raise RuntimeError("Research funding state directory must be absolute")
    return path.resolve()


def _bootstrap_complete(ledger: FundingWatcherLedger, source_url: str) -> bool:
    record = ledger.bootstraps.get(source_url)
    if not isinstance(record, dict) or set(record) not in (
        {"completedAt"},
        {"completedAt", "migrated"},
    ):
        return False
    if "migrated" in record and record["migrated"] is not True:
        return False
    completed_at = record.get("completedAt")
    if not isinstance(completed_at, str):
        return False
    try:
        parsed = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _prerequisites(
    *,
    repo_root: Path,
    home: Path,
    runner: Runner,
) -> tuple[bool, str]:
    root = repo_root.resolve()
    watcher = root / WATCHER_RELATIVE_PATH
    if not _permanent(root):
        return False, "activation requires a permanent Research checkout"
    if not watcher.is_file() or not _tracked(
        runner, root, WATCHER_RELATIVE_PATH
    ):
        return False, "funding watcher must exist and be tracked by Git"
    paths = _paths(home)
    if paths["corePlist"].exists():
        return False, "legacy CRM Core funding watcher plist must be absent"
    try:
        watcher_config = _load_object(root / "config/funding-watcher.json")
        research_config = _load_object(root / "config/research.json")
        state = _path_from_selected_home(
            watcher_config["stateDirectory"], home
        )
        ledger = FundingWatcherLedger(state / "ledger.json")
        sources = watcher_config["sources"]
        if (
            not isinstance(sources, list)
            or not sources
            or any(
                not isinstance(source, dict)
                or not isinstance(source.get("url"), str)
                or not _bootstrap_complete(ledger, source["url"])
                for source in sources
            )
        ):
            return False, "Research funding ledger is not bootstrap-complete"
        if watcher_config.get("crmResultSchemaVersion") != RESULT_SCHEMA_VERSION:
            return False, "Research funding result schemaVersion does not match CRMx handoff"
        crmx_config = research_config.get("crmx")
        if not isinstance(crmx_config, dict):
            return False, "permanent CRMx path is not configured"
        crmx_value = crmx_config.get("path")
        if not isinstance(crmx_value, str) or not crmx_value.strip():
            return False, "permanent CRMx path is not configured"
        crmx_root = Path(crmx_value).expanduser()
        if not crmx_root.is_absolute():
            crmx_root = (root / crmx_root).resolve()
        else:
            crmx_root = crmx_root.resolve()
        if not _permanent(crmx_root):
            return False, "activation requires a permanent CRMx checkout"
        db_rel = crmx_config.get("db") or "data/norman.db"
        if not isinstance(db_rel, str) or not db_rel.strip():
            return False, "CRMx db path is invalid"
        db_path = Path(db_rel).expanduser()
        if not db_path.is_absolute():
            db_path = (crmx_root / db_path).resolve()
        if not db_path.is_file():
            return False, f"CRMx SQLite missing: {db_path}"
        ingest_mod = crmx_root / "src/norman/tools/funding_ingest.py"
        if not ingest_mod.is_file():
            return False, "CRMx funding_ingest module is missing"
        uv_check = _run(runner, ["uv", "--version"])
        if uv_check.returncode:
            return False, "uv is required for CRMx funding handoff"
    except (KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
        return False, str(exc)
    return True, ""


def _atomic_write(
    path: Path,
    payload: bytes,
    *,
    mode: int = 0o644,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(mode)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


def _remove_file(path: Path) -> None:
    path.unlink(missing_ok=True)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _rollback_install(
    *,
    plist: Path,
    previous_payload: bytes | None,
    previous_mode: int | None,
    reload_previous: bool,
    runner: Runner,
    uid: int,
) -> list[str]:
    errors: list[str] = []
    restored = False
    try:
        if previous_payload is None:
            _remove_file(plist)
        elif previous_mode is None:
            errors.append("prior plist mode was unavailable")
        else:
            _atomic_write(plist, previous_payload, mode=previous_mode)
            restored = True
    except (OSError, subprocess.SubprocessError) as exc:
        errors.append(f"plist restore failed: {type(exc).__name__}: {exc}")
        if previous_payload is not None and previous_mode is not None:
            try:
                restored = (
                    plist.read_bytes() == previous_payload
                    and stat.S_IMODE(plist.stat().st_mode) == previous_mode
                )
            except OSError as verification_error:
                errors.append(
                    "prior plist verification failed: "
                    f"{type(verification_error).__name__}: "
                    f"{verification_error}"
                )
    if reload_previous:
        if previous_payload is None:
            errors.append("prior loaded service had no restorable plist")
        elif restored:
            try:
                reloaded = _run(
                    runner,
                    [
                        "launchctl",
                        "bootstrap",
                        f"gui/{uid}",
                        str(plist),
                    ],
                )
            except (OSError, subprocess.SubprocessError) as exc:
                errors.append(
                    "prior service reload failed: "
                    f"{type(exc).__name__}: {exc}"
                )
            else:
                if reloaded.returncode:
                    errors.append("prior service reload failed")
    return errors


def _post_replace_failure(
    primary: str,
    *,
    plist: Path,
    previous_payload: bytes | None,
    previous_mode: int | None,
    reload_previous: bool,
    runner: Runner,
    uid: int,
) -> int:
    rollback_errors = _rollback_install(
        plist=plist,
        previous_payload=previous_payload,
        previous_mode=previous_mode,
        reload_previous=reload_previous,
        runner=runner,
        uid=uid,
    )
    if rollback_errors:
        print(
            f"{primary}; rollback failed: {'; '.join(rollback_errors)}",
            file=sys.stderr,
        )
    else:
        print(primary, file=sys.stderr)
    return INTERNAL


def _install(
    *,
    repo_root: Path,
    home: Path,
    python_path: Path,
    runner: Runner,
    uid: int,
) -> int:
    valid, reason = _prerequisites(
        repo_root=repo_root, home=home, runner=runner
    )
    if not valid:
        print(reason, file=sys.stderr)
        return DEPENDENCY
    paths = _paths(home)
    paths["stdout"].parent.mkdir(parents=True, exist_ok=True)
    payload = render_plist(
        repo_root, home, python_path, paths["stdout"], paths["stderr"]
    )
    previous_payload: bytes | None = None
    previous_mode: int | None = None
    if paths["plist"].exists():
        previous_payload = paths["plist"].read_bytes()
        previous_mode = stat.S_IMODE(paths["plist"].stat().st_mode)
    is_loaded = _loaded(runner, uid)
    if previous_payload == payload and is_loaded:
        print(f"{LABEL} is already installed and loaded")
        return OK
    stage = "launchctl bootout"
    bootout_failed = False
    failure: str | None = None
    try:
        if is_loaded:
            bootout = _run(
                runner,
                [
                    "launchctl",
                    "bootout",
                    f"gui/{uid}",
                    str(paths["plist"]),
                ],
            )
            if bootout.returncode:
                bootout_failed = True
        if not bootout_failed:
            stage = "plist installation"
            _atomic_write(paths["plist"], payload)
            stage = "plist validation"
            lint = _run(runner, ["plutil", "-lint", str(paths["plist"])])
            if lint.returncode:
                failure = "plist validation failed"
            else:
                stage = "launchctl bootstrap"
                loaded = _run(
                    runner,
                    [
                        "launchctl",
                        "bootstrap",
                        f"gui/{uid}",
                        str(paths["plist"]),
                    ],
                )
                if loaded.returncode:
                    failure = "launchctl bootstrap failed"
    except (OSError, subprocess.SubprocessError) as exc:
        failure = f"{stage} failed: {type(exc).__name__}: {exc}"
    if bootout_failed:
        print("launchctl bootout failed", file=sys.stderr)
        return INTERNAL
    if failure is not None:
        return _post_replace_failure(
            failure,
            plist=paths["plist"],
            previous_payload=previous_payload,
            previous_mode=previous_mode,
            reload_previous=is_loaded,
            runner=runner,
            uid=uid,
        )
    print(f"installed and loaded {LABEL}")
    return OK


def _uninstall(*, home: Path, runner: Runner, uid: int) -> int:
    plist = _paths(home)["plist"]
    if _loaded(runner, uid):
        result = _run(
            runner,
            ["launchctl", "bootout", f"gui/{uid}", str(plist)],
        )
        if result.returncode:
            print("launchctl bootout failed; plist preserved", file=sys.stderr)
            return INTERNAL
    plist.unlink(missing_ok=True)
    print(f"uninstalled {LABEL}; state and logs preserved")
    return OK


def main(
    argv: list[str] | None = None,
    *,
    repo_root: Path | None = None,
    home: Path | None = None,
    python_path: Path | None = None,
    runner: Runner = subprocess.run,
    uid: int | None = None,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "status", "uninstall"))
    parser.add_argument("--yes", action="store_true")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return OK if exc.code == 0 else BAD_ARGS
    if args.action in {"install", "uninstall"} and not args.yes:
        print("install and uninstall require --yes", file=sys.stderr)
        return BAD_ARGS
    selected_home = (home or Path.home()).resolve()
    selected_uid = os.getuid() if uid is None else uid
    root = (repo_root or ROOT).resolve()
    if args.action == "status":
        plist_exists = _paths(selected_home)["plist"].exists()
        service_loaded = _loaded(runner, selected_uid)
        if plist_exists and service_loaded:
            print(f"{LABEL} is installed and loaded")
            return OK
        print(
            f"{LABEL}: plist={'present' if plist_exists else 'absent'}, "
            f"service={'loaded' if service_loaded else 'not loaded'}"
        )
        return DEPENDENCY
    if args.action == "uninstall":
        return _uninstall(
            home=selected_home, runner=runner, uid=selected_uid
        )
    return _install(
        repo_root=root,
        home=selected_home,
        python_path=(python_path or Path(sys.executable)).resolve(),
        runner=runner,
        uid=selected_uid,
    )


if __name__ == "__main__":
    raise SystemExit(main())
