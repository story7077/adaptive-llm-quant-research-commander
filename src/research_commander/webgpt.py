"""Fail-closed structured ingress for a WebGPT Research Commander."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from research_commander.binding import (
    BINDING_FIELDS,
    parse_timestamp,
    validate_research_decision,
)
from research_commander.canonical import sha256_bytes
from research_commander.errors import ContractError
from research_commander.io import write_json_exclusive
from research_commander.json_types import JsonObject
from research_commander.schema_store import validate_document


class ConversationRegistry:
    """Append-only, cross-cycle claim registry kept outside all model-visible runs."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def claim(self, conversation_id: str, cycle_id: str, role: str) -> None:
        claim_name = sha256_bytes(conversation_id.encode("utf-8")) + ".json"
        write_json_exclusive(
            self.root / claim_name,
            {
                "schema_version": "ConversationClaimV1",
                "conversation_id_hash": claim_name.removesuffix(".json"),
                "research_cycle_id": cycle_id,
                "role": role,
            },
        )


def validate_webgpt_commander_ingress(
    ingress: JsonObject,
    request: JsonObject,
    *,
    registry: ConversationRegistry,
    forbidden_conversation_ids: frozenset[str] = frozenset(),
    now: datetime | None = None,
) -> JsonObject:
    """Validate metadata and return only the structured decision, never a transcript."""
    ingress_schema = (
        "WebGPTCommanderIngressV2"
        if request.get("schema_version") == "research_request_v2"
        else "WebGPTCommanderIngressV1"
    )
    validate_document(ingress, ingress_schema)
    if request.get("selected_commander") != "WEBGPT_SOL_PRO":
        raise ContractError("WebGPT ingress does not match the selected commander")
    conversation_id = ingress.get("conversation_id")
    if not isinstance(conversation_id, str):
        raise ContractError("WebGPT conversation_id is malformed")
    if conversation_id in forbidden_conversation_ids:
        raise ContractError("Scout and Commander must use different conversations")
    created = parse_timestamp(ingress["conversation_created_at"], "conversation_created_at")
    started = parse_timestamp(ingress["request_started_at"], "request_started_at")
    completed = parse_timestamp(ingress["response_completed_at"], "response_completed_at")
    request_created = parse_timestamp(request["created_at"], "request.created_at")
    request_expiry = parse_timestamp(request["expires_at"], "request.expires_at")
    current = (now or datetime.now(UTC)).astimezone(UTC)
    if created < request_created or started < created:
        raise ContractError("WebGPT conversation was not freshly created for this request")
    if completed < started or completed > request_expiry or current > request_expiry:
        raise ContractError("WebGPT response is incomplete, late, or expired")
    binding = ingress.get("request_binding")
    if not isinstance(binding, dict):
        raise ContractError("WebGPT request binding is malformed")
    expected_binding_fields = (*BINDING_FIELDS, "schema_version", "expires_at")
    for field in expected_binding_fields:
        if binding.get(field) != request.get(field):
            raise ContractError(f"WebGPT request binding mismatch: {field}")
    decision_value = ingress.get("research_decision")
    if not isinstance(decision_value, dict):
        raise ContractError("WebGPT research decision is malformed")
    decision: JsonObject = dict(decision_value)
    validate_research_decision(decision, request, now=current)
    cycle_id = request.get("research_cycle_id")
    if not isinstance(cycle_id, str):
        raise ContractError("research_cycle_id is malformed")
    registry.claim(conversation_id, cycle_id, "RESEARCH_COMMANDER")
    return decision
