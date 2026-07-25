#!/usr/bin/env python3

"""Validate source facts, resolve Radius contracts, and render Bicep."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import re
import shlex
from typing import Any

from json_schema import validate as validate_json_schema


SKILL_DIR = Path(__file__).resolve().parent.parent
CONTRACT_PATH = SKILL_DIR / "assets" / "radius-contract.json"
SOURCE_SCHEMA_PATH = SKILL_DIR / "schemas" / "source-model.schema.json"
PLAN_SCHEMA_PATH = SKILL_DIR / "schemas" / "resolved-plan.schema.json"

def dependency_types(contract: dict[str, Any]) -> dict[str, str]:
    """Map the source model's service names onto Radius types, per the contract.

    Each protocol profile names the source-model ``kind`` it answers to, so a
    backing service is added to this skill by describing it in the contract and
    listing its name in the source-model schema. No code changes.
    """

    return {
        profile["sourceKind"]: qualified
        for qualified, profile in contract["protocolProfiles"].items()
        if "sourceKind" in profile
    }


SECRET_NAME = re.compile(
    r"(?:^|_)(?:PASSWORD|PASSWD|SECRET|TOKEN|API_?KEY|PRIVATE_?KEY|"
    r"CONNECTION_?STRING|CREDENTIAL)(?:$|_)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Expression:
    text: str


def identifier(value: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+", value)
    if not words:
        raise ValueError(f"cannot derive an identifier from {value!r}")
    result = words[0].lower() + "".join(word.capitalize() for word in words[1:])
    if result[0].isdigit():
        result = "r" + result
    return result


def slug(value: str) -> str:
    return "-".join(re.findall(r"[A-Za-z0-9]+", value)).lower()


def resource_name(value: str) -> str:
    """Slugify a human display name into a valid Radius resource name.

    Resource names become Kubernetes object names, so they must be lowercase
    DNS labels. A display name such as "Getting Started Todo App" compiles as
    Bicep but is rejected at deploy time.
    """

    slug = "-".join(re.findall(r"[A-Za-z0-9]+", value)).lower()
    if not slug:
        raise ValueError(f"cannot derive a resource name from {value!r}")
    if slug[0].isdigit():
        slug = "r-" + slug
    return slug[:63].rstrip("-")


def interpolation(parts: list[Any]) -> Expression:
    """Compose a Bicep string-interpolation expression from literals and refs."""

    body = ""
    for part in parts:
        if isinstance(part, Expression):
            body += "${" + part.text + "}"
        else:
            text = str(part)
            body += text.replace("\\", "\\\\").replace("${", "\\${").replace("'", "\\'")
    return Expression("'" + body + "'")


def pascal(value: str) -> str:
    return "".join(word.capitalize() for word in re.findall(r"[A-Za-z0-9]+", value))


def screaming(value: str) -> str:
    """connectionString -> CONNECTION_STRING, for environment variable names."""

    return re.sub(r"(?<!^)(?=[A-Z])", "_", value).replace(".", "_").upper()


# Percent-encodes one argument using only POSIX shell builtins, for the case
# where a URI component is not known until the container starts.
PERCENT_ENCODER = (
    "urlencode() { input=$1; output=''; LC_ALL=C; "
    'while [ -n "$input" ]; do '
    'char=${input%"${input#?}"}; input=${input#?}; '
    'case "$char" in [a-zA-Z0-9.~_-]) '
    'output="${output}${char}" ;; *) '
    "code=$(printf '%d' \"'$char\"); "
    "hex=$(printf '%02X' \"$((code & 255))\"); "
    'output="${output}%${hex}" ;; esac; done; '
    "printf '%s' \"$output\"; }; "
)


def bicep_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("${", "\\${").replace("'", "\\'")
    return "'" + escaped + "'"


def bicep_key(value: str) -> str:
    return value if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value) else bicep_string(value)


def environment_value(value: Any) -> Any:
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)):
        return str(value)
    return value


def render_value(value: Any, indent: int = 0) -> str:
    prefix = " " * indent
    child = " " * (indent + 2)
    if isinstance(value, Expression):
        return value.text
    if isinstance(value, str):
        return bicep_string(value)
    if value is True:
        return "true"
    if value is False:
        return "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        if not value:
            return "[]"
        lines = ["["]
        for item in value:
            rendered = render_value(item, indent + 2).splitlines()
            lines.append(child + rendered[0])
            lines.extend(rendered[1:])
        lines.append(prefix + "]")
        return "\n".join(lines)
    if isinstance(value, dict):
        if not value:
            return "{}"
        lines = ["{"]
        for name, item in value.items():
            rendered = render_value(item, indent + 2)
            rendered_lines = rendered.splitlines()
            lines.append(f"{child}{bicep_key(name)}: {rendered_lines[0]}")
            lines.extend(rendered_lines[1:])
        lines.append(prefix + "}")
        return "\n".join(lines)
    raise TypeError(f"cannot render {type(value).__name__}")


def resource(symbol: str, resource_type: str, body: dict[str, Any]) -> str:
    return (
        f"resource {symbol} {bicep_string(resource_type)} = "
        f"{render_value(body)}"
    )


def source_errors(model: dict[str, Any], contract: dict[str, Any]) -> list[str]:
    schema = json.loads(SOURCE_SCHEMA_PATH.read_text())
    errors = validate_json_schema(model, schema)
    if errors:
        return errors
    if model["status"] == "complete" and model["blockers"]:
        errors.append("$.blockers: complete source model cannot contain blockers")
    if model["status"] == "blocked" and not model["blockers"]:
        errors.append("$.blockers: blocked source model must explain the blocker")

    workload_ids = [item["id"] for item in model["workloads"]]
    dependency_ids = [item["id"] for item in model["dependencies"]]
    if len(workload_ids) != len(set(workload_ids)):
        errors.append("$.workloads: workload ids must be unique")
    if len(dependency_ids) != len(set(dependency_ids)):
        errors.append("$.dependencies: dependency ids must be unique")
    workload_set = set(workload_ids)

    writable = {
        workload["id"]: [item["path"] for item in workload["writablePaths"]]
        for workload in model["workloads"]
    }
    for index, item in enumerate(model["persistence"]):
        if item["workloadId"] not in workload_set:
            errors.append(
                f"$.persistence[{index}].workloadId: unknown workload"
            )
        elif not any(
            item["path"] == path or item["path"].startswith(path.rstrip("/") + "/")
            for path in writable[item["workloadId"]]
        ):
            errors.append(
                f"$.persistence[{index}].path: path is not under a cited "
                "runtime-writable directory"
            )

    if model["status"] == "complete" and not model["dependencies"]:
        # Selecting no backing service is a real answer, but it is the rare one:
        # most applications exist to read, write, serve or process data held
        # somewhere else. Requiring a cited disposition stops "none" from being
        # the silent default when the source supports several backends.
        disposition = model.get("selfContained")
        if not disposition:
            offered = sorted(
                kind
                for kind, qualified in dependency_types(contract).items()
                if qualified in {n.split("@", 1)[0] for n in contract["resourceTypes"]}
            )
            errors.append(
                "$.selfContained: no backing service was selected. The pinned "
                "contract offers " + ", ".join(offered) + ". Either select the "
                "service this application requires to perform its primary "
                "function, or record $.selfContained with a primaryFunction, a "
                "rationale and a path:line citation proving it needs none."
            )

    available_types = {
        name.split("@", 1)[0] for name in contract["resourceTypes"]
    }
    for index, dependency in enumerate(model["dependencies"]):
        qualified_type = dependency_types(contract)[dependency["kind"]]
        if qualified_type not in available_types:
            errors.append(
                f"$.dependencies[{index}].kind: {qualified_type} is absent "
                "from the pinned contract"
            )
        unknown = set(dependency["workloadIds"]) - workload_set
        if unknown:
            errors.append(
                f"$.dependencies[{index}].workloadIds: unknown {sorted(unknown)}"
            )
        slots = [item["slot"] for item in dependency["settings"]]
        if len(slots) != len(set(slots)):
            errors.append(
                f"$.dependencies[{index}].settings: slots must be unique"
            )
        inputs = {item["name"]: item for item in dependency["inputs"]}
        for required_input in (
            contract["protocolProfiles"]
            .get(qualified_type, {})
            .get("binding", {})
            .values()
        ):
            if (
                isinstance(required_input, str)
                and required_input.endswith("Input")
                and required_input not in inputs
            ):
                errors.append(
                    f"$.dependencies[{index}].inputs: missing {required_input!r}"
                )
        for input_index, item in enumerate(dependency["inputs"]):
            secret = SECRET_NAME.search(item["name"]) or item["name"] == "password"
            if secret and item["value"]["kind"] != "developerInput":
                errors.append(
                    f"$.dependencies[{index}].inputs[{input_index}]: "
                    "secret inputs must be developerInput"
                )
        profile = contract["protocolProfiles"].get(qualified_type, {})
        required_slots = contract_slots(profile)
        # A composite carries several settings inside one value, so reporting
        # it discharges the settings it absorbs.
        for composite in (profile.get("runtimeUri"), profile.get("runtimeComposite")):
            if composite and composite.get("setting") in slots:
                required_slots -= set(composite.get("satisfies", []))
        missing = required_slots - set(slots)
        if missing:
            errors.append(
                f"$.dependencies[{index}].settings: missing contract slots "
                f"{sorted(missing)}"
            )
        connection_uri = next(
            (
                item
                for item in dependency["settings"]
                if item["slot"] == "connectionUri"
            ),
            None,
        )
        if connection_uri is not None:
            schemes = (
                contract["protocolProfiles"]
                .get(qualified_type, {})
                .get("runtimeUri", {})
                .get("schemes", [])
            )
            # Only types whose contract declares a URI grammar can have their
            # scheme checked. Types that hand out a ready-made connection string
            # have no scheme list, and comparing against an empty one would
            # reject every model for those types.
            if schemes and connection_uri.get("scheme") not in schemes:
                errors.append(
                    f"$.dependencies[{index}].settings: connectionUri scheme "
                    f"must be one of {schemes!r}"
                )

    for index, workload in enumerate(model["workloads"]):
        image = workload["image"]
        if image["kind"] == "published":
            reference = image["reference"]
            if (
                reference.endswith(":latest")
                or (
                    "@sha256:" not in reference
                    and not re.search(r":v?\d+(?:\.\d+)+(?:[-+][A-Za-z0-9.-]+)?$", reference)
                )
            ):
                errors.append(
                    f"$.workloads[{index}].image.reference: published image "
                    "must use an immutable version or digest"
                )
            # Taking a prebuilt image forfeits building this repository, so the
            # reason has to be evidenced where it is observable: in the
            # Dockerfile that cannot be built from a clean checkout.
            if image["prebuiltReason"] == "dockerfilePackagesPrebuiltArtifact":
                dockerfile = image.get("sourceDockerfile")
                cited = image["citation"].rpartition(":")[0]
                if not dockerfile:
                    errors.append(
                        f"$.workloads[{index}].image.sourceDockerfile: required "
                        "when the reason is that the Dockerfile packages a "
                        "prebuilt artifact"
                    )
                elif cited != dockerfile:
                    errors.append(
                        f"$.workloads[{index}].image.citation: must cite "
                        f"{dockerfile!r}, the file said to package a prebuilt "
                        f"artifact, not {cited!r}"
                    )
        for config_index, setting in enumerate(workload["configuration"]):
            if setting["sensitive"] and setting["value"]["kind"] != "developerInput":
                errors.append(
                    f"$.workloads[{index}].configuration[{config_index}]: "
                    "sensitive values must be developerInput"
                )
    return errors


def selected_contract(
    model: dict[str, Any],
    contract: dict[str, Any],
) -> dict[str, Any]:
    selected = {
        "Radius.Core/applications",
        "Radius.Compute/containers",
        "Radius.Security/secrets",
    }


def plan_value(value: Any) -> dict[str, Any]:
    if isinstance(value, Expression):
        return {"kind": "expression", "value": value.text}
    if isinstance(value, dict):
        return {
            "kind": "object",
            "properties": [
                {"name": name, "value": plan_value(item)}
                for name, item in value.items()
            ],
        }
    if isinstance(value, list):
        return {"kind": "array", "items": [plan_value(item) for item in value]}
    return {"kind": "literal", "value": value}


def plan_document(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "schemaVersion": plan["schemaVersion"],
        "parameters": [
            {
                "name": name,
                "secure": properties["secure"],
                **(
                    {"default": properties["default"]}
                    if "default" in properties
                    else {}
                ),
            }
            for name, properties in plan["parameters"].items()
        ],
        "resources": [
            {
                "symbol": item["symbol"],
                "type": item["type"],
                "body": plan_value(item["body"]),
            }
            for item in plan["resources"]
        ],
    }
    if any(item["image"]["kind"] == "build" for item in model["workloads"]):
        selected.add("Radius.Compute/containerImages")
    if model["persistence"]:
        selected.add("Radius.Compute/persistentVolumes")
    if any(
        listener["external"]
        for workload in model["workloads"]
        for listener in workload["listeners"]
    ):
        selected.add("Radius.Compute/routes")
    selected.update(
        dependency_types(contract)[item["kind"]] for item in model["dependencies"]
    )
    available = {
        name.split("@", 1)[0]: name for name in contract["resourceTypes"]
    }
    return {
        "schemaVersion": contract["schemaVersion"],
        "extension": contract["extension"],
        "policies": contract["policies"],
        "bundles": {
            qualified_type: {
                "type": contract["resourceTypes"][available[qualified_type]],
                "recipe": contract["azureRecipeMappings"].get(qualified_type),
                "protocol": contract["protocolProfiles"].get(qualified_type),
            }
            for qualified_type in sorted(selected)
        },
    }


def default_input(
    contract: dict[str, Any], dependency: dict[str, Any], name: str
) -> str:
    """Derive a deployable default for a name the definition has to invent.

    Preference order: the schema default published by the pinned contract, then
    a slug derived from the dependency itself. Nothing here is specific to any
    application, so a repository never seen before gets the same treatment.
    """

    qualified = dependency_types(contract)[dependency["kind"]]
    binding = (
        contract["protocolProfiles"].get(qualified, {}).get("binding", {})
    )
    slot = next(
        (key[: -len("Input")] for key, value in binding.items()
         if key.endswith("Input") and value == name),
        name,
    )
    for qualified_name, body in contract["resourceTypes"].items():
        if qualified_name.split("@", 1)[0] != qualified:
            continue
        definition = (body.get("schema") or {}).get("properties", {}).get(slot)
        if isinstance(definition, dict) and "default" in definition:
            return str(definition["default"])
        break
    # Underscore, not hyphen: an unquoted SQL identifier accepts one and
    # rejects the other, and this value can land in a database name.
    return (slug(dependency["id"]) + "_" + slug(slot)).replace("-", "_")


def input_expression(
    contract: dict[str, Any],
    dependency: dict[str, Any],
    name: str,
    parameters: dict[str, dict[str, Any]],
) -> Any:
    item = next((item for item in dependency["inputs"] if item["name"] == name), None)
    if item is None:
        raise ValueError(f"{dependency['id']}: missing input {name!r}")
    if item["value"]["kind"] == "literal":
        return item["value"]["value"]
    secure = bool(SECRET_NAME.search(name) or name == "password")
    parameter = identifier(dependency["id"]) + pascal(name)
    spec: dict[str, Any] = {"secure": secure}
    if not secure:
        # A non-secret name the definition has to invent (a database, an admin
        # user) still has to have *some* value, or the file cannot be deployed
        # without out-of-band input. Give it a default so it is deployable as
        # written and still overridable. Only a credential is left unset.
        spec["default"] = default_input(contract, dependency, name)
    parameters.setdefault(parameter, spec)
    return Expression(parameter)


def native_setting(
    dependency: dict[str, Any],
    slot: str,
) -> dict[str, Any] | None:
    return next(
        (item for item in dependency["settings"] if item["slot"] == slot),
        None,
    )


def contract_constants(profile: dict[str, Any]) -> dict[str, str]:
    """Slots the contract fixes to a constant, written as ``slot=value``.

    ``requiredClientSettings`` mixes two kinds of entry. A bare name is a slot
    the caller has to fill; a ``name=value`` pair is a value the provider
    dictates. Reading the second kind here is what keeps constants such as the
    SASL mechanism out of this file.
    """

    constants: dict[str, str] = {}
    for entry in (
        (profile.get("requiredClientSettings") or [])
        + (profile.get("runtimeRequiredClientSettings") or [])
    ):
        name, separator, value = entry.partition("=")
        if separator:
            constants[name] = value
    return constants


def contract_slots(profile: dict[str, Any]) -> set[str]:
    return {
        entry.partition("=")[0]
        for entry in (
            (profile.get("requiredClientSettings") or [])
            + (profile.get("runtimeRequiredClientSettings") or [])
        )
    } | {
        name
        for group in (profile.get("requiredAnyClientSettings") or [])
        for name in group
    }


def scalar(text: str) -> Any:
    if text in {"true", "false"}:
        return text == "true"
    try:
        return int(text)
    except ValueError:
        return text


def setting_value(
    *,
    dependency: dict[str, Any],
    slot: str,
    symbol: str,
    qualified_type: str,
    contract: dict[str, Any],
    parameters: dict[str, dict[str, Any]],
) -> tuple[str, Any, str | None]:
    """Resolve one client setting from the contract alone.

    Every binding key is named ``<slot><Kind>``, and the suffix states how the
    value is obtained. Dispatching on that suffix rather than on the slot name
    is what lets a backing service be added to the contract without touching
    this file.
    """

    profile = contract["protocolProfiles"].get(qualified_type, {})
    binding = profile.get("binding", {})

    # Read off the provisioned resource.
    if f"{slot}Property" in binding:
        target = binding[f"{slot}Property"]
        return "value", Expression(f"{symbol}.properties.{target}"), None

    # Held in the resource's secret collection; delivered by reference.
    if f"{slot}Secret" in binding:
        return (
            "managedSecret",
            Expression(f"{symbol}.properties.secrets.name"),
            binding[f"{slot}Secret"],
        )

    # Fixed by the provider.
    if f"{slot}Literal" in binding:
        return "value", binding[f"{slot}Literal"], None

    # Composed from resource properties by a contract-supplied format string.
    if f"{slot}Transform" in binding:
        rendered = binding[f"{slot}Transform"]
        for name in re.findall(r"<([A-Za-z0-9_]+)>", rendered):
            rendered = rendered.replace(
                f"<{name}>", f"${{{symbol}.properties.{name}}}"
            )
        escaped = rendered.replace("\\", "\\\\").replace("'", "\\'")
        return "value", Expression("'" + escaped + "'"), None

    # Supplied by the definition, under the name the contract gives it.
    if f"{slot}Input" in binding:
        name = binding[f"{slot}Input"]
        value = input_expression(contract, dependency, name, parameters)
        secure = bool(SECRET_NAME.search(name) or name == "password")
        return ("secret" if secure else "value"), value, None

    constants = contract_constants(profile)
    if slot in constants:
        return "value", scalar(constants[slot]), None

    # Named by the contract with no binding of its own: the definition supplies
    # it under the slot's own name.
    if slot in contract_slots(profile):
        value = input_expression(contract, dependency, slot, parameters)
        secure = bool(SECRET_NAME.search(slot) or slot == "password")
        return ("secret" if secure else "value"), value, None

    raise ValueError(
        f"{dependency['id']}: the contract for {qualified_type} does not "
        f"define slot {slot!r}"
    )


def shell_process(workload: dict[str, Any]) -> str:
    process = workload["process"]
    if process["kind"] == "shell":
        return process["command"]
    if process["kind"] == "argv":
        return shlex.join(process["argv"])
    raise ValueError(
        f"{workload['id']}: a runtime composite requires an explicit source process"
    )


def resolve(
    model: dict[str, Any],
    contract: dict[str, Any],
    *,
    remote: str,
    commit: str,
    source_path: str,
    expose_externally: bool = False,
    persist_data: bool = False,
) -> dict[str, Any]:
    available = {
        name.split("@", 1)[0]: name for name in contract["resourceTypes"]
    }
    parameters: dict[str, dict[str, Any]] = {"environment": {"secure": False}}
    app_symbol = "app"
    resources: list[dict[str, Any]] = []
    requirements = {
        "dependencies": [],
        "persistentPaths": [],
        "secretEnvironment": [],
    }
    dependency_plans: dict[str, dict[str, Any]] = {}

    resources.append(
        {
            "symbol": app_symbol,
            "type": available["Radius.Core/applications"],
            "body": {
                "name": resource_name(model["application"]["name"]),
                "properties": {"environment": Expression("environment")},
            },
        }
    )

    for dependency in model["dependencies"]:
        symbol = identifier(dependency["id"])
        qualified_type = dependency_types(contract)[dependency["kind"]]
        recipe = contract["azureRecipeMappings"].get(qualified_type, {})
        name_parameter = symbol + "Name"
        # A provider-global name must be chosen by whoever deploys, so the
        # contract requires it be asked for rather than defaulted.
        global_name = bool(recipe.get("providerGlobalName")) and bool(
            contract["policies"].get("providerGlobalNamesUseRequiredParameters")
        )
        parameters[name_parameter] = (
            {"secure": False}
            if global_name
            else {"secure": False, "default": slug(dependency["id"])}
        )
        properties: dict[str, Any] = {
            "environment": Expression("environment"),
            "application": Expression("app.id"),
        }
        schema_properties = (
            contract["resourceTypes"][available[qualified_type]]
            .get("schema", {})
            .get("properties", {})
        )
        for item in dependency["inputs"]:
            property_name = (
                contract["protocolProfiles"]
                .get(qualified_type, {})
                .get("definitionProperties", {})
                .get(item["name"], item["name"])
            )
            if property_name not in schema_properties:
                continue
            properties[property_name] = input_expression(
                contract, dependency, item["name"], parameters
            )
        for property_name, definition in schema_properties.items():
            # A writable property carrying a schema default is part of the
            # resource's declared shape. Materialize it so the coordinate the
            # application connects to is stated in the file rather than left
            # to whatever the Recipe happens to pick.
            if property_name in properties or definition.get("readOnly"):
                continue
            if "default" not in definition:
                continue
            properties[property_name] = definition["default"]
        resources.append(
            {
                "symbol": symbol,
                "type": available[qualified_type],
                "body": {
                    "name": Expression(name_parameter),
                    "properties": properties,
                },
            }
        )
        dependency_plans[dependency["id"]] = {
            "source": dependency,
            "symbol": symbol,
            "qualifiedType": qualified_type,
        }

    workload_symbols: dict[str, str] = {}
    secret_entries: dict[str, Any] = {}
    secret_env_keys: dict[tuple[str, str], str] = {}

    for workload in model["workloads"]:
        workload_symbol = identifier(workload["id"])
        workload_symbols[workload["id"]] = workload_symbol
        image = workload["image"]
        if image["kind"] == "build":
            image_symbol = workload_symbol + "Image"
            context = image["context"].strip("/")
            app_path = "" if source_path in {"", "."} else source_path.strip("/") + "/"
            build_path = (app_path + context).strip("/")
            if build_path == ".":
                build_path = ""
            source = remote.rstrip("/")
            if source.endswith(".git"):
                source = source[:-4]
            build_source = f"git::{source}.git//{build_path}?ref={commit}"
            build: dict[str, Any] = {"source": build_source}
            # The builder already defaults to a Dockerfile at the context root,
            # so naming it only adds a path that has to stay correct.
            dockerfile = (image["dockerfile"] or "").strip("/")
            if dockerfile and dockerfile != "Dockerfile":
                build["dockerfile"] = dockerfile
            resources.append(
                {
                    "symbol": image_symbol,
                    "type": available["Radius.Compute/containerImages"],
                    "body": {
                        "name": resource_name(workload["name"]) + "-image",
                        "properties": {
                            "environment": Expression("environment"),
                            "application": Expression("app.id"),
                            "build": build,
                            "tag": commit,
                        },
                    },
                }
            )
            image_value: Any = Expression(f"{image_symbol}.properties.imageReference")
        else:
            image_value = image["reference"]

        env: dict[str, Any] = {}
        for setting in workload["configuration"]:
            value = setting["value"]
            if value["kind"] == "literal":
                env[setting["name"]] = {
                    "value": environment_value(value["value"])
                }
                continue
            parameter = workload_symbol + pascal(setting["name"])
            parameters.setdefault(parameter, {"secure": setting["sensitive"]})
            if not setting["sensitive"]:
                env[setting["name"]] = {"value": Expression(parameter)}
                continue
            # A developer-supplied credential is already protected by the
            # @secure() parameter: Radius encrypts it and injects it. Copying it
            # into an application-owned secret resource adds a second copy
            # without adding protection.
            env[setting["name"]] = {"value": Expression(parameter)}
            requirements["secretEnvironment"].append({"key": setting["name"]})

        runtime_wrappers: list[str] = []
        for dependency_id, plan in dependency_plans.items():
            dependency = plan["source"]
            if workload["id"] not in dependency["workloadIds"]:
                continue
            symbol = plan["symbol"]
            qualified_type = plan["qualifiedType"]
            ledger: list[dict[str, Any]] = []
            settings_by_slot = {
                item["slot"]: item for item in dependency["settings"]
            }
            skipped_slots: set[str] = set()
            profile = contract["protocolProfiles"].get(qualified_type, {})
            uri_spec = profile.get("runtimeUri")
            if uri_spec and uri_spec.get("setting") in settings_by_slot:
                uri = settings_by_slot[uri_spec["setting"]]
                uri_key = uri["delivery"]["name"]
                chunks = [
                    chunk
                    for chunk in re.split(r"(<[A-Za-z0-9_]+>)", uri_spec["format"])
                    if chunk
                ]
                names = [c[1:-1] for c in chunks if c.startswith("<")]
                resolved: dict[str, tuple[str, Any, str | None]] = {}
                for name in names:
                    if name == "scheme":
                        resolved[name] = ("value", uri["scheme"], None)
                        continue
                    resolved[name] = setting_value(
                        dependency=dependency,
                        slot=name,
                        symbol=symbol,
                        qualified_type=qualified_type,
                        contract=contract,
                        parameters=parameters,
                    )
                encode = set(uri_spec.get("percentEncode", []))
                deferred = {
                    name
                    for name, (kind, _, _) in resolved.items()
                    if kind == "managedSecret"
                }
                if not deferred:
                    # Every component is known where this file is authored, so
                    # compose the URI here. Encoding at runtime would require a
                    # shell wrapper, and that displaces the image's own
                    # entrypoint for no gain.
                    env[uri_key] = {
                        "value": interpolation(
                            [
                                resolved[c[1:-1]][1] if c.startswith("<") else c
                                for c in chunks
                            ]
                        )
                    }
                    ledger.append(
                        {
                            "name": uri_spec["setting"],
                            "evidence": uri["citation"],
                            "delivery": {"kind": "env", "key": uri_key},
                        }
                    )
                else:
                    # At least one component exists only as a secret reference
                    # at runtime, so the URI has to be assembled there.
                    helpers: dict[str, str] = {}
                    inline: dict[str, str] = {}
                    for name in names:
                        if name == "scheme":
                            continue
                        kind, value, managed_key = resolved[name]
                        if kind == "value" and not isinstance(value, Expression):
                            inline[name] = str(environment_value(value))
                            continue
                        key = f"RADIUS_{symbol.upper()}_{screaming(name)}"
                        helpers[name] = key
                        if kind == "managedSecret":
                            env[key] = {
                                "valueFrom": {
                                    "secretKeyRef": {
                                        "secretName": value,
                                        "key": managed_key,
                                    }
                                }
                            }
                            delivery = {
                                "kind": "secretKeyRef",
                                "key": key,
                                "secretKey": managed_key,
                            }
                        elif kind == "secret":
                            secret_key = workload_symbol + "_" + key
                            secret_entries[secret_key] = {"value": value}
                            env[key] = {
                                "valueFrom": {
                                    "secretKeyRef": {
                                        "secretName": Expression(
                                            "applicationSecrets.name"
                                        ),
                                        "key": secret_key,
                                    }
                                }
                            }
                            delivery = {
                                "kind": "secretKeyRef",
                                "key": key,
                                "secretKey": secret_key,
                            }
                        else:
                            env[key] = {"value": environment_value(value)}
                            delivery = {"kind": "env", "key": key}
                        if delivery["kind"] == "secretKeyRef":
                            requirements["secretEnvironment"].append({"key": key})
                        ledger.append(
                            {
                                "name": name,
                                "evidence": uri["citation"],
                                "delivery": delivery,
                            }
                        )
                    encoded = sorted(encode & set(helpers))
                    assembled = ""
                    for chunk in chunks:
                        if not chunk.startswith("<"):
                            assembled += chunk
                        elif chunk[1:-1] == "scheme":
                            assembled += uri["scheme"]
                        elif chunk[1:-1] in inline:
                            assembled += inline[chunk[1:-1]]
                        elif chunk[1:-1] in encode:
                            assembled += "${RADIUS_ENC_" + screaming(chunk[1:-1]) + "}"
                        else:
                            assembled += "${" + helpers[chunk[1:-1]] + "}"
                    shell = PERCENT_ENCODER if encoded else ""
                    for name in encoded:
                        shell += (
                            f"RADIUS_ENC_{screaming(name)}="
                            f'$(urlencode "${{{helpers[name]}}}"); '
                        )
                    runtime_wrappers.append(shell + f'export {uri_key}="{assembled}"')
                    ledger.append(
                        {
                            "name": uri_spec["setting"],
                            "evidence": uri["citation"],
                            "delivery": {
                                "kind": "runtimeConfig",
                                "key": uri_key,
                                "value": uri["scheme"] + "://",
                            },
                        }
                    )
                skipped_slots.add(uri_spec["setting"])
                skipped_slots.update(names)
                skipped_slots.update(uri_spec.get("satisfies", []))

            composite_spec = profile.get("runtimeComposite")
            if composite_spec and composite_spec.get("setting") in settings_by_slot:
                composite = settings_by_slot[composite_spec["setting"]]
                composite_key = composite["delivery"]["name"]
                managed = composite_spec["managedSecret"]
                secret_key = f"RADIUS_{symbol.upper()}_{screaming(managed)}"
                env[secret_key] = {
                    "valueFrom": {
                        "secretKeyRef": {
                            "secretName": Expression(
                                f"{symbol}.properties.secrets.name"
                            ),
                            "key": managed,
                        }
                    }
                }
                requirements["secretEnvironment"].append({"key": secret_key})
                # Pure concatenation, so the value can reference the secret
                # environment variable directly. Only a composite that has to
                # transform a component needs a shell process.
                rendered = composite_spec["format"]
                for name in re.findall(r"<([A-Za-z0-9_]+)>", rendered):
                    replacement = (
                        str(composite_spec[name])
                        if name in composite_spec
                        else "${" + secret_key + "}"
                    )
                    rendered = rendered.replace(f"<{name}>", replacement)
                env[composite_key] = {"value": rendered}
                for name in composite_spec.get("satisfies", []):
                    short = name.rpartition(".")[2]
                    ledger.append(
                        {
                            "name": name,
                            "evidence": composite["citation"],
                            "delivery": {
                                "kind": "runtimeConfig",
                                "key": composite_key,
                                "value": composite_spec[short],
                            }
                            if short in composite_spec
                            else {
                                "kind": "secretKeyRef",
                                "key": secret_key,
                                "secretKey": managed,
                            },
                        }
                    )
                skipped_slots.add(composite_spec["setting"])


            for slot, setting in settings_by_slot.items():
                if slot in skipped_slots:
                    continue
                delivery = setting["delivery"]
                if delivery["kind"] == "sourceDefault":
                    ledger.append(
                        {
                            "name": slot,
                            "evidence": setting["citation"],
                            "delivery": {
                                "kind": "sourceDefault",
                                "value": setting.get("sourceDefault"),
                            },
                        }
                    )
                    continue
                key = delivery["name"]
                value_kind, value, managed_key = setting_value(
                    dependency=dependency,
                    slot=slot,
                    symbol=symbol,
                    qualified_type=qualified_type,
                    contract=contract,
                    parameters=parameters,
                )
                if value_kind == "managedSecret":
                    env[key] = {
                        "valueFrom": {
                            "secretKeyRef": {
                                "secretName": value,
                                "key": managed_key,
                            }
                        }
                    }
                    requirements["secretEnvironment"].append({"key": key})
                    ledger_kind = "secretKeyRef"
                    ledger_extra = {"secretKey": managed_key}
                elif value_kind == "secret":
                    env[key] = {"value": environment_value(value)}
                    requirements["secretEnvironment"].append({"key": key})
                    ledger_kind = "parameter"
                    ledger_extra = {}
                else:
                    env[key] = {"value": environment_value(value)}
                    ledger_kind = (
                        "literal"
                        if isinstance(value, (str, int, float, bool))
                        else "env"
                    )
                    ledger_extra = {"value": value} if ledger_kind == "literal" else {}
                ledger.append(
                    {
                        "name": slot,
                        "evidence": setting["citation"],
                        "delivery": {
                            "kind": ledger_kind,
                            "key": key,
                            **ledger_extra,
                        },
                    }
                )
            requirements["dependencies"].append(
                {"resourceSymbol": symbol, "settings": ledger}
            )

        container: dict[str, Any] = {"image": image_value}
        process = workload["process"]
        if runtime_wrappers:
            command = "; ".join(runtime_wrappers + ["exec " + shell_process(workload)])
            container["command"] = ["/bin/sh", "-c"]
            container["args"] = [command]
        elif process["kind"] == "argv":
            container["command"] = process["argv"]
        elif process["kind"] == "shell":
            container["command"] = ["/bin/sh", "-c"]
            container["args"] = ["exec " + process["command"]]
        if env:
            container["env"] = env
        if workload["listeners"]:
            container["ports"] = {
                listener["name"]: {
                    "containerPort": listener["port"],
                    "protocol": "TCP",
                }
                for listener in workload["listeners"]
            }
        container_name = identifier(workload["name"])
        connections = {
            identifier(dependency_id): {
                "source": Expression(f"{plan['symbol']}.id"),
                "disableDefaultEnvVars": True,
            }
            for dependency_id, plan in dependency_plans.items()
            if workload["id"] in plan["source"]["workloadIds"]
        }
        properties: dict[str, Any] = {
            "environment": Expression("environment"),
            "application": Expression("app.id"),
            "containers": {container_name: container},
        }
        if connections:
            properties["connections"] = connections
        resources.append(
            {
                "symbol": workload_symbol,
                "type": available["Radius.Compute/containers"],
                "body": {"name": resource_name(workload["name"]), "properties": properties},
                "containerName": container_name,
            }
        )

    if secret_entries:
        secret_resource = {
            "symbol": "applicationSecrets",
            "type": available["Radius.Security/secrets"],
            "body": {
                "name": resource_name(model["application"]["name"]) + "-secrets",
                "properties": {
                    "environment": Expression("environment"),
                    "application": Expression("app.id"),
                    "kind": "generic",
                    "data": secret_entries,
                },
            },
        }
        resources.insert(1, secret_resource)

    for index, item in enumerate(model["persistence"], start=1):
        if not persist_data:
            # A container writing to a path is not a durability requirement:
            # caches, scratch space and optional config subsystems all write.
            # Durable storage is a deployment decision, so it is opt-in.
            continue
        workload_symbol = workload_symbols[item["workloadId"]]
        volume_symbol = workload_symbol + "Volume" + str(index)
        resources.insert(
            1,
            {
                "symbol": volume_symbol,
                "type": available["Radius.Compute/persistentVolumes"],
                "body": {
                    "name": f"{item['workloadId']}-data-{index}",
                    "properties": {
                        "environment": Expression("environment"),
                        "application": Expression("app.id"),
                        "sizeInGib": 1,
                    },
                },
            },
        )
        workload_resource = next(
            item for item in resources if item["symbol"] == workload_symbol
        )
        container_name = workload_resource["containerName"]
        container = workload_resource["body"]["properties"]["containers"][container_name]
        volume_name = "data" + str(index)
        container.setdefault("volumeMounts", []).append(
            {"volumeName": volume_name, "mountPath": item["path"]}
        )
        workload_resource["body"]["properties"].setdefault("volumes", {})[
            volume_name
        ] = {"persistentVolume": {"resourceId": Expression(f"{volume_symbol}.id")}}
        requirements["persistentPaths"].append(
            {
                "containerResourceSymbol": workload_symbol,
                "container": container_name,
                "path": item["path"],
                "required": True,
            }
        )

    for workload in model["workloads"]:
        workload_symbol = workload_symbols[workload["id"]]
        workload_resource = next(
            item for item in resources if item["symbol"] == workload_symbol
        )
        if not expose_externally:
            # External exposure is a deployment decision, not a source fact: a
            # compose port mapping or an EXPOSE line is local convenience. Emit a
            # route only when the request explicitly asks to expose the app.
            continue
        for listener in workload["listeners"]:
            if not listener["external"]:
                continue
            route_symbol = workload_symbol + pascal(listener["name"]) + "Route"
            destination = {
                "resourceId": Expression(f"{workload_symbol}.id"),
                "containerName": workload_resource["containerName"],
                "containerPort": listener["port"],
            }
            rule: dict[str, Any] = {"destinationContainer": destination}
            if listener["protocol"] == "http":
                rule["matches"] = [{"httpPath": "/"}]
            resources.append(
                {
                    "symbol": route_symbol,
                    "type": available["Radius.Compute/routes"],
                    "body": {
                        "name": f"{workload['name']}-{listener['name']}",
                        "properties": {
                            "environment": Expression("environment"),
                            "application": Expression("app.id"),
                            "kind": listener["protocol"].upper(),
                            "rules": [rule],
                        },
                    },
                }
            )

    return {
        "schemaVersion": "1.0",
        "parameters": parameters,
        "resources": resources,
        "requirements": requirements,
    }


def render(plan: dict[str, Any], contract: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    lines = ["extension radius", ""]
    for name, properties in plan["parameters"].items():
        if properties["secure"]:
            lines.extend(["@secure()", f"param {name} string", ""])
        elif "default" in properties:
            # A name the definition had to invent still needs a value, or the
            # file cannot be deployed as written.
            lines.extend(
                [f"param {name} string = {bicep_string(properties['default'])}", ""]
            )
        else:
            lines.extend([f"param {name} string", ""])
    for item in plan["resources"]:
        lines.extend([resource(item["symbol"], item["type"], item["body"]), ""])
    config = {
        "experimentalFeaturesEnabled": {"extensibility": True},
        "extensions": {"radius": contract["extension"]["reference"]},
    }
    return "\n".join(lines).rstrip() + "\n", config


def build_candidate(
    model: dict[str, Any],
    candidate: Path,
    *,
    remote: str,
    commit: str,
    source_path: str,
    expose_externally: bool = False,
    persist_data: bool = False,
) -> dict[str, Any]:
    contract = json.loads(CONTRACT_PATH.read_text())
    errors = source_errors(model, contract)
    if errors:
        raise ValueError("; ".join(errors))
    if model["status"] != "complete":
        raise ValueError("source model is blocked")
    plan = resolve(
        model,
        contract,
        remote=remote,
        commit=commit,
        source_path=source_path,
        expose_externally=expose_externally,
        persist_data=persist_data,
    )
    resolved = plan_document(plan)
    plan_errors = validate_json_schema(
        resolved,
        json.loads(PLAN_SCHEMA_PATH.read_text()),
    )
    if plan_errors:
        raise ValueError("resolved plan failed schema validation: " + "; ".join(plan_errors))
    app_bicep, bicepconfig = render(plan, contract)
    candidate.mkdir(parents=True, exist_ok=True)
    (candidate / "source-facts.json").write_text(
        json.dumps(model, indent=2, sort_keys=True) + "\n"
    )
    (candidate / "requirements.json").write_text(
        json.dumps(plan["requirements"], indent=2, sort_keys=True) + "\n"
    )
    (candidate / "app.bicep").write_text(app_bicep)
    (candidate / "bicepconfig.json").write_text(
        json.dumps(bicepconfig, indent=2, sort_keys=True) + "\n"
    )
    (candidate / "resolved-plan.json").write_text(
        json.dumps(resolved, indent=2, sort_keys=True) + "\n"
    )
    return plan


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-model", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--remote", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--source-path", default=".")
    parser.add_argument("--expose-externally", action="store_true")
    parser.add_argument("--persist-data", action="store_true")
    args = parser.parse_args()
    model = json.loads(args.source_model.read_text())
    try:
        build_candidate(
            model,
            args.candidate,
            remote=args.remote,
            commit=args.commit,
            source_path=args.source_path,
            expose_externally=args.expose_externally,
            persist_data=args.persist_data,
        )
    except (KeyError, TypeError, ValueError) as exc:
        print(json.dumps({"valid": False, "errors": [str(exc)]}))
        return 1
    print(json.dumps({"valid": True, "candidate": str(args.candidate)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
