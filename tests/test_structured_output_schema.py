from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime

import pytest
from conftest import Bundle
from jsonschema import Draft202012Validator

from research_commander.binding import (
    COMMANDER_CREATED_AT_SENTINEL,
    COMMANDER_HASH_SENTINEL,
    contract_hash,
    finalize_commander_output,
    validate_research_decision,
)
from research_commander.errors import ContractError
from research_commander.json_types import JsonObject, JsonValue
from research_commander.schema_store import (
    structured_output_schema,
    validate_document,
)

UNSUPPORTED_KEYS = frozenset(
    {
        "$id",
        "$ref",
        "$schema",
        "$defs",
        "allOf",
        "not",
        "dependentRequired",
        "dependentSchemas",
        "if",
        "then",
        "else",
        "uniqueItems",
    }
)


def _assert_strict_subset(value: JsonValue) -> None:
    if isinstance(value, list):
        for item in value:
            _assert_strict_subset(item)
        return
    if not isinstance(value, dict):
        return
    assert UNSUPPORTED_KEYS.isdisjoint(value)
    if "const" in value or "enum" in value:
        assert "type" in value
    if value.get("type") == "object":
        properties = value.get("properties")
        required = value.get("required")
        assert isinstance(properties, dict)
        assert isinstance(required, list)
        assert set(required) == set(properties)
        assert value.get("additionalProperties") is False
    for item in value.values():
        _assert_strict_subset(item)


def _decision(
    request: JsonObject,
    *,
    kind: str = "NO_RESEARCH_CHANGE",
    proposal: JsonObject | None = None,
) -> JsonObject:
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
        "decision": kind,
        "rationale": "Bounded synthetic decision.",
        "proposal": proposal,
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


@pytest.mark.parametrize(
    "schema_name",
    ["ResearchDecisionV1", "CandidateBuildResultV1"],
)
def test_model_facing_schemas_are_self_contained_strict_objects(
    schema_name: str,
) -> None:
    schema = structured_output_schema(schema_name)
    Draft202012Validator.check_schema(schema)
    assert schema["type"] == "object"
    assert "anyOf" not in schema
    _assert_strict_subset(schema)


def test_runtime_decision_schema_accepts_null_or_fixed_shape_proposal(
    bundle: Bundle,
    proposal: JsonObject,
) -> None:
    schema = structured_output_schema("ResearchDecisionV1")
    validator = Draft202012Validator(schema)
    no_change_transport = _decision(bundle.request)
    no_change_transport["created_at"] = COMMANDER_CREATED_AT_SENTINEL
    no_change_transport["output_hash"] = COMMANDER_HASH_SENTINEL
    validator.validate(  # pyright: ignore[reportUnknownMemberType]
        no_change_transport
    )

    runtime_proposal = deepcopy(proposal)
    runtime_proposal["proposal_hash"] = COMMANDER_HASH_SENTINEL
    runtime_proposal["estimated_cost_sensitivity"] = {
        "cost_1x": 0.01,
        "cost_2x": 0.0,
        "cost_3x": -0.02,
    }
    proposed = _decision(
        bundle.request,
        kind="PROPOSE_STRATEGY_REVISION",
        proposal=runtime_proposal,
    )
    proposed["created_at"] = COMMANDER_CREATED_AT_SENTINEL
    proposed["output_hash"] = COMMANDER_HASH_SENTINEL
    validator.validate(proposed)  # pyright: ignore[reportUnknownMemberType]
    finalized = finalize_commander_output(
        proposed,
        bundle.request,
        received_at=datetime(2026, 7, 27, 21, tzinfo=UTC),
    )
    validate_document(finalized, "ResearchDecisionV1")


def test_decision_semantics_require_proposal_only_for_proposal_kinds(
    bundle: Bundle,
    proposal: JsonObject,
) -> None:
    current = datetime(2026, 7, 27, 21, tzinfo=UTC)
    with pytest.raises(ContractError, match="requires AlgorithmProposalV1"):
        validate_research_decision(
            _decision(
                bundle.request,
                kind="PROPOSE_STRATEGY_REVISION",
            ),
            bundle.request,
            now=current,
        )
    with pytest.raises(ContractError, match="non-proposal"):
        validate_research_decision(
            _decision(bundle.request, proposal=proposal),
            bundle.request,
            now=current,
        )
