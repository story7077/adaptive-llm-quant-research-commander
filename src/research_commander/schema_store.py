"""Load and apply the repository's versioned JSON Schemas."""

from __future__ import annotations

import json
from copy import deepcopy
from importlib import resources
from pathlib import Path
from typing import cast

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError
from referencing import Registry, Resource

from research_commander.errors import SchemaContractError
from research_commander.json_types import JsonObject, JsonValue

SCHEMA_FILES = {
    "ResearchRequestV1": "research-request-v1.schema.json",
    "ResearchDecisionV1": "research-decision-v1.schema.json",
    "AlgorithmProposalV1": "algorithm-proposal-v1.schema.json",
    "WebGPTCommanderIngressV1": "webgpt-ingress-v1.schema.json",
    "ChallengerManifestV1": "challenger-manifest-v1.schema.json",
    "ValidationRequestV1": "validation-request-v1.schema.json",
    "ResearchEvidenceManifestV1": "evidence-manifest-v1.schema.json",
    "CandidateBuildResultV1": "candidate-build-result-v1.schema.json",
    "CandidateTestManifestV1": "candidate-test-manifest-v1.schema.json",
    "CandidateArtifactBundleV1": "candidate-artifact-bundle-v1.schema.json",
    "CandidateDecisionRequestV1": "candidate-decision-request-v1.schema.json",
    "CandidateDecisionResponseV1": "candidate-decision-response-v1.schema.json",
    "CandidateExecutionSecurityV1": "candidate-execution-security-v1.schema.json",
    "CandidateProcessResultV1": "candidate-process-result-v1.schema.json",
}

STRUCTURED_OUTPUT_SCHEMAS = frozenset(
    {
        "ResearchDecisionV1",
        "CandidateBuildResultV1",
    }
)

_UNSUPPORTED_COMPOSITION_KEYWORDS = frozenset(
    {
        "allOf",
        "not",
        "dependentRequired",
        "dependentSchemas",
        "if",
        "then",
        "else",
    }
)

_POST_VALIDATION_ONLY_KEYWORDS = frozenset(
    {
        # The Codex Structured Output endpoint rejects uniqueItems. The
        # authoritative schema rechecks it after generation.
        "uniqueItems",
    }
)
_COMMANDER_RUNTIME_SENTINELS = {
    "created_at": "RUNTIME_BOUND_BY_HOST",
    "output_hash": "HOST_COMPUTES_SHA256",
    "proposal_hash": "HOST_COMPUTES_SHA256",
}


def _schema_directory() -> Path:
    source_tree = Path(__file__).resolve().parents[2] / "schemas"
    if source_tree.is_dir():
        return source_tree
    packaged = resources.files("research_commander").joinpath("schemas")
    return Path(str(packaged))


def _load_schema(path: Path) -> JsonObject:
    raw: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise SchemaContractError(f"schema is not an object: {path}")
    return cast(JsonObject, raw)


def _schema_by_filename(filename: str) -> JsonObject:
    if filename not in SCHEMA_FILES.values():
        raise SchemaContractError(f"unsupported external schema reference: {filename}")
    return _load_schema(_schema_directory() / filename)


def _strict_cost_sensitivity_schema() -> JsonObject:
    """Return a fixed-shape subset accepted by strict Structured Outputs.

    The authoritative contract intentionally accepts arbitrary named numeric
    sensitivities. Strict Structured Outputs forbids dynamic object keys, so
    the model-facing schema requests the three mandatory falsification levels.
    The generated object remains valid under the broader authoritative schema.
    """
    return {
        "type": "object",
        "description": "Net economic effect under the mandatory 1x, 2x, and 3x cost stresses.",
        "additionalProperties": False,
        "required": ["cost_1x", "cost_2x", "cost_3x"],
        "properties": {
            "cost_1x": {"type": "number"},
            "cost_2x": {"type": "number"},
            "cost_3x": {"type": "number"},
        },
    }


def _strict_proposal_map_schema(field: str) -> JsonObject | None:
    """Narrow canonical JsonValue maps only for strict model transport."""
    if field == "minimum_economic_effect":
        return {
            "type": "object",
            "additionalProperties": False,
            "required": ["metric", "threshold", "comparison"],
            "properties": {
                "metric": {"type": "string"},
                "threshold": {"type": "number"},
                "comparison": {"type": "string"},
            },
        }
    if field == "estimated_capacity":
        return {
            "type": "object",
            "additionalProperties": False,
            "required": ["usd"],
            "properties": {"usd": {"type": "number"}},
        }
    if field == "estimated_turnover":
        return {
            "type": "object",
            "additionalProperties": False,
            "required": ["one_way_daily"],
            "properties": {"one_way_daily": {"type": "number"}},
        }
    return None


def _literal_json_type(value: JsonValue) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def _expand_structured_output_node(
    value: JsonValue,
    *,
    root_schema: JsonObject,
    path: tuple[str, ...] = (),
) -> JsonValue:
    if isinstance(value, list):
        return [
            _expand_structured_output_node(
                item,
                root_schema=root_schema,
                path=(*path, str(index)),
            )
            for index, item in enumerate(value)
        ]
    if not isinstance(value, dict):
        return deepcopy(value)

    if path and (
        path[-1] in {"created_at", "output_hash"}
        or (path[-1] == "proposal_hash" and "proposal" in path)
    ):
        sentinel = _COMMANDER_RUNTIME_SENTINELS[path[-1]]
        return {"type": "string", "const": sentinel}

    reference = value.get("$ref")
    if isinstance(reference, str):
        if len(value) != 1:
            raise SchemaContractError("Structured Output $ref cannot have sibling keywords")
        if reference.startswith("#/$defs/"):
            definition_name = reference.removeprefix("#/$defs/")
            definitions = root_schema.get("$defs")
            if not isinstance(definitions, dict) or definition_name not in definitions:
                raise SchemaContractError(f"unknown local schema reference: {reference}")
            return _expand_structured_output_node(
                definitions[definition_name],
                root_schema=root_schema,
                path=path,
            )
        external_schema = _schema_by_filename(reference)
        return _expand_structured_output_node(
            external_schema,
            root_schema=external_schema,
            path=path,
        )

    if path[-1:] == ("estimated_cost_sensitivity",):
        return _strict_cost_sensitivity_schema()
    if path:
        strict_map = _strict_proposal_map_schema(path[-1])
        if strict_map is not None:
            return strict_map

    unsupported = _UNSUPPORTED_COMPOSITION_KEYWORDS.intersection(value)
    if unsupported:
        rendered = ", ".join(sorted(unsupported))
        raise SchemaContractError(
            f"authoritative schema uses unsupported Structured Output keywords: {rendered}"
        )

    result: JsonObject = {}
    for key, item in value.items():
        if key in {"$schema", "$id", "$defs"} or key in _POST_VALIDATION_ONLY_KEYWORDS:
            continue
        result[key] = _expand_structured_output_node(
            item,
            root_schema=root_schema,
            path=(*path, key),
        )

    if "type" not in result and "const" in result:
        result["type"] = _literal_json_type(result["const"])
    if "type" not in result:
        enum_values = result.get("enum")
        if isinstance(enum_values, list) and enum_values:
            enum_types = {_literal_json_type(item) for item in enum_values}
            if len(enum_types) == 1:
                result["type"] = enum_types.pop()
    if result.get("type") == "object":
        properties = result.get("properties")
        if not isinstance(properties, dict):
            raise SchemaContractError("strict Structured Output objects require fixed properties")
        result["additionalProperties"] = False
        result["required"] = list(properties)
    return result


def structured_output_schema(schema_name: str) -> JsonObject:
    """Build the strict, self-contained schema supplied to a Codex invocation.

    Full post-generation validation still uses the authoritative repository
    schema. This adapter only removes transport-level incompatibilities and
    narrows the one dynamic map to the required 1x/2x/3x stress keys.
    """
    if schema_name not in STRUCTURED_OUTPUT_SCHEMAS:
        raise SchemaContractError(
            f"schema is not approved for model Structured Output: {schema_name}"
        )
    source = _load_schema(schema_path(schema_name))
    expanded = _expand_structured_output_node(source, root_schema=source)
    if not isinstance(expanded, dict):
        raise SchemaContractError("Structured Output root must be an object schema")
    if expanded.get("type") != "object" or "anyOf" in expanded:
        raise SchemaContractError("Structured Output root must be an object, not anyOf")
    return expanded


def _registry() -> tuple[Registry[object], dict[str, JsonObject]]:
    schemas: dict[str, JsonObject] = {}
    registry: Registry[object] = Registry()
    for name, filename in SCHEMA_FILES.items():
        schema = _load_schema(_schema_directory() / filename)
        schema_id = schema.get("$id")
        if not isinstance(schema_id, str):
            raise SchemaContractError(f"schema has no $id: {filename}")
        registry = registry.with_resource(schema_id, Resource.from_contents(schema))
        schemas[name] = schema
    return registry, schemas


def validate_document(document: JsonObject, schema_name: str) -> None:
    registry, schemas = _registry()
    if schema_name not in schemas:
        raise SchemaContractError(f"unknown schema: {schema_name}")
    validator = Draft202012Validator(
        schemas[schema_name],
        registry=registry,  # pyright: ignore[reportArgumentType]
        format_checker=FormatChecker(),
    )
    errors = cast(
        list[ValidationError],
        list(validator.iter_errors(document)),  # pyright: ignore[reportUnknownMemberType]
    )
    errors.sort(key=lambda item: list(item.absolute_path))
    if errors:
        error: ValidationError = errors[0]
        location = ".".join(str(part) for part in error.absolute_path) or "<root>"
        raise SchemaContractError(f"{schema_name} invalid at {location}: {error.message}")


def schema_path(schema_name: str) -> Path:
    try:
        filename = SCHEMA_FILES[schema_name]
    except KeyError as exc:
        raise SchemaContractError(f"unknown schema: {schema_name}") from exc
    return _schema_directory() / filename
