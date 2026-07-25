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

DEPENDENCY_TYPES = {
    "mysql": "Radius.Data/mySqlDatabases",
    "postgresql": "Radius.Data/postgreSqlDatabases",
    "sql-server": "Radius.Data/sqlServerDatabases",
    "mongodb": "Radius.Data/mongoDatabases",
    "redis": "Radius.Data/redisCaches",
    "kafka": "Radius.Messaging/kafka",
    "rabbitmq": "Radius.Messaging/rabbitMQ",
    "ai-model": "Radius.AI/models",
    "ai-search": "Radius.AI/search",
    "object-storage": "Radius.Storage/objectStorage",
}
PROVIDER_INPUT_PROPERTIES = {
    "ai-model": {"deploymentOrModel": "model"},
    "object-storage": {"container": "containerName"},
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


def pascal(value: str) -> str:
    return "".join(word.capitalize() for word in re.findall(r"[A-Za-z0-9]+", value))


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

    available_types = {
        name.split("@", 1)[0] for name in contract["resourceTypes"]
    }
    for index, dependency in enumerate(model["dependencies"]):
        qualified_type = DEPENDENCY_TYPES[dependency["kind"]]
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
        if dependency["kind"] == "kafka":
            required_slots = {
                "bootstrapServers",
                "security.protocol",
                "sasl.mechanism",
                "sasl.jaas.config",
            }
        elif dependency["kind"] == "postgresql" and "connectionUri" in slots:
            required_slots = {"connectionUri"}
        else:
            profile = contract["protocolProfiles"].get(qualified_type, {})
            required_slots = {
                value.partition("=")[0]
                for value in (
                    profile.get("requiredClientSettings", [])
                    + profile.get("runtimeRequiredClientSettings", [])
                )
            }
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
            if connection_uri.get("scheme") not in schemes:
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
            {"name": name, "secure": properties["secure"]}
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
        DEPENDENCY_TYPES[item["kind"]] for item in model["dependencies"]
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


def input_expression(
    dependency: dict[str, Any],
    name: str,
    parameters: dict[str, dict[str, Any]],
) -> Any:
    item = next((item for item in dependency["inputs"] if item["name"] == name), None)
    if item is None:
        raise ValueError(f"{dependency['id']}: missing input {name!r}")
    if item["value"]["kind"] == "literal":
        return item["value"]["value"]
    parameter = identifier(dependency["id"]) + pascal(name)
    parameters.setdefault(
        parameter,
        {"secure": bool(SECRET_NAME.search(name) or name == "password")},
    )
    return Expression(parameter)


def native_setting(
    dependency: dict[str, Any],
    slot: str,
) -> dict[str, Any] | None:
    return next(
        (item for item in dependency["settings"] if item["slot"] == slot),
        None,
    )


def setting_value(
    *,
    dependency: dict[str, Any],
    slot: str,
    symbol: str,
    qualified_type: str,
    contract: dict[str, Any],
    parameters: dict[str, dict[str, Any]],
) -> tuple[str, Any, str | None]:
    profile = contract["protocolProfiles"].get(qualified_type, {})
    binding = profile.get("binding", {})
    if slot == "host":
        return "value", Expression(f"{symbol}.properties.{binding['hostProperty']}"), None
    if slot == "port":
        return "value", binding.get("portLiteral", binding.get("port")), None
    if slot in {"database", "username", "password"}:
        value = input_expression(dependency, binding[f"{slot}Input"], parameters)
        return ("secret" if slot == "password" else "value"), value, None
    if slot == "endpoint":
        return "value", Expression(f"{symbol}.properties.{binding['endpointProperty']}"), None
    if slot == "apiKey":
        return "managedSecret", Expression(f"{symbol}.properties.secrets.name"), binding["apiKeySecret"]
    if slot in {"indexName", "apiVersion", "deploymentOrModel", "container", "accountName"}:
        if slot == "accountName":
            return (
                "value",
                Expression(f"{symbol}.properties.{binding['accountNameProperty']}"),
                None,
            )
        return "value", input_expression(dependency, slot, parameters), None
    if slot == "accountKeyOrConnectionString":
        key = binding.get("connectionStringSecret", binding.get("accountKeySecret"))
        return "managedSecret", Expression(f"{symbol}.properties.secrets.name"), key
    if slot == "connectionUri":
        return "managedSecret", Expression(f"{symbol}.properties.secrets.name"), binding["uriSecret"]
    if slot == "uri":
        return "managedSecret", Expression(f"{symbol}.properties.secrets.name"), binding["uriSecret"]
    if slot == "tls":
        return "value", True, None
    if slot == "certificateValidation":
        return "value", True, None
    if slot == "authMode":
        return "value", "connectionString", None
    if slot == "bootstrapServers":
        transform = binding["bootstrapTransform"].replace(
            "<host>", f"${{{symbol}.properties.host}}"
        )
        escaped = transform.replace("\\", "\\\\").replace("'", "\\'")
        return "value", Expression("'" + escaped + "'"), None
    if slot == "security.protocol":
        return "value", "SASL_SSL", None
    if slot == "sasl.mechanism":
        return "value", "PLAIN", None
    raise ValueError(f"{dependency['id']}: unsupported contract slot {slot!r}")


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
                "name": model["application"]["name"],
                "properties": {"environment": Expression("environment")},
            },
        }
    )

    for dependency in model["dependencies"]:
        symbol = identifier(dependency["id"])
        qualified_type = DEPENDENCY_TYPES[dependency["kind"]]
        recipe = contract["azureRecipeMappings"].get(qualified_type, {})
        name_parameter = symbol + "Name"
        parameters[name_parameter] = {"secure": False}
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
            property_name = PROVIDER_INPUT_PROPERTIES.get(
                dependency["kind"], {}
            ).get(item["name"], item["name"])
            if property_name not in schema_properties:
                continue
            properties[property_name] = input_expression(
                dependency, item["name"], parameters
            )
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
            build_path = app_path + context
            source = remote.rstrip("/")
            if source.endswith(".git"):
                source = source[:-4]
            build_source = f"git::{source}.git//{build_path}?ref={commit}"
            build: dict[str, Any] = {
                "source": build_source,
                "dockerfile": image["dockerfile"],
            }
            resources.append(
                {
                    "symbol": image_symbol,
                    "type": available["Radius.Compute/containerImages"],
                    "body": {
                        "name": workload["name"] + "-image",
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
            secret_key = workload_symbol + "_" + setting["name"]
            secret_entries[secret_key] = {"value": Expression(parameter)}
            secret_env_keys[(workload["id"], setting["name"])] = secret_key
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
            if (
                dependency["kind"] == "postgresql"
                and "connectionUri" in settings_by_slot
            ):
                uri = settings_by_slot["connectionUri"]
                uri_key = uri["delivery"]["name"]
                helpers = {
                    component: (
                        f"RADIUS_{symbol.upper()}_{component.upper()}"
                    )
                    for component in ("host", "username", "password", "database")
                }
                component_ledger: list[dict[str, Any]] = []
                for component in ("host", "username", "password", "database"):
                    value_kind, value, _ = setting_value(
                        dependency=dependency,
                        slot=component,
                        symbol=symbol,
                        qualified_type=qualified_type,
                        contract=contract,
                        parameters=parameters,
                    )
                    key = helpers[component]
                    if value_kind == "secret":
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
                        requirements["secretEnvironment"].append({"key": key})
                        delivery = {
                            "kind": "secretKeyRef",
                            "key": key,
                            "secretKey": secret_key,
                        }
                    else:
                        env[key] = {"value": value}
                        delivery = {"kind": "env", "key": key}
                    component_ledger.append(
                        {
                            "name": component,
                            "evidence": uri["citation"],
                            "delivery": delivery,
                        }
                    )
                port = (
                    contract["protocolProfiles"][qualified_type]["binding"][
                        "portLiteral"
                    ]
                )
                shell_encoder = (
                    "urlencode() { input=$1; output=''; LC_ALL=C; "
                    "while [ -n \"$input\" ]; do "
                    "char=${input%\"${input#?}\"}; input=${input#?}; "
                    "case \"$char\" in [a-zA-Z0-9.~_-]) "
                    "output=\"${output}${char}\" ;; *) "
                    "code=$(printf '%d' \"'$char\"); "
                    "hex=$(printf '%02X' \"$((code & 255))\"); "
                    "output=\"${output}%${hex}\" ;; esac; done; "
                    "printf '%s' \"$output\"; }; "
                    f'RADIUS_URI_USERNAME=$(urlencode "${helpers["username"]}"); '
                    f'RADIUS_URI_PASSWORD=$(urlencode "${helpers["password"]}"); '
                    f'RADIUS_URI_DATABASE=$(urlencode "${helpers["database"]}"); '
                    f'export {uri_key}="{uri["scheme"]}://'
                    f'${{RADIUS_URI_USERNAME}}:${{RADIUS_URI_PASSWORD}}@'
                    f'${{{helpers["host"]}}}:{port}/'
                    '${RADIUS_URI_DATABASE}?sslmode=require"'
                )
                runtime_wrappers.append(shell_encoder)
                ledger.extend(
                    component_ledger
                    + [
                        {
                            "name": "port",
                            "evidence": uri["citation"],
                            "delivery": {
                                "kind": "runtimeConfig",
                                "key": uri_key,
                                "value": port,
                            },
                        },
                        {
                            "name": "sslmode",
                            "evidence": uri["citation"],
                            "delivery": {
                                "kind": "runtimeConfig",
                                "key": uri_key,
                                "value": "require",
                            },
                        },
                        {
                            "name": "connectionUri",
                            "evidence": uri["citation"],
                            "delivery": {
                                "kind": "runtimeConfig",
                                "key": uri_key,
                                "value": f"{uri['scheme']}://",
                            },
                        },
                    ]
                )
                skipped_slots.add("connectionUri")
            if dependency["kind"] == "kafka":
                composite = settings_by_slot["sasl.jaas.config"]
                composite_key = composite["delivery"]["name"]
                secret_key = f"RADIUS_{symbol.upper()}_CONNECTION_STRING"
                env[secret_key] = {
                    "valueFrom": {
                        "secretKeyRef": {
                            "secretName": Expression(
                                f"{symbol}.properties.secrets.name"
                            ),
                            "key": "connectionString",
                        }
                    }
                }
                requirements["secretEnvironment"].append({"key": secret_key})
                runtime_wrappers.append(
                    f'export {composite_key}="org.apache.kafka.common.security.plain.'
                    f'PlainLoginModule required username=\\"\\$ConnectionString\\" '
                    f'password=\\"${secret_key}\\";"'
                )
                ledger.extend(
                    [
                        {
                            "name": "jaas.username",
                            "evidence": composite["citation"],
                            "delivery": {
                                "kind": "runtimeConfig",
                                "key": composite_key,
                                "value": "$ConnectionString",
                            },
                        },
                        {
                            "name": "jaas.password",
                            "evidence": composite["citation"],
                            "delivery": {
                                "kind": "secretKeyRef",
                                "key": secret_key,
                                "secretKey": "connectionString",
                            },
                        },
                    ]
                )

            for slot, setting in settings_by_slot.items():
                if slot == "sasl.jaas.config" or slot in skipped_slots:
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
                    parameter = value.text
                    secret_key = workload_symbol + "_" + key
                    secret_entries[secret_key] = {"value": Expression(parameter)}
                    secret_env_keys[(workload["id"], key)] = secret_key
                    env[key] = {
                        "valueFrom": {
                            "secretKeyRef": {
                                "secretName": Expression("applicationSecrets.name"),
                                "key": secret_key,
                            }
                        }
                    }
                    requirements["secretEnvironment"].append({"key": key})
                    ledger_kind = "secretKeyRef"
                    ledger_extra = {"secretKey": secret_key}
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
                "body": {"name": workload["name"], "properties": properties},
                "containerName": container_name,
            }
        )

    if secret_entries:
        secret_resource = {
            "symbol": "applicationSecrets",
            "type": available["Radius.Security/secrets"],
            "body": {
                "name": model["application"]["name"] + "-secrets",
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
    args = parser.parse_args()
    model = json.loads(args.source_model.read_text())
    try:
        build_candidate(
            model,
            args.candidate,
            remote=args.remote,
            commit=args.commit,
            source_path=args.source_path,
        )
    except (KeyError, TypeError, ValueError) as exc:
        print(json.dumps({"valid": False, "errors": [str(exc)]}))
        return 1
    print(json.dumps({"valid": True, "candidate": str(args.candidate)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
