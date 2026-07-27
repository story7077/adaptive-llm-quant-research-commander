from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import Bundle

from research_commander.binding import context_manifest_hash, contract_hash
from research_commander.errors import ContractError, SchemaContractError
from research_commander.json_types import JsonObject
from research_commander.webgpt import (
    ConversationRegistry,
    validate_webgpt_commander_ingress,
)


def _ingress(bundle: Bundle, proposal: JsonObject) -> JsonObject:
    request = bundle.request
    request["selected_commander"] = "WEBGPT_SOL_PRO"
    request["context_manifest_hash"] = context_manifest_hash(request)
    decision: JsonObject = {
        "schema_version": "research_decision_v1",
        "request_id": request["request_id"],
        "research_cycle_id": request["research_cycle_id"],
        "selected_commander": "WEBGPT_SOL_PRO",
        "commander_selection_id": request["commander_selection_id"],
        "commander_selection_version": request["commander_selection_version"],
        "source_snapshot_commit": request["source_snapshot_commit"],
        "champion_version": request["champion_version"],
        "experiment_family": request["experiment_family"],
        "context_manifest_hash": request["context_manifest_hash"],
        "request_schema_version": request["schema_version"],
        "request_expires_at": request["expires_at"],
        "decision": "PROPOSE_NEW_STRATEGY",
        "rationale": "Build a falsifiable versioned Challenger.",
        "proposal": proposal,
        "requested_evidence": [],
        "created_at": "2026-07-27T20:45:00Z",
        "output_hash": "0" * 64,
    }
    decision["output_hash"] = contract_hash(
        decision,
        exclude=frozenset({"output_hash"}),
        timestamp_fields=("request_expires_at", "created_at"),
    )
    return {
        "schema_version": "WebGPTCommanderIngressV1",
        "role": "RESEARCH_COMMANDER",
        "model_family": "GPT-5.6 Sol Pro",
        "reasoning_profile": "xhigh",
        "conversation_id": "conversation-fresh-commander",
        "request_id": request["request_id"],
        "browser_session_id": "browser-session-example",
        "conversation_created_at": "2026-07-27T20:31:00Z",
        "request_started_at": "2026-07-27T20:32:00Z",
        "response_completed_at": "2026-07-27T20:45:00Z",
        "response_state": "COMPLETED",
        "fresh_conversation": True,
        "request_binding": {
            "request_id": request["request_id"],
            "research_cycle_id": request["research_cycle_id"],
            "context_manifest_hash": request["context_manifest_hash"],
            "source_snapshot_commit": request["source_snapshot_commit"],
            "champion_version": request["champion_version"],
            "experiment_family": request["experiment_family"],
            "selected_commander": "WEBGPT_SOL_PRO",
            "commander_selection_id": request["commander_selection_id"],
            "commander_selection_version": request["commander_selection_version"],
            "schema_version": "research_request_v1",
            "expires_at": request["expires_at"],
        },
        "research_decision": decision,
    }


def test_valid_webgpt_ingress_claims_fresh_conversation(
    bundle: Bundle, proposal: JsonObject, tmp_path: Path
) -> None:
    ingress = _ingress(bundle, proposal)
    registry = ConversationRegistry(tmp_path / "claims")
    result = validate_webgpt_commander_ingress(
        ingress,
        bundle.request,
        registry=registry,
        now=datetime(2026, 7, 27, 21, tzinfo=UTC),
    )
    assert result["decision"] == "PROPOSE_NEW_STRATEGY"
    with pytest.raises(ContractError, match="overwrite immutable artifact"):
        validate_webgpt_commander_ingress(
            ingress,
            bundle.request,
            registry=registry,
            now=datetime(2026, 7, 27, 21, tzinfo=UTC),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("model_family", "another-model"),
        ("reasoning_profile", "high"),
        ("response_state", "INTERRUPTED"),
        ("fresh_conversation", False),
    ],
)
def test_webgpt_model_reasoning_and_completion_mismatch_fail_closed(
    bundle: Bundle,
    proposal: JsonObject,
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    ingress = _ingress(bundle, proposal)
    ingress[field] = value  # type: ignore[assignment]
    with pytest.raises(SchemaContractError):
        validate_webgpt_commander_ingress(
            ingress,
            bundle.request,
            registry=ConversationRegistry(tmp_path / "claims"),
            now=datetime(2026, 7, 27, 21, tzinfo=UTC),
        )


def test_scout_conversation_cannot_be_reused_for_commander(
    bundle: Bundle, proposal: JsonObject, tmp_path: Path
) -> None:
    ingress = _ingress(bundle, proposal)
    with pytest.raises(ContractError, match="different conversations"):
        validate_webgpt_commander_ingress(
            ingress,
            bundle.request,
            registry=ConversationRegistry(tmp_path / "claims"),
            forbidden_conversation_ids=frozenset({"conversation-fresh-commander"}),
            now=datetime(2026, 7, 27, 21, tzinfo=UTC),
        )


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("commander_selection_id", "commander-selection-example-002"),
        ("commander_selection_version", 2),
    ],
)
def test_webgpt_ingress_rejects_stale_commander_selection_record(
    bundle: Bundle,
    proposal: JsonObject,
    tmp_path: Path,
    field: str,
    replacement: object,
) -> None:
    ingress = _ingress(bundle, proposal)
    binding = ingress["request_binding"]
    assert isinstance(binding, dict)
    binding[field] = replacement  # type: ignore[assignment]
    with pytest.raises(ContractError, match=rf"WebGPT request binding mismatch: {field}"):
        validate_webgpt_commander_ingress(
            ingress,
            bundle.request,
            registry=ConversationRegistry(tmp_path / "claims"),
            now=datetime(2026, 7, 27, 21, tzinfo=UTC),
        )


@pytest.mark.parametrize(
    "field",
    ["commander_selection_id", "commander_selection_version"],
)
def test_research_decision_schema_requires_exact_selection_record(
    bundle: Bundle,
    proposal: JsonObject,
    tmp_path: Path,
    field: str,
) -> None:
    ingress = _ingress(bundle, proposal)
    decision = ingress["research_decision"]
    assert isinstance(decision, dict)
    del decision[field]
    with pytest.raises(SchemaContractError, match=field):
        validate_webgpt_commander_ingress(
            ingress,
            bundle.request,
            registry=ConversationRegistry(tmp_path / "claims"),
            now=datetime(2026, 7, 27, 21, tzinfo=UTC),
        )


def test_webgpt_transcript_is_not_accepted(
    bundle: Bundle, proposal: JsonObject, tmp_path: Path
) -> None:
    ingress = deepcopy(_ingress(bundle, proposal))
    ingress["conversation_transcript"] = "unbounded prior context"
    with pytest.raises(SchemaContractError):
        validate_webgpt_commander_ingress(
            ingress,
            bundle.request,
            registry=ConversationRegistry(tmp_path / "claims"),
            now=datetime(2026, 7, 27, 21, tzinfo=UTC),
        )
