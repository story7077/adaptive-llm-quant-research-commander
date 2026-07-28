"""Host-owned, network-denied execution of declared Challenger tests."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Protocol, cast

from research_commander.candidate import deterministic_patch
from research_commander.canonical import hash_file, hash_json, hash_tree, sha256_bytes
from research_commander.errors import ContractError, IsolationError
from research_commander.io import (
    load_json_object,
    write_json_exclusive,
    write_text_exclusive,
)
from research_commander.json_types import JsonObject, JsonValue
from research_commander.layout import RunLayout
from research_commander.patch_policy import (
    CandidatePatchPolicyVersion,
    PatchValidation,
    validate_candidate_patch,
)
from research_commander.sandbox import (
    BackendKind,
    InvocationPlan,
    candidate_tree_hash_without_reparse_points,
    load_validated_builder_result,
    native_sandbox_launcher,
    validated_native_builder_candidate_root,
)
from research_commander.schema_store import validate_document

CANDIDATE_TEST_TIMEOUT_SECONDS = 300
CANDIDATE_TEST_MAX_OUTPUT_BYTES = 1024 * 1024
CANDIDATE_TEST_MAX_JOB_MEMORY_BYTES = 1024 * 1024 * 1024
CANDIDATE_TEST_MAX_PROCESSES = 32
CANDIDATE_TEST_EXECUTION_VERSION = "candidate-test-unelevated-workspace-v1"
_OUTPUT_TAIL_BYTES = 64 * 1024
_JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
_JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_SUMMARY_COUNT = re.compile(
    r"(?P<count>[0-9]+) "
    r"(?P<label>passed|failed|skipped|errors?|xfailed|xpassed|deselected)"
)


@dataclass(frozen=True)
class CandidateInputs:
    request: JsonObject
    proposal: JsonObject
    builder_result: JsonObject
    candidate_root: Path
    source_root: Path
    patch: str
    patch_validation: PatchValidation
    source_snapshot_hash: str
    candidate_tree_hash: str
    patch_hash: str
    proposal_hash: str
    builder_result_hash: str
    declared_entrypoint: str
    declared_tests: tuple[str, ...]


@dataclass(frozen=True)
class FencedProcessResult:
    returncode: int | None
    duration_ms: int
    stdout_sha256: str
    stderr_sha256: str
    stdout_bytes: int
    stderr_bytes: int
    stdout_tail: bytes
    stderr_tail: bytes
    timed_out: bool
    output_limit_exceeded: bool


class CandidateTestProcess(Protocol):
    def __call__(
        self,
        command: tuple[str, ...],
        *,
        cwd: Path,
        env: dict[str, str],
        timeout_seconds: int,
        max_output_bytes: int,
        max_job_memory_bytes: int,
        max_processes: int,
    ) -> FencedProcessResult: ...


def candidate_sandbox_namespace_root(
    layout: RunLayout,
    plan: InvocationPlan,
    namespace: str,
) -> Path:
    if re.fullmatch(r"[a-z][a-z0-9-]{0,63}", namespace) is None:
        raise IsolationError("Candidate sandbox namespace is invalid")
    private_root = layout.root.parent.parent.resolve(strict=True)
    namespace_root = private_root / namespace
    if namespace_root.is_symlink():
        raise IsolationError("Candidate sandbox namespace root is a symlink")
    namespace_root.mkdir(exist_ok=True)
    cycle_root = namespace_root / layout.root.name
    if cycle_root.is_symlink():
        raise IsolationError("Candidate sandbox cycle root is a symlink")
    cycle_root.mkdir(exist_ok=True)
    invocation_root = cycle_root / plan.invocation_id
    if invocation_root.is_symlink():
        raise IsolationError("Candidate sandbox invocation root is a symlink")
    invocation_root.mkdir(exist_ok=True)
    return invocation_root


def candidate_test_runner_hash() -> str:
    return hash_file(Path(__file__).resolve(strict=True))


def candidate_test_manifest_path(
    plan: InvocationPlan,
    test_run_id: str,
) -> Path:
    return plan.work_root / "candidate-test-attempts" / f"{test_run_id}.json"


def kernel32_function(
    kernel32: object,
    name: str,
) -> Callable[..., int]:
    return cast(Callable[..., int], getattr(kernel32, name))


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _BasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _ExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _hash_and_tail(stream: BinaryIO) -> tuple[str, int, bytes]:
    stream.seek(0, os.SEEK_END)
    size = stream.tell()
    stream.seek(0)
    digest = hashlib.sha256()
    while chunk := stream.read(64 * 1024):
        digest.update(chunk)
    stream.seek(max(0, size - _OUTPUT_TAIL_BYTES))
    return digest.hexdigest(), size, stream.read(_OUTPUT_TAIL_BYTES)


def create_and_assign_windows_job(
    process: subprocess.Popen[bytes],
    *,
    max_job_memory_bytes: int,
    max_processes: int,
) -> tuple[object, int]:
    if os.name != "nt":
        raise IsolationError("candidate test job fencing requires native Windows")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    kernel32.CreateJobObjectW.restype = ctypes.c_void_p
    kernel32.SetInformationJobObject.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint32,
    ]
    kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    kernel32.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise IsolationError("cannot create candidate test Windows job")
    limits = _ExtendedLimitInformation()
    limits.BasicLimitInformation.LimitFlags = (
        _JOB_OBJECT_LIMIT_ACTIVE_PROCESS
        | _JOB_OBJECT_LIMIT_JOB_MEMORY
        | _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    )
    limits.BasicLimitInformation.ActiveProcessLimit = max_processes
    limits.JobMemoryLimit = max_job_memory_bytes
    configured = kernel32.SetInformationJobObject(
        job,
        _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
        ctypes.byref(limits),
        ctypes.sizeof(limits),
    )
    process_handle = getattr(process, "_handle", None)
    assigned = bool(
        configured
        and process_handle is not None
        and kernel32.AssignProcessToJobObject(
            job,
            ctypes.c_void_p(int(process_handle)),
        )
    )
    if not assigned:
        process.kill()
        process.wait(timeout=10)
        kernel32.CloseHandle(job)
        raise IsolationError("cannot assign candidate test process to its Windows job")
    return kernel32, int(job)


def run_fenced_candidate_process(
    command: tuple[str, ...],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout_seconds: int,
    max_output_bytes: int,
    max_job_memory_bytes: int,
    max_processes: int,
) -> FencedProcessResult:
    """Run one command in a kill-on-close Windows Job with bounded raw output."""
    started = time.monotonic()
    timed_out = False
    output_limit_exceeded = False
    with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
        try:
            process = subprocess.Popen(  # noqa: S603 - exact host-constructed command
                command,
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=stdout_file,
                stderr=stderr_file,
                shell=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            raise IsolationError(
                f"candidate test process could not start: {type(exc).__name__}"
            ) from None
        kernel32: object
        job_handle: int
        try:
            kernel32, job_handle = create_and_assign_windows_job(
                process,
                max_job_memory_bytes=max_job_memory_bytes,
                max_processes=max_processes,
            )
        except BaseException:
            process.kill()
            process.wait(timeout=10)
            raise
        try:
            while process.poll() is None:
                elapsed = time.monotonic() - started
                output_size = (
                    os.fstat(stdout_file.fileno()).st_size + os.fstat(stderr_file.fileno()).st_size
                )
                if elapsed >= timeout_seconds:
                    timed_out = True
                    break
                if output_size > max_output_bytes:
                    output_limit_exceeded = True
                    break
                time.sleep(0.025)
            if timed_out or output_limit_exceeded:
                terminate = kernel32_function(kernel32, "TerminateJobObject")
                terminate(ctypes.c_void_p(job_handle), 124)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                terminate = kernel32_function(kernel32, "TerminateJobObject")
                terminate(ctypes.c_void_p(job_handle), 125)
                process.wait(timeout=10)
        finally:
            close_handle = kernel32_function(kernel32, "CloseHandle")
            close_handle(ctypes.c_void_p(job_handle))
        stdout_hash, stdout_size, stdout_tail = _hash_and_tail(cast(BinaryIO, stdout_file))
        stderr_hash, stderr_size, stderr_tail = _hash_and_tail(cast(BinaryIO, stderr_file))
    return FencedProcessResult(
        returncode=process.returncode,
        duration_ms=int((time.monotonic() - started) * 1000),
        stdout_sha256=stdout_hash,
        stderr_sha256=stderr_hash,
        stdout_bytes=stdout_size,
        stderr_bytes=stderr_size,
        stdout_tail=stdout_tail,
        stderr_tail=stderr_tail,
        timed_out=timed_out,
        output_limit_exceeded=output_limit_exceeded,
    )


def _safe_declared_tests(
    candidate_root: Path,
    builder_result: JsonObject,
    validation: PatchValidation,
) -> tuple[str, ...]:
    value = builder_result.get("tests_added")
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ContractError("Builder declared tests are malformed")
    declared = tuple(sorted(cast(list[str], value)))
    if len(set(declared)) != len(declared) or set(declared) != set(validation.test_paths):
        raise ContractError("declared tests do not exactly match the candidate patch")
    permitted_test_prefixes = (
        ("tests/candidates/",)
        if validation.policy_version is CandidatePatchPolicyVersion.V2
        else ("tests/unit/", "tests/property/", "tests/research/")
    )
    for item in declared:
        path = PurePosixPath(item)
        if (
            "\\" in item
            or path.is_absolute()
            or ".." in path.parts
            or not item.startswith(permitted_test_prefixes)
            or path.suffix != ".py"
            or not path.name.startswith("test_")
        ):
            raise IsolationError(f"unsafe or non-executable declared test: {item}")
        test_path = candidate_root.joinpath(*path.parts)
        if (
            test_path.is_symlink()
            or test_path.is_junction()
            or not test_path.is_file()
            or not test_path.resolve(strict=True).is_relative_to(
                candidate_root.resolve(strict=True)
            )
        ):
            raise IsolationError(f"declared test is missing or unsafe: {item}")
    return declared


def _safe_declared_entrypoint(
    candidate_root: Path,
    builder_result: JsonObject,
    validation: PatchValidation,
) -> str:
    value = builder_result.get("declared_entrypoint")
    if (
        not isinstance(value, str)
        or re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*",
            value,
        )
        is None
    ):
        raise ContractError("Builder declared_entrypoint is malformed")
    module_name, _ = value.split(":", maxsplit=1)
    module_parts = module_name.split(".")
    candidates = (
        PurePosixPath("src", *module_parts).with_suffix(".py"),
        PurePosixPath("src", *module_parts, "__init__.py"),
    )
    changed = set(validation.changed_paths)
    for relative in candidates:
        path = candidate_root.joinpath(*relative.parts)
        if (
            relative.as_posix() in changed
            and path.is_file()
            and not path.is_symlink()
            and not path.is_junction()
            and path.resolve(strict=True).is_relative_to(candidate_root.resolve(strict=True))
        ):
            return value
    raise ContractError("declared_entrypoint must resolve to a changed candidate src module")


def load_candidate_inputs(
    layout: RunLayout,
    plan: InvocationPlan,
) -> CandidateInputs:
    if plan.backend is not BackendKind.NATIVE_WINDOWS:
        raise IsolationError("candidate tests require the native Windows sandbox")
    request = load_json_object(layout.request / "research_request.json")
    builder_result = load_validated_builder_result(plan, request)
    candidate_root = validated_native_builder_candidate_root(plan)
    source_root = layout.source_snapshot
    candidate_tree_hash = candidate_tree_hash_without_reparse_points(candidate_root)
    source_snapshot_hash = candidate_tree_hash_without_reparse_points(source_root)
    proposal = load_json_object(layout.request / "approved_algorithm_proposal.json")
    binding = load_json_object(layout.request / "builder_binding.json")
    proposal_hash = binding.get("proposal_hash")
    if not isinstance(proposal_hash, str) or proposal.get("proposal_hash") != proposal_hash:
        raise IsolationError("approved proposal hash differs from the host binding")
    patch = deterministic_patch(source_root, candidate_root)
    validation = validate_candidate_patch(
        patch,
        proposal,
        policy_version=plan.candidate_patch_policy_version,
    )
    declared_tests = _safe_declared_tests(
        candidate_root,
        builder_result,
        validation,
    )
    declared_entrypoint = _safe_declared_entrypoint(
        candidate_root,
        builder_result,
        validation,
    )
    patch_hash = sha256_bytes(patch.replace("\r\n", "\n").encode("utf-8"))
    return CandidateInputs(
        request=request,
        proposal=proposal,
        builder_result=builder_result,
        candidate_root=candidate_root,
        source_root=source_root,
        patch=patch,
        patch_validation=validation,
        source_snapshot_hash=source_snapshot_hash,
        candidate_tree_hash=candidate_tree_hash,
        patch_hash=patch_hash,
        proposal_hash=proposal_hash,
        builder_result_hash=hash_json(builder_result),
        declared_entrypoint=declared_entrypoint,
        declared_tests=declared_tests,
    )


def candidate_runtime() -> tuple[Path, tuple[Path, ...], JsonObject]:
    project_root = Path(__file__).resolve().parents[2]
    venv_root = project_root / ".venv"
    python = venv_root / "Scripts" / "python.exe"
    if (
        venv_root.is_symlink()
        or not venv_root.is_dir()
        or python.is_symlink()
        or not python.is_file()
    ):
        raise IsolationError("commander .venv Python runtime is unavailable or unsafe")
    base_runtime = Path(sys.base_prefix).resolve(strict=True)
    runtime_roots = tuple(
        sorted(
            {venv_root.resolve(strict=True), base_runtime},
            key=lambda path: str(path).casefold(),
        )
    )
    abi_tag = sys.implementation.cache_tag or "unknown-abi"
    runtime: JsonObject = {
        "implementation": platform.python_implementation(),
        "version": platform.python_version(),
        "abi_tag": abi_tag,
        "executable_sha256": hash_file(python),
    }
    return python, runtime_roots, runtime


def _host_candidate_abi_request(inputs: CandidateInputs) -> JsonObject:
    raw_universe = inputs.proposal.get("target_universe")
    strategy_id = inputs.proposal.get("proposed_strategy_id")
    strategy_version = inputs.proposal.get("proposed_strategy_version")
    if (
        not isinstance(raw_universe, list)
        or not raw_universe
        or any(not isinstance(symbol, str) for symbol in raw_universe)
        or not isinstance(strategy_id, str)
        or not isinstance(strategy_version, str)
    ):
        raise ContractError("Candidate ABI probe proposal binding is malformed")
    symbols = sorted(cast(list[str], raw_universe))
    cutoff = "2026-07-27T19:59:00Z"
    feature_time = "2026-07-27T19:58:00Z"
    instruments: list[JsonValue] = []
    caps: JsonObject = {}
    for index, symbol in enumerate(symbols):
        caps[symbol] = 0.60
        features: list[JsonValue] = [
            {
                "name": "realized_volatility_20_session",
                "value": 0.02 + index * 0.005,
                "source_event_time": feature_time,
                "available_at": feature_time,
                "source_revision": 0,
                "revision_available_at": feature_time,
                "revision_was_known_at_cutoff": True,
                "source_hash": hashlib.sha256(f"{symbol}:volatility".encode()).hexdigest(),
            },
            {
                "name": "reversal_5_session",
                "value": -0.10 if index == 0 else 0.10,
                "source_event_time": feature_time,
                "available_at": feature_time,
                "source_revision": 0,
                "revision_available_at": feature_time,
                "revision_was_known_at_cutoff": True,
                "source_hash": hashlib.sha256(f"{symbol}:reversal".encode()).hexdigest(),
            },
        ]
        instruments.append(
            {
                "symbol": symbol,
                "current_weight": 0.0,
                "membership_available_at": feature_time,
                "membership_valid_from": "2020-01-01T00:00:00Z",
                "membership_valid_until": None,
                "instrument_is_non_survivor": False,
                "features": features,
            }
        )
    parameters: JsonObject = {
        "reversal_feature_name": "reversal_5_session",
        "volatility_feature_name": "realized_volatility_20_session",
    }
    payload: JsonObject = {
        "schema_version": "candidate_decision_request_v1",
        "request_id": "host-candidate-abi-probe-v1",
        "challenger_id": "host-candidate-abi-probe",
        "candidate_artifact_hash": "0" * 64,
        "strategy_id": strategy_id,
        "strategy_version": strategy_version,
        "decision_time": "2026-07-27T20:00:00Z",
        "signal_data_cutoff": cutoff,
        "variant": {
            "parameter_neighborhood_id": "BASE",
            "data_ablation_id": "BASE",
            "date_shift_id": "BASE",
            "inversion_id": "BASE",
            "shuffle_id": "BASE",
        },
        "instruments": instruments,
        "constraints": {
            "long_only": True,
            "leverage_permitted": False,
            "new_symbols_permitted": False,
            "maximum_gross_weight": 0.80,
            "minimum_cash_weight": 0.20,
            "maximum_weight_by_symbol": caps,
            "numeric_tolerance": 0.000000001,
        },
        "strategy_parameters": parameters,
        "strategy_parameters_hash": hash_json(parameters),
        "source_data_manifest_hash": "1" * 64,
    }
    payload["request_hash"] = hash_json(payload)
    validate_document(payload, "CandidateDecisionRequestV1")
    return payload


def _write_host_candidate_abi_test(
    inputs: CandidateInputs,
    result_root: Path,
) -> Path:
    request = _host_candidate_abi_request(inputs)
    module_name, function_name = inputs.declared_entrypoint.split(":", 1)
    request_literal = json.dumps(
        json.dumps(
            request,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    source = f"""from __future__ import annotations

from copy import deepcopy
import hashlib
import importlib
import json
import math

REQUEST = json.loads({request_literal})
MODULE_NAME = {json.dumps(module_name)}
FUNCTION_NAME = {json.dumps(function_name)}


def _canonical(value):
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        assert math.isfinite(value)
        return float(format(value, ".12g"))
    if isinstance(value, list):
        return [_canonical(item) for item in value]
    if isinstance(value, dict):
        return {{key: _canonical(value[key]) for key in sorted(value)}}
    raise AssertionError(f"unsupported canonical type: {{type(value).__name__}}")


def _hash(value) -> str:
    encoded = json.dumps(
        _canonical(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def test_host_owned_candidate_decision_abi() -> None:
    entrypoint = getattr(importlib.import_module(MODULE_NAME), FUNCTION_NAME)
    original = deepcopy(REQUEST)
    first = entrypoint(deepcopy(REQUEST))
    second = entrypoint(deepcopy(REQUEST))
    assert REQUEST == original
    assert first == second
    assert set(first) == {{
        "schema_version",
        "request_id",
        "request_hash",
        "challenger_id",
        "candidate_artifact_hash",
        "targets",
        "diagnostics",
        "output_hash",
    }}
    assert first["schema_version"] == "candidate_decision_response_v1"
    for field in (
        "request_id",
        "request_hash",
        "challenger_id",
        "candidate_artifact_hash",
    ):
        assert first[field] == REQUEST[field]
    targets = first["targets"]
    assert isinstance(targets, list)
    expected_symbols = [item["symbol"] for item in REQUEST["instruments"]]
    assert [item["symbol"] for item in targets] == expected_symbols
    gross = 0.0
    for target in targets:
        assert set(target) == {{"symbol", "score", "target_weight"}}
        assert math.isfinite(target["score"])
        weight = target["target_weight"]
        assert math.isfinite(weight)
        assert 0.0 <= weight <= REQUEST["constraints"]["maximum_weight_by_symbol"][target["symbol"]]
        gross += weight
    tolerance = REQUEST["constraints"]["numeric_tolerance"]
    assert gross <= REQUEST["constraints"]["maximum_gross_weight"] + tolerance
    assert 1.0 - gross >= REQUEST["constraints"]["minimum_cash_weight"] - tolerance
    assert isinstance(first["diagnostics"], dict)
    without_hash = {{key: value for key, value in first.items() if key != "output_hash"}}
    assert first["output_hash"] == _hash(without_hash)
"""
    path = result_root / "host_candidate_abi_test.py"
    write_text_exclusive(path, source)
    return path


def _write_candidate_test_projection(
    inputs: CandidateInputs,
    result_root: Path,
) -> Path:
    projection_root = result_root / "candidate-tests"
    projection_root.mkdir()
    for declared_path in inputs.declared_tests:
        relative = PurePosixPath(declared_path)
        source = inputs.candidate_root.joinpath(*relative.parts)
        destination = projection_root.joinpath(*relative.parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    return projection_root


def candidate_sandbox_environment(
    candidate_root: Path,
    result_root: Path,
    python: Path,
) -> dict[str, str]:
    system_root = Path(os.environ.get("SYSTEMROOT", r"C:\Windows"))
    environment = {
        "SYSTEMROOT": str(system_root),
        "WINDIR": str(system_root),
        "COMSPEC": str(system_root / "System32" / "cmd.exe"),
        "PATH": os.pathsep.join((str(python.parent), str(system_root / "System32"))),
        "TEMP": str(result_root),
        "TMP": str(result_root),
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONHASHSEED": "0",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "NO_COLOR": "1",
    }
    for name in ("COMPUTERNAME", "USERNAME", "USERDOMAIN"):
        value = os.environ.get(name)
        if not value:
            raise IsolationError("native Candidate sandbox identity environment is incomplete")
        environment[name] = value
    if any(
        any(token in key.upper() for token in ("KEY", "SECRET", "TOKEN", "COOKIE"))
        for key in environment
    ):
        raise IsolationError("candidate test environment contains credential-shaped names")
    return environment


def _candidate_test_command(
    plan: InvocationPlan,
    inputs: CandidateInputs,
    *,
    result_root: Path,
    candidate_source_root: Path,
    python: Path,
    runtime_roots: tuple[Path, ...],
) -> tuple[str, ...]:
    del runtime_roots
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
            (
                "import importlib,pytest,sys;"
                "sys.path.insert(0,sys.argv[1]);"
                "module_name,function_name=sys.argv[2].split(':',1);"
                "entrypoint=getattr(importlib.import_module(module_name),function_name);"
                "assert callable(entrypoint),'declared entrypoint is not callable';"
                "raise SystemExit(pytest.main(sys.argv[3:]))"
            ),
            str(candidate_source_root.resolve(strict=True)),
            inputs.declared_entrypoint,
            str(result_root / "host_candidate_abi_test.py"),
            *(
                str(result_root / "candidate-tests" / Path(*PurePosixPath(path).parts))
                for path in inputs.declared_tests
            ),
            "-p",
            "no:cacheprovider",
            "--rootdir",
            str(result_root),
            "--basetemp",
            str(result_root / "pytest-tmp"),
            "-q",
            "--tb=no",
        )
    )
    return tuple(command)


def _pytest_counts(stdout_tail: bytes, stderr_tail: bytes) -> JsonObject:
    text = (stdout_tail + b"\n" + stderr_tail).decode("utf-8", errors="replace")
    lines = [line for line in text.splitlines() if _SUMMARY_COUNT.search(line) is not None]
    counts = {
        "passed": 0,
        "failed": 0,
        "skipped": 0,
        "errors": 0,
        "xfailed": 0,
        "xpassed": 0,
        "deselected": 0,
    }
    if lines:
        for match in _SUMMARY_COUNT.finditer(lines[-1]):
            label = match.group("label")
            if label == "error":
                label = "errors"
            counts[label] = int(match.group("count"))
    collected = sum(
        counts[key] for key in ("passed", "failed", "skipped", "errors", "xfailed", "xpassed")
    )
    return {"collected": collected, **counts}


def run_candidate_tests(
    layout: RunLayout,
    plan: InvocationPlan,
    *,
    run_process: CandidateTestProcess | None = None,
) -> JsonObject:
    inputs = load_candidate_inputs(layout, plan)
    if not isinstance(plan.builder_context_hash, str):
        raise IsolationError("candidate test run has no Builder context hash")
    runner_code_hash = candidate_test_runner_hash()
    test_run_id = (
        "candidate-test-"
        + hash_json(
            {
                "execution_contract_version": CANDIDATE_TEST_EXECUTION_VERSION,
                "invocation_id": plan.invocation_id,
                "candidate_tree_hash": inputs.candidate_tree_hash,
                "builder_result_hash": inputs.builder_result_hash,
                "runner_code_hash": runner_code_hash,
            }
        )[:24]
    )
    manifest_path = candidate_test_manifest_path(plan, test_run_id)
    attempts_root = manifest_path.parent
    if attempts_root.is_symlink():
        raise IsolationError("Candidate test attempt root is a symlink")
    attempts_root.mkdir(exist_ok=True)
    if manifest_path.is_symlink():
        raise IsolationError("Candidate test manifest is a symlink")
    if manifest_path.is_file():
        existing = load_json_object(manifest_path)
        validate_document(existing, "CandidateTestManifestV1")
        return existing
    if manifest_path.exists():
        raise IsolationError("Candidate test manifest path is unsafe")
    result_root = (
        candidate_sandbox_namespace_root(
            layout,
            plan,
            "candidate-test-runtime",
        )
        / test_run_id
    )
    if result_root.exists() or result_root.is_symlink():
        raise IsolationError("Candidate test attempt has orphaned runtime state")
    result_root.mkdir(exist_ok=False)
    host_abi_test_path = _write_host_candidate_abi_test(
        inputs,
        result_root,
    )
    host_abi_test_hash_before = hash_file(host_abi_test_path)
    projected_tests_root = _write_candidate_test_projection(
        inputs,
        result_root,
    )
    candidate_test_projection_hash_before = hash_tree(projected_tests_root)
    candidate_source_root = result_root / "candidate-source"
    source_root = inputs.candidate_root / "src"
    if source_root.is_symlink() or not source_root.is_dir():
        raise IsolationError("Candidate source root is missing or unsafe")
    shutil.copytree(source_root, candidate_source_root)
    candidate_source_projection_hash_before = hash_tree(candidate_source_root)
    python, runtime_roots, runtime = candidate_runtime()
    command = _candidate_test_command(
        plan,
        inputs,
        result_root=result_root,
        candidate_source_root=candidate_source_root,
        python=python,
        runtime_roots=runtime_roots,
    )
    environment = candidate_sandbox_environment(
        inputs.candidate_root,
        result_root,
        python,
    )
    runner = run_process or run_fenced_candidate_process
    try:
        result = runner(
            command,
            cwd=result_root,
            env=environment,
            timeout_seconds=CANDIDATE_TEST_TIMEOUT_SECONDS,
            max_output_bytes=CANDIDATE_TEST_MAX_OUTPUT_BYTES,
            max_job_memory_bytes=CANDIDATE_TEST_MAX_JOB_MEMORY_BYTES,
            max_processes=CANDIDATE_TEST_MAX_PROCESSES,
        )
    except (OSError, ContractError):
        empty_hash = sha256_bytes(b"")
        result = FencedProcessResult(
            returncode=None,
            duration_ms=0,
            stdout_sha256=empty_hash,
            stderr_sha256=empty_hash,
            stdout_bytes=0,
            stderr_bytes=0,
            stdout_tail=b"",
            stderr_tail=b"",
            timed_out=False,
            output_limit_exceeded=False,
        )
        process_error = True
    else:
        process_error = False
    candidate_tree_hash_after = candidate_tree_hash_without_reparse_points(inputs.candidate_root)
    tree_unchanged = candidate_tree_hash_after == inputs.candidate_tree_hash
    host_abi_test_hash_after = (
        hash_file(host_abi_test_path)
        if host_abi_test_path.is_file() and not host_abi_test_path.is_symlink()
        else sha256_bytes(b"")
    )
    host_abi_test_unchanged = host_abi_test_hash_after == host_abi_test_hash_before
    candidate_test_projection_hash_after = (
        hash_tree(projected_tests_root)
        if projected_tests_root.is_dir() and not projected_tests_root.is_symlink()
        else sha256_bytes(b"")
    )
    candidate_test_projection_unchanged = (
        candidate_test_projection_hash_after == candidate_test_projection_hash_before
    )
    candidate_source_projection_hash_after = (
        hash_tree(candidate_source_root)
        if candidate_source_root.is_dir() and not candidate_source_root.is_symlink()
        else sha256_bytes(b"")
    )
    candidate_source_projection_unchanged = (
        candidate_source_projection_hash_after
        == candidate_source_projection_hash_before
    )
    counts = _pytest_counts(result.stdout_tail, result.stderr_tail)
    collected = counts.get("collected")
    if process_error:
        status = "PROCESS_ERROR"
    elif (
        not tree_unchanged
        or not host_abi_test_unchanged
        or not candidate_test_projection_unchanged
        or not candidate_source_projection_unchanged
    ):
        status = "INTEGRITY_FAILURE"
    elif result.timed_out:
        status = "TIMED_OUT"
    elif result.output_limit_exceeded:
        status = "RESOURCE_LIMIT"
    elif result.returncode == 0 and isinstance(collected, int) and collected > 0:
        status = "PASSED"
    else:
        status = "FAILED"
    manifest: JsonObject = {
        "schema_version": "candidate_test_manifest_v1",
        "test_run_id": test_run_id,
        "execution_contract_version": CANDIDATE_TEST_EXECUTION_VERSION,
        "runner_code_hash": runner_code_hash,
        "invocation_id": plan.invocation_id,
        "builder_context_hash": plan.builder_context_hash,
        "source_snapshot_hash": inputs.source_snapshot_hash,
        "candidate_tree_hash_before": inputs.candidate_tree_hash,
        "candidate_tree_hash_after": candidate_tree_hash_after,
        "candidate_tree_unchanged": tree_unchanged,
        "patch_hash": inputs.patch_hash,
        "proposal_hash": inputs.proposal_hash,
        "builder_result_hash": inputs.builder_result_hash,
        "declared_entrypoint": inputs.declared_entrypoint,
        "declared_tests": list(inputs.declared_tests),
        "host_abi_test_hash_before": host_abi_test_hash_before,
        "host_abi_test_hash_after": host_abi_test_hash_after,
        "host_abi_test_unchanged": host_abi_test_unchanged,
        "candidate_test_projection_hash_before": (candidate_test_projection_hash_before),
        "candidate_test_projection_hash_after": (candidate_test_projection_hash_after),
        "candidate_test_projection_unchanged": (candidate_test_projection_unchanged),
        "candidate_source_projection_hash_before": (
            candidate_source_projection_hash_before
        ),
        "candidate_source_projection_hash_after": (
            candidate_source_projection_hash_after
        ),
        "candidate_source_projection_unchanged": (
            candidate_source_projection_unchanged
        ),
        "command_hash": hash_json(list(command)),
        "runtime": runtime,
        "limits": {
            "timeout_seconds": CANDIDATE_TEST_TIMEOUT_SECONDS,
            "max_output_bytes": CANDIDATE_TEST_MAX_OUTPUT_BYTES,
            "max_job_memory_bytes": CANDIDATE_TEST_MAX_JOB_MEMORY_BYTES,
            "max_processes": CANDIDATE_TEST_MAX_PROCESSES,
        },
        "status": status,
        "exit_code": result.returncode,
        "duration_ms": result.duration_ms,
        "test_count": counts,
        "stdout_sha256": result.stdout_sha256,
        "stderr_sha256": result.stderr_sha256,
        "stdout_bytes": result.stdout_bytes,
        "stderr_bytes": result.stderr_bytes,
        "output_limit_exceeded": result.output_limit_exceeded,
        "raw_output_persisted": False,
        "network_access_permitted": False,
        "credential_access_permitted": False,
        "broker_access_permitted": False,
        "host_principal_persisted": False,
        "real_order_routing": False,
    }
    validate_document(manifest, "CandidateTestManifestV1")
    write_json_exclusive(manifest_path, manifest)
    return manifest


def load_passing_candidate_test_manifest(
    plan: InvocationPlan,
) -> tuple[Path, JsonObject]:
    """Select the immutable passing attempt produced by the current host runner."""
    paths: list[Path] = []
    attempts_root = plan.work_root / "candidate-test-attempts"
    if attempts_root.is_symlink():
        raise IsolationError("Candidate test attempt root is a symlink")
    if attempts_root.is_dir():
        paths.extend(sorted(attempts_root.glob("candidate-test-*.json")))
    legacy_path = plan.work_root / "candidate-test-manifest.json"
    if legacy_path.is_file() and not legacy_path.is_symlink():
        paths.append(legacy_path)
    runner_code_hash = candidate_test_runner_hash()
    matches: list[tuple[Path, JsonObject]] = []
    for path in paths:
        if path.is_symlink() or not path.is_file():
            continue
        try:
            manifest = load_json_object(path)
            validate_document(manifest, "CandidateTestManifestV1")
        except ContractError:
            continue
        if (
            manifest.get("status") == "PASSED"
            and manifest.get("execution_contract_version")
            == CANDIDATE_TEST_EXECUTION_VERSION
            and manifest.get("runner_code_hash") == runner_code_hash
        ):
            matches.append((path, manifest))
    if not matches:
        raise ContractError(
            "Candidate has no passing test attempt from the current host runner"
        )
    if len(matches) != 1:
        raise IsolationError("Candidate has multiple passing current-runner test attempts")
    return matches[0]
