"""Deterministic JSON and file-tree hashing."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

from research_commander.errors import ContractError
from research_commander.json_types import JsonValue


def _canonical_json_value(value: JsonValue) -> JsonValue:
    """Match the public trading host's canonical JSON number normalization."""
    if isinstance(value, list):
        return [_canonical_json_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _canonical_json_value(value[key]) for key in sorted(value)}
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ContractError("non-finite float cannot be canonicalized")
        return float(format(value, ".12g"))
    return value


def canonical_json_bytes(value: JsonValue) -> bytes:
    """Return the repository's canonical UTF-8 JSON representation."""
    return json.dumps(
        _canonical_json_value(value),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def hash_json(value: JsonValue) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def hash_tree(root: Path) -> str:
    """Hash regular files by normalized path and content, rejecting symlinks."""
    resolved_root = root.resolve(strict=True)
    entries: list[JsonValue] = []
    for path in sorted(resolved_root.rglob("*")):
        if path.is_symlink():
            raise ContractError(f"tree contains a symlink: {path}")
        if path.is_file():
            relative = path.relative_to(resolved_root).as_posix()
            entries.append(
                {
                    "path": relative,
                    "size": path.stat().st_size,
                    "sha256": hash_file(path),
                }
            )
    return hash_json(entries)
