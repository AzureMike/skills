#!/usr/bin/env python3

"""Small, dependency-free validator for the JSON Schema subset used by this skill."""

from __future__ import annotations

from typing import Any


def validate(instance: Any, schema: dict[str, Any]) -> list[str]:
    errors: list[str] = []

    def resolve(reference: str) -> dict[str, Any]:
        if not reference.startswith("#/"):
            raise ValueError(f"unsupported schema reference {reference!r}")
        value: Any = schema
        for segment in reference[2:].split("/"):
            value = value[segment.replace("~1", "/").replace("~0", "~")]
        if not isinstance(value, dict):
            raise ValueError(f"schema reference {reference!r} is not an object")
        return value

    def check(value: Any, rule: dict[str, Any], path: str) -> None:
        if "$ref" in rule:
            check(value, resolve(rule["$ref"]), path)
            return
        if "oneOf" in rule:
            matches = []
            for option in rule["oneOf"]:
                option_errors: list[str] = []
                before = len(errors)
                check(value, option, path)
                option_errors.extend(errors[before:])
                del errors[before:]
                if not option_errors:
                    matches.append(option)
            if len(matches) != 1:
                errors.append(f"{path}: must match exactly one allowed shape")
            return
        if "const" in rule and value != rule["const"]:
            errors.append(f"{path}: must equal {rule['const']!r}")
        if "enum" in rule and value not in rule["enum"]:
            errors.append(
                f"{path}: {value!r} must be one of {rule['enum']!r}"
            )

        expected = rule.get("type")
        expected_types = expected if isinstance(expected, list) else [expected]
        type_matches = {
            "object": isinstance(value, dict),
            "array": isinstance(value, list),
            "string": isinstance(value, str),
            "integer": isinstance(value, int) and not isinstance(value, bool),
            "number": isinstance(value, (int, float)) and not isinstance(value, bool),
            "boolean": isinstance(value, bool),
            "null": value is None,
        }
        if expected and not any(type_matches.get(item, False) for item in expected_types):
            errors.append(f"{path}: expected {expected}")
            return

        if isinstance(value, dict):
            properties = rule.get("properties", {})
            for name in rule.get("required", []):
                if name not in value:
                    errors.append(f"{path}.{name}: required property is missing")
            if rule.get("additionalProperties") is False:
                for name in value:
                    if name not in properties:
                        errors.append(f"{path}.{name}: property is not allowed")
            for name, item in value.items():
                child = properties.get(name)
                if isinstance(child, dict):
                    check(item, child, f"{path}.{name}")
        elif isinstance(value, list):
            if len(value) < rule.get("minItems", 0):
                errors.append(
                    f"{path}: expected at least {rule['minItems']} item(s)"
                )
            if rule.get("uniqueItems") and len({repr(item) for item in value}) != len(value):
                errors.append(f"{path}: array items must be unique")
            child = rule.get("items")
            if isinstance(child, dict):
                for index, item in enumerate(value):
                    check(item, child, f"{path}[{index}]")
        elif isinstance(value, str):
            if len(value) < rule.get("minLength", 0):
                errors.append(
                    f"{path}: expected at least {rule['minLength']} character(s)"
                )
            pattern = rule.get("pattern")
            if pattern:
                import re

                if re.search(pattern, value) is None:
                    errors.append(f"{path}: does not match {pattern!r}")

    check(instance, schema, "$")
    return errors
