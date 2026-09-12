#!/usr/bin/env python3
"""Validate a RAG evaluation JSONL file without changing it.

The validator intentionally covers only the first-pass checks requested for the
dataset workflow. It uses a small, dependency-free JSON Schema evaluator for
the keywords used by ``schemas/rag.schema.json``.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


DEFAULT_SCHEMA_PATH = (
    Path(__file__).resolve().parents[1] / "schemas" / "rag.schema.json"
)


class SchemaConfigurationError(ValueError):
    """Raised when the validator cannot interpret the configured schema."""


@dataclass(frozen=True)
class Issue:
    line: int | None
    code: str
    path: str
    message: str


def _reject_non_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant {value!r} is not allowed")


def _load_json(value: str) -> Any:
    return json.loads(value, parse_constant=_reject_non_json_constant)


def _json_type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        return "string"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    return type(value).__name__


def _matches_json_type(value: Any, expected_type: str) -> bool:
    if expected_type == "null":
        return value is None
    if expected_type == "boolean":
        return isinstance(value, bool)
    if expected_type == "object":
        return isinstance(value, dict)
    if expected_type == "array":
        return isinstance(value, list)
    if expected_type == "string":
        return isinstance(value, str)
    if expected_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    raise SchemaConfigurationError(
        f"Unsupported JSON Schema type: {expected_type!r}"
    )


def _resolve_local_ref(root_schema: dict[str, Any], ref: str) -> dict[str, Any]:
    if not ref.startswith("#/"):
        raise SchemaConfigurationError(
            f"Only local JSON Pointer references are supported: {ref!r}"
        )

    current: Any = root_schema
    for raw_token in ref[2:].split("/"):
        token = raw_token.replace("~1", "/").replace("~0", "~")
        if not isinstance(current, dict) or token not in current:
            raise SchemaConfigurationError(f"Unresolvable schema reference: {ref!r}")
        current = current[token]

    if not isinstance(current, dict):
        raise SchemaConfigurationError(
            f"Schema reference does not resolve to an object: {ref!r}"
        )
    return current


def _child_path(path: str, key: str) -> str:
    if key.isidentifier():
        return f"{path}.{key}"
    return f"{path}[{json.dumps(key, ensure_ascii=False)}]"


def _schema_issues(
    instance: Any,
    schema: dict[str, Any],
    root_schema: dict[str, Any],
    *,
    line: int,
    path: str = "$",
) -> list[Issue]:
    issues: list[Issue] = []

    ref = schema.get("$ref")
    if ref is not None:
        if not isinstance(ref, str):
            raise SchemaConfigurationError("$ref must be a string")
        issues.extend(
            _schema_issues(
                instance,
                _resolve_local_ref(root_schema, ref),
                root_schema,
                line=line,
                path=path,
            )
        )

    expected = schema.get("type")
    if expected is not None:
        expected_types = [expected] if isinstance(expected, str) else expected
        if not (
            isinstance(expected_types, list)
            and expected_types
            and all(isinstance(item, str) for item in expected_types)
        ):
            raise SchemaConfigurationError(
                f"Invalid type declaration at schema path for {path}"
            )

        if not any(
            _matches_json_type(instance, expected_type)
            for expected_type in expected_types
        ):
            issues.append(
                Issue(
                    line=line,
                    code="schema.type",
                    path=path,
                    message=(
                        f"expected {' or '.join(expected_types)}, "
                        f"got {_json_type_name(instance)}"
                    ),
                )
            )
            return issues

    if "const" in schema and instance != schema["const"]:
        issues.append(
            Issue(
                line=line,
                code="schema.const",
                path=path,
                message=f"must equal {schema['const']!r}",
            )
        )

    if "enum" in schema:
        choices = schema["enum"]
        if not isinstance(choices, list):
            raise SchemaConfigurationError(f"enum must be an array at {path}")
        if instance not in choices:
            issues.append(
                Issue(
                    line=line,
                    code="schema.enum",
                    path=path,
                    message=f"must be one of {choices!r}",
                )
            )

    if isinstance(instance, str):
        min_length = schema.get("minLength")
        if min_length is not None and len(instance) < min_length:
            issues.append(
                Issue(
                    line=line,
                    code="schema.minLength",
                    path=path,
                    message=f"must contain at least {min_length} character(s)",
                )
            )

        pattern = schema.get("pattern")
        if pattern is not None:
            if not isinstance(pattern, str):
                raise SchemaConfigurationError(f"pattern must be a string at {path}")
            try:
                matched = re.search(pattern, instance)
            except re.error as error:
                raise SchemaConfigurationError(
                    f"Invalid regular expression at {path}: {error}"
                ) from error
            if not matched:
                issues.append(
                    Issue(
                        line=line,
                        code="schema.pattern",
                        path=path,
                        message=f"must match pattern {pattern!r}",
                    )
                )

    if isinstance(instance, list):
        min_items = schema.get("minItems")
        if min_items is not None and len(instance) < min_items:
            issues.append(
                Issue(
                    line=line,
                    code="schema.minItems",
                    path=path,
                    message=f"must contain at least {min_items} item(s)",
                )
            )

        item_schema = schema.get("items")
        if item_schema is not None:
            if not isinstance(item_schema, dict):
                raise SchemaConfigurationError(f"items must be an object at {path}")
            for index, item in enumerate(instance):
                issues.extend(
                    _schema_issues(
                        item,
                        item_schema,
                        root_schema,
                        line=line,
                        path=f"{path}[{index}]",
                    )
                )

    if isinstance(instance, dict):
        required = schema.get("required", [])
        if not isinstance(required, list) or not all(
            isinstance(item, str) for item in required
        ):
            raise SchemaConfigurationError(f"required must be a string array at {path}")

        for required_key in required:
            if required_key not in instance:
                issues.append(
                    Issue(
                        line=line,
                        code="schema.required",
                        path=path,
                        message=f"missing required property {required_key!r}",
                    )
                )

        properties = schema.get("properties", {})
        if not isinstance(properties, dict):
            raise SchemaConfigurationError(f"properties must be an object at {path}")

        for key, value in instance.items():
            property_schema = properties.get(key)
            if property_schema is not None:
                if not isinstance(property_schema, dict):
                    raise SchemaConfigurationError(
                        f"property schema for {key!r} must be an object"
                    )
                issues.extend(
                    _schema_issues(
                        value,
                        property_schema,
                        root_schema,
                        line=line,
                        path=_child_path(path, key),
                    )
                )
            elif schema.get("additionalProperties") is False:
                issues.append(
                    Issue(
                        line=line,
                        code="schema.additionalProperties",
                        path=_child_path(path, key),
                        message="additional property is not allowed",
                    )
                )

    return issues


def _load_schema(path: Path) -> dict[str, Any]:
    try:
        raw_schema = path.read_text(encoding="utf-8-sig")
    except OSError as error:
        raise SchemaConfigurationError(f"Cannot read schema {path}: {error}") from error

    try:
        schema = _load_json(raw_schema)
    except json.JSONDecodeError as error:
        raise SchemaConfigurationError(
            f"Schema is not valid JSON at line {error.lineno}, column {error.colno}: "
            f"{error.msg}"
        ) from error
    except ValueError as error:
        raise SchemaConfigurationError(f"Schema is not valid JSON: {error}") from error

    if not isinstance(schema, dict):
        raise SchemaConfigurationError("Schema root must be a JSON object")
    return schema


def _split_values(schema: dict[str, Any]) -> frozenset[str]:
    try:
        values = schema["properties"]["split"]["enum"]
    except (KeyError, TypeError) as error:
        raise SchemaConfigurationError(
            "Schema must define properties.split.enum"
        ) from error

    if not isinstance(values, list) or not values or not all(
        isinstance(value, str) for value in values
    ):
        raise SchemaConfigurationError(
            "Schema properties.split.enum must be a non-empty string array"
        )
    return frozenset(values)


def validate_dataset(dataset_path: Path, schema_path: Path) -> dict[str, Any]:
    schema = _load_schema(schema_path)
    valid_splits = _split_values(schema)

    try:
        raw_dataset = dataset_path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as error:
        return {
            "dataset": str(dataset_path.resolve()),
            "schema": str(schema_path.resolve()),
            "passed": False,
            "rows": 0,
            "parsed_rows": 0,
            "error_count": 1,
            "error_counts": {"jsonl.encoding": 1},
            "issues": [
                asdict(
                    Issue(
                        line=None,
                        code="jsonl.encoding",
                        path="$",
                        message=f"dataset is not valid UTF-8: {error}",
                    )
                )
            ],
        }
    except OSError as error:
        raise ValueError(f"Cannot read dataset {dataset_path}: {error}") from error

    issues: list[Issue] = []
    parsed_rows = 0
    seen_case_ids: dict[str, int] = {}
    seen_queries: dict[str, int] = {}
    lines = raw_dataset.splitlines()

    for line_number, raw_line in enumerate(lines, start=1):
        if not raw_line.strip():
            issues.append(
                Issue(
                    line=line_number,
                    code="jsonl.blank_line",
                    path="$",
                    message="blank lines are not valid JSONL records",
                )
            )
            continue

        try:
            case = _load_json(raw_line)
        except json.JSONDecodeError as error:
            issues.append(
                Issue(
                    line=line_number,
                    code="jsonl.invalid_json",
                    path="$",
                    message=f"column {error.colno}: {error.msg}",
                )
            )
            continue
        except ValueError as error:
            issues.append(
                Issue(
                    line=line_number,
                    code="jsonl.invalid_json",
                    path="$",
                    message=str(error),
                )
            )
            continue

        parsed_rows += 1
        issues.extend(
            _schema_issues(
                case,
                schema,
                schema,
                line=line_number,
            )
        )

        if not isinstance(case, dict):
            continue

        case_id = case.get("case_id")
        if isinstance(case_id, str) and case_id.strip():
            normalized_case_id = case_id.strip()
            first_line = seen_case_ids.get(normalized_case_id)
            if first_line is None:
                seen_case_ids[normalized_case_id] = line_number
            else:
                issues.append(
                    Issue(
                        line=line_number,
                        code="case_id.duplicate",
                        path="$.case_id",
                        message=(
                            f"duplicate case_id {normalized_case_id!r}; "
                            f"first seen on line {first_line}"
                        ),
                    )
                )

        split = case.get("split")
        if split not in valid_splits:
            issues.append(
                Issue(
                    line=line_number,
                    code="split.invalid",
                    path="$.split",
                    message=f"must be one of {sorted(valid_splits)!r}",
                )
            )

        query = case.get("query")
        if not isinstance(query, str) or not query.strip():
            issues.append(
                Issue(
                    line=line_number,
                    code="query.empty",
                    path="$.query",
                    message="query must be a non-empty string",
                )
            )
        else:
            normalized_query = query.strip()
            first_line = seen_queries.get(normalized_query)
            if first_line is None:
                seen_queries[normalized_query] = line_number
            else:
                issues.append(
                    Issue(
                        line=line_number,
                        code="query.duplicate",
                        path="$.query",
                        message=(
                            "exact duplicate query after trimming outer whitespace; "
                            f"first seen on line {first_line}"
                        ),
                    )
                )

        expected = case.get("expected")
        if not isinstance(expected, dict) or not expected:
            issues.append(
                Issue(
                    line=line_number,
                    code="expected.empty",
                    path="$.expected",
                    message="expected must be a non-empty object",
                )
            )

    error_counts: dict[str, int] = {}
    for issue in issues:
        error_counts[issue.code] = error_counts.get(issue.code, 0) + 1

    return {
        "dataset": str(dataset_path.resolve()),
        "schema": str(schema_path.resolve()),
        "passed": not issues,
        "rows": len(lines),
        "parsed_rows": parsed_rows,
        "error_count": len(issues),
        "error_counts": dict(sorted(error_counts.items())),
        "issues": [asdict(issue) for issue in issues],
    }


def _print_text_report(report: dict[str, Any]) -> None:
    print(f"Dataset: {report['dataset']}")
    print(f"Schema:  {report['schema']}")
    print(f"Rows:    {report['rows']}")
    print(f"Parsed:  {report['parsed_rows']}")
    print(f"Errors:  {report['error_count']}")
    print(f"Result:  {'PASS' if report['passed'] else 'FAIL'}")

    for issue in report["issues"]:
        location = "file" if issue["line"] is None else f"line {issue['line']}"
        print(
            f"- {location} [{issue['code']}] {issue['path']}: "
            f"{issue['message']}"
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate a RAG evaluation JSONL dataset without modifying it."
    )
    parser.add_argument("dataset", type=Path, help="RAG evaluation JSONL file")
    parser.add_argument(
        "--schema",
        type=Path,
        default=DEFAULT_SCHEMA_PATH,
        help=f"JSON Schema path (default: {DEFAULT_SCHEMA_PATH})",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the validation report as JSON",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        report = validate_dataset(args.dataset, args.schema)
    except (SchemaConfigurationError, ValueError) as error:
        print(f"Validator configuration error: {error}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_text_report(report)

    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
