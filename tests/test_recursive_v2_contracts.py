from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from research_commander.binding import (
    context_manifest_hash,
    contract_hash,
    validate_algorithm_proposal,
    validate_request,
    validate_research_decision,
)
from research_commander.errors import ContractError
from research_commander.io import load_json_object
from research_commander.json_types import JsonObject
from research_commander.layout import RunLayout
from research_commander.sandbox import (
    BUILDER_REQUEST_DIRECTORY,
    DockerBackend,
    InvocationRole,
    prepare_invocation,
)
from research_commander.schema_store import (
    structured_output_schema,
    validate_document,
)
from research_commander.webgpt import (
    ConversationRegistry,
    validate_webgpt_commander_ingress,
)

NOW = datetime(2026, 7, 28, 15, 1, tzinfo=UTC)


def _examples_root() -> Path:
    return Path(__file__).resolve().parents[1] / "examples"


def _v2_examples() -> tuple[JsonObject, JsonObject, JsonObject]:
    root = _examples_root()
    return (
        load_json_object(root / "research-request-v2.example.json"),
        load_json_object(root / "algorithm-proposal-v2.example.json"),
        load_json_object(root / "research-decision-v2.example.json"),
    )


def _evidence(request: JsonObject) -> JsonObject:
    return {
        "schema_version": "ResearchEvidenceManifestV1",
        "research_cycle_id": request["research_cycle_id"],
        "as_of": request["as_of"],
        "data_available_cutoff": request["data_available_cutoff"],
        "sources": [
            {
                "evidence_source_id": "source-synthetic-1",
                "source_tier": "TIER_1_OFFICIAL",
                "published_at": "2026-07-28T14:00:00Z",
                "first_available_at": "2026-07-28T14:01:00Z",
                "captured_at": "2026-07-28T14:02:00Z",
                "content_hash": "c" * 64,
                "url_hash": "d" * 64,
                "corroborated": True,
                "contradiction": False,
            }
        ],
    }


def test_public_v2_examples_validate_and_bind() -> None:
    request, proposal, decision = _v2_examples()
    validate_document(request, "ResearchRequestV2")
    validate_document(proposal, "AlgorithmProposalV2")
    validate_document(decision, "ResearchDecisionV2")
    assert request["context_manifest_hash"] == context_manifest_hash(request)
    validate_algorithm_proposal(proposal, request, now=NOW)
    validate_research_decision(decision, request, now=NOW)

    validate_request(
        request,
        _evidence(request),
        {"schema_version": "ResearchConstraintsV1"},
        "e" * 64,
        now=NOW,
    )


def test_v2_proposal_outside_action_plan_is_rejected() -> None:
    request, proposal, decision = _v2_examples()
    proposal["primary_action_kind"] = "REMOVE_FEATURE"
    proposal["proposal_hash"] = contract_hash(
        proposal,
        exclude=frozenset({"proposal_hash"}),
    )
    decision["proposal"] = proposal
    decision["output_hash"] = contract_hash(
        decision,
        exclude=frozenset({"output_hash"}),
        timestamp_fields=("request_expires_at", "created_at"),
    )
    with pytest.raises(ContractError, match="outside the action plan"):
        validate_research_decision(decision, request, now=NOW)


def test_v2_memory_or_plan_tampering_fails_closed() -> None:
    request, _, _ = _v2_examples()
    plan = cast(dict[str, object], request["research_action_plan"])
    ranked = cast(list[dict[str, object]], plan["ranked_actions"])
    ranked[0]["score"] = cast(float, ranked[0]["score"]) + 1.0
    request["context_manifest_hash"] = context_manifest_hash(request)
    with pytest.raises(ContractError, match="action plan hash mismatch"):
        validate_request(
            request,
            _evidence(request),
            {},
            "e" * 64,
            now=NOW,
        )


def test_v2_structured_output_schema_is_host_bound() -> None:
    schema = structured_output_schema("ResearchDecisionV2")
    assert schema["type"] == "object"
    properties = cast(dict[str, object], schema["properties"])
    assert cast(dict[str, object], properties["created_at"])["const"] == (
        "RUNTIME_BOUND_BY_HOST"
    )
    assert cast(dict[str, object], properties["output_hash"])["const"] == (
        "HOST_COMPUTES_SHA256"
    )


def test_recursive_schema_hash_manifest_matches_local_bytes() -> None:
    root = Path(__file__).resolve().parents[1] / "schemas"
    manifest = json.loads(
        (
            root / "recursive-contract-schema-hashes-v1.json"
        ).read_text(encoding="utf-8")
    )
    assert manifest["canonical_source"] == "adaptive-llm-quant-public"
    for filename, expected_hash in manifest["schemas"].items():
        payload = (root / filename).read_bytes()
        assert not payload.startswith(b"\xef\xbb\xbf")
        assert hashlib.sha256(payload).hexdigest() == expected_hash


def test_webgpt_v2_ingress_uses_same_decision_contract(
    tmp_path: Path,
) -> None:
    request, _, decision = _v2_examples()
    request["selected_commander"] = "WEBGPT_SOL_PRO"
    request["commander_selection_id"] = "selection-webgpt-v2-synthetic"
    request["context_manifest_hash"] = context_manifest_hash(request)
    for field in (
        "selected_commander",
        "commander_selection_id",
        "context_manifest_hash",
    ):
        decision[field] = request[field]
    decision["output_hash"] = contract_hash(
        decision,
        exclude=frozenset({"output_hash"}),
        timestamp_fields=("request_expires_at", "created_at"),
    )
    ingress: JsonObject = {
        "schema_version": "WebGPTCommanderIngressV2",
        "role": "RESEARCH_COMMANDER",
        "model_family": "GPT-5.6 Sol Pro",
        "reasoning_profile": "xhigh",
        "conversation_id": "conversation-webgpt-v2",
        "request_id": request["request_id"],
        "browser_session_id": "browser-session-webgpt-v2",
        "conversation_created_at": "2026-07-28T15:00:10Z",
        "request_started_at": "2026-07-28T15:00:20Z",
        "response_completed_at": "2026-07-28T15:01:00Z",
        "response_state": "COMPLETED",
        "fresh_conversation": True,
        "request_binding": {
            field: request[field]
            for field in (
                "request_id",
                "research_cycle_id",
                "context_manifest_hash",
                "source_snapshot_commit",
                "champion_version",
                "experiment_family",
                "selected_commander",
                "commander_selection_id",
                "commander_selection_version",
                "schema_version",
                "expires_at",
            )
        },
        "research_decision": decision,
    }
    accepted = validate_webgpt_commander_ingress(
        ingress,
        request,
        registry=ConversationRegistry(tmp_path / "claims"),
        now=NOW,
    )
    assert accepted["output_hash"] == decision["output_hash"]


def test_builder_request_view_excludes_full_memory_and_transcript(
    prepared_run: RunLayout,
    proposal: JsonObject,
) -> None:
    plan = prepare_invocation(
        prepared_run,
        InvocationRole.BUILDER,
        DockerBackend("runner:test", "codex-egress"),
        prompt="builder",
        approved_proposal=proposal,
    )
    request_root = (
        prepared_run.input / BUILDER_REQUEST_DIRECTORY
    )
    names = {path.name for path in request_root.iterdir()}
    assert "request_binding.json" in names
    assert "approved_algorithm_proposal.json" in names
    assert "research_request.json" not in names
    assert "evidence_manifest.json" not in names
    assert "commander-output.schema.json" not in names
    assert all("transcript" not in name for name in names)
    command_text = "\n".join(plan.command)
    assert str(request_root.resolve()) in command_text
    staged_binding = load_json_object(
        request_root / "request_binding.json"
    )
    assert "research_memory_snapshot" not in staged_binding
