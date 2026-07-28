from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from conftest import Bundle

import research_commander.artifact_bundle as artifact_bundle_module
import research_commander.candidate_execution as candidate_execution_module
import research_commander.candidate_testing as candidate_testing_module
from research_commander.artifact_bundle import (
    finalize_candidate_artifacts,
    publish_finalized_candidate,
)
from research_commander.assets import asset_text
from research_commander.candidate import deterministic_patch
from research_commander.candidate_execution import (
    CandidateExecutionBytes,
    invoke_candidate_decision,
)
from research_commander.candidate_testing import (
    CANDIDATE_TEST_EXECUTION_VERSION,
    CandidateInputs,
    FencedProcessResult,
    candidate_test_manifest_path,
    candidate_test_runner_hash,
    run_candidate_tests,
)
from research_commander.canonical import hash_file, hash_json, hash_tree
from research_commander.errors import ContractError
from research_commander.io import load_json_object, write_json_exclusive
from research_commander.json_types import JsonObject, JsonValue
from research_commander.layout import RunLayout
from research_commander.patch_policy import validate_candidate_patch
from research_commander.sandbox import BackendKind, InvocationPlan, InvocationRole
from research_commander.schema_store import schema_path, validate_document

HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64


def test_builder_prompt_defines_exact_candidate_canonical_hash() -> None:
    prompt = asset_text("prompts/builder.prompt.md")
    assert 'float(format(value, ".12g"))' in prompt
    assert "ensure_ascii=False" in prompt
    assert "allow_nan=False" in prompt
    assert "removing only `output_hash`" in prompt


def _candidate_inputs(
    layout: RunLayout,
    bundle: Bundle,
    proposal: JsonObject,
) -> CandidateInputs:
    candidate_root = layout.work / "builder" / "builder-test" / "candidate_worktree"
    candidate_root.parent.mkdir(parents=True)
    shutil.copytree(layout.source_snapshot, candidate_root)
    strategy = candidate_root / "src/trading/strategies/alpha_v2/model.py"
    strategy.parent.mkdir(parents=True)
    strategy.write_text(
        (
            "def decide(request: dict[str, object]) -> dict[str, object]:\n"
            '    return {"schema_version": "candidate_decision_response_v1", '
            '"request_id": request["request_id"]}\n'
        ),
        encoding="utf-8",
    )
    test = candidate_root / "tests/unit/test_alpha_v2.py"
    test.write_text(
        "def test_candidate() -> None:\n    assert True\n",
        encoding="utf-8",
    )
    patch = deterministic_patch(layout.source_snapshot, candidate_root)
    validation = validate_candidate_patch(patch, proposal)
    builder_result: JsonObject = {
        "schema_version": "CandidateBuildResultV1",
        "request_id": bundle.request["request_id"],
        "research_cycle_id": bundle.request["research_cycle_id"],
        "context_manifest_hash": bundle.request["context_manifest_hash"],
        "source_snapshot_commit": bundle.request["source_snapshot_commit"],
        "champion_version": bundle.request["champion_version"],
        "experiment_family": bundle.request["experiment_family"],
        "selected_commander": bundle.request["selected_commander"],
        "commander_selection_id": bundle.request["commander_selection_id"],
        "commander_selection_version": bundle.request["commander_selection_version"],
        "builder_model": "gpt-5.6-sol",
        "builder_reasoning": "max",
        "proposal_hash": proposal["proposal_hash"],
        "declared_entrypoint": "trading.strategies.alpha_v2.model:decide",
        "implementation_summary": "Synthetic bounded Candidate.",
        "files_changed": list(validation.changed_paths),
        "tests_added": list(validation.test_paths),
        "promotion_decision": "NOT_PERMITTED",
    }
    return CandidateInputs(
        request=bundle.request,
        proposal=proposal,
        builder_result=builder_result,
        candidate_root=candidate_root,
        source_root=layout.source_snapshot,
        patch=patch,
        patch_validation=validation,
        source_snapshot_hash=hash_tree(layout.source_snapshot),
        candidate_tree_hash=hash_tree(candidate_root),
        patch_hash=hashlib.sha256(patch.encode("utf-8")).hexdigest(),
        proposal_hash=cast(str, proposal["proposal_hash"]),
        builder_result_hash=hash_json(builder_result),
        declared_entrypoint="trading.strategies.alpha_v2.model:decide",
        declared_tests=validation.test_paths,
    )


def _plan(layout: RunLayout, work_root: Path) -> InvocationPlan:
    codex = work_root / "codex.exe"
    codex.parent.mkdir(parents=True, exist_ok=True)
    codex.write_bytes(b"synthetic codex")
    return InvocationPlan(
        invocation_id="builder-test",
        role=InvocationRole.BUILDER,
        backend=BackendKind.NATIVE_WINDOWS,
        run_root=layout.root,
        work_root=work_root,
        command=(str(codex), "exec"),
        prompt="builder",
        output_path=work_root / "model-output.json",
        output_schema="CandidateBuildResultV1",
        builder_context_hash=HASH_B,
    )


def _test_manifest(inputs: CandidateInputs, plan: InvocationPlan) -> JsonObject:
    return {
        "schema_version": "candidate_test_manifest_v1",
        "test_run_id": "candidate-test-example",
        "execution_contract_version": CANDIDATE_TEST_EXECUTION_VERSION,
        "runner_code_hash": candidate_test_runner_hash(),
        "invocation_id": plan.invocation_id,
        "builder_context_hash": HASH_B,
        "source_snapshot_hash": inputs.source_snapshot_hash,
        "candidate_tree_hash_before": inputs.candidate_tree_hash,
        "candidate_tree_hash_after": inputs.candidate_tree_hash,
        "candidate_tree_unchanged": True,
        "patch_hash": inputs.patch_hash,
        "proposal_hash": inputs.proposal_hash,
        "builder_result_hash": inputs.builder_result_hash,
        "declared_entrypoint": inputs.declared_entrypoint,
        "declared_tests": list(inputs.declared_tests),
        "host_abi_test_hash_before": HASH_A,
        "host_abi_test_hash_after": HASH_A,
        "host_abi_test_unchanged": True,
        "candidate_test_projection_hash_before": HASH_B,
        "candidate_test_projection_hash_after": HASH_B,
        "candidate_test_projection_unchanged": True,
        "candidate_source_projection_hash_before": HASH_C,
        "candidate_source_projection_hash_after": HASH_C,
        "candidate_source_projection_unchanged": True,
        "command_hash": HASH_A,
        "runtime": {
            "implementation": "CPython",
            "version": "3.12.0",
            "abi_tag": "cpython-312",
            "executable_sha256": HASH_C,
        },
        "limits": {
            "timeout_seconds": 300,
            "max_output_bytes": 1048576,
            "max_job_memory_bytes": 1073741824,
            "max_processes": 32,
        },
        "status": "PASSED",
        "exit_code": 0,
        "duration_ms": 1,
        "test_count": {
            "collected": 1,
            "passed": 1,
            "failed": 0,
            "skipped": 0,
            "errors": 0,
            "xfailed": 0,
            "xpassed": 0,
            "deselected": 0,
        },
        "stdout_sha256": HASH_A,
        "stderr_sha256": HASH_B,
        "stdout_bytes": 10,
        "stderr_bytes": 0,
        "output_limit_exceeded": False,
        "raw_output_persisted": False,
        "network_access_permitted": False,
        "credential_access_permitted": False,
        "broker_access_permitted": False,
        "host_principal_persisted": False,
        "real_order_routing": False,
    }


def test_candidate_decision_command_uses_disposable_network_denied_workspace(
    prepared_run: RunLayout,
    bundle: Bundle,
    proposal: JsonObject,
) -> None:
    work_root = prepared_run.work / "builder" / "builder-test"
    inputs = _candidate_inputs(prepared_run, bundle, proposal)
    plan = _plan(prepared_run, work_root)
    result_root = work_root / "candidate-runtime" / "command-test"
    result_root.mkdir(parents=True)
    candidate_source_root = result_root / "candidate-source"
    shutil.copytree(inputs.candidate_root / "src", candidate_source_root)
    python = prepared_run.root / "runtime/python.exe"
    python.parent.mkdir()
    python.write_bytes(b"python")
    command = candidate_execution_module._candidate_command(  # pyright: ignore[reportPrivateUsage]
        plan,
        candidate_root=inputs.candidate_root,
        candidate_source_root=candidate_source_root,
        result_root=result_root,
        python=python,
        runtime_roots=(python.parent,),
        declared_entrypoint=inputs.declared_entrypoint,
    )
    rendered = "\n".join(command)
    assert 'windows.sandbox="unelevated"' in command
    assert ":workspace" in command
    assert "--sandbox-state-readable-root" not in command
    assert "--sandbox-state-disable-network" in command
    assert '"COMPUTERNAME","USERNAME","USERDOMAIN"' in rendered
    assert str(inputs.candidate_root.resolve()) not in command
    assert str(candidate_source_root.resolve()) in command


def test_candidate_test_command_injects_sealed_source_path_explicitly(
    prepared_run: RunLayout,
    bundle: Bundle,
    proposal: JsonObject,
) -> None:
    work_root = prepared_run.work / "builder" / "builder-test"
    inputs = _candidate_inputs(prepared_run, bundle, proposal)
    plan = _plan(prepared_run, work_root)
    result_root = work_root / "candidate-test-result"
    result_root.mkdir(parents=True)
    candidate_testing_module._write_host_candidate_abi_test(  # pyright: ignore[reportPrivateUsage]
        inputs,
        result_root,
    )
    projection_root = candidate_testing_module._write_candidate_test_projection(  # pyright: ignore[reportPrivateUsage]
        inputs,
        result_root,
    )
    candidate_source_root = result_root / "candidate-source"
    shutil.copytree(inputs.candidate_root / "src", candidate_source_root)
    python = prepared_run.root / "runtime/python.exe"
    python.parent.mkdir()
    python.write_bytes(b"python")
    command = candidate_testing_module._candidate_test_command(  # pyright: ignore[reportPrivateUsage]
        plan,
        inputs,
        result_root=result_root,
        candidate_source_root=candidate_source_root,
        python=python,
        runtime_roots=(python.parent,),
    )
    assert str(candidate_source_root.resolve()) in command
    assert 'windows.sandbox="unelevated"' in command
    assert ":workspace" in command
    assert "--sandbox-state-readable-root" not in command
    assert any("sys.path.insert(0,sys.argv[1])" in item for item in command)
    rootdir_index = command.index("--rootdir")
    assert command[rootdir_index + 1] == str(result_root)
    for declared_path in inputs.declared_tests:
        assert str(projection_root / Path(declared_path)) in command


def test_candidate_test_projection_includes_declared_config_and_host_fixture(
    prepared_run: RunLayout,
    bundle: Bundle,
    proposal: JsonObject,
) -> None:
    inputs = _candidate_inputs(prepared_run, bundle, proposal)
    config_path = "config/strategies/alpha-v2.yaml"
    candidate_config = inputs.candidate_root / config_path
    candidate_config.parent.mkdir(parents=True, exist_ok=True)
    candidate_config.write_text("strategy_version: 1.1.0\n", encoding="utf-8")
    validation = replace(
        inputs.patch_validation,
        changed_paths=tuple(
            sorted((*inputs.patch_validation.changed_paths, config_path))
        ),
        implementation_paths=tuple(
            sorted((*inputs.patch_validation.implementation_paths, config_path))
        ),
    )
    inputs_with_config = replace(inputs, patch_validation=validation)
    result_root = prepared_run.root / "candidate-test-projection"
    result_root.mkdir()

    projection_root = candidate_testing_module._write_candidate_test_projection(  # pyright: ignore[reportPrivateUsage]
        inputs_with_config,
        result_root,
    )

    assert (projection_root / config_path).read_text(encoding="utf-8") == (
        "strategy_version: 1.1.0\n"
    )
    assert "def repository_root() -> Path:" in (
        projection_root / "tests/conftest.py"
    ).read_text(encoding="utf-8")


def test_candidate_test_manifest_is_host_owned_and_abi_bound(
    prepared_run: RunLayout,
    bundle: Bundle,
    proposal: JsonObject,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    work_root = prepared_run.work / "builder" / "builder-test"
    inputs = _candidate_inputs(prepared_run, bundle, proposal)
    plan = _plan(prepared_run, work_root)

    def fake_inputs(
        layout: RunLayout,
        supplied_plan: InvocationPlan,
    ) -> CandidateInputs:
        del layout, supplied_plan
        return inputs

    monkeypatch.setattr(
        candidate_testing_module,
        "load_candidate_inputs",
        fake_inputs,
    )
    python = prepared_run.root / "runtime/python.exe"
    python.parent.mkdir()
    python.write_bytes(b"python")
    monkeypatch.setattr(
        candidate_testing_module,
        "candidate_runtime",
        lambda: (
            python,
            (python.parent,),
            {
                "implementation": "CPython",
                "version": "3.12.0",
                "abi_tag": "cpython-312",
                "executable_sha256": hash_file(python),
            },
        ),
    )

    def fake_test_command(
        supplied_plan: InvocationPlan,
        supplied_inputs: CandidateInputs,
        *,
        result_root: Path,
        candidate_source_root: Path,
        python: Path,
        runtime_roots: tuple[Path, ...],
    ) -> tuple[str, ...]:
        del (
            supplied_plan,
            supplied_inputs,
            result_root,
            candidate_source_root,
            python,
            runtime_roots,
        )
        return ("sandbox", "candidate-tests")

    def fake_environment(
        candidate_root: Path,
        result_root: Path,
        python: Path,
    ) -> dict[str, str]:
        del candidate_root, result_root, python
        return {"PYTHONUTF8": "1"}

    monkeypatch.setattr(
        candidate_testing_module,
        "_candidate_test_command",
        fake_test_command,
    )
    monkeypatch.setattr(
        candidate_testing_module,
        "candidate_sandbox_environment",
        fake_environment,
    )
    stdout = b"1 passed in 0.01s\n"

    def fake_runner(*args: object, **kwargs: object) -> FencedProcessResult:
        del args, kwargs
        return FencedProcessResult(
            returncode=0,
            duration_ms=10,
            stdout_sha256=hashlib.sha256(stdout).hexdigest(),
            stderr_sha256=hashlib.sha256(b"").hexdigest(),
            stdout_bytes=len(stdout),
            stderr_bytes=0,
            stdout_tail=stdout,
            stderr_tail=b"",
            timed_out=False,
            output_limit_exceeded=False,
        )

    manifest = run_candidate_tests(prepared_run, plan, run_process=fake_runner)
    assert manifest["schema_version"] == "candidate_test_manifest_v1"
    assert manifest["declared_entrypoint"] == inputs.declared_entrypoint
    assert manifest["status"] == "PASSED"
    assert manifest["network_access_permitted"] is False
    validate_document(manifest, "CandidateTestManifestV1")
    external_result = (
        prepared_run.root.parent.parent
        / "candidate-test-runtime"
        / prepared_run.root.name
        / plan.invocation_id
        / str(manifest["test_run_id"])
    )
    assert external_result.is_dir()
    assert not external_result.is_relative_to(plan.work_root)
    assert candidate_test_manifest_path(
        plan,
        str(manifest["test_run_id"]),
    ).is_file()


def test_finalize_uses_only_host_owned_candidate_and_test_artifacts(
    prepared_run: RunLayout,
    bundle: Bundle,
    proposal: JsonObject,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    work_root = prepared_run.work / "builder" / "builder-test"
    inputs = _candidate_inputs(prepared_run, bundle, proposal)
    plan = _plan(prepared_run, work_root)
    manifest = _test_manifest(inputs, plan)
    write_json_exclusive(work_root / "candidate-test-manifest.json", manifest)

    def fake_inputs(
        layout: RunLayout,
        supplied_plan: InvocationPlan,
    ) -> CandidateInputs:
        del layout, supplied_plan
        return inputs

    monkeypatch.setattr(
        artifact_bundle_module,
        "load_candidate_inputs",
        fake_inputs,
    )

    finalized = finalize_candidate_artifacts(prepared_run, plan)
    artifact = finalized.artifact_bundle
    assert artifact["schema_version"] == "candidate_artifact_bundle_v1"
    assert artifact["candidate_tree_hash"] == inputs.candidate_tree_hash
    assert artifact["code_hash"] == inputs.candidate_tree_hash
    assert artifact["proposal_hash"] == proposal["proposal_hash"]
    assert artifact["declared_entrypoint"] == inputs.declared_entrypoint
    abi = artifact["candidate_abi"]
    assert isinstance(abi, dict)
    assert abi["request_schema_version"] == "candidate_decision_request_v1"
    assert abi["response_schema_version"] == "candidate_decision_response_v1"
    assert artifact["filesystem_write_permitted"] is False
    validate_document(artifact, "CandidateArtifactBundleV1")
    publish_finalized_candidate(prepared_run, finalized)
    publish_finalized_candidate(prepared_run, finalized)
    assert load_json_object(prepared_run.output / "candidate_artifact_bundle.json") == artifact


def test_candidate_decision_transport_returns_main_process_wire_contract(
    prepared_run: RunLayout,
    bundle: Bundle,
    proposal: JsonObject,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    work_root = prepared_run.work / "builder" / "builder-test"
    inputs = _candidate_inputs(prepared_run, bundle, proposal)
    plan = _plan(prepared_run, work_root)
    test_manifest = _test_manifest(inputs, plan)
    write_json_exclusive(work_root / "candidate-test-manifest.json", test_manifest)

    def fake_inputs(
        layout: RunLayout,
        supplied_plan: InvocationPlan,
    ) -> CandidateInputs:
        del layout, supplied_plan
        return inputs

    monkeypatch.setattr(
        artifact_bundle_module,
        "load_candidate_inputs",
        fake_inputs,
    )
    finalized = finalize_candidate_artifacts(prepared_run, plan)
    publish_finalized_candidate(prepared_run, finalized)
    artifact = finalized.artifact_bundle
    security: JsonObject = {
        "schema_version": "candidate_execution_security_v1",
        "isolation_kind": "native_windows",
        "isolation_version": "codex_sandbox_v1",
        "candidate_artifact_hash": artifact["bundle_hash"],
        "candidate_tree_hash": artifact["candidate_tree_hash"],
        "runtime_executable_hash": HASH_C,
        "worker_code_hash": hash_file(Path(str(candidate_execution_module.__file__))),
        "declared_entrypoint": artifact["declared_entrypoint"],
        "limits": {
            "timeout_seconds": 5,
            "maximum_stdout_bytes": 65536,
            "maximum_stderr_bytes": 65536,
            "maximum_memory_bytes": 268435456,
            "maximum_processes": 4,
        },
        "network_access_permitted": False,
        "credential_access_permitted": False,
        "broker_access_permitted": False,
        "filesystem_write_permitted": False,
        "real_order_routing": False,
        "security_contract_hash": "0" * 64,
    }
    security["security_contract_hash"] = hash_json(
        {key: value for key, value in security.items() if key != "security_contract_hash"}
    )
    request = candidate_testing_module._host_candidate_abi_request(  # pyright: ignore[reportPrivateUsage]
        inputs
    )
    request["request_id"] = "candidate-request-example"
    request["challenger_id"] = artifact["challenger_id"]
    request["candidate_artifact_hash"] = artifact["bundle_hash"]
    request["request_hash"] = hash_json(
        {key: value for key, value in request.items() if key != "request_hash"}
    )
    response: JsonObject = {
        "schema_version": "candidate_decision_response_v1",
        "request_id": request["request_id"],
        "request_hash": request["request_hash"],
        "challenger_id": request["challenger_id"],
        "candidate_artifact_hash": request["candidate_artifact_hash"],
        "targets": [
            {
                "symbol": item["symbol"],
                "score": 0.0,
                "target_weight": 0.0,
            }
            for item in cast(list[JsonObject], request["instruments"])
        ],
        "diagnostics": {},
        "output_hash": "0" * 64,
    }
    response["output_hash"] = hash_json(
        {key: value for key, value in response.items() if key != "output_hash"}
    )
    stdout = json.dumps(response, separators=(",", ":"), sort_keys=True).encode()

    def fake_candidate_command(
        supplied_plan: InvocationPlan,
        *,
        candidate_root: Path,
        candidate_source_root: Path,
        result_root: Path,
        python: Path,
        runtime_roots: tuple[Path, ...],
        declared_entrypoint: str,
    ) -> tuple[str, ...]:
        del (
            supplied_plan,
            candidate_root,
            candidate_source_root,
            result_root,
            python,
            runtime_roots,
            declared_entrypoint,
        )
        return ("sandbox", "candidate")

    monkeypatch.setattr(
        candidate_execution_module,
        "_candidate_command",
        fake_candidate_command,
    )
    python = prepared_run.root / "runtime/python.exe"
    python.parent.mkdir()
    python.write_bytes(b"python")

    def fake_runtime() -> tuple[Path, tuple[Path, ...], JsonObject]:
        return (
            python,
            (python.parent,),
            {
                "implementation": "CPython",
                "version": "3.12.0",
                "abi_tag": "cpython-312",
                "executable_sha256": HASH_C,
            },
        )

    def fake_environment(
        candidate_root: Path,
        result_root: Path,
        supplied_python: Path,
    ) -> dict[str, str]:
        del candidate_root, result_root, supplied_python
        return {"PYTHONUTF8": "1"}

    process_calls = 0

    def fake_process(
        command: tuple[str, ...],
        *,
        cwd: Path,
        env: dict[str, str],
        stdin_bytes: bytes,
        timeout_seconds: int,
        maximum_stdout_bytes: int,
        maximum_stderr_bytes: int,
        maximum_memory_bytes: int,
        maximum_processes: int,
    ) -> CandidateExecutionBytes:
        nonlocal process_calls
        process_calls += 1
        del (
            command,
            cwd,
            env,
            stdin_bytes,
            timeout_seconds,
            maximum_stdout_bytes,
            maximum_stderr_bytes,
            maximum_memory_bytes,
            maximum_processes,
        )
        return CandidateExecutionBytes(
            exit_code=0,
            stdout=stdout,
            stderr_sha256=hashlib.sha256(b"").hexdigest(),
            stderr_bytes=0,
            timed_out=False,
            resource_limit_exceeded=False,
        )

    monkeypatch.setattr(candidate_execution_module, "candidate_runtime", fake_runtime)
    monkeypatch.setattr(
        candidate_execution_module,
        "candidate_sandbox_environment",
        fake_environment,
    )
    monkeypatch.setattr(
        candidate_execution_module,
        "run_fenced_candidate_json_process",
        fake_process,
    )

    attestation = candidate_execution_module.candidate_runtime_attestation(
        prepared_run,
        plan,
    )
    assert attestation["candidate_artifact_hash"] == artifact["bundle_hash"]
    assert attestation["filesystem_write_permitted"] is False

    result = invoke_candidate_decision(
        prepared_run,
        plan,
        request=request,
        security=security,
    )
    assert result["schema_version"] == "candidate_process_result_v1"
    assert result["stdout_utf8"] == stdout.decode()
    assert result["network_access_permitted"] is False
    assert result["filesystem_write_permitted"] is False
    result_without_hash = {key: value for key, value in result.items() if key != "result_hash"}
    assert result["result_hash"] == hash_json(cast(JsonValue, result_without_hash))
    replayed = invoke_candidate_decision(
        prepared_run,
        plan,
        request=request,
        security=security,
    )
    assert replayed == result
    assert process_calls == 1
    independent_replay = invoke_candidate_decision(
        prepared_run,
        plan,
        request=request,
        security=security,
        execution_lane="REPLAY",
    )
    assert independent_replay["stdout_utf8"] == result["stdout_utf8"]
    assert independent_replay["invocation_id"] != result["invocation_id"]
    assert independent_replay["result_hash"] != result["result_hash"]
    replayed_again = invoke_candidate_decision(
        prepared_run,
        plan,
        request=request,
        security=security,
        execution_lane="REPLAY",
    )
    assert replayed_again == independent_replay
    assert process_calls == 2

    tampered_request = dict(request)
    tampered_request["strategy_parameters"] = {"tampered": True}
    with pytest.raises(ContractError, match="Candidate request hash mismatch"):
        invoke_candidate_decision(
            prepared_run,
            plan,
            request=tampered_request,
            security=security,
        )


def test_prepared_run_copies_candidate_decision_abi_schemas(
    prepared_run: RunLayout,
) -> None:
    request_schema = prepared_run.request / "candidate-decision-request-v1.schema.json"
    response_schema = prepared_run.request / "candidate-decision-response-v1.schema.json"
    assert request_schema.read_text(encoding="utf-8") == (
        schema_path("CandidateDecisionRequestV1").read_text(encoding="utf-8")
    )
    assert response_schema.read_text(encoding="utf-8") == (
        schema_path("CandidateDecisionResponseV1").read_text(encoding="utf-8")
    )
