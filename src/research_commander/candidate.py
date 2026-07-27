"""Deterministic patch generation and Challenger manifest construction."""

from __future__ import annotations

import difflib
from pathlib import Path

from research_commander.binding import contract_hash, validate_algorithm_proposal
from research_commander.canonical import hash_file, hash_json, hash_tree, sha256_bytes
from research_commander.errors import ContractError
from research_commander.json_types import JsonObject, JsonValue
from research_commander.patch_policy import PatchValidation, validate_candidate_patch
from research_commander.schema_store import validate_document

MANDATORY_FALSIFICATION_TESTS = (
    "future_data_leakage",
    "pit_constituent_leakage",
    "revised_data_backfill_leakage",
    "survivor_bias",
    "lookahead_bias",
    "parameter_instability",
    "date_shift_placebo",
    "signal_direction_inversion_placebo",
    "symbol_label_shuffle",
    "single_symbol_or_month_dependence",
    "top_five_trades_removed",
    "cost_stress_1x_2x_3x",
    "execution_delay_stress",
    "spread_widening_stress",
    "liquidity_capacity_stress",
    "market_beta_neutralization",
    "sector_beta_neutralization",
    "known_factor_neutralization",
    "regime_split",
    "parameter_neighborhood_stability",
    "partial_data_removal_sensitivity",
    "experiment_budget",
)


def _regular_text_files(root: Path) -> dict[str, Path]:
    files: dict[str, Path] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ContractError(f"candidate tree contains a symlink: {path}")
        if path.is_file():
            relative = path.relative_to(root).as_posix()
            files[relative] = path
    return files


def deterministic_patch(base_root: Path, candidate_root: Path) -> str:
    """Create a deterministic text-only unified patch without Git history."""
    base = base_root.resolve(strict=True)
    candidate = candidate_root.resolve(strict=True)
    base_files = _regular_text_files(base)
    candidate_files = _regular_text_files(candidate)
    sections: list[str] = []
    for relative in sorted(base_files.keys() | candidate_files.keys()):
        old_path = base_files.get(relative)
        new_path = candidate_files.get(relative)
        try:
            old_lines = (
                old_path.read_text(encoding="utf-8").splitlines(keepends=True)
                if old_path is not None
                else []
            )
            new_lines = (
                new_path.read_text(encoding="utf-8").splitlines(keepends=True)
                if new_path is not None
                else []
            )
        except UnicodeError as exc:
            raise ContractError(f"candidate patch contains a non-UTF-8 file: {relative}") from exc
        if old_lines == new_lines:
            continue
        sections.append(f"diff --git a/{relative} b/{relative}\n")
        sections.extend(
            difflib.unified_diff(
                old_lines,
                new_lines,
                fromfile=f"a/{relative}" if old_path is not None else "/dev/null",
                tofile=f"b/{relative}" if new_path is not None else "/dev/null",
                lineterm="\n",
            )
        )
    return "".join(sections).replace("\r\n", "\n")


def _hash_selected_files(root: Path, prefixes: tuple[str, ...]) -> str:
    records: list[JsonValue] = []
    for path in sorted(root.rglob("*")):
        if path.is_file():
            relative = path.relative_to(root).as_posix()
            if relative.startswith(prefixes):
                records.append({"path": relative, "sha256": hash_file(path)})
    return hash_json(records)


def build_challenger_manifest(
    *,
    request: JsonObject,
    proposal: JsonObject,
    patch: str,
    candidate_root: Path,
    test_manifest: JsonObject,
    protected_champion_paths: tuple[str, ...] = (),
) -> tuple[JsonObject, PatchValidation, JsonObject]:
    validate_algorithm_proposal(proposal, request)
    validation = validate_candidate_patch(
        patch,
        proposal,
        protected_champion_paths=protected_champion_paths,
    )
    normalized_patch = patch.replace("\r\n", "\n").encode("utf-8")
    patch_hash = sha256_bytes(normalized_patch)
    proposal_hash = proposal.get("proposal_hash")
    if not isinstance(proposal_hash, str):
        raise ContractError("proposal has no canonical proposal_hash")
    code_hash = hash_tree(candidate_root)
    config_hash = _hash_selected_files(candidate_root, ("config/",))
    test_manifest_hash = hash_json(test_manifest)
    identity: JsonObject = {
        "proposal_hash": proposal_hash,
        "patch_hash": patch_hash,
        "code_hash": code_hash,
        "source_commit": request["source_snapshot_commit"],
    }
    challenger_id = "challenger-" + hash_json(identity)[:24]
    target_universe = proposal.get("target_universe")
    if not isinstance(target_universe, list) or not all(
        isinstance(item, str) for item in target_universe
    ):
        raise ContractError("proposal execution universe is malformed")
    manifest: JsonObject = {
        "schema_version": "challenger_manifest_v1",
        "challenger_id": challenger_id,
        "strategy_id": proposal["proposed_strategy_id"],
        "strategy_version": proposal["proposed_strategy_version"],
        "parent_version": proposal["parent_strategy_version"],
        "hypothesis_id": proposal["hypothesis_id"],
        "experiment_family": request["experiment_family"],
        "source_commit": request["source_snapshot_commit"],
        "patch_hash": patch_hash,
        "proposal_hash": proposal_hash,
        "code_hash": code_hash,
        "config_hash": config_hash,
        "test_manifest_hash": test_manifest_hash,
        "created_by_commander": request["selected_commander"],
        "implemented_by_builder": "CODEX_SOL_MAX",
        "evidence_source_ids": proposal["evidence_source_ids"],
        "required_data": proposal["required_data"],
        "decision_horizon": proposal["target_horizon"],
        "execution_universe": target_universe,
        "estimated_turnover": proposal["estimated_turnover"],
        "estimated_capacity": proposal["estimated_capacity"],
        "status": "PROPOSED",
        "created_at": request["created_at"],
        "manifest_hash": "0" * 64,
    }
    manifest["manifest_hash"] = contract_hash(
        manifest,
        exclude=frozenset({"manifest_hash"}),
        timestamp_fields=("created_at",),
    )
    validate_document(manifest, "ChallengerManifestV1")
    validation_request: JsonObject = {
        "schema_version": "ValidationRequestV1",
        "challenger_id": challenger_id,
        "candidate_manifest_hash": manifest["manifest_hash"],
        "mandatory_test_ids": list(MANDATORY_FALSIFICATION_TESTS),
        "lockbox_request": {
            "experiment_family": request["experiment_family"],
            "hypothesis_id": proposal["hypothesis_id"],
            "requested_budget_units": 1,
        },
        "raw_oos_access_permitted": False,
        "automatic_promotion_permitted": False,
    }
    validate_document(validation_request, "ValidationRequestV1")
    return manifest, validation, validation_request
