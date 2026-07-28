"""Fresh Codex invocation plans constrained by an external filesystem jail."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol, cast

from research_commander.binding import (
    decision_schema_name,
    finalize_commander_output,
    request_binding,
    validate_algorithm_proposal,
    verify_output_binding,
)
from research_commander.candidate import deterministic_patch
from research_commander.canonical import hash_file, hash_json, hash_tree, sha256_bytes
from research_commander.errors import ContractError, IsolationError
from research_commander.io import (
    load_json_object,
    write_json_exclusive,
    write_text_exclusive,
)
from research_commander.json_types import JsonObject
from research_commander.layout import RunLayout
from research_commander.patch_policy import (
    CandidatePatchPolicyVersion,
    candidate_patch_policy_contract_hash,
    select_candidate_patch_policy_version,
    validate_candidate_patch,
)
from research_commander.schema_store import validate_document

CODEX_MODEL = "gpt-5.6-sol"
CODEX_REASONING = "max"
CODEX_RUNNER_HOME_ENV = "ADAPTIVE_QUANT_CODEX_RUNNER_HOME"
ADOPTION_STABILITY_SECONDS = 2.0
BUILDER_BINDING_FILENAME = "builder_binding.json"
APPROVED_PROPOSAL_FILENAME = "approved_algorithm_proposal.json"
BUILDER_REQUEST_BINDING_FILENAME = "request_binding.json"
BUILDER_REQUEST_DIRECTORY = "builder_request"
_CONTEXT_BEARING_RUNNER_ENTRIES = frozenset(
    {
        "AGENTS.md",
        "AGENTS.override.md",
        "config.toml",
        "history.jsonl",
        "memories",
        "memories_extensions",
        "plugins",
        "rules",
        "sessions",
        "skills",
    }
)
_MODEL_SHELL_ENVIRONMENT = (
    'shell_environment_policy.inherit="core"',
    "shell_environment_policy.ignore_default_excludes=false",
    (
        'shell_environment_policy.exclude=["CODEX_*","OPENAI_*","*KEY*",'
        '"*SECRET*","*TOKEN*","USERPROFILE","APPDATA","LOCALAPPDATA",'
        '"COMPUTERNAME","USERNAME","USERDOMAIN"]'
    ),
)
_NATIVE_SANDBOX_REQUIRED_ENVIRONMENT = ("COMPUTERNAME", "USERNAME", "USERDOMAIN")
_SYSTEM_SKILLS_MARKER = ".codex-system-skills.marker"
_CODEX_SANDBOX_GROUP = "CodexSandboxUsers"
_NATIVE_ACL_EVENT_SCHEMA = "NativeAclEventV1"
_NATIVE_ACL_SNAPSHOT_NAME = "baseline.acl"
_NATIVE_ACL_VERIFY_NAME = "verify.acl"
_SDDL_ACE_PATTERN = re.compile(r"\(([^()]*)\)")
_SDDL_CONTROL_FLAG_PATTERN = re.compile(r"AI|AR|P")


class InvocationRole(StrEnum):
    COMMANDER = "commander"
    BUILDER = "builder"


class BackendKind(StrEnum):
    DOCKER = "docker"
    EXPLICIT_JAIL = "explicit_jail"
    NATIVE_WINDOWS = "native_windows"


@dataclass(frozen=True)
class DockerBackend:
    image: str
    egress_network: str
    executable: str = "docker"

    def validate(self) -> None:
        if not self.image.strip():
            raise IsolationError("Docker image is required")
        if not self.egress_network.strip() or self.egress_network in {"bridge", "host", "default"}:
            raise IsolationError("an explicit restricted Codex egress network is required")


@dataclass(frozen=True)
class ExplicitJailBackend:
    command: tuple[str, ...]
    policy_id: str

    def validate(self) -> None:
        if not self.command or not self.command[0].strip():
            raise IsolationError("explicit jail command is required")
        if not self.policy_id.strip():
            raise IsolationError("explicit jail policy_id is required")


@dataclass(frozen=True)
class NativeWindowsSandboxBackend:
    """Use Codex's OS-enforced Windows workspace sandbox for one copied run."""

    executable: str = "codex"
    platform_name: str = os.name

    def validate(self) -> None:
        if self.platform_name != "nt":
            raise IsolationError("native_windows backend requires native Windows")
        if shutil.which(self.executable) is None:
            raise IsolationError("Codex CLI executable is unavailable")


Backend = DockerBackend | ExplicitJailBackend | NativeWindowsSandboxBackend


@dataclass(frozen=True)
class InvocationPlan:
    invocation_id: str
    role: InvocationRole
    backend: BackendKind
    run_root: Path
    work_root: Path
    command: tuple[str, ...]
    prompt: str
    output_path: Path
    output_schema: str
    sealed_input_hash: str | None = None
    builder_context_hash: str | None = None
    candidate_patch_policy_version: str | None = None
    candidate_patch_policy_contract_hash: str | None = None

    def manifest(self) -> JsonObject:
        permission_profile = (
            "research_commander" if self.role is InvocationRole.COMMANDER else "research_builder"
        )
        manifest: JsonObject = {
            "schema_version": "CodexInvocationPlanV1",
            "invocation_id": self.invocation_id,
            "role": self.role.value,
            "backend": self.backend.value,
            "model": CODEX_MODEL,
            "reasoning": CODEX_REASONING,
            "non_interactive": True,
            "ephemeral": True,
            "resume_permitted": False,
            "user_config_permitted": False,
            "persistent_history_permitted": False,
            "other_runs_visible": False,
            "permission_profile": permission_profile,
            "sealed_input_hash": self.sealed_input_hash,
            "builder_context_hash": self.builder_context_hash,
            "prompt_hash": sha256_bytes(self.prompt.encode("utf-8")),
            "command_contract_hash": hash_json(list(self.command)),
        }
        if self.candidate_patch_policy_version is not None:
            manifest["candidate_patch_policy_version"] = self.candidate_patch_policy_version
            manifest["candidate_patch_policy_contract_hash"] = (
                self.candidate_patch_policy_contract_hash
            )
        return manifest

    def runtime_manifest(self) -> JsonObject:
        return {
            **self.manifest(),
            "command": list(self.command),
            "output_path": str(self.output_path.resolve()),
            "output_schema": self.output_schema,
        }


class RunProcess(Protocol):
    def __call__(
        self,
        command: tuple[str, ...],
        *,
        cwd: Path,
        env: dict[str, str],
        input: str,
        text: bool,
        encoding: str,
        stdout: int,
        stderr: int,
        check: bool,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]: ...


class HostCommandProcess(Protocol):
    def __call__(
        self,
        command: tuple[str, ...],
        *,
        cwd: Path,
        env: dict[str, str],
        text: bool,
        encoding: str,
        stdout: int,
        stderr: int,
        check: bool,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]: ...


def scrubbed_environment(source: dict[str, str] | None = None) -> dict[str, str]:
    """Keep only operating-system necessities; never inherit keys, tokens, or cookies."""
    environment = dict(os.environ) if source is None else source
    allowed = {
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
    }
    scrubbed = {key: value for key, value in environment.items() if key.upper() in allowed}
    scrubbed.update(
        {
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "NO_COLOR": "1",
        }
    )
    return scrubbed


def _add_native_sandbox_identity_environment(
    environment: dict[str, str],
    source: dict[str, str],
) -> None:
    """Supply the three Windows identity fields required by Codex's ACL helper.

    The native orchestrator fails before sandbox setup when these values are
    absent. They remain host-process inputs only and are explicitly excluded
    from every model shell environment.
    """
    forbidden = frozenset("\r\n\x00")
    for name in _NATIVE_SANDBOX_REQUIRED_ENVIRONMENT:
        value = source.get(name)
        if not value or any(character in forbidden for character in value):
            raise IsolationError(f"{name} is required for native Windows sandbox setup")
        environment[name] = value


def _safe_mount(path: Path) -> str:
    resolved = str(path.resolve(strict=True))
    if "," in resolved or "\n" in resolved or "\r" in resolved:
        raise IsolationError(f"path cannot be represented as a Docker mount: {resolved}")
    return resolved


def _codex_arguments(
    role: InvocationRole,
    *,
    container: bool,
    native_windows: bool = False,
    launcher: tuple[str, ...] = ("codex",),
) -> tuple[str, ...]:
    if container:
        workspace = "/workspace"
        schema = (
            "/workspace/.research/request/commander-output.schema.json"
            if role is InvocationRole.COMMANDER
            else "/workspace/.research/request/builder-output.schema.json"
        )
        output = "/workspace/model-output.json"
    else:
        workspace = "."
        schema = str(
            Path(".research/request")
            / (
                "commander-output.schema.json"
                if role is InvocationRole.COMMANDER
                else "builder-output.schema.json"
            )
        )
        output = "model-output.json"
    permission_profile = f"research_{role.value}"
    workspace_rules = (
        '{".research"="read",".runtime/tmp"="write"}'
        if role is InvocationRole.COMMANDER
        else ('{".research"="read",".runtime/tmp"="write","candidate_worktree"="write"}')
    )
    arguments = [
        *launcher,
        "exec",
        "--model",
        CODEX_MODEL,
        "-c",
        f'model_reasoning_effort="{CODEX_REASONING}"',
        "-c",
        'approval_policy="never"',
        "-c",
        'history.persistence="none"',
        "-c",
        f'default_permissions="{permission_profile}"',
        "-c",
        (
            f"permissions.{permission_profile}.filesystem="
            f'{{":minimal"="read",":workspace_roots"={workspace_rules}}}'
        ),
        "-c",
        _MODEL_SHELL_ENVIRONMENT[0],
        "-c",
        _MODEL_SHELL_ENVIRONMENT[1],
        "-c",
        _MODEL_SHELL_ENVIRONMENT[2],
        "--strict-config",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--disable",
        "memories",
        "--disable",
        "plugins",
        "--disable",
        "apps",
        "--disable",
        "browser_use",
        "--disable",
        "computer_use",
        "--disable",
        "multi_agent",
        "--disable",
        "skill_search",
        "--disable",
        "plugin_sharing",
        "--skip-git-repo-check",
        "--json",
        "--cd",
        workspace,
        "--output-schema",
        schema,
        "--output-last-message",
        output,
        "-",
    ]
    if native_windows:
        windows_config_index = arguments.index("--strict-config")
        arguments[windows_config_index:windows_config_index] = [
            "-c",
            'cli_auth_credentials_store="keyring"',
            "-c",
            'windows.sandbox="elevated"',
        ]
    return tuple(arguments)


def _copy_builder_snapshot(layout: RunLayout, work_root: Path) -> None:
    destination = work_root / "candidate_worktree"
    shutil.copytree(layout.source_snapshot, destination)


def _link_read_only_inputs(
    layout: RunLayout,
    work_root: Path,
    *,
    request_root: Path,
) -> None:
    """For explicit jails, stage stable path names; the jail must enforce read-only mounts."""
    research = work_root / ".research"
    research.mkdir()
    # Marker files contain paths, not copied inputs; the external jail adapter consumes them.
    write_json_exclusive(
        research / "mount-contract.json",
        {
            "schema_version": "JailMountContractV1",
            "read_only": {
                "request": str(request_root.resolve(strict=True)),
                "input": str(layout.input.resolve(strict=True)),
            },
            "other_runs_visible": False,
        },
    )


def _stage_native_inputs(
    layout: RunLayout,
    work_root: Path,
    *,
    request_root: Path,
) -> str:
    """Copy only the current run into the sandbox root and seal it by hash."""
    research = work_root / ".research"
    request = research / "request"
    inputs = research / "input"
    shutil.copytree(request_root, request)
    shutil.copytree(layout.input, inputs)
    return hash_tree(research)


def _materialize_builder_request(
    layout: RunLayout,
    *,
    request: JsonObject,
) -> Path:
    """Expose only approved Builder inputs, never Commander memory/transcript."""

    destination = layout.input / BUILDER_REQUEST_DIRECTORY
    if destination.exists():
        raise IsolationError("Builder request view already exists")
    destination.mkdir()
    write_json_exclusive(
        destination / BUILDER_REQUEST_BINDING_FILENAME,
        {
            "schema_version": "BuilderRequestBindingV1",
            "request_schema_version": request["schema_version"],
            "expires_at": request["expires_at"],
            **request_binding(request),
        },
    )
    for filename in (
        APPROVED_PROPOSAL_FILENAME,
        BUILDER_BINDING_FILENAME,
        "constraints.json",
        "source_snapshot_manifest.json",
        "builder-output.schema.json",
        "candidate-decision-request-v1.schema.json",
        "candidate-decision-response-v1.schema.json",
        "AGENTS.md",
    ):
        source = layout.request / filename
        if source.is_symlink() or not source.is_file():
            raise IsolationError(
                f"sanitized Builder input is missing: {filename}"
            )
        shutil.copy2(source, destination / filename)
    visible = {path.name for path in destination.iterdir()}
    forbidden = {
        "research_request.json",
        "evidence_manifest.json",
        "commander-output.schema.json",
        "output.schema.json",
        "invocations",
    }
    if visible.intersection(forbidden):
        raise IsolationError("Builder request view exposes Commander context")
    return destination


def _native_codex_launcher(executable: str) -> tuple[str, ...]:
    command_shim = shutil.which(f"{executable}.cmd") if Path(executable).suffix == "" else None
    if command_shim is not None:
        packaged_root = (
            Path(command_shim).parent
            / "node_modules"
            / "@openai"
            / "codex"
            / "node_modules"
            / "@openai"
            / "codex-win32-x64"
            / "vendor"
        )
        packaged_binaries = sorted(packaged_root.glob("*/bin/codex.exe"))
        if len(packaged_binaries) == 1:
            return (str(packaged_binaries[0]),)
    resolved = shutil.which(f"{executable}.exe") if Path(executable).suffix == "" else None
    resolved = resolved or shutil.which(executable)
    if resolved is None:
        raise IsolationError("Codex CLI executable is unavailable")
    candidate = Path(resolved)
    if candidate.suffix.casefold() in {".cmd", ".bat"}:
        command_shell = os.environ.get(
            "COMSPEC",
            str(Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32" / "cmd.exe"),
        )
        return (command_shell, "/d", "/s", "/c", str(candidate))
    if not candidate.suffix:
        command_shim = candidate.with_suffix(".cmd")
        if command_shim.is_file():
            command_shell = os.environ.get(
                "COMSPEC",
                str(Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32" / "cmd.exe"),
            )
            return (command_shell, "/d", "/s", "/c", str(command_shim))
    return (str(candidate),)


def _builder_binding_document(
    request: JsonObject,
    proposal: JsonObject,
    *,
    proposal_file_sha256: str,
    candidate_patch_policy_version: CandidatePatchPolicyVersion | str | None = None,
) -> JsonObject:
    proposal_hash = proposal.get("proposal_hash")
    if not isinstance(proposal_hash, str):
        raise ContractError("approved proposal has no canonical proposal_hash")
    request_context_hash = request.get("context_manifest_hash")
    if not isinstance(request_context_hash, str):
        raise ContractError("Builder request has no context_manifest_hash")
    selected_policy = select_candidate_patch_policy_version(
        proposal,
        candidate_patch_policy_version,
    )
    context: JsonObject = {
        "schema_version": "BuilderContextV1",
        "request_context_manifest_hash": request_context_hash,
        "proposal_hash": proposal_hash,
    }
    binding: JsonObject = {
        "schema_version": "BuilderInvocationBindingV1",
        "request_binding": request_binding(request),
        "proposal_hash": proposal_hash,
        "proposal_file_sha256": proposal_file_sha256,
    }
    if selected_policy is CandidatePatchPolicyVersion.V2:
        policy_hash = candidate_patch_policy_contract_hash(selected_policy)
        context["candidate_patch_policy_version"] = selected_policy.value
        context["candidate_patch_policy_contract_hash"] = policy_hash
        binding["candidate_patch_policy_version"] = selected_policy.value
        binding["candidate_patch_policy_contract_hash"] = policy_hash
    binding["builder_context_hash"] = hash_json(context)
    return binding


def _validate_builder_binding(plan: InvocationPlan) -> None:
    if plan.role is not InvocationRole.BUILDER:
        if plan.builder_context_hash is not None:
            raise IsolationError("Commander invocation cannot carry a Builder context hash")
        return
    if not isinstance(plan.builder_context_hash, str):
        raise IsolationError("Builder invocation has no immutable context hash")

    request_root = plan.run_root / "request"
    request_path = request_root / "research_request.json"
    proposal_path = request_root / APPROVED_PROPOSAL_FILENAME
    binding_path = request_root / BUILDER_BINDING_FILENAME
    for path in (request_path, proposal_path, binding_path):
        if path.is_symlink() or not path.is_file():
            raise IsolationError("Builder binding input is missing or is a symlink")
    request = load_json_object(request_path)
    proposal = load_json_object(proposal_path)
    binding = load_json_object(binding_path)
    expected = _builder_binding_document(
        request,
        proposal,
        proposal_file_sha256=hash_file(proposal_path),
        candidate_patch_policy_version=plan.candidate_patch_policy_version,
    )
    if binding != expected:
        raise IsolationError("Builder binding input does not match its immutable inputs")
    if binding.get("builder_context_hash") != plan.builder_context_hash:
        raise IsolationError("Builder context hash mismatch")
    selected_policy = select_candidate_patch_policy_version(
        proposal,
        plan.candidate_patch_policy_version,
    )
    if selected_policy is CandidatePatchPolicyVersion.V2:
        policy_hash = candidate_patch_policy_contract_hash(selected_policy)
        if (
            plan.candidate_patch_policy_version != selected_policy.value
            or plan.candidate_patch_policy_contract_hash != policy_hash
        ):
            raise IsolationError("Builder Candidate patch policy binding mismatch")
    elif (
        plan.candidate_patch_policy_version is not None
        or plan.candidate_patch_policy_contract_hash is not None
    ):
        raise IsolationError("legacy Builder cannot carry a Candidate patch policy override")

    if plan.backend is BackendKind.NATIVE_WINDOWS:
        staged_request = plan.work_root / ".research" / "request"
        staged_proposal = staged_request / APPROVED_PROPOSAL_FILENAME
        staged_binding = staged_request / BUILDER_BINDING_FILENAME
        for path in (staged_proposal, staged_binding):
            if path.is_symlink() or not path.is_file():
                raise IsolationError("sealed Builder binding input is missing or is a symlink")
        if hash_file(staged_proposal) != hash_file(proposal_path):
            raise IsolationError("sealed approved proposal differs from its Builder binding")
        if load_json_object(staged_binding) != binding:
            raise IsolationError("sealed Builder binding differs from its host input")


def _verify_sealed_inputs(plan: InvocationPlan) -> None:
    _validate_builder_binding(plan)
    if plan.sealed_input_hash is None:
        return
    research = plan.work_root / ".research"
    if not research.is_dir() or hash_tree(research) != plan.sealed_input_hash:
        raise IsolationError("sealed current-run inputs changed")


def _normalize_bundled_system_skills(
    plan: InvocationPlan,
    runner_home: Path,
    source: dict[str, str],
) -> None:
    skills_root = runner_home / "skills"
    if not skills_root.exists():
        return
    if skills_root.is_symlink() or not skills_root.is_dir():
        raise IsolationError("dedicated Codex runner skills entry is unsafe")
    top_level = sorted(item.name for item in skills_root.iterdir())
    if top_level != [".system"]:
        raise IsolationError(
            "dedicated Codex runner home contains arbitrary or user-installed skills"
        )
    system_root = skills_root / ".system"
    marker = system_root / _SYSTEM_SKILLS_MARKER
    if (
        system_root.is_symlink()
        or not system_root.is_dir()
        or marker.is_symlink()
        or not marker.is_file()
    ):
        raise IsolationError("bundled Codex system skills marker is missing or unsafe")
    for current, directories, filenames in os.walk(
        skills_root,
        topdown=True,
        followlinks=False,
    ):
        current_root = Path(current)
        for name in (*directories, *filenames):
            candidate = current_root / name
            if candidate.is_symlink() or candidate.is_junction():
                raise IsolationError("bundled Codex system skills contain a reparse point")
    skills_hash = hash_tree(skills_root)

    local_app_data = source.get("LOCALAPPDATA")
    if not local_app_data:
        raise IsolationError("LOCALAPPDATA is required to quarantine bundled system skills")
    product_path = Path(local_app_data) / "AdaptiveLlmQuant"
    if product_path.is_symlink():
        raise IsolationError("AdaptiveLlmQuant local state root must not be a symlink")
    product_path.mkdir(parents=True, exist_ok=True)
    product_root = product_path.resolve(strict=True)
    quarantine_root = product_root / "quarantine"
    quarantine_root.mkdir(exist_ok=True)
    if quarantine_root.is_symlink() or not quarantine_root.is_dir():
        raise IsolationError("system-skills quarantine root is unsafe")
    resolved_runner_home = runner_home.resolve(strict=True)
    resolved_quarantine = quarantine_root.resolve(strict=True)
    if (
        resolved_quarantine.is_relative_to(resolved_runner_home)
        or resolved_runner_home.is_relative_to(resolved_quarantine)
        or resolved_quarantine.is_relative_to(plan.run_root.resolve(strict=True))
        or plan.run_root.resolve(strict=True).is_relative_to(resolved_quarantine)
    ):
        raise IsolationError("system-skills quarantine crosses a protected boundary")
    target = resolved_quarantine / f"system-skills-{uuid.uuid4().hex}"
    if target.exists():
        raise IsolationError("system-skills quarantine target already exists")
    try:
        skills_root.replace(target)
    except OSError as exc:
        raise IsolationError(
            f"bundled system-skills quarantine failed: {type(exc).__name__}"
        ) from None
    write_json_exclusive(
        target / "quarantine-record.json",
        {
            "schema_version": "SystemSkillsQuarantineV1",
            "tree_hash_before_move": skills_hash,
            "source_entry": "skills",
            "bundled_system_only": True,
            "user_skills_present": False,
            "reparse_points_present": False,
            "credentials_read": False,
        },
    )


def _validated_native_runner_home(
    plan: InvocationPlan,
    source: dict[str, str],
) -> Path:
    raw_home = source.get(CODEX_RUNNER_HOME_ENV)
    if not raw_home:
        local_app_data = source.get("LOCALAPPDATA")
        if not local_app_data:
            raise IsolationError(
                f"{CODEX_RUNNER_HOME_ENV} must name the dedicated private Codex runner home"
            )
        raw_home = str(Path(local_app_data) / "AdaptiveLlmQuant" / "codex-runner-home")
    runner_home = Path(raw_home).expanduser().resolve(strict=True)
    if not runner_home.is_dir():
        raise IsolationError("dedicated Codex runner home is not a directory")
    if runner_home.is_relative_to(plan.run_root) or plan.run_root.is_relative_to(runner_home):
        raise IsolationError("dedicated Codex runner home must be outside every research run")
    _normalize_bundled_system_skills(plan, runner_home, source)
    entries = {item.name.casefold(): item for item in runner_home.iterdir()}
    forbidden = sorted(
        name
        for name in entries
        if name in {item.casefold() for item in _CONTEXT_BEARING_RUNNER_ENTRIES}
        or name.endswith(".config.toml")
    )
    if forbidden:
        raise IsolationError(
            "dedicated Codex runner home contains context-bearing entries: " + ", ".join(forbidden)
        )
    if "auth.json" in entries:
        raise IsolationError("dedicated Codex runner home must use the OS keyring, not auth.json")
    return runner_home


def _native_process_path(source: dict[str, str]) -> str:
    system_root = Path(source.get("SYSTEMROOT", r"C:\Windows"))
    candidates = [
        system_root / "System32",
        system_root / "System32" / "WindowsPowerShell" / "v1.0",
        system_root,
        system_root / "System32" / "Wbem",
        Path(r"C:\Program Files\Git\cmd"),
    ]
    return os.pathsep.join(str(path) for path in candidates if path.is_dir())


def _quarantine_prestart_runtime(
    plan: InvocationPlan,
    runtime_root: Path,
    source: dict[str, str],
) -> None:
    started_marker = plan.work_root / "execution-started.json"
    if started_marker.exists():
        raise IsolationError("started invocation runtime cannot be reused or normalized")
    expected = plan.work_root.resolve(strict=True) / ".runtime"
    if (
        runtime_root.is_symlink()
        or not runtime_root.is_dir()
        or runtime_root.resolve(strict=True) != expected
    ):
        raise IsolationError("pre-start runtime residue escaped the exact invocation")
    allowed = {"host-runtime-owner.json", "codex-sqlite", "tmp"}
    entries = {item.name for item in runtime_root.iterdir()}
    if entries != allowed:
        raise IsolationError("pre-start runtime residue contains unexpected entries")
    owner_marker = load_json_object(runtime_root / "host-runtime-owner.json")
    if (
        owner_marker.get("schema_version") != "HostRuntimeOwnerV1"
        or owner_marker.get("invocation_id") != plan.invocation_id
        or owner_marker.get("host_created") is not True
        or owner_marker.get("credentials_persisted") is not False
    ):
        raise IsolationError("pre-start runtime residue has no valid host-owner marker")
    for current, directories, filenames in os.walk(
        runtime_root,
        topdown=True,
        followlinks=False,
    ):
        current_root = Path(current)
        for name in (*directories, *filenames):
            candidate = current_root / name
            if candidate.is_symlink() or candidate.is_junction():
                raise IsolationError("pre-start runtime residue contains a reparse point")
    runtime_hash = hash_tree(runtime_root)
    local_app_data = source.get("LOCALAPPDATA")
    if not local_app_data:
        raise IsolationError("LOCALAPPDATA is required to quarantine pre-start runtime")
    quarantine = Path(local_app_data) / "AdaptiveLlmQuant" / "quarantine"
    quarantine.mkdir(parents=True, exist_ok=True)
    if quarantine.is_symlink():
        raise IsolationError("pre-start runtime quarantine root is unsafe")
    resolved_quarantine = quarantine.resolve(strict=True)
    resolved_run = plan.run_root.resolve(strict=True)
    if (
        resolved_quarantine.is_relative_to(resolved_run)
        or resolved_run.is_relative_to(resolved_quarantine)
        or resolved_quarantine.is_relative_to(runtime_root.resolve(strict=True))
    ):
        raise IsolationError("pre-start runtime quarantine crosses a protected boundary")
    target = resolved_quarantine / (f"runtime-{plan.invocation_id}-{uuid.uuid4().hex}")
    try:
        runtime_root.replace(target)
    except OSError as exc:
        raise IsolationError(f"pre-start runtime quarantine failed: {type(exc).__name__}") from None
    write_json_exclusive(
        target / "quarantine-record.json",
        {
            "schema_version": "PrestartRuntimeQuarantineV1",
            "invocation_id": plan.invocation_id,
            "tree_hash_before_move": runtime_hash,
            "host_owned_marker_valid": True,
            "execution_started": False,
            "reparse_points_present": False,
            "credentials_read": False,
        },
    )


def native_invocation_environment(
    plan: InvocationPlan,
    source: dict[str, str] | None = None,
) -> dict[str, str]:
    inherited = dict(os.environ) if source is None else source
    runner_home = _validated_native_runner_home(plan, inherited)
    environment = scrubbed_environment(inherited)
    _add_native_sandbox_identity_environment(environment, inherited)
    runtime_root = plan.work_root / ".runtime"
    if runtime_root.exists():
        _quarantine_prestart_runtime(plan, runtime_root, inherited)
    if (plan.work_root / "execution-started.json").exists():
        raise IsolationError("started invocation cannot recreate its native runtime")
    runtime_root.mkdir(exist_ok=False)
    write_json_exclusive(
        runtime_root / "host-runtime-owner.json",
        {
            "schema_version": "HostRuntimeOwnerV1",
            "invocation_id": plan.invocation_id,
            "host_created": True,
            "credentials_persisted": False,
        },
    )
    sqlite_home = runtime_root / "codex-sqlite"
    temp_root = runtime_root / "tmp"
    sqlite_home.mkdir(exist_ok=False)
    temp_root.mkdir(exist_ok=False)
    environment.update(
        {
            "CODEX_HOME": str(runner_home),
            "CODEX_SQLITE_HOME": str(sqlite_home),
            "TEMP": str(temp_root),
            "TMP": str(temp_root),
            "PATH": _native_process_path(inherited),
        }
    )
    return environment


def _native_launcher(plan: InvocationPlan) -> tuple[str, ...]:
    try:
        exec_index = plan.command.index("exec")
    except ValueError as exc:
        raise IsolationError("native Codex invocation has no exec boundary") from exc
    launcher = plan.command[:exec_index]
    if len(launcher) != 1 or not Path(launcher[0]).is_file():
        raise IsolationError("native read-jail verification requires one resolved Codex executable")
    return launcher


def native_sandbox_launcher(plan: InvocationPlan) -> tuple[str, ...]:
    return _native_launcher(plan)


def _remove_native_probe_with_retry(
    plan: InvocationPlan,
    *,
    attempts: int = 6,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    if attempts < 1:
        raise IsolationError("native probe cleanup attempts must be positive")
    probe_root = plan.work_root / ".native-read-jail-probe"
    if not probe_root.exists():
        return
    expected = plan.work_root.resolve(strict=True) / ".native-read-jail-probe"
    if probe_root.is_symlink() or probe_root.resolve(strict=True) != expected:
        raise IsolationError("native probe cleanup escaped the exact invocation root")
    last_error: OSError | None = None
    for attempt in range(attempts):
        try:
            shutil.rmtree(probe_root)
            return
        except OSError as exc:
            last_error = exc
            if attempt + 1 < attempts:
                sleep(0.05 * (2**attempt))
    if probe_root.exists():
        error_name = type(last_error).__name__ if last_error is not None else "OSError"
        raise IsolationError(f"native read-jail probe cleanup remained locked: {error_name}")


def _is_reparse_point(path: Path) -> bool:
    return path.is_symlink() or path.is_junction()


def _candidate_tree_hash_without_reparse_points(candidate_root: Path) -> str:
    for current, directories, filenames in os.walk(
        candidate_root,
        topdown=True,
        followlinks=False,
    ):
        current_root = Path(current)
        for name in (*directories, *filenames):
            if _is_reparse_point(current_root / name):
                raise IsolationError("candidate ACL recovery found a reparse point")
    return hash_tree(candidate_root)


def candidate_tree_hash_without_reparse_points(candidate_root: Path) -> str:
    return _candidate_tree_hash_without_reparse_points(candidate_root)


def _native_builder_candidate_root(plan: InvocationPlan) -> Path:
    if plan.backend is not BackendKind.NATIVE_WINDOWS or plan.role is not InvocationRole.BUILDER:
        raise IsolationError("candidate ACL recovery is only valid for a native Builder")
    run_root = plan.run_root.resolve(strict=True)
    expected_work_root = run_root / "work" / InvocationRole.BUILDER.value / plan.invocation_id
    candidate_root = expected_work_root / "candidate_worktree"
    components = (
        run_root,
        run_root / "work",
        run_root / "work" / InvocationRole.BUILDER.value,
        expected_work_root,
        candidate_root,
    )
    if _is_reparse_point(plan.run_root) or _is_reparse_point(plan.work_root):
        raise IsolationError("candidate ACL recovery path contains a reparse point")
    if any(_is_reparse_point(path) for path in components):
        raise IsolationError("candidate ACL recovery path contains a reparse point")
    if (
        plan.work_root.resolve(strict=True) != expected_work_root.resolve(strict=True)
        or not candidate_root.is_dir()
        or candidate_root.resolve(strict=True) != (plan.work_root / "candidate_worktree").resolve()
    ):
        raise IsolationError("candidate ACL recovery escaped the exact invocation root")
    try:
        candidate_root.resolve(strict=True).relative_to(run_root)
    except ValueError as exc:
        raise IsolationError("candidate ACL recovery escaped the research run") from exc
    return candidate_root


def validated_native_builder_candidate_root(plan: InvocationPlan) -> Path:
    return _native_builder_candidate_root(plan)


def _host_windows_principal(source: dict[str, str]) -> str:
    domain = source.get("USERDOMAIN")
    username = source.get("USERNAME")
    if not domain or not username:
        raise IsolationError("Windows host principal is unavailable for candidate ACL recovery")
    forbidden = frozenset('\\/:*?"<>|')
    if any(
        ord(character) < 32 or character in forbidden
        for value in (domain, username)
        for character in value
    ):
        raise IsolationError("Windows host principal is unsafe for candidate ACL recovery")
    principal = f"{domain}\\{username}"
    return principal


def _codex_sandbox_group_principal(source: dict[str, str]) -> str:
    computer_name = source.get("COMPUTERNAME")
    if not computer_name:
        raise IsolationError("Windows computer name is unavailable for native ACL staging")
    forbidden = frozenset('\\/:*?"<>|')
    if any(ord(character) < 32 or character in forbidden for character in computer_name):
        raise IsolationError("Windows computer name is unsafe for native ACL staging")
    return f"{computer_name}\\{_CODEX_SANDBOX_GROUP}"


def _trusted_windows_tool(source: dict[str, str], name: str) -> Path:
    system_root = Path(source.get("SYSTEMROOT", r"C:\Windows"))
    executable = system_root / "System32" / name
    if not executable.is_file():
        raise IsolationError(f"trusted Windows {name} executable is unavailable")
    return executable.resolve(strict=True)


def _trusted_windows_powershell(source: dict[str, str]) -> Path:
    system_root = Path(source.get("SYSTEMROOT", r"C:\Windows"))
    executable = system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if not executable.is_file():
        raise IsolationError("trusted Windows PowerShell executable is unavailable")
    return executable.resolve(strict=True)


def _run_acl_host_command(
    plan: InvocationPlan,
    command: tuple[str, ...],
    *,
    source: dict[str, str],
    run_process: HostCommandProcess | None,
    elevated: bool,
    elevated_workspace: Path | None = None,
    environment_overrides: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    if elevated:
        workspace_root = (
            plan.work_root.resolve(strict=True)
            if elevated_workspace is None
            else elevated_workspace.resolve(strict=True)
        )
        if (
            _is_reparse_point(workspace_root)
            or not workspace_root.is_dir()
            or not workspace_root.is_relative_to(plan.work_root.resolve(strict=True))
        ):
            raise IsolationError("native ACL recovery workspace is unsafe")
        workspace = workspace_root.as_posix()
        command = (
            *_native_launcher(plan),
            "sandbox",
            "-c",
            'windows.sandbox="elevated"',
            "-c",
            (
                "permissions.acl_recovery.filesystem="
                '{":minimal"="read",":workspace_roots"={"."="write"}}'
            ),
            "-c",
            (f'permissions.acl_recovery.workspace_roots={{"{workspace}"=true}}'),
            "--permission-profile",
            "acl_recovery",
            "--cd",
            str(workspace_root),
            *command,
        )
    environment = scrubbed_environment(source)
    if elevated:
        _add_native_sandbox_identity_environment(environment, source)
    if environment_overrides is not None:
        allowed_overrides = {
            "ADAPTIVE_QUANT_ACL_ROLE_ROOT",
            "ADAPTIVE_QUANT_ACL_SNAPSHOT",
        }
        if set(environment_overrides) - allowed_overrides or any(
            "\x00" in value or "\r" in value or "\n" in value
            for value in environment_overrides.values()
        ):
            raise IsolationError("native ACL command environment override is unsafe")
        environment.update(environment_overrides)
    environment["PATH"] = _native_process_path(source)
    runner = run_process or cast(HostCommandProcess, subprocess.run)
    try:
        completed = runner(
            command,
            cwd=plan.work_root,
            env=environment,
            text=True,
            encoding="oem",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise IsolationError(f"native ACL command could not run: {type(exc).__name__}") from None
    if completed.returncode != 0:
        raise IsolationError("native ACL command failed")
    return completed


def _native_acl_scopes(
    plan: InvocationPlan,
) -> tuple[tuple[Path, ...], Path, tuple[Path, ...]]:
    if plan.backend is not BackendKind.NATIVE_WINDOWS:
        raise IsolationError("native ACL staging requires a native Windows invocation")
    run_root = plan.run_root.resolve(strict=True)
    work_root = plan.work_root.resolve(strict=True)
    role_root = run_root / "work" / plan.role.value
    expected_work_root = role_root / plan.invocation_id
    if work_root != expected_work_root.resolve(strict=True):
        raise IsolationError("native ACL staging escaped the exact invocation")
    traverse = (
        run_root,
        run_root / "work",
        role_root,
        work_root,
        work_root / ".runtime",
    )
    research = work_root / ".research"
    writable = [work_root / ".runtime" / "tmp"]
    if plan.role is InvocationRole.BUILDER:
        writable.append(work_root / "candidate_worktree")
    for path in (*traverse, research, *writable):
        if not path.is_dir() or _is_reparse_point(path) or path.resolve(strict=True) != path:
            raise IsolationError("native ACL staging found an unsafe scope")
    for current, directories, filenames in os.walk(
        work_root,
        topdown=True,
        followlinks=False,
    ):
        current_root = Path(current)
        for name in (*directories, *filenames):
            if _is_reparse_point(current_root / name):
                raise IsolationError("native ACL staging found a reparse point")
    return traverse, research, tuple(writable)


def _native_acl_state_root(
    plan: InvocationPlan,
    source: dict[str, str],
) -> Path:
    local_app_data = source.get("LOCALAPPDATA")
    if not local_app_data:
        raise IsolationError("LOCALAPPDATA is required for native ACL staging")
    product_root = Path(local_app_data) / "AdaptiveLlmQuant"
    product_root.mkdir(parents=True, exist_ok=True)
    if _is_reparse_point(product_root):
        raise IsolationError("native ACL state root is unsafe")
    root = product_root.resolve(strict=True) / "native-acl" / plan.invocation_id
    root.parent.mkdir(exist_ok=True)
    if _is_reparse_point(root.parent):
        raise IsolationError("native ACL state parent is unsafe")
    resolved_run = plan.run_root.resolve(strict=True)
    resolved_parent = root.parent.resolve(strict=True)
    if resolved_parent.is_relative_to(resolved_run) or resolved_run.is_relative_to(resolved_parent):
        raise IsolationError("native ACL state crosses the research run boundary")
    return root


def _native_acl_events_root(plan: InvocationPlan) -> Path:
    events_root = plan.work_root / "native-acl-events"
    if events_root.exists():
        if (
            _is_reparse_point(events_root)
            or not events_root.is_dir()
            or events_root.resolve(strict=True)
            != plan.work_root.resolve(strict=True) / "native-acl-events"
        ):
            raise IsolationError("native ACL event root is unsafe")
    else:
        events_root.mkdir(exist_ok=False)
    return events_root


def _native_acl_event_path(plan: InvocationPlan, epoch: int, event_type: str) -> Path:
    return _native_acl_events_root(plan) / f"{epoch:04d}-{event_type.casefold()}.json"


def _native_acl_epochs(plan: InvocationPlan) -> tuple[int, ...]:
    root = _native_acl_events_root(plan)
    epochs: set[int] = set()
    for path in root.iterdir():
        match = re.fullmatch(r"([0-9]{4})-(plan|staged|revoked)\.json", path.name)
        if match is None or path.is_symlink() or not path.is_file():
            raise IsolationError("native ACL event root contains an unexpected artifact")
        epochs.add(int(match.group(1)))
    return tuple(sorted(epochs))


def _native_acl_epoch_state(plan: InvocationPlan, epoch: int) -> tuple[bool, bool, bool]:
    root = _native_acl_events_root(plan)
    return (
        (root / f"{epoch:04d}-plan.json").is_file(),
        (root / f"{epoch:04d}-staged.json").is_file(),
        (root / f"{epoch:04d}-revoked.json").is_file(),
    )


def _read_icacls_snapshot(path: Path) -> dict[str, str]:
    try:
        text = path.read_bytes().decode("utf-16")
    except (OSError, UnicodeError) as exc:
        raise IsolationError("native ACL snapshot is unreadable") from exc
    lines = [line.rstrip("\r") for line in text.splitlines() if line.strip()]
    if not lines or len(lines) % 2 != 0:
        raise IsolationError("native ACL snapshot is malformed")
    entries: dict[str, str] = {}
    for index in range(0, len(lines), 2):
        relative = lines[index].replace("/", "\\")
        sddl = lines[index + 1]
        candidate = Path(relative)
        if (
            candidate.is_absolute()
            or ".." in candidate.parts
            or ":" in relative
            or not sddl.startswith("D:")
        ):
            raise IsolationError("native ACL snapshot contains an unsafe entry")
        key = relative.casefold()
        if key in entries:
            raise IsolationError("native ACL snapshot contains duplicate paths")
        entries[key] = sddl
    return entries


def _new_acl_entries_are_inherited_only(
    baseline: dict[str, str],
    current: dict[str, str],
) -> bool:
    baseline_trustees: set[str] = set()
    for baseline_sddl in baseline.values():
        for match in _SDDL_ACE_PATTERN.finditer(baseline_sddl):
            fields = match.group(1).split(";")
            if len(fields) == 6:
                baseline_trustees.add(fields[5].casefold())
    for path, current_sddl in current.items():
        baseline_sddl = baseline.get(path)
        if baseline_sddl is not None:
            if _canonical_dacl(current_sddl) != _canonical_dacl(baseline_sddl):
                return False
            continue
        for match in _SDDL_ACE_PATTERN.finditer(current_sddl):
            fields = match.group(1).split(";")
            if (
                len(fields) != 6
                or "ID" not in fields[1]
                or fields[5].casefold() not in baseline_trustees
            ):
                return False
    return True


def _canonical_dacl(sddl: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    first_ace = sddl.find("(")
    control_end = len(sddl) if first_ace < 0 else first_ace
    control = sddl[2:control_end]
    flags = tuple(
        sorted(
            flag for flag in _SDDL_CONTROL_FLAG_PATTERN.findall(control) if flag not in {"AI", "AR"}
        )
    )
    aces = tuple(sorted(match.group(1) for match in _SDDL_ACE_PATTERN.finditer(sddl)))
    return flags, aces


def _native_acl_snapshot(
    plan: InvocationPlan,
    snapshot_path: Path,
    *,
    source: dict[str, str],
    run_process: HostCommandProcess | None,
    elevated: bool,
) -> dict[str, str]:
    icacls = _trusted_windows_tool(source, "icacls.exe")
    if snapshot_path.exists():
        raise IsolationError("native ACL snapshot target already exists")
    command = (
        str(icacls),
        str(plan.work_root.resolve(strict=True)),
        "/save",
        str(snapshot_path),
        "/T",
        "/L",
        "/Q",
    )
    _run_acl_host_command(
        plan,
        command,
        source=source,
        run_process=run_process,
        elevated=elevated,
    )
    if snapshot_path.is_symlink() or not snapshot_path.is_file():
        raise IsolationError("native ACL snapshot command produced no safe snapshot")
    return _read_icacls_snapshot(snapshot_path)


def _restore_native_acl_snapshot(
    plan: InvocationPlan,
    *,
    snapshot_path: Path,
    source: dict[str, str],
    run_process: HostCommandProcess | None,
) -> None:
    """Restore baseline DACLs without requiring SeRestorePrivilege."""
    powershell = _trusted_windows_powershell(source)
    role_root = plan.work_root.resolve(strict=True).parent
    script = (
        "$ErrorActionPreference='Stop';"
        "$root=[IO.Path]::GetFullPath($env:ADAPTIVE_QUANT_ACL_ROLE_ROOT);"
        "$prefix=$root.TrimEnd([IO.Path]::DirectorySeparatorChar)"
        "+[IO.Path]::DirectorySeparatorChar;"
        "$lines=@(Get-Content -LiteralPath $env:ADAPTIVE_QUANT_ACL_SNAPSHOT -Encoding Unicode"
        "|Where-Object {-not [String]::IsNullOrWhiteSpace($_)});"
        "if($lines.Count -eq 0 -or ($lines.Count % 2) -ne 0){throw 'acl snapshot malformed'};"
        "for($i=0;$i -lt $lines.Count;$i+=2){"
        "$target=[IO.Path]::GetFullPath((Join-Path $root $lines[$i]));"
        "if(-not $target.StartsWith($prefix,[StringComparison]::OrdinalIgnoreCase))"
        "{throw 'acl snapshot path escaped'};"
        "if(Test-Path -LiteralPath $target){"
        "$acl=Get-Acl -LiteralPath $target;"
        "$acl.SetSecurityDescriptorSddlForm("
        "$lines[$i+1],[Security.AccessControl.AccessControlSections]::Access);"
        "Set-Acl -LiteralPath $target -AclObject $acl"
        "}"
        "}"
    )
    _run_acl_host_command(
        plan,
        (
            str(powershell),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            script,
        ),
        source=source,
        run_process=run_process,
        elevated=False,
        environment_overrides={
            "ADAPTIVE_QUANT_ACL_ROLE_ROOT": str(role_root),
            "ADAPTIVE_QUANT_ACL_SNAPSHOT": str(snapshot_path.resolve(strict=True)),
        },
    )


def _native_acl_event(
    plan: InvocationPlan,
    epoch: int,
    event_type: str,
    *,
    snapshot_hash: str,
    sandbox_principal_hash: str,
    extra: JsonObject | None = None,
) -> JsonObject:
    event: JsonObject = {
        "schema_version": _NATIVE_ACL_EVENT_SCHEMA,
        "invocation_id": plan.invocation_id,
        "epoch": epoch,
        "event_type": event_type,
        "scope": "exact_invocation",
        "snapshot_hash": snapshot_hash,
        "sandbox_principal_hash": sandbox_principal_hash,
        "other_runs_granted": False,
        "reparse_points_followed": False,
        "credential_used": False,
    }
    if extra is not None:
        event.update(extra)
    return event


def stage_native_invocation_acl(
    plan: InvocationPlan,
    *,
    source_environment: dict[str, str] | None = None,
    run_process: HostCommandProcess | None = None,
) -> int:
    """Grant the sandbox only the exact current-run paths needed by one child."""
    inherited = dict(os.environ) if source_environment is None else source_environment
    traverse, research, writable = _native_acl_scopes(plan)
    events_root = _native_acl_events_root(plan)
    epochs = _native_acl_epochs(plan)
    for epoch in epochs:
        planned, staged, revoked = _native_acl_epoch_state(plan, epoch)
        if not planned or (revoked and not staged):
            raise IsolationError("native ACL event history is malformed")
        if staged and not revoked:
            raise IsolationError("native ACL staging is already active")
        if planned and not staged and not revoked:
            raise IsolationError("incomplete native ACL staging requires manual recovery")
    epoch = (epochs[-1] + 1) if epochs else 1
    state_root = _native_acl_state_root(plan, inherited)
    state_root.mkdir(exist_ok=True)
    if _is_reparse_point(state_root):
        raise IsolationError("native ACL invocation state is unsafe")
    epoch_root = state_root / f"{epoch:04d}"
    epoch_root.mkdir(exist_ok=False)
    snapshot_path = epoch_root / _NATIVE_ACL_SNAPSHOT_NAME
    baseline = _native_acl_snapshot(
        plan,
        snapshot_path,
        source=inherited,
        run_process=run_process,
        elevated=False,
    )
    snapshot_hash = hash_file(snapshot_path)
    principal = _codex_sandbox_group_principal(inherited)
    host_principal = _host_windows_principal(inherited)
    principal_hash = sha256_bytes(principal.casefold().encode("utf-8"))
    scope_hash = hash_json(
        {
            "traverse": [str(path.resolve(strict=True)) for path in traverse],
            "read": [str(research.resolve(strict=True))],
            "write": [str(path.resolve(strict=True)) for path in writable],
        }
    )
    write_json_exclusive(
        events_root / f"{epoch:04d}-plan.json",
        _native_acl_event(
            plan,
            epoch,
            "PLAN",
            snapshot_hash=snapshot_hash,
            sandbox_principal_hash=principal_hash,
            extra={
                "baseline_entry_count": len(baseline),
                "scope_hash": scope_hash,
            },
        ),
    )
    icacls = _trusted_windows_tool(inherited, "icacls.exe")
    commands: list[tuple[str, ...]] = []
    commands.extend(
        (
            str(icacls),
            str(path.resolve(strict=True)),
            "/grant",
            f"{principal}:(X)",
            "/L",
            "/Q",
        )
        for path in traverse
    )
    commands.append(
        (
            str(icacls),
            str(research.resolve(strict=True)),
            "/grant",
            f"{principal}:(OI)(CI)RX",
            "/T",
            "/L",
            "/Q",
        )
    )
    commands.extend(
        (
            str(icacls),
            str(path.resolve(strict=True)),
            "/grant",
            f"{host_principal}:(OI)(CI)F",
            "/T",
            "/L",
            "/Q",
        )
        for path in writable
    )
    commands.extend(
        (
            str(icacls),
            str(path.resolve(strict=True)),
            "/grant",
            f"{principal}:(OI)(CI)M",
            "/T",
            "/L",
            "/Q",
        )
        for path in writable
    )
    try:
        for command in commands:
            _run_acl_host_command(
                plan,
                command,
                source=inherited,
                run_process=run_process,
                elevated=False,
            )
    except IsolationError:
        raise IsolationError(
            "native ACL staging failed after its recovery snapshot was sealed"
        ) from None
    write_json_exclusive(
        events_root / f"{epoch:04d}-staged.json",
        _native_acl_event(
            plan,
            epoch,
            "STAGED",
            snapshot_hash=snapshot_hash,
            sandbox_principal_hash=principal_hash,
            extra={
                "baseline_entry_count": len(baseline),
                "scope_hash": scope_hash,
            },
        ),
    )
    return epoch


def _active_native_acl_epoch(plan: InvocationPlan) -> int | None:
    active: list[int] = []
    for epoch in _native_acl_epochs(plan):
        planned, staged, revoked = _native_acl_epoch_state(plan, epoch)
        if not planned or (revoked and not staged):
            raise IsolationError("native ACL event history is malformed")
        if staged and not revoked:
            active.append(epoch)
    if len(active) > 1:
        raise IsolationError("multiple native ACL epochs are active")
    return active[0] if active else None


def revoke_native_invocation_acl(
    plan: InvocationPlan,
    *,
    child_exit_confirmed: bool,
    source_environment: dict[str, str] | None = None,
    run_process: HostCommandProcess | None = None,
) -> None:
    """Restore the exact pre-stage DACLs after the one sandbox child exits."""
    if not child_exit_confirmed:
        raise IsolationError("native ACL revocation requires confirmed child exit")
    inherited = dict(os.environ) if source_environment is None else source_environment
    epoch = _active_native_acl_epoch(plan)
    if epoch is None:
        return
    traverse, _research, writable = _native_acl_scopes(plan)
    state_root = _native_acl_state_root(plan, inherited)
    epoch_root = state_root / f"{epoch:04d}"
    snapshot_path = epoch_root / _NATIVE_ACL_SNAPSHOT_NAME
    plan_event = load_json_object(_native_acl_event_path(plan, epoch, "plan"))
    staged_event = load_json_object(_native_acl_event_path(plan, epoch, "staged"))
    if plan_event != staged_event | {"event_type": "PLAN"}:
        raise IsolationError("native ACL plan and staged events do not match")
    expected_snapshot_hash = plan_event.get("snapshot_hash")
    if (
        not isinstance(expected_snapshot_hash, str)
        or snapshot_path.is_symlink()
        or not snapshot_path.is_file()
        or hash_file(snapshot_path) != expected_snapshot_hash
    ):
        raise IsolationError("native ACL recovery snapshot changed")
    baseline = _read_icacls_snapshot(snapshot_path)
    principal = _codex_sandbox_group_principal(inherited)
    principal_hash = sha256_bytes(principal.casefold().encode("utf-8"))
    if plan_event.get("sandbox_principal_hash") != principal_hash:
        raise IsolationError("native ACL sandbox principal changed")
    host_principal = _host_windows_principal(inherited)
    icacls = _trusted_windows_tool(inherited, "icacls.exe")
    for path in writable:
        for operation in (
            ("/grant", f"{host_principal}:(OI)(CI)F"),
            ("/setowner", host_principal),
        ):
            _run_acl_host_command(
                plan,
                (
                    str(icacls),
                    str(path.resolve(strict=True)),
                    *operation,
                    "/T",
                    "/L",
                    "/Q",
                ),
                source=inherited,
                run_process=run_process,
                elevated=False,
            )
    _run_acl_host_command(
        plan,
        (
            str(icacls),
            str(plan.work_root.resolve(strict=True)),
            "/reset",
            "/T",
            "/L",
            "/Q",
        ),
        source=inherited,
        run_process=run_process,
        elevated=False,
    )
    _restore_native_acl_snapshot(
        plan,
        snapshot_path=snapshot_path,
        source=inherited,
        run_process=run_process,
    )
    for path in traverse[:3]:
        _run_acl_host_command(
            plan,
            (
                str(icacls),
                str(path.resolve(strict=True)),
                "/remove:g",
                principal,
                "/L",
                "/Q",
            ),
            source=inherited,
            run_process=run_process,
            elevated=False,
        )
    verify_path = epoch_root / _NATIVE_ACL_VERIFY_NAME
    if verify_path.exists():
        if (
            verify_path.is_symlink()
            or not verify_path.is_file()
            or verify_path.resolve(strict=True)
            != epoch_root.resolve(strict=True) / _NATIVE_ACL_VERIFY_NAME
        ):
            raise IsolationError("native ACL verification snapshot is unsafe")
        _read_icacls_snapshot(verify_path)
        verify_path.unlink()
    current = _native_acl_snapshot(
        plan,
        verify_path,
        source=inherited,
        run_process=run_process,
        elevated=False,
    )
    if not _new_acl_entries_are_inherited_only(baseline, current):
        raise IsolationError("native ACL revocation left an explicit ACL delta")
    write_json_exclusive(
        _native_acl_event_path(plan, epoch, "revoked"),
        _native_acl_event(
            plan,
            epoch,
            "REVOKED",
            snapshot_hash=expected_snapshot_hash,
            sandbox_principal_hash=principal_hash,
            extra={
                "baseline_entry_count": len(baseline),
                "verified_entry_count": len(current),
                "new_entries_inherited_only": True,
                "host_owner_restored": True,
                "snapshot_retained_outside_run": True,
            },
        ),
    )


def recover_native_builder_candidate_acl(
    plan: InvocationPlan,
    *,
    child_exit_confirmed: bool,
    source_environment: dict[str, str] | None = None,
    run_process: HostCommandProcess | None = None,
) -> None:
    """Restore host access to one exited native Builder candidate tree."""
    if not child_exit_confirmed:
        raise IsolationError("candidate ACL recovery requires confirmed child exit")
    inherited = dict(os.environ) if source_environment is None else source_environment
    staged_epoch = _active_native_acl_epoch(plan)
    if staged_epoch is not None:
        revoke_native_invocation_acl(
            plan,
            child_exit_confirmed=child_exit_confirmed,
            source_environment=inherited,
            run_process=run_process,
        )
    candidate_root = _native_builder_candidate_root(plan)
    principal = _host_windows_principal(inherited)
    principal_hash = sha256_bytes(principal.casefold().encode("utf-8"))
    marker_path = plan.work_root / "candidate-acl-recovery.json"
    if marker_path.is_symlink():
        raise IsolationError("candidate ACL recovery marker must not be a symlink")
    if marker_path.exists():
        marker = load_json_object(marker_path)
        if (
            marker.get("schema_version") != "CandidateAclRecoveryV1"
            or marker.get("invocation_id") != plan.invocation_id
            or marker.get("scope") != "candidate_worktree"
            or marker.get("host_principal_hash") != principal_hash
            or marker.get("credential_used") is not False
            or marker.get("reparse_points_followed") is not False
        ):
            raise IsolationError("candidate ACL recovery marker is malformed")
        try:
            candidate_tree_hash = _candidate_tree_hash_without_reparse_points(candidate_root)
        except OSError:
            raise IsolationError("recovered candidate tree is not host-readable") from None
        if marker.get("candidate_tree_hash_after_recovery") != candidate_tree_hash:
            raise IsolationError("candidate tree changed after ACL recovery")
        return

    if staged_epoch is not None:
        candidate_tree_hash = _candidate_tree_hash_without_reparse_points(candidate_root)
        write_json_exclusive(
            marker_path,
            {
                "schema_version": "CandidateAclRecoveryV1",
                "invocation_id": plan.invocation_id,
                "scope": "candidate_worktree",
                "candidate_tree_hash_after_recovery": candidate_tree_hash,
                "host_principal_hash": principal_hash,
                "recursive": True,
                "reparse_points_followed": False,
                "credential_used": False,
                "recovery_mode": "RESTORED_PRESTAGE_DACL",
                "native_acl_epoch": staged_epoch,
            },
        )
        return

    icacls = _trusted_windows_tool(inherited, "icacls.exe")
    candidate_workspace = candidate_root.resolve(strict=True).as_posix()
    command = (
        *_native_launcher(plan),
        "sandbox",
        "-c",
        'windows.sandbox="elevated"',
        "-c",
        (
            "permissions.acl_recovery.filesystem="
            '{":minimal"="read",":workspace_roots"={"."="write"}}'
        ),
        "-c",
        (f'permissions.acl_recovery.workspace_roots={{"{candidate_workspace}"=true}}'),
        "--permission-profile",
        "acl_recovery",
        "--cd",
        str(candidate_root.resolve(strict=True)),
        str(icacls.resolve(strict=True)),
        str(candidate_root.resolve(strict=True)),
        "/grant",
        f"{principal}:(OI)(CI)F",
        "/T",
        "/L",
        "/Q",
    )
    environment = scrubbed_environment(inherited)
    _add_native_sandbox_identity_environment(environment, inherited)
    environment["PATH"] = _native_process_path(inherited)
    runner = run_process or cast(HostCommandProcess, subprocess.run)
    try:
        completed = runner(
            command,
            cwd=candidate_root,
            env=environment,
            text=True,
            encoding="utf-8",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise IsolationError(
            f"candidate ACL recovery could not run: {type(exc).__name__}"
        ) from None
    if completed.returncode != 0:
        raise IsolationError("candidate ACL recovery failed")
    try:
        candidate_tree_hash = _candidate_tree_hash_without_reparse_points(candidate_root)
    except OSError:
        raise IsolationError("recovered candidate tree is not host-readable") from None
    write_json_exclusive(
        marker_path,
        {
            "schema_version": "CandidateAclRecoveryV1",
            "invocation_id": plan.invocation_id,
            "scope": "candidate_worktree",
            "candidate_tree_hash_after_recovery": candidate_tree_hash,
            "host_principal_hash": principal_hash,
            "recursive": True,
            "reparse_points_followed": False,
            "credential_used": False,
        },
    )


def _quarantine_unstarted_native_preflight(
    plan: InvocationPlan,
    source: dict[str, str],
) -> None:
    result_path = plan.work_root / "native-read-jail-preflight.json"
    if not result_path.exists():
        return
    if (plan.work_root / "execution-started.json").exists():
        raise IsolationError("started invocation preflight cannot be replaced")
    if (
        result_path.is_symlink()
        or not result_path.is_file()
        or result_path.resolve(strict=True)
        != plan.work_root.resolve(strict=True) / result_path.name
    ):
        raise IsolationError("native read-jail preflight artifact is unsafe")
    result = load_json_object(result_path)
    allowed_keys = {
        "schema_version",
        "invocation_id",
        "write_inside_workspace",
        "read_outside_workspace_denied",
        "credential_used",
    }
    if (
        not set(result).issubset(allowed_keys)
        or set(result)
        not in (
            allowed_keys,
            allowed_keys - {"invocation_id"},
        )
        or result.get("schema_version") != "NativeReadJailPreflightV1"
        or ("invocation_id" in result and result.get("invocation_id") != plan.invocation_id)
        or not isinstance(result.get("write_inside_workspace"), bool)
        or not isinstance(result.get("read_outside_workspace_denied"), bool)
        or result.get("credential_used") is not False
    ):
        raise IsolationError("native read-jail preflight artifact is malformed")
    local_app_data = source.get("LOCALAPPDATA")
    if not local_app_data:
        raise IsolationError("LOCALAPPDATA is required to quarantine a prior preflight")
    quarantine_root = Path(local_app_data) / "AdaptiveLlmQuant" / "quarantine"
    quarantine_root.mkdir(parents=True, exist_ok=True)
    if _is_reparse_point(quarantine_root):
        raise IsolationError("native preflight quarantine root is unsafe")
    resolved_quarantine = quarantine_root.resolve(strict=True)
    resolved_run = plan.run_root.resolve(strict=True)
    if resolved_quarantine.is_relative_to(resolved_run) or resolved_run.is_relative_to(
        resolved_quarantine
    ):
        raise IsolationError("native preflight quarantine crosses the research run")
    target = resolved_quarantine / f"read-jail-preflight-{plan.invocation_id}-{uuid.uuid4().hex}"
    target.mkdir(exist_ok=False)
    artifact_hash = hash_file(result_path)
    try:
        result_path.replace(target / result_path.name)
    except OSError as exc:
        raise IsolationError(f"native preflight quarantine failed: {type(exc).__name__}") from None
    write_json_exclusive(
        target / "quarantine-record.json",
        {
            "schema_version": "NativeReadJailPreflightQuarantineV1",
            "invocation_id": plan.invocation_id,
            "artifact_hash": artifact_hash,
            "execution_started": False,
            "schema_valid": True,
            "exact_invocation_path": True,
            "credential_used": False,
        },
    )


def _grant_native_probe_acl(
    plan: InvocationPlan,
    probe_root: Path,
    workspace: Path,
    *,
    source: dict[str, str],
    run_process: HostCommandProcess | None,
) -> None:
    expected_probe = plan.work_root.resolve(strict=True) / ".native-read-jail-probe"
    if (
        _is_reparse_point(probe_root)
        or _is_reparse_point(workspace)
        or probe_root.resolve(strict=True) != expected_probe
        or workspace.resolve(strict=True) != expected_probe / "workspace"
    ):
        raise IsolationError("native read-jail probe ACL scope is unsafe")
    principal = _codex_sandbox_group_principal(source)
    host_principal = _host_windows_principal(source)
    icacls = _trusted_windows_tool(source, "icacls.exe")
    commands = (
        (
            str(icacls),
            str(probe_root.resolve(strict=True)),
            "/grant",
            f"{principal}:(X)",
            "/L",
            "/Q",
        ),
        (
            str(icacls),
            str(workspace.resolve(strict=True)),
            "/grant",
            f"{host_principal}:(OI)(CI)F",
            "/T",
            "/L",
            "/Q",
        ),
        (
            str(icacls),
            str(workspace.resolve(strict=True)),
            "/grant",
            f"{principal}:(OI)(CI)M",
            "/T",
            "/L",
            "/Q",
        ),
    )
    for command in commands:
        _run_acl_host_command(
            plan,
            command,
            source=source,
            run_process=run_process,
            elevated=False,
        )


def verify_native_read_jail(
    plan: InvocationPlan,
    source_environment: dict[str, str] | None = None,
    *,
    acl_process: HostCommandProcess | None = None,
    acl_already_staged: bool = False,
) -> None:
    """Prove the native sandbox denies sibling reads before exposing run inputs."""
    inherited = dict(os.environ) if source_environment is None else source_environment
    _quarantine_unstarted_native_preflight(plan, inherited)
    if not acl_already_staged:
        stage_native_invocation_acl(
            plan,
            source_environment=inherited,
            run_process=acl_process,
        )
    try:
        _verify_native_read_jail_staged(
            plan,
            inherited,
            acl_process=acl_process,
        )
    finally:
        if not acl_already_staged:
            revoke_native_invocation_acl(
                plan,
                child_exit_confirmed=True,
                source_environment=inherited,
                run_process=acl_process,
            )


def _verify_native_read_jail_staged(
    plan: InvocationPlan,
    inherited: dict[str, str],
    *,
    acl_process: HostCommandProcess | None,
) -> None:
    probe_root = plan.work_root / ".native-read-jail-probe"
    workspace = probe_root / "workspace"
    sibling = probe_root / "sibling"
    workspace.mkdir(parents=True, exist_ok=False)
    sibling.mkdir()
    write_text_exclusive(sibling / "sentinel.txt", "BENIGN_READ_JAIL_SENTINEL\n")
    _grant_native_probe_acl(
        plan,
        probe_root,
        workspace,
        source=inherited,
        run_process=acl_process,
    )
    workspace_path = workspace.resolve().as_posix()
    environment = scrubbed_environment(inherited)
    _add_native_sandbox_identity_environment(environment, inherited)
    for name in ("CODEX_HOME", "CODEX_SQLITE_HOME", "TEMP", "TMP"):
        value = inherited.get(name)
        if value:
            environment[name] = value
    environment["PATH"] = _native_process_path(inherited)
    command_shell = str(Path(inherited.get("SYSTEMROOT", r"C:\Windows")) / "System32" / "cmd.exe")
    launcher = _native_launcher(plan)
    write_command = (
        *launcher,
        "sandbox",
        "-c",
        'windows.sandbox="elevated"',
        "-c",
        (
            "permissions.read_jail_probe.filesystem="
            '{":minimal"="read",":workspace_roots"={"."="write"}}'
        ),
        "-c",
        (f'permissions.read_jail_probe.workspace_roots={{"{workspace_path}"=true}}'),
        "--permission-profile",
        "read_jail_probe",
        "--cd",
        str(workspace.resolve()),
        command_shell,
        "/d",
        "/c",
        "echo OK>inside.txt",
    )
    read_command = (
        *launcher,
        "sandbox",
        "-c",
        'windows.sandbox="elevated"',
        "-c",
        (
            "permissions.read_jail_probe.filesystem="
            '{":minimal"="read",":workspace_roots"={"."="write"}}'
        ),
        "-c",
        (f'permissions.read_jail_probe.workspace_roots={{"{workspace_path}"=true}}'),
        "--permission-profile",
        "read_jail_probe",
        "--cd",
        str(workspace.resolve()),
        command_shell,
        "/d",
        "/c",
        f'type "{(sibling / "sentinel.txt").resolve()}"',
    )
    preflight_error: BaseException | None = None
    try:
        write_result = subprocess.run(  # noqa: S603 - fixed trusted executable and args
            write_command,
            cwd=workspace,
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=60,
        )
        read_result = subprocess.run(  # noqa: S603 - fixed trusted executable and args
            read_command,
            cwd=workspace,
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=60,
        )
        write_ok = write_result.returncode == 0
        read_denied = read_result.returncode != 0
    except (OSError, subprocess.TimeoutExpired) as exc:
        preflight_error = IsolationError(
            f"native read-jail verification could not run: {type(exc).__name__}"
        )
        write_ok = False
        read_denied = False
    finally:
        try:
            _remove_native_probe_with_retry(plan)
        except BaseException as exc:
            preflight_error = exc
    write_json_exclusive(
        plan.work_root / "native-read-jail-preflight.json",
        {
            "schema_version": "NativeReadJailPreflightV1",
            "invocation_id": plan.invocation_id,
            "write_inside_workspace": write_ok,
            "read_outside_workspace_denied": read_denied,
            "credential_used": False,
        },
    )
    if preflight_error is not None:
        raise preflight_error
    if not write_ok:
        raise IsolationError("native elevated sandbox cannot write its candidate workspace")
    if not read_denied:
        raise IsolationError(
            "native Windows sandbox does not enforce the required sibling-read jail; "
            "use Docker, WSL2, or another externally reviewed OS jail"
        )


def prepare_invocation(
    layout: RunLayout,
    role: InvocationRole,
    backend: Backend,
    *,
    prompt: str,
    approved_proposal: JsonObject | None = None,
    candidate_patch_policy_version: CandidatePatchPolicyVersion | str | None = None,
) -> InvocationPlan:
    request = load_json_object(layout.request / "research_request.json")
    if role is InvocationRole.COMMANDER and request.get("selected_commander") != "CODEX_SOL_MAX":
        raise ContractError("Codex Commander is not selected for this cycle")
    backend.validate()
    invocation_id = f"{role.value}-{uuid.uuid4().hex}"
    role_root = layout.work / role.value
    if any(role_root.iterdir()):
        raise IsolationError(f"{role.value} invocation already exists for this cycle")
    work_root = role_root / invocation_id
    work_root.mkdir(exist_ok=False)
    builder_context_hash: str | None = None
    bound_patch_policy_version: str | None = None
    bound_patch_policy_contract_hash: str | None = None
    invocation_request_root = layout.request
    if role is InvocationRole.BUILDER:
        if approved_proposal is None:
            raise ContractError("Builder requires an approved AlgorithmProposal")
        validate_algorithm_proposal(approved_proposal, request)
        selected_patch_policy = select_candidate_patch_policy_version(
            approved_proposal,
            candidate_patch_policy_version,
        )
        if selected_patch_policy is CandidatePatchPolicyVersion.V2:
            bound_patch_policy_version = selected_patch_policy.value
            bound_patch_policy_contract_hash = candidate_patch_policy_contract_hash(
                selected_patch_policy
            )
        proposal_path = layout.request / APPROVED_PROPOSAL_FILENAME
        write_json_exclusive(
            proposal_path,
            approved_proposal,
        )
        builder_binding = _builder_binding_document(
            request,
            approved_proposal,
            proposal_file_sha256=hash_file(proposal_path),
            candidate_patch_policy_version=bound_patch_policy_version,
        )
        builder_context_hash_value = builder_binding.get("builder_context_hash")
        if not isinstance(builder_context_hash_value, str):
            raise ContractError("host failed to construct the Builder context hash")
        builder_context_hash = builder_context_hash_value
        write_json_exclusive(
            layout.request / BUILDER_BINDING_FILENAME,
            builder_binding,
        )
        invocation_request_root = _materialize_builder_request(
            layout,
            request=request,
        )
        _copy_builder_snapshot(layout, work_root)
    elif candidate_patch_policy_version is not None:
        raise ContractError("Commander cannot select a Candidate patch policy")
    output_schema = (
        decision_schema_name(request)
        if role is InvocationRole.COMMANDER
        else "CandidateBuildResultV1"
    )
    output_path = work_root / "model-output.json"
    sealed_input_hash: str | None = None
    if isinstance(backend, DockerBackend):
        request_mount = _safe_mount(invocation_request_root)
        input_mount = _safe_mount(layout.input)
        work_mount = _safe_mount(work_root)
        command = (
            backend.executable,
            "run",
            "--rm",
            "--init",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--pids-limit=256",
            "--memory=4g",
            "--cpus=2",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=1g",  # noqa: S108 - isolated container tmpfs
            "--mount",
            f"type=bind,src={work_mount},dst=/workspace",
            "--mount",
            f"type=bind,src={request_mount},dst=/workspace/.research/request,readonly",
            "--mount",
            f"type=bind,src={input_mount},dst=/workspace/.research/input,readonly",
            "--network",
            backend.egress_network,
            backend.image,
            *_codex_arguments(role, container=True),
        )
        backend_kind = BackendKind.DOCKER
    elif isinstance(backend, ExplicitJailBackend):
        _link_read_only_inputs(
            layout,
            work_root,
            request_root=invocation_request_root,
        )
        command = (
            *backend.command,
            "--policy",
            backend.policy_id,
            "--work-root",
            str(work_root.resolve(strict=True)),
            "--read-only",
            str(invocation_request_root.resolve(strict=True)),
            "--read-only",
            str(layout.input.resolve(strict=True)),
            "--deny-sibling-runs",
            "--",
            *_codex_arguments(role, container=False),
        )
        backend_kind = BackendKind.EXPLICIT_JAIL
    else:
        sealed_input_hash = _stage_native_inputs(
            layout,
            work_root,
            request_root=invocation_request_root,
        )
        command = _codex_arguments(
            role,
            container=False,
            native_windows=True,
            launcher=_native_codex_launcher(backend.executable),
        )
        backend_kind = BackendKind.NATIVE_WINDOWS
    if "resume" in command:
        raise IsolationError("Codex resume is forbidden")
    plan = InvocationPlan(
        invocation_id=invocation_id,
        role=role,
        backend=backend_kind,
        run_root=layout.root,
        work_root=work_root,
        command=command,
        prompt=prompt,
        output_path=output_path,
        output_schema=output_schema,
        sealed_input_hash=sealed_input_hash,
        builder_context_hash=builder_context_hash,
        candidate_patch_policy_version=bound_patch_policy_version,
        candidate_patch_policy_contract_hash=bound_patch_policy_contract_hash,
    )
    _validate_builder_binding(plan)
    write_json_exclusive(work_root / "invocation-plan.json", plan.manifest())
    write_json_exclusive(
        layout.request / "invocations" / f"{invocation_id}.json",
        plan.runtime_manifest(),
    )
    return plan


def load_invocation_plan(
    layout: RunLayout,
    role: InvocationRole,
    *,
    prompt: str,
) -> InvocationPlan:
    manifests = sorted((layout.request / "invocations").glob(f"{role.value}-*.json"))
    if len(manifests) != 1:
        raise IsolationError(f"expected exactly one planned {role.value} invocation")
    runtime = load_json_object(manifests[0])
    invocation_id = runtime.get("invocation_id")
    backend_value = runtime.get("backend")
    command_value = runtime.get("command")
    output_path_value = runtime.get("output_path")
    output_schema = runtime.get("output_schema")
    sealed_input_hash_value = runtime.get("sealed_input_hash")
    builder_context_hash_value = runtime.get("builder_context_hash")
    patch_policy_version_value = runtime.get("candidate_patch_policy_version")
    patch_policy_contract_hash_value = runtime.get("candidate_patch_policy_contract_hash")
    if (
        not isinstance(invocation_id, str)
        or not isinstance(backend_value, str)
        or not isinstance(command_value, list)
        or not all(isinstance(item, str) for item in command_value)
        or not isinstance(output_path_value, str)
        or not isinstance(output_schema, str)
        or (sealed_input_hash_value is not None and not isinstance(sealed_input_hash_value, str))
        or (
            builder_context_hash_value is not None
            and not isinstance(builder_context_hash_value, str)
        )
        or (
            patch_policy_version_value is not None
            and not isinstance(patch_policy_version_value, str)
        )
        or (
            patch_policy_contract_hash_value is not None
            and not isinstance(patch_policy_contract_hash_value, str)
        )
        or ((patch_policy_version_value is None) != (patch_policy_contract_hash_value is None))
    ):
        raise IsolationError("persisted invocation plan is malformed")
    if runtime.get("prompt_hash") != sha256_bytes(prompt.encode("utf-8")):
        raise IsolationError("persisted invocation prompt hash mismatch")
    command = tuple(item for item in command_value if isinstance(item, str))
    if runtime.get("command_contract_hash") != hash_json(list(command)):
        raise IsolationError("persisted invocation command hash mismatch")
    if CODEX_MODEL not in command or f'model_reasoning_effort="{CODEX_REASONING}"' not in command:
        raise IsolationError("persisted invocation changed the fixed model contract")
    expected_permission_profile = (
        'default_permissions="research_commander"'
        if role is InvocationRole.COMMANDER
        else 'default_permissions="research_builder"'
    )
    if expected_permission_profile not in command or "--sandbox" in command:
        raise IsolationError("persisted invocation changed the fixed permission profile")
    if (
        backend_value == BackendKind.NATIVE_WINDOWS.value
        and 'windows.sandbox="elevated"' not in command
    ):
        raise IsolationError("native Windows invocation requires the elevated read jail")
    if "resume" in command or "--ephemeral" not in command or "--ignore-user-config" not in command:
        raise IsolationError("persisted invocation violates fresh-session isolation")
    work_root = layout.work / role.value / invocation_id
    expected_output = work_root / "model-output.json"
    output_path = Path(output_path_value)
    if output_path.resolve() != expected_output.resolve():
        raise IsolationError("persisted invocation output escapes its work directory")
    request = load_json_object(layout.request / "research_request.json")
    expected_schema = (
        decision_schema_name(request)
        if role is InvocationRole.COMMANDER
        else "CandidateBuildResultV1"
    )
    if output_schema != expected_schema:
        raise IsolationError("persisted invocation changed its output schema")
    try:
        backend_kind = BackendKind(backend_value)
    except ValueError as exc:
        raise IsolationError("persisted invocation has an unknown jail backend") from exc
    plan = InvocationPlan(
        invocation_id=invocation_id,
        role=role,
        backend=backend_kind,
        run_root=layout.root,
        work_root=work_root,
        command=command,
        prompt=prompt,
        output_path=output_path,
        output_schema=output_schema,
        sealed_input_hash=sealed_input_hash_value,
        builder_context_hash=builder_context_hash_value,
        candidate_patch_policy_version=patch_policy_version_value,
        candidate_patch_policy_contract_hash=patch_policy_contract_hash_value,
    )
    _validate_builder_binding(plan)
    return plan


def execute_invocation(
    plan: InvocationPlan,
    request: JsonObject,
    *,
    timeout_seconds: float = 3600,
    run_process: RunProcess | None = None,
) -> JsonObject:
    """Execute exactly once. Output streams are discarded to prevent secret/model-content logs."""
    started_marker = plan.work_root / "execution-started.json"
    completed_marker = plan.work_root / "execution-completed.json"
    if started_marker.exists() or completed_marker.exists():
        raise IsolationError("an invocation cannot be resumed or retried")
    _verify_sealed_inputs(plan)
    native_acl_staged = False
    native_acl_source: dict[str, str] | None = None
    if plan.backend is BackendKind.NATIVE_WINDOWS and run_process is None:
        native_acl_source = dict(os.environ)
        process_environment = native_invocation_environment(plan, native_acl_source)
        preflight_source = {**native_acl_source, **process_environment}
        _quarantine_unstarted_native_preflight(plan, native_acl_source)
        stage_native_invocation_acl(
            plan,
            source_environment=native_acl_source,
        )
        native_acl_staged = True
        try:
            verify_native_read_jail(
                plan,
                preflight_source,
                # The child receives only the scrubbed environment; ACL setup
                # retains host identity variables solely in this host process.
                acl_process=None,
                acl_already_staged=True,
            )
        except BaseException:
            revoke_native_invocation_acl(
                plan,
                child_exit_confirmed=True,
                source_environment=native_acl_source,
            )
            raise
    else:
        process_environment = scrubbed_environment()
    write_json_exclusive(
        started_marker,
        {
            "schema_version": "InvocationStartedV1",
            "invocation_id": plan.invocation_id,
            "fresh_process": True,
        },
    )
    started = time.monotonic()
    runner = run_process or cast(RunProcess, subprocess.run)
    try:
        completed = runner(
            plan.command,
            cwd=plan.work_root,
            env=process_environment,
            input=plan.prompt,
            text=True,
            encoding="utf-8",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        if native_acl_staged:
            if native_acl_source is None:
                raise IsolationError("native ACL source environment was lost") from None
            if plan.role is InvocationRole.BUILDER:
                recover_native_builder_candidate_acl(
                    plan,
                    child_exit_confirmed=True,
                    source_environment=native_acl_source,
                )
            else:
                revoke_native_invocation_acl(
                    plan,
                    child_exit_confirmed=True,
                    source_environment=native_acl_source,
                )
        raise ContractError(f"isolated Codex process launch failed: {type(exc).__name__}") from None
    duration_ms = int((time.monotonic() - started) * 1000)
    if native_acl_staged:
        if native_acl_source is None:
            raise IsolationError("native ACL source environment was lost")
        if plan.role is InvocationRole.BUILDER:
            recover_native_builder_candidate_acl(
                plan,
                child_exit_confirmed=True,
                source_environment=native_acl_source,
            )
        else:
            revoke_native_invocation_acl(
                plan,
                child_exit_confirmed=True,
                source_environment=native_acl_source,
            )
    if completed.returncode != 0:
        raise ContractError(
            "isolated Codex process failed: "
            + _classify_codex_failure(
                "\n".join((completed.stdout or "", completed.stderr or "")),
                completed.returncode,
            )
        )
    if (
        plan.backend is BackendKind.NATIVE_WINDOWS
        and plan.role is InvocationRole.BUILDER
        and not native_acl_staged
    ):
        recover_native_builder_candidate_acl(
            plan,
            child_exit_confirmed=True,
        )
    _verify_sealed_inputs(plan)
    output = _load_and_validate_invocation_output(plan, request)
    write_json_exclusive(
        completed_marker,
        {
            "schema_version": "InvocationCompletedV1",
            "invocation_id": plan.invocation_id,
            "exit_code": completed.returncode,
            "duration_ms": duration_ms,
            "output_hash": hash_json(output),
            "stdout_logged": False,
            "stderr_logged": False,
        },
    )
    return output


def _load_and_validate_invocation_output(
    plan: InvocationPlan,
    request: JsonObject,
) -> JsonObject:
    if plan.output_path.is_symlink():
        raise IsolationError("schema-bound output must be a regular file, not a symlink")
    if not plan.output_path.is_file():
        raise ContractError("Codex did not create its schema-bound output")
    output = load_json_object(plan.output_path)
    if plan.role is InvocationRole.COMMANDER:
        output = finalize_commander_output(output, request)
    else:
        validate_document(output, plan.output_schema)
        verify_output_binding(output, request)
        _validate_builder_output(plan, output)
    return output


def load_validated_builder_result(
    plan: InvocationPlan,
    request: JsonObject,
) -> JsonObject:
    """Revalidate one completed Builder output against host-owned artifacts."""
    if plan.role is not InvocationRole.BUILDER:
        raise IsolationError("candidate validation requires a Builder invocation")
    completed_path = plan.work_root / "execution-completed.json"
    if completed_path.is_symlink() or not completed_path.is_file():
        raise IsolationError("Builder invocation has no immutable completion marker")
    completed = load_json_object(completed_path)
    if (
        completed.get("schema_version") != "InvocationCompletedV1"
        or completed.get("invocation_id") != plan.invocation_id
    ):
        raise IsolationError("Builder completion marker does not match the invocation")
    if plan.backend is BackendKind.NATIVE_WINDOWS:
        recover_native_builder_candidate_acl(
            plan,
            child_exit_confirmed=True,
        )
    _verify_sealed_inputs(plan)
    output = _load_and_validate_invocation_output(plan, request)
    output_hash = hash_json(output)
    if completed.get("output_hash") != output_hash:
        raise IsolationError("Builder completion marker output hash mismatch")
    published_path = plan.run_root / "output" / "candidate_build_result.json"
    if published_path.is_symlink() or not published_path.is_file():
        raise IsolationError("host-published Builder result is missing or unsafe")
    published = load_json_object(published_path)
    if hash_json(published) != output_hash:
        raise IsolationError("host-published Builder result differs from model output")
    return published


def _invocation_artifact_hashes(
    plan: InvocationPlan,
) -> tuple[str, str | None]:
    if plan.output_path.is_symlink():
        raise IsolationError("schema-bound output must be a regular file, not a symlink")
    if not plan.output_path.is_file():
        raise ContractError("Codex did not create its schema-bound output")
    candidate_tree_hash: str | None = None
    if plan.role is InvocationRole.BUILDER:
        candidate_root = plan.work_root / "candidate_worktree"
        if candidate_root.is_symlink() or not candidate_root.is_dir():
            raise IsolationError("Builder candidate worktree is missing or is a symlink")
        candidate_tree_hash = hash_tree(candidate_root)
    return hash_file(plan.output_path), candidate_tree_hash


def _published_output_path(plan: InvocationPlan) -> Path:
    filename = (
        "research_decision.json"
        if plan.role is InvocationRole.COMMANDER
        else "candidate_build_result.json"
    )
    return plan.run_root / "output" / filename


def _publish_validated_output(plan: InvocationPlan, output: JsonObject) -> Path:
    destination = _published_output_path(plan)
    if destination.is_symlink():
        raise IsolationError("published invocation output must not be a symlink")
    if destination.exists():
        if not destination.is_file():
            raise IsolationError("published invocation output is not a regular file")
        existing = load_json_object(destination)
        if hash_json(existing) != hash_json(output):
            raise IsolationError(
                "published invocation output conflicts with the recovered model output"
            )
        return destination
    write_json_exclusive(destination, output)
    return destination


def adopt_invocation_output(
    plan: InvocationPlan,
    request: JsonObject,
    *,
    child_exit_confirmed: bool,
    stability_seconds: float = ADOPTION_STABILITY_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> JsonObject:
    """Validate and publish an orphaned output without launching or resuming Codex.

    This recovery path is intentionally manual. The existing one-shot marker does
    not contain a process handle, so the caller must first confirm through the
    external supervisor that the exact child process has exited.
    """
    if not child_exit_confirmed:
        raise IsolationError(
            "host-only adoption requires external confirmation that the exact child process exited"
        )
    if stability_seconds <= 0:
        raise ContractError("adoption stability window must be positive")
    started_marker = plan.work_root / "execution-started.json"
    completed_marker = plan.work_root / "execution-completed.json"
    if started_marker.is_symlink() or not started_marker.is_file():
        raise IsolationError("host-only adoption requires an execution-started marker")
    if completed_marker.exists():
        raise IsolationError("invocation already has an execution-completed marker")
    started = load_json_object(started_marker)
    if (
        started.get("schema_version") != "InvocationStartedV1"
        or started.get("invocation_id") != plan.invocation_id
        or started.get("fresh_process") is not True
    ):
        raise IsolationError("execution-started marker does not match the invocation")

    if plan.backend is BackendKind.NATIVE_WINDOWS:
        if plan.role is InvocationRole.BUILDER:
            recover_native_builder_candidate_acl(
                plan,
                child_exit_confirmed=child_exit_confirmed,
            )
        else:
            revoke_native_invocation_acl(
                plan,
                child_exit_confirmed=child_exit_confirmed,
            )
    _verify_sealed_inputs(plan)
    initial_hashes = _invocation_artifact_hashes(plan)
    sleep(stability_seconds)
    stable_hashes = _invocation_artifact_hashes(plan)
    if stable_hashes != initial_hashes:
        raise IsolationError("orphaned invocation artifacts changed during stability check")
    if completed_marker.exists():
        raise IsolationError("invocation completed while host-only adoption was checking it")

    output = _load_and_validate_invocation_output(plan, request)
    _verify_sealed_inputs(plan)
    final_hashes = _invocation_artifact_hashes(plan)
    if final_hashes != stable_hashes:
        raise IsolationError("orphaned invocation artifacts changed during validation")
    if completed_marker.exists():
        raise IsolationError("invocation completed while host-only adoption was validating it")

    destination = _publish_validated_output(plan, output)
    write_json_exclusive(
        completed_marker,
        {
            "schema_version": "InvocationCompletedV1",
            "invocation_id": plan.invocation_id,
            "completion_mode": "HOST_ADOPTED_AFTER_SUPERVISOR_TIMEOUT",
            "exit_code": None,
            "duration_ms": None,
            "output_hash": hash_json(output),
            "raw_output_file_hash": final_hashes[0],
            "candidate_tree_hash": final_hashes[1],
            "published_output": destination.relative_to(plan.run_root).as_posix(),
            "child_exit_confirmed": True,
            "stability_window_ms": max(1, round(stability_seconds * 1000)),
            "stdout_logged": False,
            "stderr_logged": False,
        },
    )
    return output


def _declared_candidate_paths(value: object, field: str) -> set[str]:
    if not isinstance(value, list):
        raise ContractError(f"Builder {field} is malformed")
    items = cast(list[object], value)
    if not all(isinstance(item, str) for item in items):
        raise ContractError(f"Builder {field} is malformed")
    string_items = cast(list[str], items)
    normalized: set[str] = set()
    for item in string_items:
        path = item.replace("\\", "/")
        if path.startswith("candidate_worktree/"):
            path = path.removeprefix("candidate_worktree/")
        normalized.add(path)
    return normalized


def _validate_builder_output(plan: InvocationPlan, output: JsonObject) -> None:
    _validate_builder_binding(plan)
    proposal = load_json_object(plan.run_root / "request" / APPROVED_PROPOSAL_FILENAME)
    binding = load_json_object(plan.run_root / "request" / BUILDER_BINDING_FILENAME)
    base_root = plan.run_root / "input" / "clean_source_snapshot"
    candidate_root = plan.work_root / "candidate_worktree"
    patch = deterministic_patch(base_root, candidate_root)
    validation = validate_candidate_patch(
        patch,
        proposal,
        policy_version=plan.candidate_patch_policy_version,
    )
    actual_paths = set(validation.changed_paths)
    declared_paths = _declared_candidate_paths(
        output.get("files_changed"),
        "files_changed",
    )
    if declared_paths != actual_paths:
        raise ContractError("Builder files_changed does not match the immutable candidate diff")
    declared_tests = _declared_candidate_paths(
        output.get("tests_added"),
        "tests_added",
    )
    if declared_tests != set(validation.test_paths):
        raise ContractError("Builder tests_added does not match the immutable candidate diff")
    supplied_proposal_hash = binding.get("proposal_hash")
    if (
        not isinstance(supplied_proposal_hash, str)
        or output.get("proposal_hash") != supplied_proposal_hash
    ):
        raise ContractError("Builder output did not echo the supplied proposal_hash")


def _classify_codex_failure(stderr: str | None, return_code: int) -> str:
    normalized = (stderr or "").casefold()
    schema_keyword_categories = (
        ("uniqueitems", "SCHEMA_REJECTED_UNIQUE_ITEMS"),
        ("minproperties", "SCHEMA_REJECTED_MIN_PROPERTIES"),
        ("maxlength", "SCHEMA_REJECTED_MAX_LENGTH"),
        ("minlength", "SCHEMA_REJECTED_MIN_LENGTH"),
        ("maxitems", "SCHEMA_REJECTED_MAX_ITEMS"),
        ("minitems", "SCHEMA_REJECTED_MIN_ITEMS"),
        ("multipleof", "SCHEMA_REJECTED_MULTIPLE_OF"),
        ("patternproperties", "SCHEMA_REJECTED_PATTERN_PROPERTIES"),
        ("additionalproperties", "SCHEMA_REJECTED_ADDITIONAL_PROPERTIES"),
        ("'format' is not permitted", "SCHEMA_REJECTED_FORMAT"),
        ('"format" is not permitted', "SCHEMA_REJECTED_FORMAT"),
        ("'pattern' is not permitted", "SCHEMA_REJECTED_PATTERN"),
        ('"pattern" is not permitted', "SCHEMA_REJECTED_PATTERN"),
        ("'minimum' is not permitted", "SCHEMA_REJECTED_MINIMUM"),
        ('"minimum" is not permitted', "SCHEMA_REJECTED_MINIMUM"),
        ("'maximum' is not permitted", "SCHEMA_REJECTED_MAXIMUM"),
        ('"maximum" is not permitted', "SCHEMA_REJECTED_MAXIMUM"),
        ("too many object properties", "SCHEMA_REJECTED_PROPERTY_LIMIT"),
        ("nesting depth", "SCHEMA_REJECTED_NESTING_LIMIT"),
        ("too large", "SCHEMA_REJECTED_SIZE_LIMIT"),
    )
    if "invalid_json_schema" in normalized or "invalid json schema" in normalized:
        for keyword, category in schema_keyword_categories:
            if keyword in normalized:
                return category
        return "SCHEMA_REJECTED"
    categories = (
        (
            (
                "strict config",
                "unknown config",
                "unrecognized",
                "not recognized",
                "feature is removed",
            ),
            "CONFIG_REJECTED",
        ),
        (("authentication", "login", "unauthorized"), "AUTH_UNAVAILABLE"),
        (("sandbox", "access is denied", "permission denied"), "SANDBOX_UNAVAILABLE"),
        (
            ("model not found", "unknown model", "does not have access to model"),
            "MODEL_UNAVAILABLE",
        ),
        (
            (
                "output schema",
                "json schema",
                "json_schema",
                "schema is invalid",
                "invalid schema",
            ),
            "SCHEMA_REJECTED",
        ),
        (("network", "connection", "timed out"), "SERVICE_UNAVAILABLE"),
    )
    for needles, category in categories:
        if any(needle in normalized for needle in needles):
            return category
    return f"PROCESS_EXIT_{return_code}"
