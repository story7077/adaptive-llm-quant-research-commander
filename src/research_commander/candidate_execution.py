"""Network-denied, resource-fenced CandidateDecisionRequestV1 transport."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Literal, cast

from research_commander.candidate import (
    hash_selected_files,
    selected_file_hash_records,
)
from research_commander.candidate_testing import (
    candidate_runtime,
    candidate_sandbox_environment,
    candidate_sandbox_namespace_root,
    create_and_assign_windows_job,
    kernel32_function,
)
from research_commander.canonical import hash_file, hash_json, hash_tree
from research_commander.errors import ContractError, IsolationError
from research_commander.io import load_json_object, write_json_exclusive
from research_commander.json_types import JsonObject, JsonValue
from research_commander.layout import RunLayout
from research_commander.sandbox import (
    BackendKind,
    InvocationPlan,
    candidate_tree_hash_without_reparse_points,
    native_sandbox_launcher,
    validated_native_builder_candidate_root,
)
from research_commander.schema_store import validate_document

_REQUEST_FIELDS = frozenset(
    {
        "schema_version",
        "request_id",
        "challenger_id",
        "candidate_artifact_hash",
        "strategy_id",
        "strategy_version",
        "decision_time",
        "signal_data_cutoff",
        "variant",
        "instruments",
        "constraints",
        "strategy_parameters",
        "strategy_parameters_hash",
        "source_data_manifest_hash",
        "request_hash",
    }
)
_HASH_LENGTH = 64
CandidateExecutionLane = Literal["PRIMARY", "REPLAY"]
_RUNNER = (
    "import importlib,json,sys;"
    "sys.path.insert(0,sys.argv[1]);"
    "request=json.load(sys.stdin);"
    "module_name,function_name=sys.argv[2].split(':',1);"
    "entrypoint=getattr(importlib.import_module(module_name),function_name);"
    "assert callable(entrypoint),'declared entrypoint is not callable';"
    "response=entrypoint(request);"
    "assert isinstance(response,dict),'candidate response must be a raw JSON object';"
    "sys.stdout.write(json.dumps(response,ensure_ascii=False,allow_nan=False,"
    "separators=(',',':'),sort_keys=True))"
)


@dataclass(frozen=True)
class CandidateExecutionBytes:
    exit_code: int | None
    stdout: bytes
    stderr_sha256: str
    stderr_bytes: int
    timed_out: bool
    resource_limit_exceeded: bool


def _read_bytes(stream: BinaryIO) -> bytes:
    stream.seek(0)
    return stream.read()


def run_fenced_candidate_json_process(
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
    """Execute through a kill-on-close Windows Job without persisting raw input."""
    started = time.monotonic()
    timed_out = False
    resource_limit_exceeded = False
    with (
        tempfile.TemporaryFile() as stdin_file,
        tempfile.TemporaryFile() as stdout_file,
        tempfile.TemporaryFile() as stderr_file,
    ):
        stdin_file.write(stdin_bytes)
        stdin_file.seek(0)
        try:
            process = subprocess.Popen(  # noqa: S603 - exact host-constructed command
                command,
                cwd=cwd,
                env=env,
                stdin=stdin_file,
                stdout=stdout_file,
                stderr=stderr_file,
                shell=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            raise IsolationError(
                f"Candidate process could not start: {type(exc).__name__}"
            ) from None
        try:
            kernel32, job_handle = create_and_assign_windows_job(
                process,
                max_job_memory_bytes=maximum_memory_bytes,
                max_processes=maximum_processes,
            )
        except BaseException:
            process.kill()
            process.wait(timeout=10)
            raise
        try:
            while process.poll() is None:
                if time.monotonic() - started >= timeout_seconds:
                    timed_out = True
                    break
                if (
                    os.fstat(stdout_file.fileno()).st_size > maximum_stdout_bytes
                    or os.fstat(stderr_file.fileno()).st_size > maximum_stderr_bytes
                ):
                    resource_limit_exceeded = True
                    break
                time.sleep(0.01)
            if timed_out or resource_limit_exceeded:
                kernel32_function(kernel32, "TerminateJobObject")(
                    ctypes.c_void_p(job_handle),
                    124,
                )
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                resource_limit_exceeded = True
                kernel32_function(kernel32, "TerminateJobObject")(
                    ctypes.c_void_p(job_handle),
                    125,
                )
                process.wait(timeout=10)
        finally:
            kernel32_function(kernel32, "CloseHandle")(ctypes.c_void_p(job_handle))
        stdout = _read_bytes(cast(BinaryIO, stdout_file))
        stderr = _read_bytes(cast(BinaryIO, stderr_file))
    if len(stdout) > maximum_stdout_bytes or len(stderr) > maximum_stderr_bytes:
        resource_limit_exceeded = True
        stdout = b""
    return CandidateExecutionBytes(
        exit_code=process.returncode,
        stdout=stdout,
        stderr_sha256=hashlib.sha256(stderr).hexdigest(),
        stderr_bytes=len(stderr),
        timed_out=timed_out,
        resource_limit_exceeded=resource_limit_exceeded,
    )


def _bounded_candidate_request(request: JsonObject, bundle: JsonObject) -> str:
    validate_document(request, "CandidateDecisionRequestV1")
    if set(request) != set(_REQUEST_FIELDS):
        raise ContractError("Candidate request fields differ from candidate_decision_request_v1")
    if request.get("schema_version") != "candidate_decision_request_v1":
        raise ContractError("Candidate request schema version mismatch")
    if request.get("candidate_artifact_hash") != bundle.get("bundle_hash"):
        raise ContractError("Candidate request artifact binding mismatch")
    request_hash = request.get("request_hash")
    if not isinstance(request_hash, str) or len(request_hash) != _HASH_LENGTH:
        raise ContractError("Candidate request_hash is malformed")
    request_payload = {key: value for key, value in request.items() if key != "request_hash"}
    if request_hash != hash_json(cast(JsonValue, request_payload)):
        raise ContractError("Candidate request hash mismatch")
    return request_hash


def _validated_candidate_response(
    stdout_utf8: str,
    request: JsonObject,
) -> None:
    try:
        decoded: object = json.loads(stdout_utf8)
    except (json.JSONDecodeError, TypeError):
        raise ContractError("Candidate response is not one complete JSON object") from None
    if not isinstance(decoded, dict):
        raise ContractError("Candidate response is not a JSON object")
    response = cast(JsonObject, decoded)
    validate_document(response, "CandidateDecisionResponseV1")
    for field in (
        "request_id",
        "request_hash",
        "challenger_id",
        "candidate_artifact_hash",
    ):
        if response.get(field) != request.get(field):
            raise ContractError(f"Candidate response binding mismatch: {field}")
    instruments = request.get("instruments")
    constraints = request.get("constraints")
    targets = response.get("targets")
    if (
        not isinstance(instruments, list)
        or not isinstance(constraints, dict)
        or not isinstance(targets, list)
    ):
        raise ContractError("Candidate decision ABI collections are malformed")
    expected_symbols = [item.get("symbol") for item in instruments if isinstance(item, dict)]
    actual_symbols = [item.get("symbol") for item in targets if isinstance(item, dict)]
    if actual_symbols != expected_symbols:
        raise ContractError("Candidate response introduced, omitted, or reordered a symbol")
    caps = constraints.get("maximum_weight_by_symbol")
    tolerance = constraints.get("numeric_tolerance")
    maximum_gross = constraints.get("maximum_gross_weight")
    minimum_cash = constraints.get("minimum_cash_weight")
    if (
        not isinstance(caps, dict)
        or not isinstance(tolerance, (int, float))
        or isinstance(tolerance, bool)
        or not isinstance(maximum_gross, (int, float))
        or isinstance(maximum_gross, bool)
        or not isinstance(minimum_cash, (int, float))
        or isinstance(minimum_cash, bool)
    ):
        raise ContractError("Candidate response constraints are malformed")
    gross = 0.0
    for target in targets:
        if not isinstance(target, dict):
            raise ContractError("Candidate target is malformed")
        symbol = target.get("symbol")
        weight = target.get("target_weight")
        cap = caps.get(symbol) if isinstance(symbol, str) else None
        if (
            not isinstance(weight, (int, float))
            or isinstance(weight, bool)
            or not isinstance(cap, (int, float))
            or isinstance(cap, bool)
            or float(weight) > float(cap) + float(tolerance)
        ):
            raise ContractError("Candidate target exceeds a host-owned symbol cap")
        gross += float(weight)
    if gross > float(maximum_gross) + float(tolerance):
        raise ContractError("Candidate response exceeds maximum gross exposure")
    if 1.0 - gross < float(minimum_cash) - float(tolerance):
        raise ContractError("Candidate response violates minimum cash")
    output_hash = response.get("output_hash")
    without_hash = {key: value for key, value in response.items() if key != "output_hash"}
    if output_hash != hash_json(cast(JsonValue, without_hash)):
        raise ContractError("Candidate response hash mismatch")


def _validated_security(
    security: JsonObject,
    bundle: JsonObject,
    candidate_tree_hash: str,
    *,
    runtime_executable_hash: str,
    worker_code_hash: str,
) -> tuple[str, JsonObject]:
    validate_document(security, "CandidateExecutionSecurityV1")
    expected_false = (
        "network_access_permitted",
        "credential_access_permitted",
        "broker_access_permitted",
        "filesystem_write_permitted",
        "real_order_routing",
    )
    if any(security.get(field) is not False for field in expected_false):
        raise ContractError("Candidate execution security permits a forbidden capability")
    if (
        security.get("schema_version") != "candidate_execution_security_v1"
        or security.get("candidate_artifact_hash") != bundle.get("bundle_hash")
        or security.get("candidate_tree_hash") != candidate_tree_hash
        or security.get("declared_entrypoint") != bundle.get("declared_entrypoint")
        or security.get("runtime_executable_hash") != runtime_executable_hash
        or security.get("worker_code_hash") != worker_code_hash
    ):
        raise ContractError("Candidate execution security binding mismatch")
    security_hash = security.get("security_contract_hash")
    if not isinstance(security_hash, str) or security_hash != hash_json(
        {key: value for key, value in security.items() if key != "security_contract_hash"}
    ):
        raise ContractError("Candidate execution security hash mismatch")
    limits = security.get("limits")
    if not isinstance(limits, dict):
        raise ContractError("Candidate execution limits are malformed")
    for field in (
        "timeout_seconds",
        "maximum_stdout_bytes",
        "maximum_stderr_bytes",
        "maximum_memory_bytes",
        "maximum_processes",
    ):
        value = limits.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ContractError(f"Candidate execution limit is invalid: {field}")
    return security_hash, dict(limits)


def candidate_execution_worker_hash() -> str:
    return hash_file(Path(__file__).resolve(strict=True))


def _load_persisted_candidate_result(
    path: Path,
    *,
    invocation_id: str,
    request_hash: str,
    artifact_hash: str,
    security_hash: str,
    request: JsonObject,
) -> JsonObject:
    if path.is_symlink() or not path.is_file():
        raise IsolationError("Candidate decision has orphaned runtime state")
    result = load_json_object(path)
    validate_document(result, "CandidateProcessResultV1")
    if (
        result.get("invocation_id") != invocation_id
        or result.get("request_hash") != request_hash
        or result.get("candidate_artifact_hash") != artifact_hash
        or result.get("security_contract_hash") != security_hash
    ):
        raise IsolationError("Persisted Candidate result binding mismatch")
    if any(
        result.get(field) is not False
        for field in (
            "network_access_permitted",
            "credential_access_permitted",
            "broker_access_permitted",
            "filesystem_write_permitted",
            "real_order_routing",
        )
    ):
        raise IsolationError("Persisted Candidate result permits a forbidden capability")
    expected_hash = hash_json(
        cast(
            JsonValue,
            {key: value for key, value in result.items() if key != "result_hash"},
        )
    )
    if result.get("result_hash") != expected_hash:
        raise IsolationError("Persisted Candidate result hash mismatch")
    stdout_utf8 = result.get("stdout_utf8")
    exit_code = result.get("exit_code")
    timed_out = result.get("timed_out")
    resource_limit_exceeded = result.get("resource_limit_exceeded")
    if not isinstance(stdout_utf8, str):
        raise IsolationError("Persisted Candidate stdout is malformed")
    if exit_code == 0 and timed_out is False and resource_limit_exceeded is False:
        _validated_candidate_response(stdout_utf8, request)
    return result


def candidate_runtime_attestation(
    layout: RunLayout,
    plan: InvocationPlan,
) -> JsonObject:
    """Expose hashes needed by the public host to build its security contract."""
    if plan.backend is not BackendKind.NATIVE_WINDOWS:
        raise IsolationError("Candidate decisions require the native Windows sandbox")
    bundle_path = layout.output / "candidate_artifact_bundle.json"
    if bundle_path.is_symlink() or not bundle_path.is_file():
        raise IsolationError("finalized Candidate artifact bundle is unavailable")
    bundle = load_json_object(bundle_path)
    validate_document(bundle, "CandidateArtifactBundleV1")
    candidate_root = validated_native_builder_candidate_root(plan)
    candidate_tree_hash = candidate_tree_hash_without_reparse_points(candidate_root)
    if candidate_tree_hash != bundle.get("candidate_tree_hash"):
        raise IsolationError("Candidate tree differs from its finalized artifact")
    _, _, runtime = candidate_runtime()
    if runtime != bundle.get("runtime"):
        raise IsolationError("Candidate runtime differs from its finalized artifact")
    artifact_hash = bundle.get("bundle_hash")
    candidate_config_hash = bundle.get("config_hash")
    entrypoint = bundle.get("declared_entrypoint")
    if (
        not isinstance(artifact_hash, str)
        or not isinstance(candidate_config_hash, str)
        or not isinstance(entrypoint, str)
    ):
        raise IsolationError("Candidate artifact ABI binding is malformed")
    config_records = selected_file_hash_records(candidate_root, ("config/",))
    if not config_records:
        raise IsolationError("Candidate artifact has no attested config files")
    if hash_selected_files(candidate_root, ("config/",)) != candidate_config_hash:
        raise IsolationError("Candidate config differs from its finalized artifact")
    return {
        "schema_version": "candidate_runtime_attestation_v1",
        "isolation_kind": "native_windows_codex_sandbox",
        "isolation_version": "candidate_runtime_v1",
        "candidate_artifact_hash": artifact_hash,
        "candidate_tree_hash": candidate_tree_hash,
        "candidate_config_hash": candidate_config_hash,
        "candidate_config_files": cast(JsonValue, config_records),
        "runtime": runtime,
        "worker_code_hash": candidate_execution_worker_hash(),
        "declared_entrypoint": entrypoint,
        "network_access_permitted": False,
        "credential_access_permitted": False,
        "broker_access_permitted": False,
        "filesystem_write_permitted": False,
        "real_order_routing": False,
    }


def _candidate_command(
    plan: InvocationPlan,
    *,
    candidate_root: Path,
    candidate_source_root: Path,
    result_root: Path,
    python: Path,
    runtime_roots: tuple[Path, ...],
    declared_entrypoint: str,
) -> tuple[str, ...]:
    del candidate_root, runtime_roots
    command: list[str] = [
        *native_sandbox_launcher(plan),
        "sandbox",
        "-c",
        'windows.sandbox="unelevated"',
        "-c",
        'shell_environment_policy.inherit="core"',
        "-c",
        "shell_environment_policy.ignore_default_excludes=false",
        "-c",
        (
            'shell_environment_policy.exclude=["CODEX_*","OPENAI_*","*KEY*",'
            '"*SECRET*","*TOKEN*","USERPROFILE","APPDATA","LOCALAPPDATA",'
            '"COMPUTERNAME","USERNAME","USERDOMAIN"]'
        ),
        "--permission-profile",
        ":workspace",
    ]
    command.extend(
        (
            "--sandbox-state-disable-network",
            "--cd",
            str(result_root.resolve(strict=True)),
            str(python.resolve(strict=True)),
            "-B",
            "-c",
            _RUNNER,
            str(candidate_source_root.resolve(strict=True)),
            declared_entrypoint,
        )
    )
    return tuple(command)


def invoke_candidate_decision(
    layout: RunLayout,
    plan: InvocationPlan,
    *,
    request: JsonObject,
    security: JsonObject,
    execution_lane: CandidateExecutionLane = "PRIMARY",
) -> JsonObject:
    """Return the public host's CandidateProcessResultV1 wire document."""
    if execution_lane not in ("PRIMARY", "REPLAY"):
        raise ContractError("Candidate execution lane is invalid")
    if plan.backend is not BackendKind.NATIVE_WINDOWS:
        raise IsolationError("Candidate decisions require the native Windows sandbox")
    bundle_path = layout.output / "candidate_artifact_bundle.json"
    if bundle_path.is_symlink() or not bundle_path.is_file():
        raise IsolationError("finalized Candidate artifact bundle is unavailable")
    bundle = load_json_object(bundle_path)
    validate_document(bundle, "CandidateArtifactBundleV1")
    request_hash = _bounded_candidate_request(request, bundle)
    candidate_root = validated_native_builder_candidate_root(plan)
    candidate_tree_hash = candidate_tree_hash_without_reparse_points(candidate_root)
    if candidate_tree_hash != bundle.get("candidate_tree_hash"):
        raise IsolationError("Candidate tree differs from its finalized artifact")
    entrypoint = bundle.get("declared_entrypoint")
    artifact_hash = bundle.get("bundle_hash")
    if not isinstance(entrypoint, str) or not isinstance(artifact_hash, str):
        raise IsolationError("Candidate artifact ABI binding is malformed")
    python, runtime_roots, runtime = candidate_runtime()
    if runtime != bundle.get("runtime"):
        raise IsolationError("Candidate runtime differs from its finalized artifact")
    executable_hash = runtime.get("executable_sha256")
    if not isinstance(executable_hash, str):
        raise IsolationError("Candidate runtime executable hash is malformed")
    security_hash, limits = _validated_security(
        security,
        bundle,
        candidate_tree_hash,
        runtime_executable_hash=executable_hash,
        worker_code_hash=candidate_execution_worker_hash(),
    )
    runtime_root = candidate_sandbox_namespace_root(
        layout,
        plan,
        "candidate-decision-runtime",
    )
    invocation_id = (
        "candidate-invocation-"
        + hash_json(
            {
                "request_hash": request_hash,
                "security_contract_hash": security_hash,
                "execution_lane": execution_lane,
            }
        )[:24]
    )
    result_root = runtime_root / invocation_id
    result_path = result_root / "candidate-process-result.json"
    if result_root.is_symlink():
        raise IsolationError("Candidate decision runtime path is a symlink")
    if result_root.is_dir():
        return _load_persisted_candidate_result(
            result_path,
            invocation_id=invocation_id,
            request_hash=request_hash,
            artifact_hash=artifact_hash,
            security_hash=security_hash,
            request=request,
        )
    if result_root.exists():
        raise IsolationError("Candidate decision runtime path is unsafe")
    result_root.mkdir()
    source_root = candidate_root / "src"
    if source_root.is_symlink() or not source_root.is_dir():
        raise IsolationError("Candidate source root is missing or unsafe")
    candidate_source_root = result_root / "candidate-source"
    shutil.copytree(source_root, candidate_source_root)
    candidate_source_hash = hash_tree(candidate_source_root)
    command = _candidate_command(
        plan,
        candidate_root=candidate_root,
        candidate_source_root=candidate_source_root,
        result_root=result_root,
        python=python,
        runtime_roots=runtime_roots,
        declared_entrypoint=entrypoint,
    )
    environment = candidate_sandbox_environment(
        candidate_root,
        result_root,
        python,
    )
    serialized = json.dumps(
        request,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    result = run_fenced_candidate_json_process(
        command,
        cwd=result_root,
        env=environment,
        stdin_bytes=serialized,
        timeout_seconds=cast(int, limits["timeout_seconds"]),
        maximum_stdout_bytes=cast(int, limits["maximum_stdout_bytes"]),
        maximum_stderr_bytes=cast(int, limits["maximum_stderr_bytes"]),
        maximum_memory_bytes=cast(int, limits["maximum_memory_bytes"]),
        maximum_processes=cast(int, limits["maximum_processes"]),
    )
    if candidate_tree_hash_without_reparse_points(candidate_root) != candidate_tree_hash:
        raise IsolationError("Candidate tree changed during decision execution")
    if (
        not candidate_source_root.is_dir()
        or candidate_source_root.is_symlink()
        or hash_tree(candidate_source_root) != candidate_source_hash
    ):
        raise IsolationError("Candidate source projection changed during decision execution")
    try:
        stdout_utf8 = result.stdout.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        stdout_utf8 = ""
    if result.exit_code == 0 and not result.timed_out and not result.resource_limit_exceeded:
        _validated_candidate_response(stdout_utf8, request)
    payload: JsonObject = {
        "schema_version": "candidate_process_result_v1",
        "invocation_id": invocation_id,
        "request_hash": request_hash,
        "candidate_artifact_hash": artifact_hash,
        "security_contract_hash": security_hash,
        "exit_code": result.exit_code,
        "timed_out": result.timed_out,
        "resource_limit_exceeded": result.resource_limit_exceeded,
        "stdout_utf8": stdout_utf8,
        "stdout_sha256": hashlib.sha256(result.stdout).hexdigest(),
        "stderr_sha256": result.stderr_sha256,
        "stdout_bytes": len(result.stdout),
        "stderr_bytes": result.stderr_bytes,
        "network_access_permitted": False,
        "credential_access_permitted": False,
        "broker_access_permitted": False,
        "filesystem_write_permitted": False,
        "real_order_routing": False,
    }
    payload["result_hash"] = hash_json(cast(JsonValue, payload))
    validate_document(payload, "CandidateProcessResultV1")
    write_json_exclusive(result_path, payload)
    return payload
