from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from research_commander.binding import context_manifest_hash
from research_commander.canonical import hash_json
from research_commander.io import load_json_object
from research_commander.json_types import JsonObject
from research_commander.snapshot import create_clean_snapshot


def _write_json(path: Path, payload: JsonObject) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cycle-id", default="cycle-example-001")
    parser.add_argument("--request-id", default="request-example-001")
    parser.add_argument(
        "--proposal-id",
        default="proposal-example-reversal-v1",
    )
    parser.add_argument(
        "--hypothesis-id",
        default="hypothesis-example-reversal-v1",
    )
    parser.add_argument(
        "--proposed-strategy-version",
        default="1.1.0",
    )
    parser.add_argument(
        "--why-current-model-failed",
        default="The synthetic parent does not represent short-horizon reversal.",
    )
    parser.add_argument("--output-dir", type=Path)
    return parser.parse_args()


def main() -> None:
    args = _arguments()
    root = Path(__file__).resolve().parents[1]
    examples = root / "examples" if args.output_dir is None else args.output_dir.resolve()
    source = examples / "synthetic-source"
    strategy = source / "src" / "trading" / "strategies" / "alpha_v1"
    tests = source / "tests" / "unit"
    config = source / "config" / "strategies"
    strategy.mkdir(parents=True, exist_ok=True)
    tests.mkdir(parents=True, exist_ok=True)
    config.mkdir(parents=True, exist_ok=True)
    (strategy / "model.py").write_text(
        'VERSION = "1.0.0"\n',
        encoding="utf-8",
        newline="\n",
    )
    (tests / "test_alpha_v1.py").write_text(
        "def test_baseline() -> None:\n    assert True\n",
        encoding="utf-8",
        newline="\n",
    )
    (config / "alpha_v1.json").write_text(
        '{"version":"1.0.0"}\n',
        encoding="utf-8",
        newline="\n",
    )
    constraints = load_json_object(root / "config" / "default-constraints.json")
    evidence: JsonObject = {
        "schema_version": "ResearchEvidenceManifestV1",
        "research_cycle_id": args.cycle_id,
        "as_of": "2026-07-27T21:00:00Z",
        "data_available_cutoff": "2026-07-27T20:00:00Z",
        "sources": [
            {
                "evidence_source_id": "official-example-1",
                "source_tier": "TIER_1_OFFICIAL",
                "url_hash": "0" * 64,
                "content_hash": "1" * 64,
                "published_at": "2026-07-27T18:00:00Z",
                "first_available_at": "2026-07-27T18:01:00Z",
                "captured_at": "2026-07-27T19:00:00Z",
                "corroborated": True,
                "contradiction": False,
            }
        ],
    }
    allowlist = constraints.get("snapshot_allowlist")
    if not isinstance(allowlist, list):
        raise ValueError("snapshot_allowlist is malformed")
    with tempfile.TemporaryDirectory(prefix="research-example-") as temporary:
        manifest = create_clean_snapshot(
            source,
            Path(temporary) / "snapshot",
            allowlist=[str(item) for item in allowlist],
        )
    request: JsonObject = {
        "schema_version": "research_request_v1",
        "request_id": args.request_id,
        "research_cycle_id": args.cycle_id,
        "selected_commander": "CODEX_SOL_MAX",
        "commander_selection_id": "commander-selection-example-003",
        "commander_selection_version": 3,
        "created_at": "2026-07-26T19:30:00Z",
        "as_of": "2026-07-27T21:00:00Z",
        "data_available_cutoff": "2026-07-27T20:00:00Z",
        "expires_at": "2099-07-28T20:30:00Z",
        "source_snapshot_commit": "a" * 40,
        "champion_version": "1.0.0",
        "experiment_family": "cross-sectional-alpha",
        "champion_manifest": {
            "strategy_id": "alpha",
            "strategy_version": "1.0.0",
        },
        "active_challenger_manifests": [],
        "strategy_performance_summary": {"common_sessions": 252},
        "failure_case_clusters": [{"cluster_id": "late-reversal"}],
        "regime_summary": {"regimes": ["up", "down", "sideways"]},
        "execution_cost_summary": {"basis_points": 8.0},
        "capacity_summary": {"estimated_usd": 1000000.0},
        "recent_market_evidence": [
            {
                "evidence_source_id": "official-example-1",
                "manifest_hash": "2" * 64,
                "available_at": "2026-07-27T18:01:00Z",
            }
        ],
        "recent_web_research": [],
        "available_data_catalog": {
            "schema_version": "available_data_catalog_v1",
            "catalog_id": "catalog-us-listed-v1",
            "as_of": "2026-07-27T20:00:00Z",
            "data_available_cutoff": "2026-07-27T20:00:00Z",
            "instruments": [
                {
                    "symbol": "EXPA",
                    "asset_class": "US_EQUITY",
                    "first_available_at": "2020-01-01T00:00:00Z",
                    "last_available_at": None,
                    "point_in_time_membership_available": True,
                    "daily_history_sessions": 1500,
                    "intraday_history_sessions": 500,
                    "execution_supported": True,
                    "research_tags": ["synthetic"],
                },
                {
                    "symbol": "EXPB",
                    "asset_class": "US_ETF",
                    "first_available_at": "2020-01-01T00:00:00Z",
                    "last_available_at": None,
                    "point_in_time_membership_available": True,
                    "daily_history_sessions": 1500,
                    "intraday_history_sessions": 500,
                    "execution_supported": True,
                    "research_tags": ["synthetic"],
                },
            ],
            "dataset_versions": {
                "pit-adjusted-bars": "bars-v1",
                "oos-lockbox": "lockbox-v1",
            },
            "catalog_hash": "3" * 64,
        },
        "allowed_change_scope": constraints["builder_allowed_paths"],
        "forbidden_change_scope": constraints["builder_forbidden_paths"],
        "experiment_budget": {
            "family_submission_limit": 10,
            "family_submissions_used": 2,
            "oos_budget_limit": 3,
            "oos_budget_used": 1,
        },
        "context_manifest_hash": "0" * 64,
    }
    request["context_manifest_hash"] = context_manifest_hash(
        request,
        evidence,
        constraints,
        hash_json(manifest),
    )
    proposal: JsonObject = {
        "schema_version": "algorithm_proposal_v1",
        "proposal_id": args.proposal_id,
        "hypothesis_id": args.hypothesis_id,
        "hypothesis": (
            "A point-in-time five-session cross-sectional reversal feature may "
            "improve matched net returns."
        ),
        "economic_mechanism": (
            "Temporary liquidity imbalance may mean-revert after conservative costs."
        ),
        "why_current_model_failed": args.why_current_model_failed,
        "parent_strategy_id": "alpha",
        "parent_strategy_version": "1.0.0",
        "proposed_strategy_id": "alpha",
        "proposed_strategy_version": args.proposed_strategy_version,
        "target_horizon": "five completed sessions",
        "target_universe": ["EXPA", "EXPB"],
        "required_data": ["pit-adjusted-bars"],
        "feature_changes": ["Add a versioned five-session reversal feature."],
        "signal_formula_changes": ["Rank the bounded reversal feature cross-sectionally."],
        "entry_rule_changes": [
            "Enter only after a completed session and never open a short position."
        ],
        "exit_rule_changes": ["Exit after five completed sessions or invalidation."],
        "position_sizing_changes": [
            "Use capped long-only inverse-volatility sizing with residual USD cash."
        ],
        "regime_activation_changes": [],
        "calibration_changes": [],
        "expected_edge_source": "Liquidity-provision premium.",
        "expected_failure_modes": ["Persistent news trends can prevent mean reversion."],
        "invalidation_conditions": ["Net OOS effect is below the declared minimum."],
        "placebo_tests": ["Shift the signal date by one completed session."],
        "stress_tests": ["Apply three times modeled execution cost."],
        "minimum_economic_effect": {
            "metric": "annualized_net_return_difference",
            "threshold": 0.01,
            "comparison": "matched parent strategy",
        },
        "estimated_capacity": {"usd": 1000000.0},
        "estimated_turnover": {"one_way_daily": 0.25},
        "estimated_cost_sensitivity": {
            "cost_1x": 0.01,
            "cost_2x": 0.0,
            "cost_3x": -0.02,
        },
        "files_allowed_to_change": [
            "src/trading/strategies/alpha_v2/**",
            "tests/unit/**",
        ],
        "tests_required": ["PIT, date-shift placebo, and exact CandidateDecision ABI tests"],
        "evidence_source_ids": ["official-example-1"],
        "raw_confidence": 0.55,
        "proposal_hash": "0" * 64,
    }
    proposal["proposal_hash"] = hash_json(
        {key: value for key, value in proposal.items() if key != "proposal_hash"}
    )
    _write_json(examples / "research-request.example.json", request)
    _write_json(examples / "evidence-manifest.example.json", evidence)
    _write_json(examples / "approved-proposal.example.json", proposal)


if __name__ == "__main__":
    main()
