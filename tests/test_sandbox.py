from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from conftest import Bundle

from research_commander.assets import asset_text
from research_commander.binding import contract_hash
from research_commander.canonical import canonical_json_bytes, hash_file, hash_json
from research_commander.errors import ContractError, IsolationError
from research_commander.io import load_json_object, write_json_exclusive
from research_commander.json_types import JsonObject, JsonValue
from research_commander.layout import RunLayout
from research_commander.sandbox import (
    CODEX_MODEL,
    CODEX_REASONING,
    CODEX_RUNNER_HOME_ENV,
    DockerBackend,
    ExplicitJailBackend,
    InvocationPlan,
    InvocationRole,
    NativeWindowsSandboxBackend,
    _remove_native_probe_with_retry,  # pyright: ignore[reportPrivateUsage]
    adopt_invocation_output,
    execute_invocation,
    load_invocation_plan,
    native_invocation_environment,
    prepare_invocation,
    recover_native_builder_candidate_acl,
    revoke_native_invocation_acl,
    scrubbed_environment,
    stage_native_invocation_acl,
    verify_native_read_jail,
)


def _decision(request: JsonObject) -> JsonObject:
    decision: JsonObject = {
        "schema_version": "research_decision_v1",
        "request_id": request["request_id"],
        "research_cycle_id": request["research_cycle_id"],
        "selected_commander": request["selected_commander"],
        "commander_selection_id": request["commander_selection_id"],
        "commander_selection_version": request["commander_selection_version"],
        "source_snapshot_commit": request["source_snapshot_commit"],
        "champion_version": request["champion_version"],
        "experiment_family": request["experiment_family"],
        "context_manifest_hash": request["context_manifest_hash"],
        "request_schema_version": request["schema_version"],
        "request_expires_at": request["expires_at"],
        "decision": "NO_RESEARCH_CHANGE",
        "rationale": "No bounded change is justified by current evidence.",
        "proposal": None,
        "requested_evidence": [],
        "created_at": request["created_at"],
        "output_hash": "0" * 64,
    }
    decision["output_hash"] = contract_hash(
        decision,
        exclude=frozenset({"output_hash"}),
        timestamp_fields=("request_expires_at", "created_at"),
    )
    return decision


def _mark_started(plan: InvocationPlan) -> None:
    write_json_exclusive(
        plan.work_root / "execution-started.json",
        {
            "schema_version": "InvocationStartedV1",
            "invocation_id": plan.invocation_id,
            "fresh_process": True,
        },
    )


def _synthetic_native_acl_environment(tmp_path: Path) -> dict[str, str]:
    system_root = tmp_path / "synthetic-windows"
    system32 = system_root / "System32"
    system32.mkdir(parents=True, exist_ok=True)
    (system32 / "icacls.exe").write_bytes(b"synthetic executable fixture")
    powershell = system32 / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    powershell.parent.mkdir(parents=True)
    powershell.write_bytes(b"synthetic executable fixture")
    return {
        "SYSTEMROOT": str(system_root),
        "LOCALAPPDATA": str(tmp_path / "local-app-data"),
        "COMPUTERNAME": "SYNTHETIC_HOST",
        "USERDOMAIN": "SYNTHETIC_HOST",
        "USERNAME": "synthetic-user",
        "PATH": "untrusted-path",
        "OPENAI_API_KEY": "must-not-be-forwarded",
    }


def _synthetic_native_identity() -> dict[str, str]:
    return {
        "COMPUTERNAME": "SYNTHETIC_HOST",
        "USERDOMAIN": "SYNTHETIC_HOST",
        "USERNAME": "synthetic-user",
    }


def _write_synthetic_acl_snapshot(target: Path, root: Path) -> None:
    entries = [root, *sorted(root.rglob("*"))]
    lines: list[str] = []
    for path in entries:
        relative = path.relative_to(root.parent)
        lines.extend(
            (
                str(relative),
                "D:AI(A;ID;FA;;;SY)(A;ID;FA;;;BA)",
            )
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(("\r\n".join(lines) + "\r\n").encode("utf-16"))


def _prepare_synthetic_native_runtime(plan: InvocationPlan) -> None:
    (plan.work_root / ".runtime" / "tmp").mkdir(parents=True)


def _builder_result(
    request: JsonObject,
    *,
    proposal_hash: str,
    files_changed: list[str],
    tests_added: list[str],
) -> JsonObject:
    return {
        "schema_version": "CandidateBuildResultV1",
        "request_id": request["request_id"],
        "research_cycle_id": request["research_cycle_id"],
        "context_manifest_hash": request["context_manifest_hash"],
        "source_snapshot_commit": request["source_snapshot_commit"],
        "champion_version": request["champion_version"],
        "experiment_family": request["experiment_family"],
        "selected_commander": request["selected_commander"],
        "commander_selection_id": request["commander_selection_id"],
        "commander_selection_version": request["commander_selection_version"],
        "builder_model": CODEX_MODEL,
        "builder_reasoning": CODEX_REASONING,
        "proposal_hash": proposal_hash,
        "declared_entrypoint": "trading.strategies.alpha_v2.model:decide",
        "implementation_summary": "Added a bounded synthetic strategy and its unit test.",
        "files_changed": cast(JsonValue, files_changed),
        "tests_added": cast(JsonValue, tests_added),
        "promotion_decision": "NOT_PERMITTED",
    }


def test_docker_plan_is_ephemeral_and_mounts_only_current_run(
    prepared_run: RunLayout,
) -> None:
    plan = prepare_invocation(
        prepared_run,
        InvocationRole.COMMANDER,
        DockerBackend("runner:test", "codex-egress"),
        prompt="bounded commander prompt",
    )
    assert CODEX_MODEL in plan.command
    assert f'model_reasoning_effort="{CODEX_REASONING}"' in plan.command
    assert "--ephemeral" in plan.command
    assert "--ignore-user-config" in plan.command
    assert "--ignore-rules" in plan.command
    assert "resume" not in plan.command
    mounts = [item for item in plan.command if item.startswith("type=bind,src=")]
    assert mounts
    assert all(
        str(prepared_run.root.resolve()) in item
        and f"src={prepared_run.root.parent.resolve()},dst=" not in item
        for item in mounts
    )
    command_text = "\n".join(plan.command)
    assert str(prepared_run.request) in command_text
    assert str(prepared_run.input) in command_text
    assert "--read-only" in plan.command
    assert "--cap-drop=ALL" in plan.command


def test_same_role_cannot_be_invoked_twice(prepared_run: RunLayout) -> None:
    backend = DockerBackend("runner:test", "codex-egress")
    prepare_invocation(
        prepared_run,
        InvocationRole.COMMANDER,
        backend,
        prompt="first process",
    )
    with pytest.raises(IsolationError, match="already exists"):
        prepare_invocation(
            prepared_run,
            InvocationRole.COMMANDER,
            backend,
            prompt="second process",
        )


def test_saved_plan_can_be_loaded_without_creating_another_invocation(
    prepared_run: RunLayout,
) -> None:
    prompt = "fixed commander prompt"
    original = prepare_invocation(
        prepared_run,
        InvocationRole.COMMANDER,
        DockerBackend("runner:test", "codex-egress"),
        prompt=prompt,
    )
    loaded = load_invocation_plan(
        prepared_run,
        InvocationRole.COMMANDER,
        prompt=prompt,
    )
    assert loaded.invocation_id == original.invocation_id
    assert loaded.command == original.command
    assert loaded.output_path == original.output_path


def test_commander_and_builder_have_separate_workdirs(
    prepared_run: RunLayout,
    proposal: JsonObject,
) -> None:
    backend = DockerBackend("runner:test", "codex-egress")
    commander = prepare_invocation(
        prepared_run,
        InvocationRole.COMMANDER,
        backend,
        prompt="commander",
    )
    builder = prepare_invocation(
        prepared_run,
        InvocationRole.BUILDER,
        backend,
        prompt="builder",
        approved_proposal=proposal,
    )
    assert commander.invocation_id != builder.invocation_id
    assert commander.work_root != builder.work_root
    assert not (commander.work_root / "candidate_worktree").exists()
    assert (builder.work_root / "candidate_worktree").is_dir()


def test_builder_uses_host_supplied_canonical_proposal_hash(
    prepared_run: RunLayout,
    proposal: JsonObject,
) -> None:
    prompt = asset_text("prompts/builder.prompt.md")
    plan = prepare_invocation(
        prepared_run,
        InvocationRole.BUILDER,
        DockerBackend("runner:test", "codex-egress"),
        prompt=prompt,
        approved_proposal=proposal,
    )
    proposal_path = prepared_run.request / "approved_algorithm_proposal.json"
    binding = load_json_object(prepared_run.request / "builder_binding.json")
    canonical_proposal_hash = proposal["proposal_hash"]
    assert isinstance(canonical_proposal_hash, str)
    raw_proposal_file_hash = hash_file(proposal_path)

    assert raw_proposal_file_hash != canonical_proposal_hash
    assert binding["proposal_hash"] == canonical_proposal_hash
    assert binding["proposal_file_sha256"] == raw_proposal_file_hash
    assert binding["builder_context_hash"] == plan.builder_context_hash
    bound_request = binding.get("request_binding")
    assert isinstance(bound_request, dict)
    assert (
        bound_request["context_manifest_hash"]
        == load_json_object(prepared_run.request / "research_request.json")["context_manifest_hash"]
    )
    assert "exact `proposal_hash` supplied" in prompt
    assert "Never recompute it" in prompt


def test_commander_prompt_matches_fail_closed_universe_contract() -> None:
    prompt = asset_text("prompts/commander.prompt.md")

    assert "`proposal.target_universe`" in prompt
    assert "`point_in_time_membership_available=true`" in prompt
    assert "`REQUEST_MORE_EVIDENCE`" in prompt
    assert "not limited to any one sector" in prompt
    assert "`proposed_strategy_version` to a value different from" in prompt


def test_load_and_adoption_reject_tampered_builder_binding(
    prepared_run: RunLayout,
    bundle: Bundle,
    proposal: JsonObject,
) -> None:
    prompt = asset_text("prompts/builder.prompt.md")
    plan = prepare_invocation(
        prepared_run,
        InvocationRole.BUILDER,
        DockerBackend("runner:test", "codex-egress"),
        prompt=prompt,
        approved_proposal=proposal,
    )
    binding_path = prepared_run.request / "builder_binding.json"
    binding = load_json_object(binding_path)
    binding["proposal_hash"] = hash_file(prepared_run.request / "approved_algorithm_proposal.json")
    binding_path.write_bytes(canonical_json_bytes(binding) + b"\n")

    with pytest.raises(IsolationError, match="binding input"):
        load_invocation_plan(
            prepared_run,
            InvocationRole.BUILDER,
            prompt=prompt,
        )

    _mark_started(plan)
    write_json_exclusive(plan.output_path, {"malformed": True})
    with pytest.raises(IsolationError, match="binding input"):
        adopt_invocation_output(
            plan,
            bundle.request,
            child_exit_confirmed=True,
            stability_seconds=0.25,
            sleep=lambda _seconds: None,
        )
    assert not (plan.work_root / "execution-completed.json").exists()


def test_native_builder_exposes_only_sealed_inputs_and_candidate_as_writable(
    prepared_run: RunLayout,
    proposal: JsonObject,
) -> None:
    plan = prepare_invocation(
        prepared_run,
        InvocationRole.BUILDER,
        NativeWindowsSandboxBackend(
            executable=sys.executable,
            platform_name="nt",
        ),
        prompt="builder",
        approved_proposal=proposal,
    )
    assert 'default_permissions="research_builder"' in plan.command
    assert 'windows.sandbox="elevated"' in plan.command
    assert "candidate_worktree" in "\n".join(plan.command)
    assert "--sandbox" not in plan.command
    assert "--add-dir" not in plan.command
    cd_index = plan.command.index("--cd")
    assert plan.command[cd_index + 1] == "."
    schema_index = plan.command.index("--output-schema")
    assert plan.command[schema_index + 1].startswith(".research")
    output_index = plan.command.index("--output-last-message")
    assert plan.command[output_index + 1] == "model-output.json"
    assert plan.builder_context_hash is not None
    host_binding = load_json_object(prepared_run.request / "builder_binding.json")
    staged_binding = load_json_object(plan.work_root / ".research/request/builder_binding.json")
    assert staged_binding == host_binding


def test_direct_host_fallback_does_not_exist_and_jail_is_explicit(
    prepared_run: RunLayout,
) -> None:
    with pytest.raises(IsolationError, match="policy_id"):
        prepare_invocation(
            prepared_run,
            InvocationRole.COMMANDER,
            ExplicitJailBackend(("jail-adapter",), ""),
            prompt="commander",
        )
    with pytest.raises(IsolationError, match="egress network"):
        prepare_invocation(
            prepared_run,
            InvocationRole.COMMANDER,
            DockerBackend("runner:test", "bridge"),
            prompt="commander",
        )


def test_native_windows_plan_stages_only_current_run_and_seals_inputs(
    prepared_run: RunLayout,
) -> None:
    plan = prepare_invocation(
        prepared_run,
        InvocationRole.COMMANDER,
        NativeWindowsSandboxBackend(
            executable=sys.executable,
            platform_name="nt",
        ),
        prompt="commander",
    )
    assert plan.backend.value == "native_windows"
    assert plan.command[0] == sys.executable
    assert 'default_permissions="research_commander"' in plan.command
    assert 'windows.sandbox="elevated"' in plan.command
    assert "--sandbox" not in plan.command
    assert 'shell_environment_policy.inherit="core"' in plan.command
    assert "--disable" in plan.command
    assert "memories" in plan.command
    assert plan.sealed_input_hash is not None
    assert (plan.work_root / ".research" / "request").is_dir()
    assert (plan.work_root / ".research" / "input").is_dir()
    assert str(prepared_run.root.parent.resolve()) not in "\n".join(plan.command)


def test_native_windows_execution_rejects_mutated_sealed_inputs(
    prepared_run: RunLayout,
    bundle: Bundle,
) -> None:
    plan = prepare_invocation(
        prepared_run,
        InvocationRole.COMMANDER,
        NativeWindowsSandboxBackend(
            executable=sys.executable,
            platform_name="nt",
        ),
        prompt="commander",
    )
    write_json_exclusive(
        plan.work_root / ".research" / "request" / "mutation.json",
        {"unexpected": True},
    )

    def should_not_run(
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
    ) -> subprocess.CompletedProcess[str]:
        del command, cwd, env, input, text, encoding, stdout, stderr, check, timeout
        raise AssertionError("mutated sealed inputs must stop before execution")

    with pytest.raises(IsolationError, match="sealed current-run inputs changed"):
        execute_invocation(
            plan,
            bundle.request,
            run_process=should_not_run,
        )


def test_environment_scrub_removes_credentials_and_history_paths() -> None:
    environment = scrubbed_environment(
        {
            "PATH": "bin",
            "USERPROFILE": "runtime-home",
            "APPDATA": "runtime-appdata",
            "LOCALAPPDATA": "runtime-localappdata",
            "OPENAI_API_KEY": "not-forwarded",
            "APCA_API_SECRET_KEY": "not-forwarded",
            "CODEX_HOME": "persistent-history",
            "HOME": "global-home",
            "COOKIE": "browser-state",
        }
    )
    assert environment["PATH"] == "bin"
    assert environment["USERPROFILE"] == "runtime-home"
    assert environment["PYTHONUTF8"] == "1"
    assert "OPENAI_API_KEY" not in environment
    assert "APCA_API_SECRET_KEY" not in environment
    assert "CODEX_HOME" not in environment
    assert "HOME" not in environment
    assert "COOKIE" not in environment


def test_native_runner_home_must_be_keyring_only_and_context_free(
    prepared_run: RunLayout,
    tmp_path: Path,
) -> None:
    plan = prepare_invocation(
        prepared_run,
        InvocationRole.COMMANDER,
        NativeWindowsSandboxBackend(
            executable=sys.executable,
            platform_name="nt",
        ),
        prompt="commander",
    )
    runner_home = tmp_path / "private-runner-home"
    runner_home.mkdir()
    environment = native_invocation_environment(
        plan,
        {
            **_synthetic_native_identity(),
            CODEX_RUNNER_HOME_ENV: str(runner_home),
            "SYSTEMROOT": str(tmp_path / "system"),
            "PATH": "untrusted-global-path",
        },
    )
    assert environment["CODEX_HOME"] == str(runner_home.resolve())
    assert environment["CODEX_SQLITE_HOME"].startswith(str(plan.work_root))
    assert "untrusted-global-path" not in environment["PATH"]


def test_native_prestart_runtime_retry_quarantines_only_host_owned_residue(
    prepared_run: RunLayout,
    tmp_path: Path,
) -> None:
    plan = prepare_invocation(
        prepared_run,
        InvocationRole.COMMANDER,
        NativeWindowsSandboxBackend(
            executable=sys.executable,
            platform_name="nt",
        ),
        prompt="commander",
    )
    runner_home = tmp_path / "private-runner-home"
    runner_home.mkdir()
    local_app_data = tmp_path / "local-app-data"
    source = {
        **_synthetic_native_identity(),
        CODEX_RUNNER_HOME_ENV: str(runner_home),
        "LOCALAPPDATA": str(local_app_data),
        "SYSTEMROOT": str(tmp_path / "system"),
    }
    first = native_invocation_environment(plan, source)
    first_runtime = plan.work_root / ".runtime"
    first_hash = hash_json(load_json_object(first_runtime / "host-runtime-owner.json"))

    second = native_invocation_environment(plan, source)

    assert first["CODEX_SQLITE_HOME"] == second["CODEX_SQLITE_HOME"]
    assert first_runtime.is_dir()
    assert hash_json(load_json_object(first_runtime / "host-runtime-owner.json")) == first_hash
    quarantined = list(
        (local_app_data / "AdaptiveLlmQuant/quarantine").glob(f"runtime-{plan.invocation_id}-*")
    )
    assert len(quarantined) == 1
    record = load_json_object(quarantined[0] / "quarantine-record.json")
    assert record["host_owned_marker_valid"] is True
    assert record["execution_started"] is False


def test_native_prestart_runtime_retry_refuses_started_invocation(
    prepared_run: RunLayout,
    tmp_path: Path,
) -> None:
    plan = prepare_invocation(
        prepared_run,
        InvocationRole.COMMANDER,
        NativeWindowsSandboxBackend(
            executable=sys.executable,
            platform_name="nt",
        ),
        prompt="commander",
    )
    runner_home = tmp_path / "private-runner-home"
    runner_home.mkdir()
    source = {
        **_synthetic_native_identity(),
        CODEX_RUNNER_HOME_ENV: str(runner_home),
        "LOCALAPPDATA": str(tmp_path / "local-app-data"),
        "SYSTEMROOT": str(tmp_path / "system"),
    }
    native_invocation_environment(plan, source)
    _mark_started(plan)

    with pytest.raises(IsolationError, match="started invocation runtime"):
        native_invocation_environment(plan, source)
    assert (plan.work_root / ".runtime").is_dir()


def test_native_runner_home_quarantines_only_bundled_system_skills(
    prepared_run: RunLayout,
    tmp_path: Path,
) -> None:
    plan = prepare_invocation(
        prepared_run,
        InvocationRole.COMMANDER,
        NativeWindowsSandboxBackend(
            executable=sys.executable,
            platform_name="nt",
        ),
        prompt="commander",
    )
    runner_home = tmp_path / "private-runner-home"
    system_skills = runner_home / "skills/.system"
    system_skills.mkdir(parents=True)
    (system_skills / ".codex-system-skills.marker").write_text(
        "synthetic bundled marker\n",
        encoding="utf-8",
    )
    (system_skills / "bundled-skill").mkdir()
    (system_skills / "bundled-skill/SKILL.md").write_text(
        "# Synthetic bundled skill\n",
        encoding="utf-8",
    )
    local_app_data = tmp_path / "local-app-data"

    environment = native_invocation_environment(
        plan,
        {
            **_synthetic_native_identity(),
            CODEX_RUNNER_HOME_ENV: str(runner_home),
            "LOCALAPPDATA": str(local_app_data),
            "SYSTEMROOT": str(tmp_path / "system"),
        },
    )

    assert environment["CODEX_HOME"] == str(runner_home.resolve())
    assert not (runner_home / "skills").exists()
    quarantine = local_app_data / "AdaptiveLlmQuant/quarantine"
    quarantined = list(quarantine.glob("system-skills-*"))
    assert len(quarantined) == 1
    record = load_json_object(quarantined[0] / "quarantine-record.json")
    assert record["bundled_system_only"] is True
    assert record["user_skills_present"] is False
    assert record["credentials_read"] is False


def test_native_runner_home_does_not_quarantine_user_skills(
    prepared_run: RunLayout,
    tmp_path: Path,
) -> None:
    plan = prepare_invocation(
        prepared_run,
        InvocationRole.COMMANDER,
        NativeWindowsSandboxBackend(
            executable=sys.executable,
            platform_name="nt",
        ),
        prompt="commander",
    )
    runner_home = tmp_path / "private-runner-home"
    system_skills = runner_home / "skills/.system"
    system_skills.mkdir(parents=True)
    (system_skills / ".codex-system-skills.marker").write_text(
        "synthetic bundled marker\n",
        encoding="utf-8",
    )
    (runner_home / "skills/user-skill").mkdir()

    with pytest.raises(IsolationError, match="user-installed skills"):
        native_invocation_environment(
            plan,
            {
                CODEX_RUNNER_HOME_ENV: str(runner_home),
                "LOCALAPPDATA": str(tmp_path / "local-app-data"),
                "SYSTEMROOT": str(tmp_path / "system"),
            },
        )
    assert (runner_home / "skills/user-skill").is_dir()
    assert not (tmp_path / "local-app-data/AdaptiveLlmQuant/quarantine").exists()


@pytest.mark.parametrize("forbidden_name", ["auth.json", "AGENTS.md", "skills"])
def test_native_runner_home_rejects_credentials_or_context(
    prepared_run: RunLayout,
    tmp_path: Path,
    forbidden_name: str,
) -> None:
    plan = prepare_invocation(
        prepared_run,
        InvocationRole.COMMANDER,
        NativeWindowsSandboxBackend(
            executable=sys.executable,
            platform_name="nt",
        ),
        prompt="commander",
    )
    runner_home = tmp_path / f"runner-{forbidden_name.replace('.', '-')}"
    runner_home.mkdir()
    forbidden = runner_home / forbidden_name
    if "." in forbidden_name:
        forbidden.write_text("not-a-secret-fixture", encoding="utf-8")
    else:
        forbidden.mkdir()
    with pytest.raises(
        IsolationError,
        match=r"context-bearing|OS keyring|user-installed skills",
    ):
        native_invocation_environment(
            plan,
            {
                CODEX_RUNNER_HOME_ENV: str(runner_home),
                "SYSTEMROOT": str(tmp_path / "system"),
            },
        )


def test_native_read_jail_preflight_rejects_visible_sibling(
    prepared_run: RunLayout,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plan = prepare_invocation(
        prepared_run,
        InvocationRole.COMMANDER,
        NativeWindowsSandboxBackend(
            executable=sys.executable,
            platform_name="nt",
        ),
        prompt="commander",
    )
    _prepare_synthetic_native_runtime(plan)
    environment = _synthetic_native_acl_environment(tmp_path)
    acl_calls: list[tuple[str, ...]] = []

    def fake_acl_process(
        command: tuple[str, ...],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        del kwargs
        acl_calls.append(command)
        if "/save" in command:
            target = Path(command[command.index("/save") + 1])
            _write_synthetic_acl_snapshot(target, plan.work_root)
        return subprocess.CompletedProcess(command, returncode=0, stdout="", stderr="")

    call_count = 0

    def fake_run(
        command: tuple[str, ...],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        nonlocal call_count
        del kwargs
        call_count += 1
        if call_count == 1:
            workspace = Path(command[command.index("--cd") + 1])
            (workspace / "inside.txt").write_text("OK\n", encoding="utf-8")
        return subprocess.CompletedProcess(command, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("research_commander.sandbox.subprocess.run", fake_run)
    with pytest.raises(IsolationError, match="sibling-read jail"):
        verify_native_read_jail(
            plan,
            environment,
            acl_process=fake_acl_process,
        )
    result = load_json_object(plan.work_root / "native-read-jail-preflight.json")
    assert result["invocation_id"] == plan.invocation_id
    assert result["write_inside_workspace"] is True
    assert result["read_outside_workspace_denied"] is False
    assert any("/grant" in command for command in acl_calls)
    assert any("/reset" in command for command in acl_calls)
    assert any(
        any("SetSecurityDescriptorSddlForm" in argument for argument in command)
        for command in acl_calls
    )


def test_native_acl_staging_is_exact_and_revoked_after_confirmed_exit(
    prepared_run: RunLayout,
    proposal: JsonObject,
    tmp_path: Path,
) -> None:
    plan = prepare_invocation(
        prepared_run,
        InvocationRole.BUILDER,
        NativeWindowsSandboxBackend(
            executable=sys.executable,
            platform_name="nt",
        ),
        prompt="builder",
        approved_proposal=proposal,
    )
    _prepare_synthetic_native_runtime(plan)
    sibling_run = prepared_run.root.parent / "cycle-sibling-must-remain-unreadable"
    sibling_run.mkdir()
    (sibling_run / "sentinel.txt").write_text("unreadable\n", encoding="utf-8")
    environment = _synthetic_native_acl_environment(tmp_path)
    calls: list[tuple[str, ...]] = []
    forwarded_environments: list[dict[str, str]] = []

    def fake_acl_process(
        command: tuple[str, ...],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        forwarded_environment = kwargs.get("env")
        assert isinstance(forwarded_environment, dict)
        forwarded_environments.append(cast(dict[str, str], forwarded_environment))
        if "/save" in command:
            target = Path(command[command.index("/save") + 1])
            _write_synthetic_acl_snapshot(target, plan.work_root)
        return subprocess.CompletedProcess(command, returncode=0, stdout="", stderr="")

    epoch = stage_native_invocation_acl(
        plan,
        source_environment=environment,
        run_process=fake_acl_process,
    )
    assert epoch == 1
    grants = [command for command in calls if "/grant" in command]
    assert grants
    assert all(str(sibling_run.resolve()) not in command for command in grants)
    assert any(
        str((plan.work_root / ".research").resolve()) in command
        and "SYNTHETIC_HOST\\CodexSandboxUsers:(OI)(CI)RX" in command
        for command in grants
    )
    assert any(
        str((plan.work_root / "candidate_worktree").resolve()) in command
        and "SYNTHETIC_HOST\\CodexSandboxUsers:(OI)(CI)M" in command
        for command in grants
    )
    assert any(
        str(prepared_run.root.resolve()) in command
        and "SYNTHETIC_HOST\\CodexSandboxUsers:(X)" in command
        for command in grants
    )
    assert all("OPENAI_API_KEY" not in forwarded for forwarded in forwarded_environments)

    child_created = (
        plan.work_root
        / "candidate_worktree"
        / "src"
        / "trading"
        / "strategies"
        / "alpha_v2"
        / "model.py"
    )
    child_created.parent.mkdir(parents=True)
    child_created.write_text('VERSION = "1.1.0"\n', encoding="utf-8")
    revoke_native_invocation_acl(
        plan,
        child_exit_confirmed=True,
        source_environment=environment,
        run_process=fake_acl_process,
    )
    assert any("/setowner" in command for command in calls)
    assert any("/reset" in command for command in calls)
    assert any(
        any("SetSecurityDescriptorSddlForm" in argument for argument in command)
        for command in calls
    )
    remove_commands = [command for command in calls if "/remove:g" in command]
    assert len(remove_commands) == 3
    assert all(str(sibling_run.resolve()) not in command for command in remove_commands)
    revoked = load_json_object(plan.work_root / "native-acl-events/0001-revoked.json")
    assert revoked["new_entries_inherited_only"] is True
    assert revoked["other_runs_granted"] is False
    assert "SYNTHETIC_HOST" not in str(revoked)

    call_count = len(calls)
    revoke_native_invocation_acl(
        plan,
        child_exit_confirmed=True,
        source_environment=environment,
        run_process=fake_acl_process,
    )
    assert len(calls) == call_count


def test_native_read_jail_preflight_retry_quarantines_unstarted_result(
    prepared_run: RunLayout,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plan = prepare_invocation(
        prepared_run,
        InvocationRole.COMMANDER,
        NativeWindowsSandboxBackend(
            executable=sys.executable,
            platform_name="nt",
        ),
        prompt="commander",
    )
    _prepare_synthetic_native_runtime(plan)
    environment = _synthetic_native_acl_environment(tmp_path)

    def fake_acl_process(
        command: tuple[str, ...],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        del kwargs
        if "/save" in command:
            target = Path(command[command.index("/save") + 1])
            _write_synthetic_acl_snapshot(target, plan.work_root)
        return subprocess.CompletedProcess(command, returncode=0, stdout="", stderr="")

    preflight_attempt = 0
    command_in_attempt = 0

    def fake_run(
        command: tuple[str, ...],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        nonlocal preflight_attempt, command_in_attempt
        del kwargs
        if command_in_attempt == 0:
            preflight_attempt += 1
        command_in_attempt += 1
        if command_in_attempt == 1:
            if preflight_attempt == 2:
                workspace = Path(command[command.index("--cd") + 1])
                (workspace / "inside.txt").write_text("OK\n", encoding="utf-8")
            return subprocess.CompletedProcess(
                command,
                returncode=0 if preflight_attempt == 2 else 1,
                stdout="",
                stderr="",
            )
        command_in_attempt = 0
        return subprocess.CompletedProcess(command, returncode=1, stdout="", stderr="")

    monkeypatch.setattr("research_commander.sandbox.subprocess.run", fake_run)
    with pytest.raises(IsolationError, match="cannot write"):
        verify_native_read_jail(
            plan,
            environment,
            acl_process=fake_acl_process,
        )
    first_result = load_json_object(plan.work_root / "native-read-jail-preflight.json")
    assert first_result["write_inside_workspace"] is False
    assert not (plan.work_root / "execution-started.json").exists()

    verify_native_read_jail(
        plan,
        environment,
        acl_process=fake_acl_process,
    )
    second_result = load_json_object(plan.work_root / "native-read-jail-preflight.json")
    assert second_result["write_inside_workspace"] is True
    assert second_result["read_outside_workspace_denied"] is True
    assert (plan.work_root / "native-acl-events/0001-revoked.json").is_file()
    assert (plan.work_root / "native-acl-events/0002-revoked.json").is_file()
    quarantine = Path(environment["LOCALAPPDATA"]) / "AdaptiveLlmQuant" / "quarantine"
    quarantined = list(
        quarantine.glob(
            f"read-jail-preflight-{plan.invocation_id}-*/native-read-jail-preflight.json"
        )
    )
    assert len(quarantined) == 1


def test_native_probe_cleanup_retries_bounded_windows_lock(
    prepared_run: RunLayout,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = prepare_invocation(
        prepared_run,
        InvocationRole.COMMANDER,
        NativeWindowsSandboxBackend(
            executable=sys.executable,
            platform_name="nt",
        ),
        prompt="commander",
    )
    probe_root = plan.work_root / ".native-read-jail-probe"
    probe_root.mkdir()
    (probe_root / "sentinel.txt").write_text("synthetic\n", encoding="utf-8")
    actual_rmtree = shutil.rmtree
    attempts = 0
    delays: list[float] = []

    def locked_then_remove(path: Path) -> None:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise PermissionError("synthetic delayed Windows handle")
        actual_rmtree(path)

    monkeypatch.setattr("research_commander.sandbox.shutil.rmtree", locked_then_remove)
    _remove_native_probe_with_retry(
        plan,
        attempts=4,
        sleep=delays.append,
    )
    assert attempts == 3
    assert delays == [0.05, 0.1]
    assert not probe_root.exists()


def test_native_probe_cleanup_fails_closed_when_lock_persists(
    prepared_run: RunLayout,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = prepare_invocation(
        prepared_run,
        InvocationRole.COMMANDER,
        NativeWindowsSandboxBackend(
            executable=sys.executable,
            platform_name="nt",
        ),
        prompt="commander",
    )
    probe_root = plan.work_root / ".native-read-jail-probe"
    probe_root.mkdir()

    def always_locked(_path: Path) -> None:
        raise PermissionError("synthetic persistent Windows handle")

    monkeypatch.setattr("research_commander.sandbox.shutil.rmtree", always_locked)
    with pytest.raises(IsolationError, match="remained locked"):
        _remove_native_probe_with_retry(
            plan,
            attempts=2,
            sleep=lambda _seconds: None,
        )
    assert probe_root.exists()


def test_native_builder_acl_recovery_is_exit_gated_and_exactly_scoped(
    prepared_run: RunLayout,
    proposal: JsonObject,
    tmp_path: Path,
) -> None:
    plan = prepare_invocation(
        prepared_run,
        InvocationRole.BUILDER,
        NativeWindowsSandboxBackend(
            executable=sys.executable,
            platform_name="nt",
        ),
        prompt="builder",
        approved_proposal=proposal,
    )
    system_root = tmp_path / "synthetic-windows"
    system32 = system_root / "System32"
    system32.mkdir(parents=True)
    (system32 / "icacls.exe").write_bytes(b"synthetic executable fixture")
    environment = {
        "COMPUTERNAME": "SYNTHETIC_HOST",
        "SYSTEMROOT": str(system_root),
        "USERDOMAIN": "SYNTHETIC_HOST",
        "USERNAME": "synthetic-user",
        "PATH": "untrusted-path",
        "OPENAI_API_KEY": "must-not-be-forwarded",
    }
    calls: list[tuple[str, ...]] = []

    def fake_acl_process(
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
    ) -> subprocess.CompletedProcess[str]:
        del text, encoding, stdout, stderr, check, timeout
        calls.append(command)
        assert cwd == plan.work_root / "candidate_worktree"
        assert "OPENAI_API_KEY" not in env
        assert "untrusted-path" not in env["PATH"]
        return subprocess.CompletedProcess(command, returncode=0, stdout="", stderr="")

    with pytest.raises(IsolationError, match="confirmed child exit"):
        recover_native_builder_candidate_acl(
            plan,
            child_exit_confirmed=False,
            source_environment=environment,
            run_process=fake_acl_process,
        )
    assert calls == []

    recover_native_builder_candidate_acl(
        plan,
        child_exit_confirmed=True,
        source_environment=environment,
        run_process=fake_acl_process,
    )
    assert len(calls) == 1
    command = calls[0]
    candidate_root = str((plan.work_root / "candidate_worktree").resolve())
    assert command[command.index("--cd") + 1] == candidate_root
    assert command[command.index("/grant") + 1] == ("SYNTHETIC_HOST\\synthetic-user:(OI)(CI)F")
    assert "/T" in command
    assert "/L" in command
    assert str(prepared_run.root.parent.resolve()) not in command
    marker = load_json_object(plan.work_root / "candidate-acl-recovery.json")
    assert marker["scope"] == "candidate_worktree"
    assert marker["credential_used"] is False
    assert marker["reparse_points_followed"] is False
    assert "SYNTHETIC_HOST" not in str(marker)

    def should_not_repeat_acl(
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
    ) -> subprocess.CompletedProcess[str]:
        del command, cwd, env, text, encoding, stdout, stderr, check, timeout
        raise AssertionError("completed ACL recovery must be idempotent")

    recover_native_builder_candidate_acl(
        plan,
        child_exit_confirmed=True,
        source_environment=environment,
        run_process=should_not_repeat_acl,
    )


def test_native_builder_acl_recovery_rejects_work_root_escape(
    prepared_run: RunLayout,
    proposal: JsonObject,
) -> None:
    plan = prepare_invocation(
        prepared_run,
        InvocationRole.BUILDER,
        NativeWindowsSandboxBackend(
            executable=sys.executable,
            platform_name="nt",
        ),
        prompt="builder",
        approved_proposal=proposal,
    )
    escaped = replace(plan, work_root=prepared_run.work / "builder")
    with pytest.raises(IsolationError, match="exact invocation root"):
        recover_native_builder_candidate_acl(
            escaped,
            child_exit_confirmed=True,
            source_environment={
                "USERDOMAIN": "SYNTHETIC_HOST",
                "USERNAME": "synthetic-user",
            },
        )


def test_native_builder_acl_recovery_rejects_reparse_boundary(
    prepared_run: RunLayout,
    proposal: JsonObject,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = prepare_invocation(
        prepared_run,
        InvocationRole.BUILDER,
        NativeWindowsSandboxBackend(
            executable=sys.executable,
            platform_name="nt",
        ),
        prompt="builder",
        approved_proposal=proposal,
    )
    candidate_root = plan.work_root / "candidate_worktree"

    def fake_reparse_check(path: Path) -> bool:
        return path == candidate_root

    monkeypatch.setattr(
        "research_commander.sandbox._is_reparse_point",
        fake_reparse_check,
    )
    with pytest.raises(IsolationError, match="reparse point"):
        recover_native_builder_candidate_acl(
            plan,
            child_exit_confirmed=True,
            source_environment={
                "USERDOMAIN": "SYNTHETIC_HOST",
                "USERNAME": "synthetic-user",
            },
        )


def test_execution_is_one_shot_and_schema_bound(
    prepared_run: RunLayout,
    bundle: Bundle,
) -> None:
    plan = prepare_invocation(
        prepared_run,
        InvocationRole.COMMANDER,
        DockerBackend("runner:test", "codex-egress"),
        prompt="commander",
    )

    def fake_process(
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
    ) -> subprocess.CompletedProcess[str]:
        del command, cwd, env, input, text, encoding, stdout, stderr, check, timeout
        write_json_exclusive(plan.output_path, _decision(bundle.request))
        return subprocess.CompletedProcess(args=(), returncode=0)

    output = execute_invocation(
        plan,
        bundle.request,
        run_process=fake_process,
    )
    assert output["decision"] == "NO_RESEARCH_CHANGE"
    with pytest.raises(IsolationError, match="cannot be resumed"):
        execute_invocation(plan, bundle.request, run_process=fake_process)


def test_native_builder_execution_recovers_acl_before_validation(
    prepared_run: RunLayout,
    bundle: Bundle,
    proposal: JsonObject,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = prepare_invocation(
        prepared_run,
        InvocationRole.BUILDER,
        NativeWindowsSandboxBackend(
            executable=sys.executable,
            platform_name="nt",
        ),
        prompt="builder",
        approved_proposal=proposal,
    )
    binding = load_json_object(prepared_run.request / "builder_binding.json")
    supplied_proposal_hash = binding.get("proposal_hash")
    assert isinstance(supplied_proposal_hash, str)
    assert supplied_proposal_hash != hash_file(
        prepared_run.request / "approved_algorithm_proposal.json"
    )
    recovery_calls: list[bool] = []

    def fake_recovery(
        recovery_plan: InvocationPlan,
        *,
        child_exit_confirmed: bool,
    ) -> None:
        assert recovery_plan is plan
        recovery_calls.append(child_exit_confirmed)

    monkeypatch.setattr(
        "research_commander.sandbox.recover_native_builder_candidate_acl",
        fake_recovery,
    )

    def fake_process(
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
    ) -> subprocess.CompletedProcess[str]:
        del command, cwd, env, input, text, encoding, stdout, stderr, check, timeout
        strategy = plan.work_root / "candidate_worktree/src/trading/strategies/alpha_v2/model.py"
        strategy.parent.mkdir(parents=True)
        strategy.write_text('VERSION = "1.1.0"\n', encoding="utf-8")
        test_file = plan.work_root / "candidate_worktree/tests/unit/test_alpha_v2.py"
        test_file.write_text(
            "def test_candidate() -> None:\n    assert True\n",
            encoding="utf-8",
        )
        write_json_exclusive(
            plan.output_path,
            _builder_result(
                bundle.request,
                proposal_hash=supplied_proposal_hash,
                files_changed=[
                    "src/trading/strategies/alpha_v2/model.py",
                    "tests/unit/test_alpha_v2.py",
                ],
                tests_added=["tests/unit/test_alpha_v2.py"],
            ),
        )
        return subprocess.CompletedProcess(args=(), returncode=0)

    output = execute_invocation(
        plan,
        bundle.request,
        run_process=fake_process,
    )
    assert output["proposal_hash"] == supplied_proposal_hash
    assert recovery_calls == [True]


def test_failed_process_does_not_blindly_retry(
    prepared_run: RunLayout,
    bundle: Bundle,
) -> None:
    plan = prepare_invocation(
        prepared_run,
        InvocationRole.COMMANDER,
        DockerBackend("runner:test", "codex-egress"),
        prompt="commander",
    )

    def failed_process(
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
    ) -> subprocess.CompletedProcess[str]:
        del command, cwd, env, input, text, encoding, stdout, stderr, check, timeout
        return subprocess.CompletedProcess(args=(), returncode=9)

    with pytest.raises(ContractError, match="PROCESS_EXIT_9"):
        execute_invocation(plan, bundle.request, run_process=failed_process)
    with pytest.raises(IsolationError, match="cannot be resumed"):
        execute_invocation(plan, bundle.request, run_process=failed_process)


def test_orphaned_commander_output_can_be_adopted_without_model_reexecution(
    prepared_run: RunLayout,
    bundle: Bundle,
) -> None:
    plan = prepare_invocation(
        prepared_run,
        InvocationRole.COMMANDER,
        DockerBackend("runner:test", "codex-egress"),
        prompt="commander",
    )
    _mark_started(plan)
    write_json_exclusive(plan.output_path, _decision(bundle.request))
    sleep_calls: list[float] = []

    output = adopt_invocation_output(
        plan,
        bundle.request,
        child_exit_confirmed=True,
        stability_seconds=0.25,
        sleep=sleep_calls.append,
    )

    assert output["decision"] == "NO_RESEARCH_CHANGE"
    assert sleep_calls == [0.25]
    published = load_json_object(prepared_run.output / "research_decision.json")
    assert hash_json(published) == hash_json(output)
    completed = load_json_object(plan.work_root / "execution-completed.json")
    assert completed["completion_mode"] == "HOST_ADOPTED_AFTER_SUPERVISOR_TIMEOUT"
    assert completed["child_exit_confirmed"] is True
    assert completed["exit_code"] is None
    assert completed["candidate_tree_hash"] is None


def test_orphaned_builder_adoption_revalidates_candidate_diff(
    prepared_run: RunLayout,
    bundle: Bundle,
    proposal: JsonObject,
) -> None:
    plan = prepare_invocation(
        prepared_run,
        InvocationRole.BUILDER,
        DockerBackend("runner:test", "codex-egress"),
        prompt="builder",
        approved_proposal=proposal,
    )
    strategy = plan.work_root / "candidate_worktree/src/trading/strategies/alpha_v2/model.py"
    strategy.parent.mkdir(parents=True)
    strategy.write_text('VERSION = "1.1.0"\n', encoding="utf-8")
    test_file = plan.work_root / "candidate_worktree/tests/unit/test_alpha_v2.py"
    test_file.write_text("def test_candidate() -> None:\n    assert True\n", encoding="utf-8")
    changed = [
        "src/trading/strategies/alpha_v2/model.py",
        "tests/unit/test_alpha_v2.py",
    ]
    _mark_started(plan)
    binding = load_json_object(prepared_run.request / "builder_binding.json")
    supplied_proposal_hash = binding.get("proposal_hash")
    assert isinstance(supplied_proposal_hash, str)
    write_json_exclusive(
        plan.output_path,
        _builder_result(
            bundle.request,
            proposal_hash=supplied_proposal_hash,
            files_changed=changed,
            tests_added=["tests/unit/test_alpha_v2.py"],
        ),
    )

    output = adopt_invocation_output(
        plan,
        bundle.request,
        child_exit_confirmed=True,
        stability_seconds=0.25,
        sleep=lambda _seconds: None,
    )

    assert output["files_changed"] == changed
    completed = load_json_object(plan.work_root / "execution-completed.json")
    assert isinstance(completed["candidate_tree_hash"], str)
    assert load_json_object(prepared_run.output / "candidate_build_result.json") == output


def test_orphaned_output_adoption_requires_confirmed_child_exit(
    prepared_run: RunLayout,
    bundle: Bundle,
) -> None:
    plan = prepare_invocation(
        prepared_run,
        InvocationRole.COMMANDER,
        DockerBackend("runner:test", "codex-egress"),
        prompt="commander",
    )
    _mark_started(plan)
    write_json_exclusive(plan.output_path, _decision(bundle.request))

    with pytest.raises(IsolationError, match="exact child process exited"):
        adopt_invocation_output(
            plan,
            bundle.request,
            child_exit_confirmed=False,
            sleep=lambda _seconds: None,
        )
    assert not (plan.work_root / "execution-completed.json").exists()
    assert not (prepared_run.output / "research_decision.json").exists()


def test_orphaned_output_adoption_rejects_unstable_artifacts(
    prepared_run: RunLayout,
    bundle: Bundle,
) -> None:
    plan = prepare_invocation(
        prepared_run,
        InvocationRole.COMMANDER,
        DockerBackend("runner:test", "codex-egress"),
        prompt="commander",
    )
    _mark_started(plan)
    write_json_exclusive(plan.output_path, _decision(bundle.request))

    def mutate_output(_seconds: float) -> None:
        plan.output_path.write_text('{"changed":true}\n', encoding="utf-8")

    with pytest.raises(IsolationError, match="changed during stability check"):
        adopt_invocation_output(
            plan,
            bundle.request,
            child_exit_confirmed=True,
            stability_seconds=0.25,
            sleep=mutate_output,
        )
    assert not (plan.work_root / "execution-completed.json").exists()
    assert not (prepared_run.output / "research_decision.json").exists()


def test_orphaned_output_adoption_uses_normal_binding_validation(
    prepared_run: RunLayout,
    bundle: Bundle,
) -> None:
    plan = prepare_invocation(
        prepared_run,
        InvocationRole.COMMANDER,
        DockerBackend("runner:test", "codex-egress"),
        prompt="commander",
    )
    _mark_started(plan)
    invalid = _decision(bundle.request)
    invalid["request_id"] = "request-from-another-cycle"
    write_json_exclusive(plan.output_path, invalid)

    with pytest.raises(ContractError, match="output binding mismatch"):
        adopt_invocation_output(
            plan,
            bundle.request,
            child_exit_confirmed=True,
            stability_seconds=0.25,
            sleep=lambda _seconds: None,
        )
    assert not (plan.work_root / "execution-completed.json").exists()
    assert not (prepared_run.output / "research_decision.json").exists()
