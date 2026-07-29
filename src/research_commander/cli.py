"""Command line interface for preparation, isolation, and public checks."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import NoReturn

from research_commander.artifact_bundle import (
    FinalizedCandidate,
    finalize_candidate_outcome,
    publish_failed_candidate,
    publish_finalized_candidate,
)
from research_commander.assets import asset_path, asset_text
from research_commander.candidate_execution import (
    candidate_runtime_attestation,
    invoke_candidate_decision,
)
from research_commander.candidate_testing import (
    candidate_test_manifest_path,
    run_candidate_tests,
)
from research_commander.errors import ContractError
from research_commander.io import load_json_object, write_json_exclusive
from research_commander.json_types import JsonObject
from research_commander.layout import RunLayout, prepare_run
from research_commander.patch_policy import CandidatePatchPolicyVersion
from research_commander.public_scan import scan_public_tree
from research_commander.sandbox import (
    DockerBackend,
    ExplicitJailBackend,
    InvocationRole,
    NativeWindowsSandboxBackend,
    adopt_invocation_output,
    execute_invocation,
    load_invocation_plan,
    prepare_invocation,
)
from research_commander.schema_store import validate_document
from research_commander.webgpt import (
    ConversationRegistry,
    validate_webgpt_commander_ingress,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="research-commander")
    subcommands = parser.add_subparsers(dest="command", required=True)

    validate = subcommands.add_parser("validate")
    validate.add_argument("--schema", required=True)
    validate.add_argument("document", type=Path)

    prepare = subcommands.add_parser("prepare")
    prepare.add_argument("--request", type=Path, required=True)
    prepare.add_argument("--evidence", type=Path, required=True)
    prepare.add_argument(
        "--constraints",
        type=Path,
        default=asset_path("config/default-constraints.json"),
    )
    prepare.add_argument("--source-snapshot", type=Path, required=True)
    prepare.add_argument("--runs-root", type=Path, required=True)

    plan = subcommands.add_parser("plan")
    plan.add_argument("--run", type=Path, required=True)
    plan.add_argument("--role", choices=[item.value for item in InvocationRole], required=True)
    plan.add_argument(
        "--backend",
        choices=["docker", "explicit_jail", "native_windows"],
        required=True,
    )
    plan.add_argument("--image", default="adaptive-llm-quant-codex-runner:local")
    plan.add_argument("--egress-network", default="codex-egress")
    plan.add_argument("--jail-command")
    plan.add_argument("--jail-policy")
    plan.add_argument("--proposal", type=Path)
    plan.add_argument(
        "--candidate-patch-policy",
        choices=[item.value for item in CandidatePatchPolicyVersion],
    )
    plan.add_argument("--execute", action="store_true")

    execute_plan = subcommands.add_parser("execute-plan")
    execute_plan.add_argument("--run", type=Path, required=True)
    execute_plan.add_argument(
        "--role",
        choices=[item.value for item in InvocationRole],
        required=True,
    )

    adopt_plan = subcommands.add_parser("adopt-plan-output")
    adopt_plan.add_argument("--run", type=Path, required=True)
    adopt_plan.add_argument(
        "--role",
        choices=[item.value for item in InvocationRole],
        required=True,
    )
    adopt_plan.add_argument(
        "--confirm-child-exited",
        action="store_true",
        required=True,
        help="confirm the external supervisor observed the exact Codex child exit",
    )

    ingress = subcommands.add_parser("verify-webgpt")
    ingress.add_argument("--run", type=Path, required=True)
    ingress.add_argument("--ingress", type=Path, required=True)
    ingress.add_argument("--conversation-registry", type=Path, required=True)
    ingress.add_argument("--scout-conversation-id", action="append", default=[])

    candidate_test = subcommands.add_parser("test-candidate")
    candidate_test.add_argument("--run", type=Path, required=True)

    finalize = subcommands.add_parser("finalize-candidate")
    finalize.add_argument("--run", type=Path, required=True)
    finalize.add_argument("--protected-champion-path", action="append", default=[])

    invoke_candidate = subcommands.add_parser("invoke-candidate")
    invoke_candidate.add_argument("--run", type=Path, required=True)
    invoke_candidate.add_argument("--request", type=Path, required=True)
    invoke_candidate.add_argument("--security", type=Path, required=True)
    invoke_candidate.add_argument(
        "--execution-lane",
        choices=("PRIMARY", "REPLAY"),
        default="PRIMARY",
    )

    runtime_info = subcommands.add_parser("candidate-runtime-info")
    runtime_info.add_argument("--run", type=Path, required=True)

    public_scan = subcommands.add_parser("public-scan")
    public_scan.add_argument("root", type=Path)
    public_scan.add_argument("--allow-uninitialized", action="store_true")
    public_scan.add_argument("--expected-repository")
    return parser


def _print_json(value: JsonObject) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _fail(message: str) -> NoReturn:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(2)


def _backend(
    args: argparse.Namespace,
) -> DockerBackend | ExplicitJailBackend | NativeWindowsSandboxBackend:
    if args.backend == "docker":
        return DockerBackend(image=args.image, egress_network=args.egress_network)
    if args.backend == "native_windows":
        return NativeWindowsSandboxBackend()
    if not args.jail_command or not args.jail_policy:
        raise ContractError("explicit_jail requires --jail-command and --jail-policy")
    return ExplicitJailBackend(command=(args.jail_command,), policy_id=args.jail_policy)


def _cmd_prepare(args: argparse.Namespace) -> None:
    layout = prepare_run(
        args.runs_root,
        request=load_json_object(args.request),
        evidence_manifest=load_json_object(args.evidence),
        constraints=load_json_object(args.constraints),
        source_root=args.source_snapshot,
        agents_text=asset_text("AGENTS.md"),
    )
    _print_json({"run": str(layout.root), "status": "PREPARED"})


def _cmd_plan(args: argparse.Namespace) -> None:
    layout = RunLayout(args.run.resolve(strict=True))
    role = InvocationRole(args.role)
    proposal = load_json_object(args.proposal) if args.proposal else None
    prompt = asset_text(f"prompts/{role.value}.prompt.md")
    plan = prepare_invocation(
        layout,
        role,
        _backend(args),
        prompt=prompt,
        approved_proposal=proposal,
        candidate_patch_policy_version=args.candidate_patch_policy,
    )
    if not args.execute:
        _print_json(plan.manifest())
        return
    request = load_json_object(layout.request / "research_request.json")
    output = execute_invocation(plan, request)
    destination = (
        layout.output / "research_decision.json"
        if role is InvocationRole.COMMANDER
        else layout.output / "candidate_build_result.json"
    )
    write_json_exclusive(destination, output)
    _print_json({"output": str(destination), "output_schema": plan.output_schema})


def _cmd_execute_plan(args: argparse.Namespace) -> None:
    layout = RunLayout(args.run.resolve(strict=True))
    role = InvocationRole(args.role)
    prompt = asset_text(f"prompts/{role.value}.prompt.md")
    plan = load_invocation_plan(layout, role, prompt=prompt)
    request = load_json_object(layout.request / "research_request.json")
    output = execute_invocation(plan, request)
    destination = (
        layout.output / "research_decision.json"
        if role is InvocationRole.COMMANDER
        else layout.output / "candidate_build_result.json"
    )
    write_json_exclusive(destination, output)
    _print_json({"output": str(destination), "output_schema": plan.output_schema})


def _cmd_adopt_plan_output(args: argparse.Namespace) -> None:
    layout = RunLayout(args.run.resolve(strict=True))
    role = InvocationRole(args.role)
    prompt = asset_text(f"prompts/{role.value}.prompt.md")
    plan = load_invocation_plan(layout, role, prompt=prompt)
    request = load_json_object(layout.request / "research_request.json")
    adopt_invocation_output(
        plan,
        request,
        child_exit_confirmed=args.confirm_child_exited,
    )
    destination = (
        layout.output / "research_decision.json"
        if role is InvocationRole.COMMANDER
        else layout.output / "candidate_build_result.json"
    )
    _print_json(
        {
            "output": str(destination),
            "output_schema": plan.output_schema,
            "status": "HOST_ADOPTED_AFTER_SUPERVISOR_TIMEOUT",
        }
    )


def _cmd_webgpt(args: argparse.Namespace) -> None:
    layout = RunLayout(args.run.resolve(strict=True))
    request = load_json_object(layout.request / "research_request.json")
    decision = validate_webgpt_commander_ingress(
        load_json_object(args.ingress),
        request,
        registry=ConversationRegistry(args.conversation_registry),
        forbidden_conversation_ids=frozenset(args.scout_conversation_id),
    )
    destination = layout.output / "research_decision.json"
    write_json_exclusive(destination, decision)
    _print_json({"output": str(destination), "status": "ACCEPTED"})


def _cmd_finalize(args: argparse.Namespace) -> None:
    layout = RunLayout(args.run.resolve(strict=True))
    plan = load_invocation_plan(
        layout,
        InvocationRole.BUILDER,
        prompt=asset_text("prompts/builder.prompt.md"),
    )
    finalized = finalize_candidate_outcome(
        layout,
        plan,
        protected_champion_paths=tuple(args.protected_champion_path),
    )
    if isinstance(finalized, FinalizedCandidate):
        publish_finalized_candidate(layout, finalized)
        status = "PROPOSED"
        artifact_bundle: str | None = str(
            layout.output / "candidate_artifact_bundle.json"
        )
        candidate_test_manifest = str(
            layout.output / "candidate_test_manifest.json"
        )
    else:
        publish_failed_candidate(layout, finalized)
        status = "TEST_FAILED"
        artifact_bundle = None
        candidate_test_manifest = str(
            layout.output / "candidate_test_manifest.json"
        )
    _print_json(
        {
            "challenger_id": finalized.challenger_manifest["challenger_id"],
            "candidate_artifact_bundle": artifact_bundle,
            "candidate_test_manifest": candidate_test_manifest,
            "changed_paths": list(finalized.changed_paths),
            "status": status,
        }
    )


def _cmd_test_candidate(args: argparse.Namespace) -> None:
    layout = RunLayout(args.run.resolve(strict=True))
    plan = load_invocation_plan(
        layout,
        InvocationRole.BUILDER,
        prompt=asset_text("prompts/builder.prompt.md"),
    )
    manifest = run_candidate_tests(layout, plan)
    test_run_id = manifest.get("test_run_id")
    if not isinstance(test_run_id, str):
        raise ContractError("Candidate test manifest has no test_run_id")
    _print_json(
        {
            "candidate_test_manifest": str(
                candidate_test_manifest_path(plan, test_run_id)
            ),
            "status": manifest["status"],
        }
    )


def _cmd_invoke_candidate(args: argparse.Namespace) -> None:
    layout = RunLayout(args.run.resolve(strict=True))
    plan = load_invocation_plan(
        layout,
        InvocationRole.BUILDER,
        prompt=asset_text("prompts/builder.prompt.md"),
    )
    result = invoke_candidate_decision(
        layout,
        plan,
        request=load_json_object(args.request),
        security=load_json_object(args.security),
        execution_lane=args.execution_lane,
    )
    _print_json(result)


def _cmd_candidate_runtime_info(args: argparse.Namespace) -> None:
    layout = RunLayout(args.run.resolve(strict=True))
    plan = load_invocation_plan(
        layout,
        InvocationRole.BUILDER,
        prompt=asset_text("prompts/builder.prompt.md"),
    )
    _print_json(candidate_runtime_attestation(layout, plan))


def _cmd_public_scan(args: argparse.Namespace) -> None:
    findings = scan_public_tree(
        args.root,
        require_clean_root=not args.allow_uninitialized,
        expected_repository=args.expected_repository,
    )
    if findings:
        for finding in findings:
            print(f"{finding.rule}: {finding.path}", file=sys.stderr)
        raise SystemExit(1)
    _print_json({"status": "PUBLIC_SAFE", "root": str(args.root.resolve())})


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "validate":
            validate_document(load_json_object(args.document), args.schema)
            _print_json({"schema": args.schema, "status": "VALID"})
        elif args.command == "prepare":
            _cmd_prepare(args)
        elif args.command == "plan":
            _cmd_plan(args)
        elif args.command == "execute-plan":
            _cmd_execute_plan(args)
        elif args.command == "adopt-plan-output":
            _cmd_adopt_plan_output(args)
        elif args.command == "verify-webgpt":
            _cmd_webgpt(args)
        elif args.command == "test-candidate":
            _cmd_test_candidate(args)
        elif args.command == "finalize-candidate":
            _cmd_finalize(args)
        elif args.command == "invoke-candidate":
            _cmd_invoke_candidate(args)
        elif args.command == "candidate-runtime-info":
            _cmd_candidate_runtime_info(args)
        elif args.command == "public-scan":
            _cmd_public_scan(args)
        else:
            _fail(f"unknown command: {args.command}")
    except ContractError as exc:
        _fail(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
