from __future__ import annotations

import json
import re
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import cast

import pytest

import research_commander.candidate_testing as candidate_testing_module
from research_commander.assets import asset_text
from research_commander.canonical import canonical_json_bytes, hash_json
from research_commander.errors import IsolationError, PatchPolicyError
from research_commander.io import load_json_object
from research_commander.json_types import JsonObject, JsonValue
from research_commander.layout import RunLayout
from research_commander.patch_policy import (
    CANDIDATE_PATCH_POLICY_V1,
    CANDIDATE_PATCH_POLICY_V2,
    CANDIDATE_PATCH_POLICY_V2_CONTRACT,
    CANDIDATE_PATCH_POLICY_V2_CONTRACT_HASH,
    V2_ALLOWED_PATTERNS,
    V2_FORBIDDEN_PATTERNS,
    CandidatePatchPolicyVersion,
    candidate_patch_policy_contract_hash,
    select_candidate_patch_policy_version,
    validate_candidate_patch,
)
from research_commander.sandbox import (
    DockerBackend,
    InvocationRole,
    load_invocation_plan,
    prepare_invocation,
)
from research_commander.schema_store import schema_path

EXPECTED_V2_CONTRACT_HASH = "73af5956c12a042eb99c0c15929b7f4db2b3b45110373204d39c8163fedc716c"


def _proposal(*, schema_version: str = "algorithm_proposal_v2") -> JsonObject:
    return {
        "schema_version": schema_version,
        "parent_strategy_version": "1.0.0",
        "proposed_strategy_version": "2.0.0",
        "files_allowed_to_change": [
            "src/trading/strategies/challengers/**",
            "src/trading/features/challengers/**",
            "src/trading/calibration/challengers/**",
            "src/trading/experiments/challengers/**",
            "config/strategies/challengers/**",
            "tests/candidates/**",
            "docs/research/challengers/**",
        ],
    }


def _patch(*paths: str) -> str:
    sections: list[str] = []
    for path in paths:
        sections.append(f"diff --git a/{path} b/{path}\n")
        sections.append("--- /dev/null\n")
        sections.append(f"+++ b/{path}\n")
        sections.append("@@ -0,0 +1 @@\n")
        sections.append("+synthetic = True\n")
    return "".join(sections)


def _modified_patch(path: str) -> str:
    return (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        "@@ -1 +1 @@\n"
        "-synthetic = False\n"
        "+synthetic = True\n"
    )


def _deleted_patch(path: str) -> str:
    return (
        f"diff --git a/{path} b/{path}\n"
        "deleted file mode 100644\n"
        f"--- a/{path}\n"
        "+++ /dev/null\n"
        "@@ -1 +0,0 @@\n"
        "-synthetic = True\n"
    )


def _valid_v2_paths() -> tuple[str, str]:
    return (
        "src/trading/strategies/challengers/alpha_v2/model.py",
        "tests/candidates/test_alpha_v2.py",
    )


def test_v2_contract_and_hash_match_the_public_host_authority() -> None:
    assert CANDIDATE_PATCH_POLICY_V2_CONTRACT == {
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
    assert CANDIDATE_PATCH_POLICY_V2_CONTRACT_HASH == EXPECTED_V2_CONTRACT_HASH
    assert (
        hash_json(cast(JsonValue, CANDIDATE_PATCH_POLICY_V2_CONTRACT)) == EXPECTED_V2_CONTRACT_HASH
    )
    assert (
        candidate_patch_policy_contract_hash(CandidatePatchPolicyVersion.V2)
        == EXPECTED_V2_CONTRACT_HASH
    )


def test_v2_contract_fixture_matches_the_runtime_contract() -> None:
    repository_root = Path(__file__).resolve().parents[1]
    fixture = json.loads(
        (
            repository_root
            / "contracts"
            / "candidate-patch-policy-v2.json"
        ).read_text(encoding="utf-8")
    )
    assert fixture == json.loads(
        json.dumps(CANDIDATE_PATCH_POLICY_V2_CONTRACT)
    )
    assert hash_json(cast(JsonValue, fixture)) == EXPECTED_V2_CONTRACT_HASH


def test_policy_selection_is_explicit_or_automatic_for_algorithm_proposal_v2() -> None:
    legacy = _proposal(schema_version="algorithm_proposal_v1")
    recursive = _proposal()
    assert select_candidate_patch_policy_version(legacy) is CandidatePatchPolicyVersion.V1
    assert (
        select_candidate_patch_policy_version(
            legacy,
            CANDIDATE_PATCH_POLICY_V2,
        )
        is CandidatePatchPolicyVersion.V2
    )
    assert select_candidate_patch_policy_version(recursive) is CandidatePatchPolicyVersion.V2
    with pytest.raises(PatchPolicyError, match="cannot downgrade"):
        select_candidate_patch_policy_version(
            recursive,
            CANDIDATE_PATCH_POLICY_V1,
        )
    with pytest.raises(PatchPolicyError, match="unsupported"):
        select_candidate_patch_policy_version(legacy, "candidate_patch_policy_v999")


@pytest.mark.parametrize(
    "implementation",
    [
        "src/trading/strategies/challengers/alpha_v2/model.py",
        "src/trading/features/challengers/alpha_v2/reversal.py",
        "src/trading/calibration/challengers/alpha_v2/parameters.py",
        "src/trading/experiments/challengers/alpha_v2/variants.py",
        "config/strategies/challengers/alpha_v2.json",
    ],
)
def test_v2_accepts_each_challenger_implementation_namespace(
    implementation: str,
) -> None:
    test = "tests/candidates/test_alpha_v2.py"
    validation = validate_candidate_patch(
        _patch(implementation, test),
        _proposal(),
    )
    assert validation.policy_version is CandidatePatchPolicyVersion.V2
    assert validation.policy_contract_hash == EXPECTED_V2_CONTRACT_HASH
    assert validation.implementation_paths == (implementation,)
    assert validation.test_paths == (test,)


def test_explicit_v2_uses_the_same_policy_for_algorithm_proposal_v1() -> None:
    implementation, test = _valid_v2_paths()
    validation = validate_candidate_patch(
        _patch(implementation, test),
        _proposal(schema_version="algorithm_proposal_v1"),
        policy_version=CANDIDATE_PATCH_POLICY_V2,
    )
    assert validation.policy_version is CandidatePatchPolicyVersion.V2
    assert validation.policy_contract_hash == EXPECTED_V2_CONTRACT_HASH


def test_v2_allows_only_challenger_documentation_as_a_nonimplementation_change() -> None:
    implementation, test = _valid_v2_paths()
    documentation = "docs/research/challengers/alpha_v2.md"
    validation = validate_candidate_patch(
        _patch(implementation, documentation, test),
        _proposal(),
    )
    assert validation.changed_paths == (documentation, implementation, test)


def test_default_v1_patch_policy_remains_unchanged() -> None:
    proposal = _proposal(schema_version="algorithm_proposal_v1")
    proposal["files_allowed_to_change"] = [
        "src/trading/strategies/**",
        "tests/unit/**",
    ]
    validation = validate_candidate_patch(
        _patch(
            "src/trading/strategies/alpha_v2/model.py",
            "tests/unit/test_alpha_v2.py",
        ),
        proposal,
    )
    assert validation.policy_version is CandidatePatchPolicyVersion.V1
    assert validation.policy_contract_hash is None


def test_v2_keeps_the_approved_proposal_as_an_additional_narrowing_overlay() -> None:
    implementation, test = _valid_v2_paths()
    proposal = _proposal()
    proposal["files_allowed_to_change"] = ["tests/candidates/**"]
    with pytest.raises(PatchPolicyError, match="outside the approved proposal"):
        validate_candidate_patch(
            _patch(implementation, test),
            proposal,
        )


def test_v2_rejects_a_protected_champion_path_inside_the_challenger_namespace() -> None:
    implementation, test = _valid_v2_paths()
    with pytest.raises(PatchPolicyError, match="Champion path changed"):
        validate_candidate_patch(
            _patch(implementation, test),
            _proposal(),
            protected_champion_paths=(implementation,),
        )


@pytest.mark.parametrize(
    "sensitive_path",
    [
        "src/trading/research/meta_controller.py",
        "src/trading/evaluation/sharpe.py",
        "src/trading/research/oos_worker.py",
        "src/trading/research/promotion.py",
        "config/research/research-plane.yaml",
        "src/trading/persistence/models.py",
        "src/trading/execution/paper.py",
        "src/trading/risk/state_machine.py",
        "src/trading/ledger/service.py",
        "src/trading/security/release.py",
        "src/trading/broker/alpaca.py",
        "migrations/versions/9999_recursive_candidate.py",
        "tests/research/test_trusted_promotion_evidence.py",
        ".github/workflows/public-release-security.yml",
    ],
)
def test_v2_rejects_trusted_host_and_evaluation_paths(
    sensitive_path: str,
) -> None:
    implementation, test = _valid_v2_paths()
    proposal = _proposal()
    allowed = proposal["files_allowed_to_change"]
    assert isinstance(allowed, list)
    allowed.append(sensitive_path)
    with pytest.raises(
        PatchPolicyError,
        match=r"forbidden path|outside the repository allowlist",
    ):
        validate_candidate_patch(
            _patch(implementation, sensitive_path, test),
            proposal,
        )


@pytest.mark.parametrize(
    "unsafe_path",
    [
        "../src/trading/strategies/challengers/escape.py",
        "/src/trading/strategies/challengers/absolute.py",
        "C:/src/trading/strategies/challengers/drive.py",
        r"..\src\trading\strategies\challengers\backslash.py",
        "src/trading/strategies/challengers/../../research/oos_worker.py",
        "./src/trading/strategies/challengers/dot.py",
    ],
)
def test_v2_rejects_relative_path_bypasses(unsafe_path: str) -> None:
    implementation, test = _valid_v2_paths()
    with pytest.raises(PatchPolicyError, match="unsafe patch path"):
        validate_candidate_patch(
            _patch(implementation, unsafe_path, test),
            _proposal(),
        )


def test_v2_rejects_symbolic_link_patch_mode() -> None:
    implementation, test = _valid_v2_paths()
    patch = (
        f"diff --git a/{implementation} b/{implementation}\n"
        "new file mode 120000\n"
        "--- /dev/null\n"
        f"+++ b/{implementation}\n"
        "@@ -0,0 +1 @@\n"
        "+../../research/oos_worker.py\n" + _patch(test)
    )
    with pytest.raises(PatchPolicyError, match="symbolic-link"):
        validate_candidate_patch(patch, _proposal())


@pytest.mark.parametrize("patch_factory", (_modified_patch, _deleted_patch))
def test_v2_rejects_changes_to_existing_files(
    patch_factory: Callable[[str], str],
) -> None:
    implementation, test = _valid_v2_paths()
    with pytest.raises(PatchPolicyError, match=r"new files only|new-file"):
        validate_candidate_patch(
            patch_factory(implementation) + _patch(test),
            _proposal(),
        )


def test_v2_rejects_rename_style_diff_sections() -> None:
    old = "src/trading/strategies/challengers/alpha_v1/model.py"
    new = "src/trading/strategies/challengers/alpha_v2/model.py"
    _, test = _valid_v2_paths()
    patch = (
        f"diff --git a/{old} b/{new}\n"
        "similarity index 100%\n"
        f"rename from {old}\n"
        f"rename to {new}\n"
        + _patch(test)
    )
    with pytest.raises(PatchPolicyError, match="new files only"):
        validate_candidate_patch(patch, _proposal())


@pytest.mark.parametrize(
    "paths",
    [
        ("docs/research/challengers/alpha_v2.md", "tests/candidates/test_alpha.py"),
        ("src/trading/strategies/challengers/alpha_v2/model.py",),
    ],
)
def test_v2_requires_candidate_implementation_and_candidate_test(
    paths: tuple[str, ...],
) -> None:
    with pytest.raises(PatchPolicyError, match=r"implementation|tests"):
        validate_candidate_patch(_patch(*paths), _proposal())


def test_v1_replay_still_accepts_existing_file_modification() -> None:
    implementation = "src/trading/strategies/alpha_v2/model.py"
    test = "tests/unit/test_alpha_v2.py"
    proposal = _proposal(schema_version="algorithm_proposal_v1")
    proposal["files_allowed_to_change"] = [
        "src/trading/strategies/**",
        "tests/unit/**",
    ]
    validation = validate_candidate_patch(
        _modified_patch(implementation) + _modified_patch(test),
        proposal,
    )
    assert validation.policy_version is CandidatePatchPolicyVersion.V1
    assert validation.changed_paths == (implementation, test)


def test_v2_candidate_tests_are_accepted_by_the_host_runtime(tmp_path: Path) -> None:
    implementation, test = _valid_v2_paths()
    candidate_root = tmp_path / "candidate"
    test_path = candidate_root / test
    test_path.parent.mkdir(parents=True)
    test_path.write_text("def test_candidate() -> None:\n    assert True\n", encoding="utf-8")
    validation = validate_candidate_patch(
        _patch(implementation, test),
        _proposal(),
    )
    declared = candidate_testing_module._safe_declared_tests(  # pyright: ignore[reportPrivateUsage]
        candidate_root,
        {"tests_added": [test]},
        validation,
    )
    assert declared == (test,)
    manifest_schema = json.loads(
        schema_path("CandidateTestManifestV1").read_text(encoding="utf-8")
    )
    safe_path_pattern = manifest_schema["$defs"]["safe_path"]["pattern"]
    assert re.fullmatch(safe_path_pattern, test) is not None


def test_explicit_v2_policy_is_sealed_into_the_builder_context(
    prepared_run: RunLayout,
    proposal: JsonObject,
) -> None:
    plan = prepare_invocation(
        prepared_run,
        InvocationRole.BUILDER,
        DockerBackend("runner:test", "codex-egress"),
        prompt=asset_text("prompts/builder.prompt.md"),
        approved_proposal=deepcopy(proposal),
        candidate_patch_policy_version=CANDIDATE_PATCH_POLICY_V2,
    )
    binding = load_json_object(prepared_run.request / "builder_binding.json")
    assert plan.candidate_patch_policy_version == CANDIDATE_PATCH_POLICY_V2
    assert plan.candidate_patch_policy_contract_hash == CANDIDATE_PATCH_POLICY_V2_CONTRACT_HASH
    assert binding["candidate_patch_policy_version"] == CANDIDATE_PATCH_POLICY_V2
    assert (
        binding["candidate_patch_policy_contract_hash"] == CANDIDATE_PATCH_POLICY_V2_CONTRACT_HASH
    )
    expected_context_hash = hash_json(
        {
            "schema_version": "BuilderContextV1",
            "request_context_manifest_hash": prepared_run_request_context(prepared_run),
            "proposal_hash": proposal["proposal_hash"],
            "candidate_patch_policy_version": CANDIDATE_PATCH_POLICY_V2,
            "candidate_patch_policy_contract_hash": (CANDIDATE_PATCH_POLICY_V2_CONTRACT_HASH),
        }
    )
    assert binding["builder_context_hash"] == expected_context_hash
    assert plan.builder_context_hash == expected_context_hash


def test_default_v1_builder_binding_and_manifest_keep_the_legacy_shape(
    prepared_run: RunLayout,
    proposal: JsonObject,
) -> None:
    plan = prepare_invocation(
        prepared_run,
        InvocationRole.BUILDER,
        DockerBackend("runner:test", "codex-egress"),
        prompt=asset_text("prompts/builder.prompt.md"),
        approved_proposal=deepcopy(proposal),
    )
    binding = load_json_object(prepared_run.request / "builder_binding.json")
    assert "candidate_patch_policy_version" not in binding
    assert "candidate_patch_policy_contract_hash" not in binding
    assert "candidate_patch_policy_version" not in plan.manifest()
    assert "candidate_patch_policy_contract_hash" not in plan.manifest()
    expected_context_hash = hash_json(
        {
            "schema_version": "BuilderContextV1",
            "request_context_manifest_hash": prepared_run_request_context(prepared_run),
            "proposal_hash": proposal["proposal_hash"],
        }
    )
    assert binding["builder_context_hash"] == expected_context_hash
    assert plan.builder_context_hash == expected_context_hash


def test_v2_builder_binding_tampering_is_rejected(
    prepared_run: RunLayout,
    proposal: JsonObject,
) -> None:
    prompt = asset_text("prompts/builder.prompt.md")
    prepare_invocation(
        prepared_run,
        InvocationRole.BUILDER,
        DockerBackend("runner:test", "codex-egress"),
        prompt=prompt,
        approved_proposal=deepcopy(proposal),
        candidate_patch_policy_version=CANDIDATE_PATCH_POLICY_V2,
    )
    binding_path = prepared_run.request / "builder_binding.json"
    binding = load_json_object(binding_path)
    binding["candidate_patch_policy_contract_hash"] = "f" * 64
    binding_path.write_bytes(canonical_json_bytes(binding) + b"\n")
    with pytest.raises(IsolationError, match="binding input"):
        load_invocation_plan(
            prepared_run,
            InvocationRole.BUILDER,
            prompt=prompt,
        )


def prepared_run_request_context(prepared_run: RunLayout) -> str:
    value = load_json_object(prepared_run.request / "research_request.json")[
        "context_manifest_hash"
    ]
    assert isinstance(value, str)
    return value
