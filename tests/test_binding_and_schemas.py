from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime

import pytest
from conftest import Bundle

from research_commander.binding import (
    BINDING_FIELDS,
    validate_algorithm_proposal,
    validate_request,
    verify_output_binding,
)
from research_commander.canonical import hash_json
from research_commander.errors import ContractError, SchemaContractError
from research_commander.json_types import JsonObject
from research_commander.schema_store import validate_document
from research_commander.snapshot import create_clean_snapshot


def test_request_context_hash_and_expiry_are_fail_closed(
    bundle: Bundle, tmp_path_factory: pytest.TempPathFactory
) -> None:
    preview = tmp_path_factory.mktemp("binding") / "snapshot"
    allowlist_value = bundle.constraints.get("snapshot_allowlist")
    assert isinstance(allowlist_value, list)
    assert all(isinstance(item, str) for item in allowlist_value)
    manifest = create_clean_snapshot(
        bundle.source,
        preview,
        allowlist=[item for item in allowlist_value if isinstance(item, str)],
    )
    validate_request(
        bundle.request,
        bundle.evidence,
        bundle.constraints,
        hash_json(manifest),
        now=datetime(2026, 7, 27, 21, tzinfo=UTC),
    )
    changed = deepcopy(bundle.request)
    changed["strategy_performance_summary"] = {"common_sessions": 999}
    with pytest.raises(ContractError, match="context_manifest_hash mismatch"):
        validate_request(
            changed,
            bundle.evidence,
            bundle.constraints,
            hash_json(manifest),
            now=datetime(2026, 7, 27, 21, tzinfo=UTC),
        )
    with pytest.raises(ContractError, match="expired"):
        validate_request(
            bundle.request,
            bundle.evidence,
            bundle.constraints,
            hash_json(manifest),
            now=datetime(2100, 1, 1, tzinfo=UTC),
        )


def test_stale_commander_selection_is_rejected(bundle: Bundle) -> None:
    output: JsonObject = {field: bundle.request[field] for field in BINDING_FIELDS}
    output["selected_commander"] = "WEBGPT_SOL_PRO"
    with pytest.raises(ContractError, match="stale commander selection"):
        verify_output_binding(
            output,
            bundle.request,
            now=datetime(2026, 7, 27, 21, tzinfo=UTC),
        )


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("commander_selection_id", "commander-selection-example-002"),
        ("commander_selection_version", 2),
    ],
)
def test_exact_commander_selection_record_mismatch_is_rejected_as_stale(
    bundle: Bundle,
    field: str,
    replacement: object,
) -> None:
    output: JsonObject = {
        binding_field: bundle.request[binding_field] for binding_field in BINDING_FIELDS
    }
    output[field] = replacement  # type: ignore[assignment]
    with pytest.raises(ContractError, match=rf"stale commander selection: {field} mismatch"):
        verify_output_binding(
            output,
            bundle.request,
            now=datetime(2026, 7, 27, 21, tzinfo=UTC),
        )


@pytest.mark.parametrize(
    "field",
    ["commander_selection_id", "commander_selection_version"],
)
def test_missing_commander_selection_binding_is_rejected(
    bundle: Bundle,
    field: str,
) -> None:
    output: JsonObject = {
        binding_field: bundle.request[binding_field] for binding_field in BINDING_FIELDS
    }
    del output[field]
    with pytest.raises(
        ContractError,
        match=rf"stale commander selection: output binding is missing {field}",
    ):
        verify_output_binding(
            output,
            bundle.request,
            now=datetime(2026, 7, 27, 21, tzinfo=UTC),
        )


@pytest.mark.parametrize(
    "field",
    ["commander_selection_id", "commander_selection_version"],
)
def test_request_schema_requires_exact_commander_selection_record(
    bundle: Bundle,
    field: str,
) -> None:
    invalid = deepcopy(bundle.request)
    del invalid[field]
    with pytest.raises(SchemaContractError, match=field):
        validate_document(invalid, "ResearchRequestV1")


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("commander_selection_id", "commander-selection-example-004"),
        ("commander_selection_version", 4),
    ],
)
def test_selection_record_is_part_of_context_manifest_hash(
    bundle: Bundle,
    tmp_path_factory: pytest.TempPathFactory,
    field: str,
    replacement: object,
) -> None:
    preview = tmp_path_factory.mktemp("selection-binding") / "snapshot"
    allowlist_value = bundle.constraints.get("snapshot_allowlist")
    assert isinstance(allowlist_value, list)
    manifest = create_clean_snapshot(
        bundle.source,
        preview,
        allowlist=[str(item) for item in allowlist_value],
    )
    changed = deepcopy(bundle.request)
    changed[field] = replacement  # type: ignore[assignment]
    with pytest.raises(ContractError, match="context_manifest_hash mismatch"):
        validate_request(
            changed,
            bundle.evidence,
            bundle.constraints,
            hash_json(manifest),
            now=datetime(2026, 7, 27, 21, tzinfo=UTC),
        )


def test_generic_us_equity_and_etf_universe_is_catalog_bounded(
    bundle: Bundle, proposal: JsonObject
) -> None:
    validate_algorithm_proposal(
        proposal,
        bundle.request,
        now=datetime(2026, 7, 27, 21, tzinfo=UTC),
    )
    invalid = deepcopy(proposal)
    target = invalid["target_universe"]
    assert isinstance(target, list)
    invalid["target_universe"] = ["MISSING"]
    invalid["proposal_hash"] = hash_json(
        {key: value for key, value in invalid.items() if key != "proposal_hash"}
    )
    with pytest.raises(ContractError, match="outside the versioned data catalog"):
        validate_algorithm_proposal(
            invalid,
            bundle.request,
            now=datetime(2026, 7, 27, 21, tzinfo=UTC),
        )


def test_algorithm_proposal_cannot_expand_to_risk_code(
    bundle: Bundle, proposal: JsonObject
) -> None:
    invalid = deepcopy(proposal)
    invalid["files_allowed_to_change"] = ["src/trading/risk/**"]
    invalid["proposal_hash"] = hash_json(
        {key: value for key, value in invalid.items() if key != "proposal_hash"}
    )
    with pytest.raises(ContractError, match=r"allowed change scope|forbidden"):
        validate_algorithm_proposal(
            invalid,
            bundle.request,
            now=datetime(2026, 7, 27, 21, tzinfo=UTC),
        )


def test_schema_rejects_unknown_fields(bundle: Bundle) -> None:
    invalid = deepcopy(bundle.request)
    invalid["hidden_transcript"] = "not allowed"
    with pytest.raises(SchemaContractError):
        validate_document(invalid, "ResearchRequestV1")
