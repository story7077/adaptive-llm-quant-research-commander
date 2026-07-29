"""Canonical public-host contracts and exact request/output binding."""

from __future__ import annotations

import fnmatch
from copy import deepcopy
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


def request_schema_name(request: JsonObject) -> str:
    version = request.get("schema_version")
    if version == "research_request_v1":
        return "ResearchRequestV1"
    if version == "research_request_v2":
        return "ResearchRequestV2"
    raise ContractError("unsupported Research request schema")


def decision_schema_name(request: JsonObject) -> str:
    return (
        "ResearchDecisionV2"
        if request_schema_name(request) == "ResearchRequestV2"
        else "ResearchDecisionV1"
    )


def proposal_schema_name(proposal: JsonObject) -> str:
    version = proposal.get("schema_version")
    if version == "algorithm_proposal_v1":
        return "AlgorithmProposalV1"
    if version == "algorithm_proposal_v2":
        return "AlgorithmProposalV2"
    raise ContractError("unsupported AlgorithmProposal schema")


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
    payload = (
        _v2_request_hash_payload(request)
        if request.get("schema_version") == "research_request_v2"
        else request
    )
    return contract_hash(
        payload,
        exclude=frozenset({"context_manifest_hash"}),
        timestamp_fields=_REQUEST_TIME_FIELDS,
    )


def _normalized_v2_snapshot(snapshot: JsonObject) -> JsonObject:
    normalized = deepcopy(snapshot)
    for field in ("as_of", "data_available_cutoff", "created_at"):
        if field in normalized:
            normalized[field] = canonical_timestamp(
                normalized[field],
                f"research_memory_snapshot.{field}",
            )
    analogs = normalized.get("nearest_historical_analogs")
    if isinstance(analogs, list):
        for index, value in enumerate(analogs):
            if isinstance(value, dict) and "available_at" in value:
                value["available_at"] = canonical_timestamp(
                    value["available_at"],
                    (
                        "research_memory_snapshot."
                        f"nearest_historical_analogs[{index}].available_at"
                    ),
                )
    return normalized


def _normalized_v2_plan(plan: JsonObject) -> JsonObject:
    normalized = deepcopy(plan)
    if "generated_at" in normalized:
        normalized["generated_at"] = canonical_timestamp(
            normalized["generated_at"],
            "research_action_plan.generated_at",
        )
    return normalized


def _v2_request_hash_payload(request: JsonObject) -> JsonObject:
    payload = deepcopy(request)
    snapshot = payload.get("research_memory_snapshot")
    plan = payload.get("research_action_plan")
    if isinstance(snapshot, dict):
        payload["research_memory_snapshot"] = _normalized_v2_snapshot(
            cast(JsonObject, snapshot)
        )
    if isinstance(plan, dict):
        payload["research_action_plan"] = _normalized_v2_plan(
            cast(JsonObject, plan)
        )
    return payload


def _validate_request_v2_artifacts(request: JsonObject) -> None:
    snapshot_value = request.get("research_memory_snapshot")
    plan_value = request.get("research_action_plan")
    if not isinstance(snapshot_value, dict) or not isinstance(plan_value, dict):
        raise ContractError("ResearchRequestV2 artifacts are malformed")
    snapshot = cast(JsonObject, snapshot_value)
    plan = cast(JsonObject, plan_value)
    validate_document(snapshot, "ResearchMemorySnapshotV1")
    validate_document(plan, "ResearchActionPlanV1")

    normalized_snapshot = _normalized_v2_snapshot(snapshot)
    expected_snapshot_hash = contract_hash(
        normalized_snapshot,
        exclude=frozenset({"snapshot_hash"}),
    )
    if snapshot.get("snapshot_hash") != expected_snapshot_hash:
        raise ContractError("research memory snapshot hash mismatch")
    snapshot_as_of = parse_timestamp(snapshot["as_of"], "memory.as_of")
    snapshot_cutoff = parse_timestamp(
        snapshot["data_available_cutoff"],
        "memory.data_available_cutoff",
    )
    snapshot_created = parse_timestamp(
        snapshot["created_at"],
        "memory.created_at",
    )
    if snapshot_cutoff > snapshot_as_of or snapshot_created < snapshot_as_of:
        raise ContractError("research memory snapshot time ordering is invalid")

    normalized_plan = _normalized_v2_plan(plan)
    expected_plan_hash = contract_hash(
        normalized_plan,
        exclude=frozenset({"plan_hash"}),
    )
    if plan.get("plan_hash") != expected_plan_hash:
        raise ContractError("research action plan hash mismatch")
    context_value = plan.get("context")
    if not isinstance(context_value, dict):
        raise ContractError("research action plan context is malformed")
    context = cast(JsonObject, context_value)
    expected_context_hash = contract_hash(
        context,
        exclude=frozenset({"context_hash"}),
    )
    if (
        context.get("context_hash") != expected_context_hash
        or plan.get("context_hash") != expected_context_hash
    ):
        raise ContractError("research action plan context hash mismatch")
    if plan.get("research_cycle_id") != request.get("research_cycle_id"):
        raise ContractError("research action plan belongs to another cycle")
    if plan.get("research_memory_snapshot_hash") != snapshot.get(
        "snapshot_hash"
    ):
        raise ContractError("research action plan belongs to another snapshot")
    request_created = parse_timestamp(request["created_at"], "created_at")
    if snapshot_created > request_created:
        raise ContractError("research memory snapshot was created after request")
    if parse_timestamp(plan["generated_at"], "plan.generated_at") > request_created:
        raise ContractError("research action plan was generated after request")

    ranked = plan.get("ranked_actions")
    if not isinstance(ranked, list) or not ranked:
        raise ContractError("research action plan ranking is malformed")
    identities: list[str] = []
    ranked_keys: list[tuple[float, str]] = []
    allocated_total = 0
    funded = 0
    for item in ranked:
        if not isinstance(item, dict):
            raise ContractError("research action plan ranking is malformed")
        action = item.get("action_kind")
        score = item.get("score")
        budget = item.get("allocated_submission_budget")
        if (
            not isinstance(action, str)
            or action == "UNKNOWN_LEGACY"
            or not isinstance(score, (int, float))
            or isinstance(score, bool)
            or not isinstance(budget, int)
            or isinstance(budget, bool)
            or budget < 0
        ):
            raise ContractError("research action plan ranking is malformed")
        identities.append(action)
        ranked_keys.append((-float(score), action))
        allocated_total += budget
        funded += int(budget > 0)
    if len(set(identities)) != len(identities):
        raise ContractError("research action plan contains duplicate actions")
    if ranked_keys != sorted(ranked_keys):
        raise ContractError("research action plan ranking is not deterministic")
    maximum_actions = plan.get("maximum_actions")
    maximum_submissions = plan.get("maximum_total_submissions")
    if (
        not isinstance(maximum_actions, int)
        or isinstance(maximum_actions, bool)
        or not isinstance(maximum_submissions, int)
        or isinstance(maximum_submissions, bool)
        or maximum_actions <= 0
        or maximum_submissions <= 0
        or funded > maximum_actions
        or allocated_total != maximum_submissions
    ):
        raise ContractError("research action plan budget arithmetic mismatch")
    budget = request.get("experiment_budget")
    if not isinstance(budget, dict):
        raise ContractError("experiment_budget is malformed")
    limit = budget.get("family_submission_limit")
    used = budget.get("family_submissions_used")
    if (
        not isinstance(limit, int)
        or isinstance(limit, bool)
        or not isinstance(used, int)
        or isinstance(used, bool)
        or maximum_submissions > limit - used
    ):
        raise ContractError(
            "research action plan exceeds the remaining submission budget"
        )


def _validate_algorithm_proposal_v2(
    proposal: JsonObject,
    request: JsonObject,
) -> None:
    if proposal.get("patch_policy_version") != "candidate_patch_policy_v2":
        raise ContractError("AlgorithmProposalV2 patch policy mismatch")
    primary = proposal.get("primary_action_kind")
    if not isinstance(primary, str) or primary == "UNKNOWN_LEGACY":
        raise ContractError("AlgorithmProposalV2 primary action is malformed")
    plan_value = request.get("research_action_plan")
    if not isinstance(plan_value, dict):
        raise ContractError("ResearchRequestV2 action plan is malformed")
    ranked = plan_value.get("ranked_actions")
    if not isinstance(ranked, list):
        raise ContractError("ResearchRequestV2 action plan is malformed")
    permitted: set[str] = set()
    for item in ranked:
        if not isinstance(item, dict):
            continue
        action = item.get("action_kind")
        budget = item.get("allocated_submission_budget")
        if (
            isinstance(action, str)
            and isinstance(budget, int)
            and not isinstance(budget, bool)
            and budget > 0
        ):
            permitted.add(action)
    if primary not in permitted:
        raise ContractError("proposal primary action is outside the action plan")
    secondary = proposal.get("secondary_action_kinds")
    if (
        not isinstance(secondary, list)
        or not all(isinstance(item, str) for item in secondary)
        or len(set(cast(list[str], secondary))) != len(secondary)
        or primary in secondary
        or "UNKNOWN_LEGACY" in secondary
    ):
        raise ContractError("AlgorithmProposalV2 secondary actions are malformed")
    prediction = proposal.get("predicted_portfolio_delta_sharpe")
    if not isinstance(prediction, dict):
        raise ContractError("predicted portfolio delta Sharpe is malformed")
    lower = prediction.get("lower")
    median = prediction.get("median")
    upper = prediction.get("upper")
    if (
        not all(
            isinstance(item, (int, float)) and not isinstance(item, bool)
            for item in (lower, median, upper)
        )
        or not cast(float, lower)
        <= cast(float, median)
        <= cast(float, upper)
    ):
        raise ContractError(
            "predicted portfolio delta Sharpe bounds are not ordered"
        )
    for field in ("mechanism_tags", "predicted_failure_codes"):
        value = proposal.get(field)
        if (
            not isinstance(value, list)
            or not all(isinstance(item, str) for item in value)
            or value != sorted(set(cast(list[str], value)))
        ):
            raise ContractError(f"AlgorithmProposalV2 {field} is not canonical")


def validate_request(
    request: JsonObject,
    evidence_manifest: JsonObject,
    constraints: JsonObject,
    source_snapshot_manifest_hash: str,
    *,
    now: datetime | None = None,
) -> None:
    request_schema = request_schema_name(request)
    validate_document(request, request_schema)
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
    if request_schema == "ResearchRequestV2":
        _validate_request_v2_artifacts(request)
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
                    sources = reference.get("sources")
                    if isinstance(sources, list):
                        for source in sources:
                            if not isinstance(source, dict):
                                continue
                            for key in ("evidence_source_id", "source_id"):
                                source_id = source.get(key)
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
    """Validate a versioned proposal using the public host's semantics."""
    proposal_schema = proposal_schema_name(proposal)
    validate_document(proposal, proposal_schema)
    if proposal.get("proposal_hash") != contract_hash(
        proposal,
        exclude=frozenset({"proposal_hash"}),
    ):
        raise ContractError("proposal_hash mismatch")
    if proposal.get("proposed_strategy_version") == proposal.get(
        "parent_strategy_version"
    ):
        raise ContractError(
            "proposed strategy version must differ from parent strategy version"
        )
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
    if proposal_schema == "AlgorithmProposalV2":
        if request_schema_name(request) != "ResearchRequestV2":
            raise ContractError(
                "AlgorithmProposalV2 requires ResearchRequestV2"
            )
        _validate_algorithm_proposal_v2(proposal, request)

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
    decision_schema = decision_schema_name(request)
    validate_document(decision, decision_schema)
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
    proposal_label = (
        "AlgorithmProposalV2"
        if decision_schema == "ResearchDecisionV2"
        else "AlgorithmProposalV1"
    )
    if decision_kind in PROPOSAL_DECISIONS:
        if not isinstance(proposal, dict):
            raise ContractError(
                f"proposal decision requires {proposal_label}"
            )
        validate_algorithm_proposal(dict(proposal), request, now=current)
    elif proposal is not None:
        raise ContractError(
            f"non-proposal decision cannot include {proposal_label}"
        )
    if decision_schema == "ResearchDecisionV2":
        memory = request.get("research_memory_snapshot")
        plan = request.get("research_action_plan")
        if not isinstance(memory, dict) or not isinstance(plan, dict):
            raise ContractError("ResearchRequestV2 artifacts are malformed")
        if decision.get("research_memory_snapshot_hash") != memory.get(
            "snapshot_hash"
        ):
            raise ContractError("decision memory snapshot binding mismatch")
        if decision.get("research_action_plan_hash") != plan.get("plan_hash"):
            raise ContractError("decision action plan binding mismatch")
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
