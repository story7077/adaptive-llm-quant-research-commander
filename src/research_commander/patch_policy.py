"""Validate a Builder patch against the immutable safety boundary."""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from pathlib import PurePosixPath

from research_commander.errors import PatchPolicyError
from research_commander.json_types import JsonObject

DEFAULT_ALLOWED = (
    "src/trading/features/**",
    "src/trading/strategies/**",
    "src/trading/calibration/**",
    "src/trading/research/**",
    "src/trading/experiments/**",
    "config/strategies/**",
    "config/research/**",
    "tests/unit/**",
    "tests/property/**",
    "tests/research/**",
    "docs/research/**",
)

DEFAULT_FORBIDDEN = (
    "src/trading/risk/**",
    "src/trading/execution/**",
    "src/trading/ledger/**",
    "src/trading/persistence/db.py",
    "src/trading/persistence/models.py",
    "src/trading/security/**",
    "src/trading/broker/**",
    "migrations/**",
    ".github/workflows/public-release-security.yml",
    "credentials/**",
    ".env",
    ".env.*",
)

DIFF_HEADER = re.compile(r"^diff --git a/(.+) b/(.+)$")


def _normal_path(value: str) -> str:
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or ".." in path.parts or "\0" in normalized:
        raise PatchPolicyError(f"unsafe patch path: {value}")
    result = path.as_posix()
    if not result or result == ".":
        raise PatchPolicyError("empty patch path")
    return result


def _matches(path: str, patterns: tuple[str, ...] | list[str]) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def changed_paths(patch: str) -> tuple[str, ...]:
    paths: set[str] = set()
    for line in patch.splitlines():
        match = DIFF_HEADER.fullmatch(line)
        if match:
            paths.add(_normal_path(match.group(1)))
            paths.add(_normal_path(match.group(2)))
        elif line.startswith("GIT binary patch"):
            raise PatchPolicyError("binary patches are forbidden")
    if not paths:
        raise PatchPolicyError("patch has no diff headers")
    return tuple(sorted(paths))


@dataclass(frozen=True)
class PatchValidation:
    changed_paths: tuple[str, ...]
    implementation_paths: tuple[str, ...]
    test_paths: tuple[str, ...]


def validate_candidate_patch(
    patch: str,
    proposal: JsonObject,
    *,
    protected_champion_paths: tuple[str, ...] = (),
    allowed: tuple[str, ...] = DEFAULT_ALLOWED,
    forbidden: tuple[str, ...] = DEFAULT_FORBIDDEN,
) -> PatchValidation:
    parent_version = proposal.get("parent_strategy_version")
    proposed_version = proposal.get("proposed_strategy_version")
    if not isinstance(parent_version, str) or not isinstance(proposed_version, str):
        raise PatchPolicyError("proposal strategy versions are malformed")
    if parent_version == proposed_version:
        raise PatchPolicyError("a Challenger must not reuse the Champion version")
    proposed_patterns_value = proposal.get("files_allowed_to_change")
    if not isinstance(proposed_patterns_value, list) or not all(
        isinstance(item, str) for item in proposed_patterns_value
    ):
        raise PatchPolicyError("proposal files_allowed_to_change is malformed")
    proposed_patterns = [item for item in proposed_patterns_value if isinstance(item, str)]
    paths = changed_paths(patch)
    for path in paths:
        if _matches(path, forbidden):
            raise PatchPolicyError(f"forbidden path changed: {path}")
        if _matches(path, list(protected_champion_paths)):
            raise PatchPolicyError(f"Champion path changed in place: {path}")
        if not _matches(path, allowed):
            raise PatchPolicyError(f"path is outside the repository allowlist: {path}")
        if not _matches(path, proposed_patterns):
            raise PatchPolicyError(f"path is outside the approved proposal: {path}")
    implementation = tuple(
        path
        for path in paths
        if path.startswith(("src/trading/", "config/strategies/", "config/research/"))
    )
    tests = tuple(path for path in paths if path.startswith("tests/"))
    if not implementation:
        raise PatchPolicyError("candidate has no strategy implementation")
    if not tests:
        raise PatchPolicyError("candidate has no tests")
    return PatchValidation(paths, implementation, tests)
