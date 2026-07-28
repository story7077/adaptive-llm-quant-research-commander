"""Create a sanitized, history-free source snapshot for one run."""

from __future__ import annotations

import fnmatch
import os
import shutil
from pathlib import Path

from research_commander.canonical import hash_file, hash_json
from research_commander.errors import IsolationError, PublicSafetyError
from research_commander.json_types import JsonObject, JsonValue

MAX_SOURCE_FILE_BYTES = 5 * 1024 * 1024
PROHIBITED_NAMES = {
    ".env",
    ".local",
    "auth.json",
    "credentials",
    "secrets",
    "cookies.json",
    "user-data-dir",
    "raw",
}


def _matches_any(relative: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(relative, pattern) for pattern in patterns)


def _looks_binary(path: Path) -> bool:
    with path.open("rb") as stream:
        chunk = stream.read(8192)
    return b"\0" in chunk


def _safe_relative(path: Path, root: Path) -> str:
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError as exc:
        raise IsolationError(f"path escapes source root: {path}") from exc
    if relative.startswith("../") or relative.startswith("/"):
        raise IsolationError(f"unsafe snapshot path: {relative}")
    return relative


def create_clean_snapshot(
    source_root: Path,
    destination: Path,
    *,
    allowlist: list[str],
) -> JsonObject:
    """Copy only allowlisted UTF-8/public-safe source files, without Git history."""
    source = source_root.resolve(strict=True)
    if not source.is_dir():
        raise IsolationError("source snapshot root must be a directory")
    if destination.exists():
        raise IsolationError(f"snapshot destination already exists: {destination}")
    destination.mkdir(parents=True, exist_ok=False)
    files: list[JsonValue] = []
    for current, directory_names, file_names in os.walk(source):
        current_path = Path(current)
        directory_names[:] = sorted(name for name in directory_names if name != ".git")
        for directory_name in directory_names:
            directory_path = current_path / directory_name
            if directory_path.is_symlink():
                raise PublicSafetyError(f"source contains a symlink: {directory_path}")
            if directory_name.casefold() in PROHIBITED_NAMES:
                raise PublicSafetyError(f"source contains prohibited directory: {directory_path}")
        for file_name in sorted(file_names):
            source_path = current_path / file_name
            if source_path.is_symlink():
                raise PublicSafetyError(f"source contains a symlink: {source_path}")
            relative = _safe_relative(source_path, source)
            if not _matches_any(relative, allowlist):
                continue
            if file_name.casefold() in PROHIBITED_NAMES or file_name.casefold().startswith(".env."):
                raise PublicSafetyError(f"source contains prohibited file: {relative}")
            size = source_path.stat().st_size
            if size > MAX_SOURCE_FILE_BYTES:
                raise PublicSafetyError(f"source file exceeds snapshot size limit: {relative}")
            if _looks_binary(source_path):
                raise PublicSafetyError(f"binary source is not permitted: {relative}")
            target = destination / Path(relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source_path, target)
            files.append(
                {
                    "path": relative,
                    "size": size,
                    "sha256": hash_file(target),
                }
            )
    if not files:
        raise PublicSafetyError("clean source snapshot contains no files")
    manifest: JsonObject = {
        "schema_version": "CleanSourceSnapshotManifestV1",
        "history_included": False,
        "symlinks_included": False,
        "files": files,
    }
    manifest["tree_hash"] = hash_json(files)
    return manifest
