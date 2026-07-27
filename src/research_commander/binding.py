"""Canonical public-host contracts and exact request/output binding."""

from __future__ import annotations

import fnmatch
from datetime import UTC, datetime
from typing import cast

from research_commander.canonical import hash_json
from research_commander.errors import ContractError
from research_commander.json_types import JsonObject, JsonValue
from research_commander.schema_store import validate_document

BINDING_FIELDS = (
    "request_id",
    "research_cycle_id",
    "context_manifest_hash",
    "source_snapshot_commit",
    "champion_version",
    "experiment_family",
    "selected_commander",
    "commander_selection_id",
    "commander_selection_version",
)
COMMANDER_SELECTION_BINDING_FIELDS = (
    "selected_commander",
    "commander_selection_id",
    "commander_selection_version",
)
PROPOSAL_DECISIONS = frozenset(
    {
        "PROPOSE_NEW_STRATEGY",
        "PROPOSE_STRATEGY_REVISION",
        "PROPOSE_FEATURE_REVISION",
        "PROPOSE_CALIBRATION_REVISION",
    }
)
_REQUEST_TIME_FIELDS = (
    "created_at",
    "as_of",
    "data_available_cutoff",
    "expires_at",
)
_DECISION_TIME_FIELDS = ("request_expires_at", "created_at")
COMMANDER_CREATED_AT_SENTINEL = "RUNTIME_BOUND_BY_HOST"
COMMANDER_HASH_SENTINEL = "HOST_COMPUTES_SHA256"


def _scope_within(proposed: str, allowed: str) -> bool:
    if proposed == allowed or fnmatch.fnmatchcase(proposed, allowed):
        return True
    allowed_prefix = allowed.split("*", maxsplit=1)[0]
    proposed_prefix = proposed.split("*", maxsplit=1)[0]
    return bool(allowed_prefix) and proposed_prefix.startswith(allowed_prefix)


def _scopes_overlap(first: str, second: str) -> bool:
    return _scope_within(first, second) or _scope_within(second, first)


def parse_timestamp(value: JsonValue, field: str) -> datetime:
    if not isinstance(value, str):
        raise ContractError(f"{field} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ContractError(f"{field} must include a timezone")
    return parsed.astimezone(UTC)


def canonical_timestamp(value: JsonValue, field: str) -> str:
    """Render timestamps exactly as the public Pydantic contract hashes them."""
    return parse_timestamp(value, field).isoformat(timespec="microseconds").replace("+00:00", "Z")


def contract_hash(
    document: JsonObject,
    *,
    exclude: frozenset[str] = frozenset(),
    timestamp_fields: tuple[str, ...] = (),
) -> str:
    payload: JsonObject = {key: value for key, value in document.items() if key not in exclude}
    for field in timestamp_fields:
        if field in payload:
            payload[field] = canonical_timestamp(payload[field], field)
    return hash_json(payload)


def context_manifest_hash(
    request: JsonObject,
    evidence_manifest: JsonObject | None = None,
    constraints: JsonObject | None = None,
    source_snapshot_manifest_hash: str | None = None,
) -> str:
    """Hash the canonical ResearchRequestV1 payload.

    The extra arguments remain accepted for source compatibility with older
    callers, but the public host's canonical request hash is intentionally the
    request payload alone. Evidence, constraints, and snapshot bytes are
    independently sealed by the run manifest and Builder context.
    """
    del evidence_manifest, constraints, source_snapshot_manifest_hash
    return contract_hash(
        request,
        exclude=frozenset({"context_manifest_hash"}),
        timestamp_fields=_REQUEST_TIME_FIELDS,
    )


def validate_request(
    request: JsonObject,
    evidence_manifest: JsonObject,
    constraints: JsonObject,
    source_snapshot_manifest_hash: str,
    *,
    now: datetime | None = None,
) -> None:
    validate_document(request, "ResearchRequestV1")
    validate_document(evidence_manifest, "ResearchEvidenceManifestV1")
    current = (now or datetime.now(UTC)).astimezone(UTC)
    created_at = parse_timestamp(request["created_at"], "created_at")
    as_of = parse_timestamp(request["as_of"], "as_of")
    cutoff = parse_timestamp(request["data_available_cutoff"], "data_available_cutoff")
    expires_at = parse_timestamp(request["expires_at"], "expires_at")
    if not created_at <= cutoff <= as_of:
        raise ContractError("request time ordering is invalid")
    if expires_at <= created_at:
        raise ContractError("expires_at must follow created_at")
    if current >= expires_at:
        raise ContractError("research request is expired")
    if evidence_manifest.get("research_cycle_id") != request.get("research_cycle_id"):
        raise ContractError("evidence manifest is bound to another research cycle")
    if evidence_manifest.get("data_available_cutoff") != request.get("data_available_cutoff"):
        raise ContractError("evidence and request data cutoffs differ")
    sources = evidence_manifest.get("sources")
    if isinstance(sources, list):
        for index, source in enumerate(sources):
            if not isinstance(source, dict):
                raise ContractError(f"evidence source {index} is malformed")
            available_at = parse_timestamp(
                source.get("first_available_at"),
                "first_available_at",
            )
            if available_at > cutoff:
                raise ContractError("evidence became available after the request cutoff")
    expected_hash = context_manifest_hash(request)
    if request.get("context_manifest_hash") != expected_hash:
        raise ContractError("context_manifest_hash mismatch")
    if len(source_snapshot_manifest_hash) != 64:
        raise ContractError("source snapshot manifest hash is malformed")
    budget = request.get("experiment_budget")
    if not isinstance(budget, dict):
        raise ContractError("experiment_budget is malformed")
    submission_limit = budget.get("family_submission_limit")
    submission_used = budget.get("family_submissions_used")
    oos_limit = budget.get("oos_budget_limit")
    oos_used = budget.get("oos_budget_used")
    if (
        not isinstance(submission_limit, int)
        or not isinstance(submission_used, int)
        or submission_used >= submission_limit
    ):
        raise ContractError("experiment-family submission budget is exhausted")
    if not isinstance(oos_limit, int) or not isinstance(oos_used, int) or oos_used > oos_limit:
        raise ContractError("OOS budget accounting is invalid")


def request_binding(request: JsonObject) -> JsonObject:
    result: JsonObject = {}
    for field in BINDING_FIELDS:
        value = request.get(field)
        if value is None:
            raise ContractError(f"request binding is missing {field}")
        result[field] = value
    return result


def verify_output_binding(
    output: JsonObject,
    request: JsonObject,
    *,
    now: datetime | None = None,
) -> None:
    for field in BINDING_FIELDS:
        if field not in request or request[field] is None:
            raise ContractError(f"request binding is missing {field}")
        if field not in output or output[field] is None:
            if field in COMMANDER_SELECTION_BINDING_FIELDS:
                raise ContractError(f"stale commander selection: output binding is missing {field}")
            raise ContractError(f"output binding is missing {field}")
        if output.get(field) != request.get(field):
            if field in COMMANDER_SELECTION_BINDING_FIELDS:
                raise ContractError(f"stale commander selection: {field} mismatch")
            raise ContractError(f"output binding mismatch: {field}")
    expires_at = parse_timestamp(request["expires_at"], "expires_at")
    if (now or datetime.now(UTC)).astimezone(UTC) >= expires_at:
        raise ContractError("output arrived after request expiry")


def _permitted_evidence_ids(request: JsonObject) -> set[str]:
    permitted: set[str] = set()
    for field in ("recent_market_evidence", "recent_web_research"):
        references = request.get(field)
        if isinstance(references, list):
            for reference in references:
                if isinstance(reference, dict):
                    for key in ("evidence_source_id", "source_id"):
                        source_id = reference.get(key)
                        if isinstance(source_id, str):
                            permitted.add(source_id)
    return permitted


def _available_instruments(request: JsonObject) -> dict[str, JsonObject]:
    catalog = request.get("available_data_catalog")
    if not isinstance(catalog, dict):
        raise ContractError("available data catalog is malformed")
    instruments = catalog.get("instruments")
    if not isinstance(instruments, list):
        raise ContractError("available data catalog instruments are malformed")
    by_symbol: dict[str, JsonObject] = {}
    for item in instruments:
        if not isinstance(item, dict):
            raise ContractError("available data catalog instrument is malformed")
        symbol = item.get("symbol")
        if not isinstance(symbol, str) or symbol in by_symbol:
            raise ContractError("available data catalog symbol is malformed or duplicated")
        by_symbol[symbol] = dict(item)
    return by_symbol


def validate_algorithm_proposal(
    proposal: JsonObject,
    request: JsonObject,
    *,
    now: datetime | None = None,
) -> None:
    """Validate AlgorithmProposalV1 using the public host's canonical semantics."""
    validate_document(proposal, "AlgorithmProposalV1")
    if proposal.get("proposal_hash") != contract_hash(
        proposal,
        exclude=frozenset({"proposal_hash"}),
    ):
        raise ContractError("proposal_hash mismatch")
    if proposal.get("proposed_strategy_id") == proposal.get("parent_strategy_id") and proposal.get(
        "proposed_strategy_version"
    ) == proposal.get("parent_strategy_version"):
        raise ContractError("a proposal cannot overwrite its parent strategy version")
    if (now or datetime.now(UTC)).astimezone(UTC) >= parse_timestamp(
        request["expires_at"], "request.expires_at"
    ):
        raise ContractError("research request is expired")
    champion_manifest = request.get("champion_manifest")
    if isinstance(champion_manifest, dict):
        champion_strategy_id = champion_manifest.get("strategy_id")
        if (
            isinstance(champion_strategy_id, str)
            and proposal.get("parent_strategy_id") != champion_strategy_id
        ):
            raise ContractError("proposal parent strategy does not match the current Champion")
    if proposal.get("parent_strategy_version") != request.get("champion_version"):
        raise ContractError("proposal parent version does not match the current Champion")

    universe = proposal.get("target_universe")
    if not isinstance(universe, list) or not all(isinstance(item, str) for item in universe):
        raise ContractError("proposal target_universe is malformed")
    universe_symbols = cast(list[str], universe)
    instruments = _available_instruments(request)
    missing = sorted(symbol for symbol in universe_symbols if symbol not in instruments)
    if missing:
        raise ContractError(
            "target universe is outside the versioned data catalog: " + ",".join(missing)
        )
    data_failures: list[str] = []
    unsupported: list[str] = []
    for symbol in universe_symbols:
        instrument = instruments[symbol]
        history_sessions = instrument.get("daily_history_sessions")
        if not isinstance(history_sessions, int) or history_sessions <= 0:
            data_failures.append(f"{symbol}:NO_DAILY_HISTORY")
        if (
            instrument.get("asset_class") == "US_EQUITY"
            and instrument.get("point_in_time_membership_available") is not True
        ):
            data_failures.append(f"{symbol}:NO_PIT_MEMBERSHIP")
        if instrument.get("execution_supported") is not True:
            unsupported.append(symbol)
    if data_failures:
        raise ContractError(
            "target universe lacks mandatory research data: " + ",".join(data_failures)
        )
    if unsupported:
        raise ContractError("shadow execution is unsupported for: " + ",".join(sorted(unsupported)))

    allowed_scope = request.get("allowed_change_scope")
    proposal_scope = proposal.get("files_allowed_to_change")
    if not isinstance(allowed_scope, list) or not isinstance(proposal_scope, list):
        raise ContractError("proposal change scope is malformed")
    if not all(
        isinstance(proposed, str)
        and any(
            isinstance(allowed, str) and _scope_within(proposed, allowed)
            for allowed in allowed_scope
        )
        for proposed in proposal_scope
    ):
        raise ContractError("proposal expands the request's allowed change scope")
    forbidden_scope = request.get("forbidden_change_scope")
    if not isinstance(forbidden_scope, list):
        raise ContractError("request forbidden change scope is malformed")
    if any(
        isinstance(proposed, str)
        and isinstance(forbidden, str)
        and _scopes_overlap(proposed, forbidden)
        for proposed in proposal_scope
        for forbidden in forbidden_scope
    ):
        raise ContractError("proposal intersects the forbidden change scope")
    permitted_evidence_ids = _permitted_evidence_ids(request)
    proposal_evidence = proposal.get("evidence_source_ids")
    if not isinstance(proposal_evidence, list) or not all(
        isinstance(item, str) and item in permitted_evidence_ids for item in proposal_evidence
    ):
        raise ContractError("proposal cites evidence outside the bounded request")


def validate_research_decision(
    decision: JsonObject,
    request: JsonObject,
    *,
    now: datetime | None = None,
) -> None:
    """Validate the canonical decision contract after trusted-host hashing."""
    validate_document(decision, "ResearchDecisionV1")
    current = (now or datetime.now(UTC)).astimezone(UTC)
    verify_output_binding(decision, request, now=current)
    if decision.get("request_schema_version") != request.get("schema_version"):
        raise ContractError("research decision request_schema_version mismatch")
    if decision.get("request_expires_at") != request.get("expires_at"):
        raise ContractError("research decision expiry is not bound to its request")
    created_at = parse_timestamp(decision["created_at"], "decision.created_at")
    request_created_at = parse_timestamp(request["created_at"], "request.created_at")
    if not request_created_at <= created_at <= current:
        raise ContractError("research decision created_at is outside its receipt interval")
    if decision.get("output_hash") != contract_hash(
        decision,
        exclude=frozenset({"output_hash"}),
        timestamp_fields=_DECISION_TIME_FIELDS,
    ):
        raise ContractError("output_hash mismatch")
    decision_kind = decision.get("decision")
    proposal = decision.get("proposal")
    requested_evidence = decision.get("requested_evidence")
    if decision_kind in PROPOSAL_DECISIONS:
        if not isinstance(proposal, dict):
            raise ContractError("proposal decision requires AlgorithmProposalV1")
        validate_algorithm_proposal(dict(proposal), request, now=current)
    elif proposal is not None:
        raise ContractError("non-proposal decision cannot include AlgorithmProposalV1")
    if decision_kind == "REQUEST_MORE_EVIDENCE":
        if not isinstance(requested_evidence, list) or not requested_evidence:
            raise ContractError("REQUEST_MORE_EVIDENCE requires requested_evidence")
    elif requested_evidence:
        raise ContractError("requested_evidence is limited to REQUEST_MORE_EVIDENCE")


def finalize_commander_output(
    model_output: JsonObject,
    request: JsonObject,
    *,
    received_at: datetime | None = None,
) -> JsonObject:
    """Replace model placeholders with trusted host time and canonical hashes."""
    if model_output.get("created_at") != COMMANDER_CREATED_AT_SENTINEL:
        validate_research_decision(model_output, request, now=received_at)
        return model_output
    if model_output.get("output_hash") != COMMANDER_HASH_SENTINEL:
        raise ContractError("output_hash must be computed by the trusted host")
    output: JsonObject = dict(model_output)
    proposal_value = output.get("proposal")
    if proposal_value is not None:
        if not isinstance(proposal_value, dict):
            raise ContractError("proposal must be a JSON object")
        proposal: JsonObject = dict(proposal_value)
        if proposal.get("proposal_hash") != COMMANDER_HASH_SENTINEL:
            raise ContractError("proposal_hash must be computed by the trusted host")
        proposal["proposal_hash"] = contract_hash(
            proposal,
            exclude=frozenset({"proposal_hash"}),
        )
        output["proposal"] = proposal
    captured_at = (received_at or datetime.now(UTC)).astimezone(UTC)
    output["created_at"] = captured_at.isoformat(timespec="microseconds").replace("+00:00", "Z")
    output["output_hash"] = contract_hash(
        output,
        exclude=frozenset({"output_hash"}),
        timestamp_fields=_DECISION_TIME_FIELDS,
    )
    validate_research_decision(output, request, now=captured_at)
    return output
