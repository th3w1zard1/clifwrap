from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import tomllib

__all__ = ["__version__"]


def _source_tree_version() -> str:
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    if not pyproject.exists():
        return "0+unknown"
    return str(tomllib.loads(pyproject.read_text()).get("project", {}).get("version", "0+unknown"))


try:
    __version__ = version("clifwrap")
except PackageNotFoundError:
    __version__ = _source_tree_version()
