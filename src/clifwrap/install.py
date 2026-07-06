from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from .config import DEFAULT_BIN_DIR, SHIM_ENV, state_dir


STATE_FILE = "install-state.json"
SHIM_MARKER = "# managed-by=clifwrap"


@dataclass
class InstalledShim:
    app: str
    target: str
    backup: str


def _state_path() -> Path:
    directory = state_dir()
    directory.mkdir(parents=True, exist_ok=True)
    return directory / STATE_FILE


def load_state() -> dict[str, dict[str, str]]:
    path = _state_path()
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def save_state(state: dict[str, dict[str, str]]) -> None:
    path = _state_path()
    path.write_text(json.dumps(state, indent=2) + "\n")


def discover_target(app: str, bin_dir: Path) -> Path:
    resolved = shutil.which(app)
    if not resolved:
        raise FileNotFoundError(f"Could not find '{app}' on PATH")
    target = Path(resolved)
    if target.parent == bin_dir and _is_managed_shim(target):
        state = load_state().get(app)
        if not state:
            raise RuntimeError(f"{app} is already shimmed but missing state")
        return Path(state["backup"])
    return target


def backup_path_for(target: Path) -> Path:
    backup_root = state_dir() / "originals"
    backup_root.mkdir(parents=True, exist_ok=True)
    return backup_root / target.name


def _shim_contents(app: str) -> str:
    quoted_python = str(Path(sys.executable).resolve()).replace('"', '\\"')
    exec_line = f'exec "{quoted_python}" -m clifwrap shim "$@"'
    return "\n".join(
        [
            "#!/bin/sh",
            SHIM_MARKER,
            f'export {SHIM_ENV}="{app}"',
            exec_line,
            "",
        ]
    )


def _is_managed_shim(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        return SHIM_MARKER in path.read_text()
    except UnicodeDecodeError:
        return False


def is_managed_shim(path: Path) -> bool:
    return _is_managed_shim(path)


def install_shim(app: str, *, bin_dir: Path | None = None) -> InstalledShim:
    bin_dir = (bin_dir or DEFAULT_BIN_DIR).resolve()
    bin_dir.mkdir(parents=True, exist_ok=True)
    state = load_state()
    resolved = shutil.which(app)
    if not resolved:
        raise FileNotFoundError(f"Could not find '{app}' on PATH")
    target = Path(resolved)
    backup = backup_path_for(target)
    if target.exists() and _is_managed_shim(target) and backup.exists():
        if app not in state:
            state[app] = {"target": str(target), "backup": str(backup)}
            save_state(state)
        return InstalledShim(app=app, target=str(target), backup=str(backup))
    if app in state:
        recorded_target = Path(state[app]["target"])
        recorded_backup = Path(state[app]["backup"])
        if recorded_target.exists() and _is_managed_shim(recorded_target) and recorded_backup.exists():
            return InstalledShim(app=app, target=str(recorded_target), backup=str(recorded_backup))
    if backup.exists() and target.exists() and not _is_managed_shim(target):
        raise RuntimeError(
            f"Refusing to install {app}: backup already exists at {backup}, "
            f"but {target} is not a managed shim. Run 'clifwrap uninstall {app}' "
            "or inspect both files before retrying."
        )
    if not backup.exists():
        if target.is_symlink():
            link_target = Path(os.readlink(target))
            if not link_target.is_absolute():
                link_target = (target.parent / link_target).resolve()
            target.unlink()
            backup.symlink_to(link_target)
        else:
            target.rename(backup)
    target.write_text(_shim_contents(app))
    target.chmod(0o755)
    state[app] = {"target": str(target), "backup": str(backup)}
    save_state(state)
    return InstalledShim(app=app, target=str(target), backup=str(backup))


def uninstall_shim(app: str) -> InstalledShim:
    state = load_state()
    info = state.get(app)
    if not info:
        raise FileNotFoundError(f"No installed shim recorded for '{app}'")
    target = Path(info["target"])
    backup = Path(info["backup"])
    if not backup.exists():
        raise FileNotFoundError(f"Cannot uninstall {app}: original backup is missing at {backup}")
    if target.exists() and not _is_managed_shim(target):
        raise RuntimeError(f"Refusing to uninstall {app}: {target} is not a managed clifwrap shim")
    if target.exists() and _is_managed_shim(target):
        target.unlink()
    backup.rename(target)
    state.pop(app, None)
    save_state(state)
    return InstalledShim(app=app, target=str(target), backup=str(backup))


def original_command_for(app: str) -> list[str] | None:
    state = load_state()
    info = state.get(app)
    if info:
        return [info["backup"]]
    resolved = shutil.which(app)
    if resolved:
        return [str(Path(resolved).resolve())]
    return None
