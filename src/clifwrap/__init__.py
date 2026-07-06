from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import sys
import tomllib

__all__ = ["__version__"]

_FROZEN_VERSION = "0.2.2"  # x-release-please-version


def _source_tree_version() -> str:
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    if not pyproject.exists():
        return "0+unknown"
    return str(tomllib.loads(pyproject.read_text()).get("project", {}).get("version", "0+unknown"))


_source_version = _source_tree_version()
if _source_version != "0+unknown":
    __version__ = _source_version
elif getattr(sys, "frozen", False):
    __version__ = _FROZEN_VERSION
else:
    try:
        __version__ = version("clifwrap")
    except PackageNotFoundError:
        __version__ = _FROZEN_VERSION
