from __future__ import annotations

import shutil
from copy import deepcopy
from pathlib import Path

import pytest
from conftest import Bundle

from research_commander.candidate import (
    MANDATORY_FALSIFICATION_TESTS,
    build_challenger_manifest,
    deterministic_patch,
)
from research_commander.errors import PatchPolicyError
from research_commander.json_types import JsonObject
from research_commander.layout import RunLayout
from research_commander.patch_policy import validate_candidate_patch


def _candidate(base: Path, destination: Path) -> Path:
    shutil.copytree(base, destination)
    strategy = destination / "src/trading/strategies/alpha_v2/model.py"
    strategy.parent.mkdir(parents=True)
    strategy.write_text(
        (
            'VERSION = "1.1.0"\n'
            'FEATURE = "five_session_reversal"\n\n'
            "def decide(request: object) -> dict[str, object]:\n"
            '    return {"request": request}\n'
        ),
        encoding="utf-8",
    )
    test = destination / "tests/unit/test_alpha_v2.py"
    test.write_text(
        "def test_date_shift_placebo() -> None:\n    assert True\n",
        encoding="utf-8",
    )
    return destination


def test_deterministic_patch_and_manifest(
    prepared_run: RunLayout,
    bundle: Bundle,
    proposal: JsonObject,
    tmp_path: Path,
) -> None:
    candidate = _candidate(prepared_run.source_snapshot, tmp_path / "candidate")
    patch = deterministic_patch(prepared_run.source_snapshot, candidate)
    assert patch == deterministic_patch(prepared_run.source_snapshot, candidate)
    test_manifest: JsonObject = {
        "schema_version": "candidate_test_manifest_v1",
        "tests": ["date_shift_placebo"],
    }
    first, validation, validation_request = build_challenger_manifest(
        request=bundle.request,
        proposal=proposal,
        patch=patch,
        candidate_root=candidate,
        test_manifest=test_manifest,
    )
    second, _, second_validation_request = build_challenger_manifest(
        request=bundle.request,
        proposal=proposal,
        patch=patch,
        candidate_root=candidate,
        test_manifest=test_manifest,
    )
    assert first == second
    assert validation_request == second_validation_request
    assert first["status"] == "PROPOSED"
    assert isinstance(first["manifest_hash"], str)
    assert first["proposal_hash"] == proposal["proposal_hash"]
    assert validation.test_paths == ("tests/unit/test_alpha_v2.py",)
    mandatory_ids = validation_request.get("mandatory_test_ids")
    assert isinstance(mandatory_ids, list)
    assert all(isinstance(item, str) for item in mandatory_ids)
    assert {item for item in mandatory_ids if isinstance(item, str)} == set(
        MANDATORY_FALSIFICATION_TESTS
    )
    assert validation_request["raw_oos_access_permitted"] is False
    assert validation_request["automatic_promotion_permitted"] is False


def test_risk_execution_and_ledger_paths_are_rejected(
    proposal: JsonObject,
) -> None:
    proposal["files_allowed_to_change"] = ["src/trading/risk/**", "tests/unit/**"]
    patch = (
        "diff --git a/src/trading/risk/limits.py b/src/trading/risk/limits.py\n"
        "--- a/src/trading/risk/limits.py\n"
        "+++ b/src/trading/risk/limits.py\n"
        "@@ -1 +1 @@\n-old\n+unsafe\n"
        "diff --git a/tests/unit/test_limits.py b/tests/unit/test_limits.py\n"
        "--- /dev/null\n+++ b/tests/unit/test_limits.py\n@@ -0,0 +1 @@\n+assert True\n"
    )
    with pytest.raises(PatchPolicyError, match="forbidden path"):
        validate_candidate_patch(patch, proposal)


def test_champion_path_cannot_be_changed_in_place(
    proposal: JsonObject,
) -> None:
    proposal["files_allowed_to_change"] = [
        "src/trading/strategies/**",
        "tests/unit/**",
    ]
    patch = (
        "diff --git a/src/trading/strategies/alpha_v1/model.py "
        "b/src/trading/strategies/alpha_v1/model.py\n"
        "--- a/src/trading/strategies/alpha_v1/model.py\n"
        "+++ b/src/trading/strategies/alpha_v1/model.py\n"
        "@@ -1 +1 @@\n-old\n+modified\n"
        "diff --git a/tests/unit/test_alpha.py b/tests/unit/test_alpha.py\n"
        "--- /dev/null\n+++ b/tests/unit/test_alpha.py\n@@ -0,0 +1 @@\n+assert True\n"
    )
    with pytest.raises(PatchPolicyError, match="Champion path changed"):
        validate_candidate_patch(
            patch,
            proposal,
            protected_champion_paths=("src/trading/strategies/alpha_v1/**",),
        )


def test_same_strategy_version_is_not_a_challenger(
    proposal: JsonObject,
) -> None:
    invalid = deepcopy(proposal)
    invalid["proposed_strategy_version"] = invalid["parent_strategy_version"]
    patch = (
        "diff --git a/src/trading/strategies/alpha_v2/model.py "
        "b/src/trading/strategies/alpha_v2/model.py\n"
        "--- /dev/null\n+++ b/src/trading/strategies/alpha_v2/model.py\n"
        "@@ -0,0 +1 @@\n+value = 1\n"
        "diff --git a/tests/unit/test_alpha.py b/tests/unit/test_alpha.py\n"
        "--- /dev/null\n+++ b/tests/unit/test_alpha.py\n@@ -0,0 +1 @@\n+assert True\n"
    )
    with pytest.raises(PatchPolicyError, match="must not reuse"):
        validate_candidate_patch(patch, invalid)
