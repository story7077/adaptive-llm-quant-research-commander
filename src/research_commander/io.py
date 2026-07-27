"""UTF-8-only, exclusive JSON persistence."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import cast

from research_commander.canonical import canonical_json_bytes
from research_commander.errors import ContractError
from research_commander.json_types import JsonObject, JsonValue


def load_json_object(path: Path) -> JsonObject:
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot load UTF-8 JSON: {path}") from exc
    if not isinstance(raw, dict):
        raise ContractError(f"expected a JSON object: {path}")
    return cast(JsonObject, raw)


def write_json_exclusive(path: Path, value: JsonValue) -> None:
    """Create a JSON file once; never replace an existing append-only artifact."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = canonical_json_bytes(value) + b"\n"
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise ContractError(f"refusing to overwrite immutable artifact: {path}") from exc
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def write_text_exclusive(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise ContractError(f"refusing to overwrite immutable artifact: {path}") from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
