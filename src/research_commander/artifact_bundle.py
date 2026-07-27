"""Trusted-host finalization of one tested Candidate artifact bundle."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from research_commander.binding import request_binding
from research_commander.candidate import build_challenger_manifest
from research_commander.candidate_testing import (
    load_candidate_inputs,
    load_passing_candidate_test_manifest,
)
from research_commander.canonical import canonical_json_bytes, hash_json
from research_commander.errors import IsolationError
from research_commander.io import write_json_exclusive, write_text_exclusive
from research_commander.json_types import JsonObject
from research_commander.layout import RunLayout
from research_commander.sandbox import InvocationPlan
from research_commander.schema_store import validate_document


@dataclass(frozen=True)
class FinalizedCandidate:
    patch: str
    challenger_manifest: JsonObject
    validation_request: JsonObject
    artifact_bundle: JsonObject
    changed_paths: tuple[str, ...]


def _require_hash_match(
    document: JsonObject,
    field: str,
    expected: str,
) -> None:
    if document.get(field) != expected:
        raise IsolationError(f"candidate test manifest {field} mismatch")


def finalize_candidate_artifacts(
    layout: RunLayout,
    plan: InvocationPlan,
    *,
    protected_champion_paths: tuple[str, ...] = (),
) -> FinalizedCandidate:
    """Finalize only the host-owned Builder result and host-run test manifest."""
    inputs = load_candidate_inputs(layout, plan)
    _, test_manifest = load_passing_candidate_test_manifest(plan)
    if test_manifest.get("candidate_tree_unchanged") is not True:
        raise IsolationError("Candidate tree changed during its test run")
    if test_manifest.get("host_abi_test_unchanged") is not True:
        raise IsolationError("host-owned Candidate ABI test changed during its test run")
    _require_hash_match(
        test_manifest,
        "host_abi_test_hash_after",
        str(test_manifest.get("host_abi_test_hash_before")),
    )
    if test_manifest.get("candidate_test_projection_unchanged") is not True:
        raise IsolationError("host-owned Candidate test projection changed during its test run")
    _require_hash_match(
        test_manifest,
        "candidate_test_projection_hash_after",
        str(test_manifest.get("candidate_test_projection_hash_before")),
    )
    if test_manifest.get("candidate_source_projection_unchanged") is not True:
        raise IsolationError("Candidate source projection changed during its test run")
    _require_hash_match(
        test_manifest,
        "candidate_source_projection_hash_after",
        str(test_manifest.get("candidate_source_projection_hash_before")),
    )
    _require_hash_match(
        test_manifest,
        "source_snapshot_hash",
        inputs.source_snapshot_hash,
    )
    _require_hash_match(
        test_manifest,
        "candidate_tree_hash_before",
        inputs.candidate_tree_hash,
    )
    _require_hash_match(
        test_manifest,
        "candidate_tree_hash_after",
        inputs.candidate_tree_hash,
    )
    _require_hash_match(test_manifest, "patch_hash", inputs.patch_hash)
    _require_hash_match(test_manifest, "proposal_hash", inputs.proposal_hash)
    _require_hash_match(
        test_manifest,
        "builder_result_hash",
        inputs.builder_result_hash,
    )
    if test_manifest.get("declared_entrypoint") != inputs.declared_entrypoint:
        raise IsolationError("Candidate test entrypoint differs from Builder output")

    manifest, validation, validation_request = build_challenger_manifest(
        request=inputs.request,
        proposal=inputs.proposal,
        patch=inputs.patch,
        candidate_root=inputs.candidate_root,
        test_manifest=test_manifest,
        protected_champion_paths=protected_champion_paths,
    )
    manifest_hash = manifest.get("manifest_hash")
    config_hash = manifest.get("config_hash")
    code_hash = manifest.get("code_hash")
    challenger_id = manifest.get("challenger_id")
    runtime = test_manifest.get("runtime")
    if not all(
        isinstance(value, str) for value in (manifest_hash, config_hash, code_hash, challenger_id)
    ):
        raise IsolationError("trusted host produced an incomplete Challenger manifest")
    if not isinstance(runtime, dict):
        raise IsolationError("Candidate test runtime attestation is malformed")
    test_manifest_hash = hash_json(test_manifest)
    validation_request_hash = hash_json(validation_request)
    identity: JsonObject = {
        "challenger_id": challenger_id,
        "candidate_tree_hash": inputs.candidate_tree_hash,
        "proposal_hash": inputs.proposal_hash,
        "test_manifest_hash": test_manifest_hash,
    }
    bundle_id = "candidate-bundle-" + hash_json(identity)[:24]
    bundle: JsonObject = {
        "schema_version": "candidate_artifact_bundle_v1",
        "bundle_id": bundle_id,
        "challenger_id": challenger_id,
        "request_binding": request_binding(inputs.request),
        "source_snapshot_hash": inputs.source_snapshot_hash,
        "candidate_tree_hash": inputs.candidate_tree_hash,
        "code_hash": code_hash,
        "config_hash": config_hash,
        "patch_hash": inputs.patch_hash,
        "proposal_hash": inputs.proposal_hash,
        "builder_result_hash": inputs.builder_result_hash,
        "test_manifest_hash": test_manifest_hash,
        "challenger_manifest_hash": manifest_hash,
        "validation_request_hash": validation_request_hash,
        "runtime": dict(runtime),
        "declared_entrypoint": inputs.declared_entrypoint,
        "candidate_abi": {
            "request_schema_version": "candidate_decision_request_v1",
            "response_schema_version": "candidate_decision_response_v1",
            "entrypoint_input": "RAW_JSON_OBJECT",
            "entrypoint_output": "RAW_JSON_OBJECT",
            "orders_permitted": False,
            "fills_permitted": False,
            "returns_or_pnl_permitted": False,
        },
        "broker_access_permitted": False,
        "credential_access_permitted": False,
        "network_access_permitted": False,
        "filesystem_write_permitted": False,
        "real_order_routing": False,
        "bundle_hash": "0" * 64,
    }
    bundle["bundle_hash"] = hash_json(
        {key: value for key, value in bundle.items() if key != "bundle_hash"}
    )
    validate_document(bundle, "CandidateArtifactBundleV1")
    return FinalizedCandidate(
        patch=inputs.patch,
        challenger_manifest=manifest,
        validation_request=validation_request,
        artifact_bundle=bundle,
        changed_paths=validation.changed_paths,
    )


def publish_finalized_candidate(
    layout: RunLayout,
    finalized: FinalizedCandidate,
) -> None:
    """Append the prevalidated final Candidate artifacts to the cycle output."""
    outputs: tuple[tuple[Path, str | JsonObject], ...] = (
        (layout.output / "patch.diff", finalized.patch),
        (layout.output / "candidate_manifest.json", finalized.challenger_manifest),
        (layout.output / "validation_request.json", finalized.validation_request),
        (layout.output / "candidate_artifact_bundle.json", finalized.artifact_bundle),
    )
    for path, value in outputs:
        expected = (
            value.encode("utf-8") if isinstance(value, str) else canonical_json_bytes(value) + b"\n"
        )
        if path.is_symlink():
            raise IsolationError("Candidate finalization output is a symlink")
        if path.exists():
            if not path.is_file() or path.read_bytes() != expected:
                raise IsolationError(
                    "Candidate finalization output conflicts with immutable content"
                )
            continue
        if isinstance(value, str):
            write_text_exclusive(path, value)
        else:
            write_json_exclusive(path, value)
