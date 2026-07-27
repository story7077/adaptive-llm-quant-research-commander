"""Immutable per-cycle directory preparation."""

from __future__ import annotations

import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from research_commander.binding import validate_request
from research_commander.canonical import hash_json
from research_commander.errors import IsolationError
from research_commander.io import write_json_exclusive, write_text_exclusive
from research_commander.json_types import JsonObject
from research_commander.schema_store import schema_path, structured_output_schema
from research_commander.snapshot import create_clean_snapshot

SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


@dataclass(frozen=True)
class RunLayout:
    root: Path

    @property
    def request(self) -> Path:
        return self.root / "request"

    @property
    def input(self) -> Path:
        return self.root / "input"

    @property
    def work(self) -> Path:
        return self.root / "work"

    @property
    def output(self) -> Path:
        return self.root / "output"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def source_snapshot(self) -> Path:
        return self.input / "clean_source_snapshot"

    def resolve_inside(self, relative: str) -> Path:
        candidate = (self.root / relative).resolve()
        root = self.root.resolve(strict=True)
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise IsolationError(f"path escapes current run: {relative}") from exc
        return candidate


def prepare_run(
    runs_root: Path,
    *,
    request: JsonObject,
    evidence_manifest: JsonObject,
    constraints: JsonObject,
    source_root: Path,
    agents_text: str,
) -> RunLayout:
    cycle_id_value = request.get("research_cycle_id")
    if not isinstance(cycle_id_value, str) or SAFE_ID.fullmatch(cycle_id_value) is None:
        raise IsolationError("unsafe research_cycle_id")
    runs = runs_root.resolve()
    runs.mkdir(parents=True, exist_ok=True)
    final_root = runs / cycle_id_value
    if final_root.exists():
        raise IsolationError("research cycle already exists and cannot be reused")
    temporary_root = Path(
        tempfile.mkdtemp(prefix=f".{cycle_id_value}.preparing-", dir=runs)
    ).resolve()
    try:
        layout = RunLayout(temporary_root)
        layout.request.mkdir()
        (layout.request / "invocations").mkdir()
        layout.input.mkdir()
        layout.work.mkdir()
        (layout.work / "commander").mkdir()
        (layout.work / "builder").mkdir()
        layout.output.mkdir()
        layout.logs.mkdir()
        allowlist_value = constraints.get("snapshot_allowlist")
        if not isinstance(allowlist_value, list) or not all(
            isinstance(item, str) for item in allowlist_value
        ):
            raise IsolationError("constraints.snapshot_allowlist must be a string array")
        allowlist = [item for item in allowlist_value if isinstance(item, str)]
        source_manifest = create_clean_snapshot(
            source_root,
            layout.source_snapshot,
            allowlist=allowlist,
        )
        source_manifest_hash = hash_json(source_manifest)
        validate_request(
            request,
            evidence_manifest,
            constraints,
            source_manifest_hash,
        )
        write_json_exclusive(layout.request / "research_request.json", request)
        write_json_exclusive(layout.request / "evidence_manifest.json", evidence_manifest)
        write_json_exclusive(layout.request / "constraints.json", constraints)
        write_json_exclusive(
            layout.request / "source_snapshot_manifest.json",
            source_manifest,
        )
        commander_output_schema = structured_output_schema("ResearchDecisionV1")
        write_json_exclusive(
            layout.request / "output.schema.json",
            commander_output_schema,
        )
        write_json_exclusive(
            layout.request / "commander-output.schema.json",
            commander_output_schema,
        )
        write_json_exclusive(
            layout.request / "builder-output.schema.json",
            structured_output_schema("CandidateBuildResultV1"),
        )
        write_text_exclusive(
            layout.request / "algorithm-proposal-v1.schema.json",
            schema_path("AlgorithmProposalV1").read_text(encoding="utf-8"),
        )
        write_text_exclusive(
            layout.request / "candidate-decision-request-v1.schema.json",
            schema_path("CandidateDecisionRequestV1").read_text(encoding="utf-8"),
        )
        write_text_exclusive(
            layout.request / "candidate-decision-response-v1.schema.json",
            schema_path("CandidateDecisionResponseV1").read_text(encoding="utf-8"),
        )
        write_text_exclusive(layout.request / "AGENTS.md", agents_text)
        write_text_exclusive(
            layout.logs / "sanitized-run.log",
            (
                '{"event":"RUN_PREPARED","external_content_logged":false,'
                '"credentials_logged":false}\n'
            ),
        )
        temporary_root.replace(final_root)
        return RunLayout(final_root)
    except BaseException:
        if temporary_root.exists():
            shutil.rmtree(temporary_root)
        raise
