"""Resolve source-tree and packaged policy assets."""

from __future__ import annotations

from importlib import resources
from pathlib import Path

from research_commander.errors import ContractError


def asset_path(relative: str) -> Path:
    source_root = Path(__file__).resolve().parents[2]
    source_path = source_root / relative
    if source_path.is_file():
        return source_path
    packaged = resources.files("research_commander").joinpath(relative)
    candidate = Path(str(packaged))
    if not candidate.is_file():
        raise ContractError(f"required packaged asset is missing: {relative}")
    return candidate


def asset_text(relative: str) -> str:
    return asset_path(relative).read_text(encoding="utf-8")
