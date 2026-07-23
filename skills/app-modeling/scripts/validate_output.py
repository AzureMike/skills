#!/usr/bin/env python3

import argparse
import json
import os
from pathlib import Path
import posixpath
import re
import shutil
import subprocess
import sys
from urllib.parse import unquote


SECRET_NAME = re.compile(
    r"(?:PASSWORD|PASSWD|PWD|SECRET|APIKEY|API_KEY|TOKEN|ACCOUNTKEY|ACCOUNT_KEY|"
    r"CONNECTIONSTRING|CONNECTION_STRING|MASTER_KEY|COOKIESECRET|SESSIONSECRET|"
    r"CLIENT_SECRET|PRIVATE_KEY|JAAS_CONFIG)$",
    re.IGNORECASE,
)
MASKED_PLACEHOLDER = re.compile(
    r"\*{3,}|\b(?:REDACTED|CHANGEME|TODO)\b|"
    r"<\s*(?:password|secret|token|api[_ -]?key|connection[_ -]?string)\s*>",
    re.IGNORECASE,
)
SECRET_ARGUMENT = re.compile(
    r"--[A-Za-z0-9_-]*(?:password|passwd|secret|token|key|credential)"
    r"[A-Za-z0-9_-]*(?:=|\s+)[\"']?\$\{?([A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE,
)


class OutputValidator:
    def __init__(self, plan, contract, source, template, config, diagnostics, build_status):
        self.plan = plan
        self.contract = contract
        self.source = source
        self.template = template or {}
        self.config = config
        self.diagnostics = diagnostics
        self.build_status = build_status
        self.errors = []
        self.resources = normalize_resources(self.template)
        self.resource_by_symbol = {item["symbol"]: item for item in self.resources}
        self.workloads = normalize_workloads(self.resources)
        self.parameters = self.template.get("parameters", {})

    def error(self, code, path, message, evidence=None):
        self.errors.append(
            {
                "code": code,
                "path": path,
                "message": message,
                "evidence": evidence,
            }
        )

    def validate(self):
        self.validate_compile()
        self.validate_config()
        if not self.template:
            return self.errors
        self.validate_parameters()
        self.validate_resources()
        self.validate_workloads()
        self.validate_connections()
        self.validate_security()
        return self.errors

    def validate_compile(self):
        if self.build_status != 0:
            self.error(
                "BICEP_BUILD",
                "$.compile",
                f"Bicep build exited with status {self.build_status}.",
            )
        for diagnostic in self.diagnostics:
            self.error(
                "BICEP_DIAGNOSTIC",
                "$.compile",
                f"{diagnostic.get('ruleId')}: {diagnostic.get('message')}",
                diagnostic,
            )
        if re.search(r"\bany\s*\(", strip_comments(self.source)):
            self.error(
                "SCHEMA_ESCAPE",
                "$.appBicep",
                "any() is forbidden because it bypasses the pinned type contract.",
            )

    def validate_config(self):
        expected = self.contract.get("extension", {}).get("reference")
        actual = self.config.get("extensions", {}).get("radius") if isinstance(self.config, dict) else None
        if actual != expected:
            self.error(
                "EXTENSION_REF",
                "$.bicepconfig.extensions.radius",
                f"Expected {expected!r}, got {actual!r}.",
            )
        if self.config.get("experimentalFeaturesEnabled", {}).get("extensibility") is not True:
            self.error(
                "EXTENSIBILITY_DISABLED",
                "$.bicepconfig.experimentalFeaturesEnabled.extensibility",
                "The Radius extension requires extensibility=true.",
            )
        if analyzer_disabled(self.config):
            self.error(
                "DIAGNOSTIC_SUPPRESSION",
                "$.bicepconfig",
                "Analyzer diagnostics must not be disabled.",
            )

    def validate_parameters(self):
        type_names = {
            "string": "string",
            "int": "int",
            "bool": "bool",
            "object": "object",
            "array": "array",
        }
        planned = {
            parameter["name"]: parameter
            for parameter in self.plan.get("parameters", [])
            if isinstance(parameter, dict) and parameter.get("name")
        }
        if set(self.parameters) != set(planned):
            self.error(
                "PARAMETER_SET",
                "$.parameters",
                f"Expected parameters {sorted(planned)}, got {sorted(self.parameters)}.",
            )
        for name, expected in planned.items():
            actual = self.parameters.get(name)
            if not actual:
                continue
            expected_type = type_names.get(expected.get("type"))
            if expected.get("secure"):
                expected_type = (
                    "secureObject"
                    if expected.get("type") == "object"
                    else "securestring"
                )
            if actual.get("type") != expected_type:
                self.error(
                    "PARAMETER_TYPE",
                    f"$.parameters.{name}.type",
                    f"Expected {expected_type}, got {actual.get('type')}.",
                )
            if "default" in expected:
                if actual.get("defaultValue") != expected["default"]:
                    self.error(
                        "PARAMETER_DEFAULT",
                        f"$.parameters.{name}.defaultValue",
                        "Compiled parameter default differs from the validated plan.",
                    )
            elif "defaultValue" in actual:
                self.error(
                    "PARAMETER_DEFAULT",
                    f"$.parameters.{name}.defaultValue",
                    "A parameter without a planned default gained one.",
                )

    def validate_resources(self):
        plan_resources = {
            resource["id"]: resource
            for resource in self.plan.get("resources", [])
            if isinstance(resource, dict) and resource.get("id")
        }
        expected_types = {
            f"{resource.get('type')}@{resource.get('apiVersion')}"
            for resource in plan_resources.values()
        }
        allowed_compute = {
            "Radius.Compute/containerImages",
            "Radius.Compute/containers",
        }
        if self.plan.get("profile", {}).get("exposure") == "external":
            allowed_compute.add("Radius.Compute/routes")
        unexpected = [
            resource["fullType"]
            for resource in self.resources
            if resource["fullType"] not in expected_types
            and resource["qualifiedType"] not in allowed_compute
        ]
        if unexpected:
            self.error(
                "RESOURCE_UNPLANNED",
                "$.resources",
                f"Unplanned resources appeared: {sorted(unexpected)}.",
            )
        for resource_id, planned in plan_resources.items():
            path = f"$.resources.{resource_id}"
            actual = self.resource_by_symbol.get(resource_id)
            expected_type = f"{planned.get('type')}@{planned.get('apiVersion')}"
            if not actual:
                self.error(
                    "RESOURCE_MISSING",
                    path,
                    f"Planned resource symbol {resource_id!r} is missing.",
                )
                continue
            if actual["fullType"] != expected_type:
                self.error(
                    "RESOURCE_TYPE",
                    f"{path}.type",
                    f"Expected {expected_type}, got {actual['fullType']}.",
                )
            self.validate_binding(
                planned.get("name"),
                actual.get("name"),
                f"{path}.name",
            )
            for name, binding in planned.get("properties", {}).items():
                if name not in actual["properties"]:
                    self.error(
                        "PROPERTY_MISSING",
                        f"{path}.properties.{name}",
                        f"Property {name} is missing.",
                    )
                    continue
                self.validate_binding(
                    binding,
                    actual["properties"][name],
                    f"{path}.properties.{name}",
                )
            if planned.get("type") == "Radius.Core/applications":
                if actual["properties"].get("environment") != "[parameters('environment')]":
                    self.error(
                        "APPLICATION_ENVIRONMENT",
                        f"{path}.properties.environment",
                        "Application environment must reference the environment parameter.",
                    )
            else:
                if actual["properties"].get("environment") != "[parameters('environment')]":
                    self.error(
                        "RESOURCE_ENVIRONMENT",
                        f"{path}.properties.environment",
                        "Resource environment must reference the environment parameter.",
                    )
                if not any(
                    references_symbol(actual["properties"].get("application"), app_id)
                    for app_id, app in plan_resources.items()
                    if app.get("type") == "Radius.Core/applications"
                ):
                    self.error(
                        "RESOURCE_APPLICATION",
                        f"{path}.properties.application",
                        "Backing resource must reference the planned application.",
                    )

        actual_plan_types = [
            resource
            for resource in self.resources
            if resource["qualifiedType"] not in allowed_compute
        ]
        if len(actual_plan_types) != len(plan_resources):
            self.error(
                "RESOURCE_COUNT",
                "$.resources",
                f"Expected {len(plan_resources)} non-compute resources; found {len(actual_plan_types)}.",
            )

    def validate_workloads(self):
        plan_workloads = {
            workload["id"]: workload
            for workload in self.plan.get("workloads", [])
            if isinstance(workload, dict) and workload.get("id")
        }
        actual_names = {workload["name"] for workload in self.workloads}
        if actual_names != set(plan_workloads):
            self.error(
                "WORKLOAD_SET",
                "$.workloads",
                f"Expected workloads {sorted(plan_workloads)}, got {sorted(actual_names)}.",
            )
        for workload_id, planned in plan_workloads.items():
            path = f"$.workloads.{workload_id}"
            matches = [item for item in self.workloads if item["name"] == workload_id]
            if len(matches) != 1:
                self.error(
                    "WORKLOAD_COUNT",
                    path,
                    f"Expected one workload named {workload_id}; found {len(matches)}.",
                )
                continue
            actual = matches[0]
            self.validate_image(planned.get("image", {}), actual, f"{path}.image")
            if planned.get("command") and actual["body"].get("command") != planned["command"]:
                self.error(
                    "WORKLOAD_COMMAND",
                    f"{path}.command",
                    "Generated command differs from the validated plan.",
                )
            if planned.get("args") and actual["body"].get("args") != planned["args"]:
                self.error(
                    "WORKLOAD_ARGS",
                    f"{path}.args",
                    "Generated args differ from the validated plan.",
                )

            expected_ports = {
                port["name"]: {
                    "containerPort": port["containerPort"],
                    **(
                        {"protocol": port["protocol"]}
                        if port.get("protocol") and port["protocol"] != "TCP"
                        else {}
                    ),
                }
                for port in planned.get("ports", [])
            }
            actual_ports = actual["body"].get("ports", {})
            for name, expected in expected_ports.items():
                actual_port = actual_ports.get(name)
                if not actual_port or actual_port.get("containerPort") != expected["containerPort"]:
                    self.error(
                        "PORT_MISSING",
                        f"{path}.ports.{name}",
                        f"Expected container port {expected['containerPort']}.",
                    )
            if set(actual_ports) != set(expected_ports):
                self.error(
                    "PORT_SET",
                    f"{path}.ports",
                    f"Expected ports {sorted(expected_ports)}, got {sorted(actual_ports)}.",
                )

            expected_env = {
                entry["name"]: entry["binding"]
                for entry in planned.get("environment", [])
            }
            actual_env = actual["body"].get("env", {})
            if set(actual_env) != set(expected_env):
                self.error(
                    "ENV_SET",
                    f"{path}.environment",
                    f"Expected env {sorted(expected_env)}, got {sorted(actual_env)}.",
                )
            for name, binding in expected_env.items():
                if name not in actual_env:
                    continue
                self.validate_env_binding(
                    binding,
                    actual_env[name],
                    f"{path}.environment.{name}",
                )

            startup = "\n".join(
                value
                for key in ("command", "args")
                for value in strings(actual["body"].get(key))
            )
            if MASKED_PLACEHOLDER.search(startup):
                self.error(
                    "MASKED_PLACEHOLDER",
                    path,
                    "Startup configuration contains a masked or redacted placeholder.",
                )
            for name, body in actual_env.items():
                value = body.get("value") if isinstance(body, dict) else None
                if isinstance(value, str) and MASKED_PLACEHOLDER.search(value):
                    self.error(
                        "MASKED_PLACEHOLDER",
                        f"{path}.environment.{name}",
                        "Environment configuration contains a masked or redacted placeholder.",
                    )
            secure_environment = secure_environment_names(
                planned.get("environment", [])
            )
            exposed = sorted(
                {
                    match.group(1)
                    for match in SECRET_ARGUMENT.finditer(startup)
                    if match.group(1) in secure_environment
                }
            )
            if exposed:
                self.error(
                    "SECRET_PROCESS_ARGUMENT",
                    path,
                    f"Secrets must not be passed through process arguments: {exposed}.",
                )
            syntax_error = shell_syntax_error(actual["body"])
            if syntax_error:
                self.error(
                    "STARTUP_SYNTAX",
                    path,
                    syntax_error,
                )
            for generated in planned.get("generatedFiles", []):
                if generated.get("path") not in startup:
                    self.error(
                        "FILE_PATH",
                        f"{path}.generatedFiles",
                        f"Startup logic does not create {generated.get('path')}.",
                    )
                content_template = str(generated.get("contentTemplate", ""))
                if not content_template or content_template not in startup:
                    self.error(
                        "FILE_CONTENT_TEMPLATE",
                        f"{path}.generatedFiles",
                        "Generated-file content differs from the validated plan.",
                    )
                if not (re.search(r"\bumask\s+0?77\b", startup) or re.search(r"\bchmod\s+0?600\b", startup)):
                    self.error(
                        "FILE_MODE",
                        f"{path}.generatedFiles",
                        "Startup logic does not enforce mode 0600.",
                    )
                if not re.search(r"\btrap\b", startup) or not re.search(r"\brm\s+-f\b", startup):
                    self.error(
                        "FILE_CLEANUP",
                        f"{path}.generatedFiles",
                        "Startup logic lacks explicit signal/exit cleanup.",
                    )
            closure = environment_closure(actual["body"])
            if closure["missing"]:
                self.error(
                    "ENV_CLOSURE",
                    path,
                    f"Startup logic references undefined variables: {closure['missing']}.",
                    closure,
                )

        image_resources = [
            resource
            for resource in self.resources
            if resource["qualifiedType"] == "Radius.Compute/containerImages"
        ]
        planned_builds = len(
            {
                build_signature(workload.get("image", {}))
                for workload in plan_workloads.values()
                if workload.get("image", {}).get("kind") == "build"
            }
        )
        referenced_images = {
            referenced_symbol(workload["body"].get("image"))
            for workload in self.workloads
            if referenced_symbol(workload["body"].get("image"))
        }
        referenced_image_resources = {
            resource["symbol"]
            for resource in image_resources
            if resource["symbol"] in referenced_images
        }
        if len(referenced_image_resources) != planned_builds:
            self.error(
                "IMAGE_RESOURCE_COUNT",
                "$.workloads",
                f"Expected {planned_builds} referenced image resources; found {len(referenced_image_resources)}.",
            )

    def validate_image(self, planned, actual_workload, path):
        actual_image = actual_workload["body"].get("image")
        if planned.get("kind") == "published":
            if actual_image != planned.get("image"):
                self.error(
                    "IMAGE_VALUE",
                    path,
                    f"Expected image {planned.get('image')!r}, got {actual_image!r}.",
                )
            return
        image_symbol = referenced_symbol(actual_image)
        image_resource = self.resource_by_symbol.get(image_symbol)
        if not image_resource or image_resource["qualifiedType"] != "Radius.Compute/containerImages":
            self.error(
                "IMAGE_RESOURCE",
                path,
                "Source-built workload must reference a Radius containerImages resource.",
            )
            return
        build = image_resource["properties"].get("build", {})
        source = build.get("source")
        if not source_matches_plan(source, planned, self.plan.get("source", {})):
            self.error(
                "BUILD_SOURCE",
                f"{path}.source",
                f"Build source {source!r} does not match the exact repository and ref.",
            )
        expected_dockerfile = planned.get("dockerfile")
        actual_dockerfile = build.get("dockerfile", "Dockerfile")
        if expected_dockerfile != actual_dockerfile:
            self.error(
                "BUILD_DOCKERFILE",
                f"{path}.dockerfile",
                f"Expected {expected_dockerfile!r}, got {actual_dockerfile!r}.",
            )
        if planned.get("platforms") and build.get("platforms") != planned["platforms"]:
            self.error(
                "BUILD_PLATFORMS",
                f"{path}.platforms",
                f"Expected {planned['platforms']!r}, got {build.get('platforms')!r}.",
            )

    def validate_connections(self):
        actual = []
        for resource in self.resources:
            if resource["qualifiedType"] != "Radius.Compute/containers":
                continue
            for name, body in resource["properties"].get("connections", {}).items():
                actual.append(
                    {
                        "containerResource": resource["symbol"],
                        "name": name,
                        "source": body.get("source"),
                        "disableDefaultEnvVars": body.get("disableDefaultEnvVars", False),
                        "workloads": list(resource["properties"].get("containers", {})),
                    }
                )
        for index, planned in enumerate(self.plan.get("connections", [])):
            path = f"$.connections[{index}]"
            matches = [
                item
                for item in actual
                if item["name"] == planned.get("name")
                and planned.get("targetWorkload") in item["workloads"]
                and references_symbol(item["source"], planned.get("sourceResource"))
            ]
            if not matches:
                self.error(
                    "CONNECTION_MISSING",
                    path,
                    "Planned source, target, and connection name were not emitted.",
                )
                continue
            if planned.get("disableDefaultEnvVars") and not any(
                item["disableDefaultEnvVars"] is True for item in matches
            ):
                self.error(
                    "CONNECTION_DEFAULT_ENV",
                    path,
                    "disableDefaultEnvVars=true was not preserved.",
                )
        expected_backing = {
            resource["id"]
            for resource in self.plan.get("resources", [])
            if resource.get("type") != "Radius.Core/applications"
        }
        actual_sources = {
            symbol
            for item in actual
            for symbol in expected_backing
            if references_symbol(item["source"], symbol)
        }
        if actual_sources != expected_backing:
            self.error(
                "CONNECTION_SOURCE_SET",
                "$.connections",
                f"Expected backing connection sources {sorted(expected_backing)}, got {sorted(actual_sources)}.",
            )

        routes = [
            resource
            for resource in self.resources
            if resource["qualifiedType"] == "Radius.Compute/routes"
        ]
        external = self.plan.get("profile", {}).get("exposure") == "external"
        if external and not routes:
            self.error("ROUTE_MISSING", "$.profile.exposure", "External exposure requires a route.")
        if not external and routes:
            self.error("ROUTE_UNREQUESTED", "$.profile.exposure", "A route was emitted without external exposure.")

    def validate_security(self):
        secure_parameters = {
            name
            for name, body in self.parameters.items()
            if str(body.get("type", "")).lower() in {"securestring", "secureobject"}
        }
        planned_secure = {
            parameter["name"]
            for parameter in self.plan.get("parameters", [])
            if parameter.get("secure")
        }
        if secure_parameters != planned_secure:
            self.error(
                "SECURE_PARAMETER_SET",
                "$.parameters",
                f"Expected secure parameters {sorted(planned_secure)}, got {sorted(secure_parameters)}.",
            )
        for name, body in self.parameters.items():
            if (
                str(body.get("type", "")).lower()
                in {"securestring", "secureobject"}
                and "defaultValue" in body
            ):
                self.error(
                    "SECURE_PARAMETER_DEFAULT",
                    f"$.parameters.{name}",
                    "Secure parameters must not have defaults.",
                )
            if (
                SECRET_NAME.search(name)
                and str(body.get("type", "")).lower()
                not in {"securestring", "secureobject"}
            ):
                self.error(
                    "SECRET_PARAMETER_TYPE",
                    f"$.parameters.{name}",
                    "Secret-like parameters must compile as securestring.",
                )

    def validate_binding(self, binding, actual, path):
        if not isinstance(binding, dict):
            self.error("BINDING_PLAN", path, "Planned binding is invalid.")
            return
        kind = binding.get("kind")
        if kind == "literal":
            valid = actual == binding.get("value")
        elif kind in {"parameter", "secureParameter"}:
            valid = actual == f"[parameters('{binding.get('parameter')}')]"
        elif kind == "resourceProperty":
            valid = actual == (
                f"[reference('{binding.get('resource')}').properties."
                f"{binding.get('property')}]"
            )
        else:
            valid = False
        if not valid:
            self.error(
                "BINDING_VALUE",
                path,
                f"Generated value {actual!r} does not match planned {kind} binding.",
            )

    def validate_env_binding(self, binding, actual, path):
        kind = binding.get("kind") if isinstance(binding, dict) else None
        if kind == "managedSecret":
            secret = actual.get("valueFrom", {}).get("secretKeyRef", {})
            valid = (
                secret.get("secretName")
                == f"[reference('{binding.get('resource')}').properties.secrets.name]"
                and secret.get("key") == binding.get("key")
            )
        elif kind == "runtimeExpansion":
            valid = actual.get("value") == binding.get("expression")
        else:
            valid = "value" in actual
            if valid:
                before = len(self.errors)
                self.validate_binding(binding, actual["value"], path)
                valid = len(self.errors) == before
                if not valid:
                    return
        if not valid:
            self.error(
                "ENV_BINDING",
                path,
                f"Generated environment binding does not match planned {kind} binding.",
                actual,
            )


def normalize_resources(template):
    result = []
    for symbol, raw in template.get("resources", {}).items():
        full_type = raw.get("type", "")
        qualified, _, api_version = full_type.rpartition("@")
        result.append(
            {
                "symbol": symbol,
                "fullType": full_type,
                "qualifiedType": qualified or full_type,
                "apiVersion": api_version or None,
                "name": raw.get("properties", {}).get("name"),
                "properties": raw.get("properties", {}).get("properties", {}),
                "raw": raw,
            }
        )
    return result


def normalize_workloads(resources):
    result = []
    for resource in resources:
        if resource["qualifiedType"] != "Radius.Compute/containers":
            continue
        for name, body in resource["properties"].get("containers", {}).items():
            result.append(
                {
                    "containerResource": resource["symbol"],
                    "name": name,
                    "body": body,
                }
            )
    return result


def references_symbol(value, symbol):
    return isinstance(value, str) and f"reference('{symbol}')" in value


def referenced_symbol(value):
    if not isinstance(value, str):
        return None
    return re.match(r"^\[reference\('([^']+)'\)\.properties\.imageReference\]$", value).group(1) if re.match(
        r"^\[reference\('([^']+)'\)\.properties\.imageReference\]$", value
    ) else None


def source_matches_plan(source, planned, source_plan):
    if not isinstance(source, str):
        return False
    ref = re.search(r"[?&]ref=([^&#]+)", source)
    if not ref or ref.group(1) != planned.get("ref"):
        return False
    repository = planned.get("repositoryUrl", "").removesuffix(".git")
    normalized_source = unquote(source.removeprefix("git::").split("?", 1)[0])
    matched_repository = next(
        (
            candidate
            for candidate in (f"{repository}.git", repository)
            if normalized_source == candidate
            or normalized_source.startswith(f"{candidate}//")
        ),
        None,
    )
    if not matched_repository:
        return False
    actual_subdirectory = normalized_source[len(matched_repository) :]
    actual_subdirectory = (
        actual_subdirectory[2:].strip("/")
        if actual_subdirectory.startswith("//")
        else "."
    )
    expected_subdirectory = posixpath.normpath(
        posixpath.join(
            source_plan.get("subdirectory") or ".",
            planned.get("context") or ".",
        )
    ).strip("/")
    return (actual_subdirectory or ".") == (expected_subdirectory or ".")


def build_signature(image):
    return json.dumps(
        {
            key: image.get(key)
            for key in (
                "repositoryUrl",
                "ref",
                "context",
                "dockerfile",
                "platforms",
            )
        },
        sort_keys=True,
    )


def environment_closure(body):
    env = body.get("env", {}) if isinstance(body, dict) else {}
    defined = {
        *env,
        "HOME",
        "HOSTNAME",
        "PATH",
        "PWD",
        "SHELL",
        "SHLVL",
        "TERM",
        "TMPDIR",
    }
    referenced = set()
    scripts = [
        value
        for key in ("command", "args")
        for value in strings(body.get(key))
    ]
    for script in scripts:
        for match in re.finditer(
            r"(?<!\\)\$(?:\{([A-Za-z_][A-Za-z0-9_]*)[^}]*\}|"
            r"([A-Za-z_][A-Za-z0-9_]*))",
            script,
        ):
            referenced.add(match.group(1) or match.group(2))
        for match in re.finditer(
            r"(?:^|\n)\s*(?:export\s+|local\s+|readonly\s+)?"
            r"([A-Za-z_][A-Za-z0-9_]*)=",
            script,
        ):
            defined.add(match.group(1))
        for match in re.finditer(
            r"\bfor\s+([A-Za-z_][A-Za-z0-9_]*)\s+in\b",
            script,
        ):
            defined.add(match.group(1))
    return {
        "defined": sorted(defined),
        "referenced": sorted(referenced),
        "missing": sorted(referenced - defined),
    }


def secure_environment_names(environment):
    secure = set()
    for entry in environment:
        if not isinstance(entry, dict) or not entry.get("name"):
            continue
        binding = entry.get("binding")
        kind = binding.get("kind") if isinstance(binding, dict) else None
        if kind in {"secureParameter", "managedSecret"}:
            secure.add(entry["name"])
        elif kind == "runtimeExpansion":
            referenced = set(
                re.findall(
                    r"\$\(([A-Za-z_][A-Za-z0-9_]*)\)",
                    str(binding.get("expression", "")),
                )
            )
            if referenced & secure:
                secure.add(entry["name"])
    return secure


def shell_syntax_error(body):
    command = strings(body.get("command")) if isinstance(body, dict) else []
    args = strings(body.get("args")) if isinstance(body, dict) else []
    if not command:
        return None
    shell_name = Path(command[0]).name.lower()
    if shell_name not in {"sh", "ash", "dash", "bash"}:
        return None
    combined = command[1:] + args
    try:
        command_index = combined.index("-c")
    except ValueError:
        return None
    if command_index + 1 >= len(combined):
        return "A shell -c command is missing its script argument."
    script = combined[command_index + 1]
    validator_name = "bash" if shell_name == "bash" else "sh"
    validator = shutil.which(validator_name)
    if not validator:
        return None
    try:
        result = subprocess.run(
            [validator, "-n"],
            input=script,
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return f"Unable to check shell startup syntax: {error}."
    if result.returncode == 0:
        return None
    detail = (result.stderr or result.stdout).strip()
    return f"Shell startup syntax is invalid: {detail or f'exit {result.returncode}'}."


def strings(value):
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []


def strip_comments(source):
    return re.sub(r"/\*.*?\*/", "", re.sub(r"//.*$", "", source, flags=re.MULTILINE), flags=re.DOTALL)


def analyzer_disabled(value):
    if isinstance(value, dict):
        if value.get("level") == "off":
            return True
        return any(analyzer_disabled(item) for item in value.values())
    if isinstance(value, list):
        return any(analyzer_disabled(item) for item in value)
    return False


def parse_sarif(text):
    candidates = [text]
    first = text.find("{")
    last = text.rfind("}")
    if first >= 0 and last > first:
        candidates.append(text[first : last + 1])
    for candidate in candidates:
        try:
            document = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(document, dict) and isinstance(document.get("runs"), list):
            diagnostics = []
            for run in document["runs"]:
                for result in run.get("results", []):
                    location = (
                        result.get("locations", [{}])[0]
                        .get("physicalLocation", {})
                    )
                    diagnostics.append(
                        {
                            "ruleId": result.get("ruleId"),
                            "level": result.get("level", "warning"),
                            "message": result.get("message", {}).get("text", ""),
                            "file": location.get("artifactLocation", {}).get("uri"),
                            "line": location.get("region", {}).get("startLine"),
                        }
                    )
            return document, diagnostics
    return None, [
        {
            "ruleId": "SARIF_PARSE",
            "level": "error",
            "message": "Bicep did not emit valid SARIF diagnostics.",
        }
    ]


def find_bicep():
    configured = os.environ.get("BICEP")
    candidates = [
        Path(configured) if configured else None,
        Path.home() / ".rad" / "bin" / "bicep",
        Path(shutil.which("bicep")) if shutil.which("bicep") else None,
    ]
    return next((path for path in candidates if path and path.is_file()), None)


def read_json(path, label):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        raise SystemExit(f"{label} not found: {path}")
    except json.JSONDecodeError as error:
        raise SystemExit(f"{label} is invalid JSON: {error}")


def main():
    parser = argparse.ArgumentParser(description="Validate generated Radius Bicep against a typed plan.")
    parser.add_argument("--plan", required=True)
    parser.add_argument("--app-bicep", required=True)
    parser.add_argument("--bicepconfig", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument(
        "--contract",
        default=str(Path(__file__).resolve().parent.parent / "assets" / "radius-contract.json"),
    )
    parser.add_argument(
        "--extension",
        default=str(
            Path(__file__).resolve().parent.parent
            / "assets"
            / "radius-validation-extension.tgz"
        ),
    )
    args = parser.parse_args()

    output_dir = Path(args.output_dir).resolve()
    if output_dir.exists() and any(output_dir.iterdir()):
        raise SystemExit(f"Output directory must be empty: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    build_dir = output_dir / "build"
    build_dir.mkdir()

    plan = read_json(args.plan, "Plan")
    contract = read_json(args.contract, "Contract")
    delivered_config = read_json(args.bicepconfig, "bicepconfig.json")
    source_path = Path(args.app_bicep)
    source = source_path.read_text()
    extension = Path(args.extension)
    bicep = find_bicep()
    if not bicep:
        raise SystemExit("Bicep CLI was not found.")
    if not extension.is_file():
        raise SystemExit(f"Validation extension was not found: {extension}")

    shutil.copy2(source_path, build_dir / "app.bicep")
    shutil.copy2(extension, build_dir / "radius-validation-extension.tgz")
    (build_dir / "bicepconfig.json").write_text(
        json.dumps(
            {"extensions": {"radius": "./radius-validation-extension.tgz"}},
            indent=2,
        )
        + "\n"
    )
    build = subprocess.run(
        [
            str(bicep),
            "build",
            str(build_dir / "app.bicep"),
            "--diagnostics-format",
            "sarif",
            "--stdout",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    (output_dir / "bicep-stdout.json").write_text(build.stdout)
    (output_dir / "bicep-stderr.txt").write_text(build.stderr)
    sarif, diagnostics = parse_sarif(build.stderr)
    if sarif:
        (output_dir / "sarif.json").write_text(json.dumps(sarif, indent=2) + "\n")
    try:
        template = json.loads(build.stdout) if build.stdout else None
    except json.JSONDecodeError:
        template = None

    validator = OutputValidator(
        plan,
        contract,
        source,
        template,
        delivered_config,
        diagnostics,
        build.returncode,
    )
    errors = validator.validate()
    report = {
        "schemaVersion": 1,
        "valid": not errors,
        "compile": {
            "status": build.returncode,
            "diagnostics": diagnostics,
            "bicep": str(bicep),
            "validationExtension": str(extension),
        },
        "counts": {"errors": len(errors)},
        "errors": errors,
    }
    (output_dir / "validation-result.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n"
    )
    print(
        json.dumps(
            {
                "valid": report["valid"],
                "errors": len(errors),
                "output": str(output_dir),
            },
            sort_keys=True,
        )
    )
    raise SystemExit(0 if report["valid"] else 1)


if __name__ == "__main__":
    main()
