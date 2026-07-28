"""Validate a Builder patch against the immutable safety boundary."""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath
from typing import cast

from research_commander.canonical import hash_json
from research_commander.errors import PatchPolicyError
from research_commander.json_types import JsonObject, JsonValue

# V1 remains the default for historical AlgorithmProposalV1 artifacts.
CANDIDATE_PATCH_POLICY_V1 = "candidate_patch_policy_v1"
CANDIDATE_PATCH_POLICY_V2 = "candidate_patch_policy_v2"

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

DEFAULT_ALLOWED_PREFIXES = (
    "src/trading/features/",
    "src/trading/strategies/",
    "src/trading/calibration/",
    "src/trading/research/",
    "src/trading/experiments/",
    "config/strategies/",
    "config/research/",
    "tests/unit/",
    "tests/property/",
    "tests/research/",
    "docs/research/",
)

DEFAULT_FORBIDDEN_PREFIXES = (
    "src/trading/risk/",
    "src/trading/execution/",
    "src/trading/ledger/",
    "src/trading/security/",
    "src/trading/broker/",
    "migrations/",
    "credentials/",
)

DEFAULT_FORBIDDEN_EXACT = (
    "src/trading/persistence/db.py",
    "src/trading/persistence/models.py",
    ".github/workflows/public-release-security.yml",
)

V2_ALLOWED_PATTERNS = (
    "src/trading/strategies/challengers/**",
    "src/trading/features/challengers/**",
    "src/trading/calibration/challengers/**",
    "src/trading/experiments/challengers/**",
    "config/strategies/challengers/**",
    "tests/candidates/**",
    "docs/research/challengers/**",
)

V2_FORBIDDEN_PATTERNS = (
    "src/trading/research/**",
    "src/trading/persistence/**",
    "src/trading/execution/**",
    "src/trading/risk/**",
    "src/trading/ledger/**",
    "src/trading/security/**",
    "src/trading/broker/**",
    "config/research/**",
    "tests/research/**",
    "migrations/**",
    ".github/**",
)

CANDIDATE_PATCH_POLICY_V2_CONTRACT = {
    "schema_version": "candidate_patch_policy_contract_v1",
    "policy_version": CANDIDATE_PATCH_POLICY_V2,
    "path_match_semantics": "POSIX_GLOB_V1",
    "allowed_paths": V2_ALLOWED_PATTERNS,
    "forbidden_paths": V2_FORBIDDEN_PATTERNS,
    "candidate_implementation_required": True,
    "candidate_test_required": True,
    "relative_paths_only": True,
    "symlinks_forbidden": True,
    "new_files_only": True,
}
CANDIDATE_PATCH_POLICY_V2_CONTRACT_HASH = hash_json(
    cast(JsonValue, CANDIDATE_PATCH_POLICY_V2_CONTRACT)
)


class CandidatePatchPolicyVersion(StrEnum):
    V1 = CANDIDATE_PATCH_POLICY_V1
    V2 = CANDIDATE_PATCH_POLICY_V2


DIFF_HEADER = re.compile(r"^diff --git a/(.+) b/(.+)$")
V2_DIFF_HEADER = re.compile(
    r"^diff --git a/([^ \t\r\n]+) b/([^ \t\r\n]+)$"
)
SYMLINK_MODE = re.compile(
    r"^(?:new file mode|old mode|new mode) 120000$",
    re.MULTILINE,
)
V2_NEW_FILE_HUNK = re.compile(
    r"^@@ -0,0 \+[1-9][0-9]*(?:,[1-9][0-9]*)? @@(?: .*)?$"
)
V2_NEW_FILE_METADATA = re.compile(
    r"^(?:new file mode (?!120000$)[0-7]{6}|"
    r"index 0+\.\.[0-9a-fA-F]+(?: [0-7]{6})?)$"
)


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
    policy_version: CandidatePatchPolicyVersion = CandidatePatchPolicyVersion.V1
    policy_contract_hash: str | None = None


def select_candidate_patch_policy_version(
    proposal: JsonObject,
    explicit: CandidatePatchPolicyVersion | str | None = None,
) -> CandidatePatchPolicyVersion:
    """Select V2 explicitly or automatically for a future AlgorithmProposalV2."""
    inferred = (
        CandidatePatchPolicyVersion.V2
        if proposal.get("schema_version") == "algorithm_proposal_v2"
        else CandidatePatchPolicyVersion.V1
    )
    if explicit is None:
        return inferred
    try:
        selected = CandidatePatchPolicyVersion(explicit)
    except ValueError:
        raise PatchPolicyError(f"unsupported candidate patch policy: {explicit}") from None
    if inferred is CandidatePatchPolicyVersion.V2 and selected is not inferred:
        raise PatchPolicyError("AlgorithmProposalV2 cannot downgrade its candidate patch policy")
    return selected


def candidate_patch_policy_contract_hash(
    policy_version: CandidatePatchPolicyVersion | str,
) -> str:
    try:
        version = CandidatePatchPolicyVersion(policy_version)
    except ValueError:
        raise PatchPolicyError(f"unsupported candidate patch policy: {policy_version}") from None
    if version is CandidatePatchPolicyVersion.V2:
        return CANDIDATE_PATCH_POLICY_V2_CONTRACT_HASH
    return hash_json(
        cast(
            JsonValue,
            {
                "policy_version": CANDIDATE_PATCH_POLICY_V1,
                "allowed_prefixes": DEFAULT_ALLOWED_PREFIXES,
                "forbidden_prefixes": DEFAULT_FORBIDDEN_PREFIXES,
                "forbidden_exact": DEFAULT_FORBIDDEN_EXACT,
            },
        )
    )


def _normal_v2_path(value: str) -> str:
    normalized = value.replace("\\", "/").strip()
    raw_parts = normalized.split("/")
    path = PurePosixPath(normalized)
    if (
        not normalized
        or normalized.startswith("/")
        or path.is_absolute()
        or ":" in raw_parts[0]
        or any(part in {"", ".", ".."} for part in raw_parts)
        or "\0" in normalized
    ):
        raise PatchPolicyError(f"unsafe patch path: {value}")
    return path.as_posix()


def _changed_paths_v2(patch: str) -> tuple[str, ...]:
    if "\0" in patch:
        raise PatchPolicyError("binary patches are forbidden")
    if re.search(
        r"^(?:old mode|deleted file mode|rename from|rename to|"
        r"copy from|copy to)\b",
        patch,
        re.MULTILINE,
    ):
        raise PatchPolicyError("candidate patch policy V2 permits new files only")
    if SYMLINK_MODE.search(patch) is not None:
        raise PatchPolicyError("symbolic-link candidate patches are forbidden")
    if "GIT binary patch" in patch or "Binary files " in patch:
        raise PatchPolicyError("binary patches are forbidden")

    lines = patch.splitlines()
    section_starts = [
        index for index, line in enumerate(lines) if line.startswith("diff --git ")
    ]
    if not section_starts:
        raise PatchPolicyError("patch has no diff headers")
    if any(line for line in lines[: section_starts[0]]):
        raise PatchPolicyError("candidate patch has content before its first section")

    paths: list[str] = []
    for position, start in enumerate(section_starts):
        end = (
            section_starts[position + 1]
            if position + 1 < len(section_starts)
            else len(lines)
        )
        header = V2_DIFF_HEADER.fullmatch(lines[start])
        if header is None:
            raise PatchPolicyError("malformed candidate diff section header")
        old_path = _normal_v2_path(header.group(1))
        new_path = _normal_v2_path(header.group(2))
        if old_path != new_path:
            raise PatchPolicyError(
                "candidate patch policy V2 forbids rename or copy sections"
            )
        if new_path in paths:
            raise PatchPolicyError(
                f"candidate patch contains duplicate diff section: {new_path}"
            )

        body = lines[start + 1 : end]
        try:
            from_index = body.index("--- /dev/null")
            to_index = body.index(f"+++ b/{new_path}")
        except ValueError as exc:
            raise PatchPolicyError(
                f"candidate patch policy V2 requires a new-file section: {new_path}"
            ) from exc
        if to_index != from_index + 1:
            raise PatchPolicyError(
                f"malformed new-file section headers: {new_path}"
            )
        if any(
            V2_NEW_FILE_METADATA.fullmatch(line) is None
            for line in body[:from_index]
        ):
            raise PatchPolicyError(
                f"unsupported new-file section metadata: {new_path}"
            )

        hunks = body[to_index + 1 :]
        if not hunks or V2_NEW_FILE_HUNK.fullmatch(hunks[0]) is None:
            raise PatchPolicyError(
                f"candidate new-file section has no valid hunk: {new_path}"
            )
        for line in hunks:
            if line.startswith("@@ "):
                if V2_NEW_FILE_HUNK.fullmatch(line) is None:
                    raise PatchPolicyError(
                        f"candidate new-file hunk is malformed: {new_path}"
                    )
            elif not line.startswith(("+", r"\ No newline at end of file")):
                raise PatchPolicyError(
                    f"candidate patch policy V2 forbids existing-file content: {new_path}"
                )
        paths.append(new_path)
    return tuple(sorted(paths))


def _validate_candidate_patch_v2(
    patch: str,
    proposal: JsonObject,
    *,
    protected_champion_paths: tuple[str, ...],
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
    paths = _changed_paths_v2(patch)
    for path in paths:
        if _matches(path, V2_FORBIDDEN_PATTERNS):
            raise PatchPolicyError(f"forbidden path changed: {path}")
        if _matches(path, list(protected_champion_paths)):
            raise PatchPolicyError(f"Champion path changed in place: {path}")
        if not _matches(path, V2_ALLOWED_PATTERNS):
            raise PatchPolicyError(f"path is outside the repository allowlist: {path}")
        if not _matches(path, proposed_patterns):
            raise PatchPolicyError(f"path is outside the approved proposal: {path}")
    implementation_prefixes = (
        "src/trading/strategies/challengers/",
        "src/trading/features/challengers/",
        "src/trading/calibration/challengers/",
        "src/trading/experiments/challengers/",
        "config/strategies/challengers/",
    )
    implementation = tuple(path for path in paths if path.startswith(implementation_prefixes))
    tests = tuple(path for path in paths if path.startswith("tests/candidates/"))
    if not implementation:
        raise PatchPolicyError("candidate has no strategy implementation")
    if not tests:
        raise PatchPolicyError("candidate has no tests")
    return PatchValidation(
        paths,
        implementation,
        tests,
        CandidatePatchPolicyVersion.V2,
        CANDIDATE_PATCH_POLICY_V2_CONTRACT_HASH,
    )


def validate_candidate_patch(
    patch: str,
    proposal: JsonObject,
    *,
    protected_champion_paths: tuple[str, ...] = (),
    allowed: tuple[str, ...] = DEFAULT_ALLOWED,
    forbidden: tuple[str, ...] = DEFAULT_FORBIDDEN,
    policy_version: CandidatePatchPolicyVersion | str | None = None,
) -> PatchValidation:
    selected_policy = select_candidate_patch_policy_version(
        proposal,
        policy_version,
    )
    if selected_policy is CandidatePatchPolicyVersion.V2:
        if allowed != DEFAULT_ALLOWED or forbidden != DEFAULT_FORBIDDEN:
            raise PatchPolicyError("candidate patch policy V2 cannot be caller-customized")
        return _validate_candidate_patch_v2(
            patch,
            proposal,
            protected_champion_paths=protected_champion_paths,
        )
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
