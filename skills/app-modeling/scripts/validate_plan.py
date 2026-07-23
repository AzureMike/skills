#!/usr/bin/env python3

import argparse
import json
from pathlib import Path
import re
import sys


SCHEMA_VERSION = 1
TOP_LEVEL_KEYS = {
    "schemaVersion",
    "source",
    "profile",
    "reviewedEvidence",
    "parameters",
    "resources",
    "workloads",
    "connections",
    "ambiguities",
    "unsupported",
}
SECRET_NAME = re.compile(
    r"(?:PASSWORD|PASSWD|PWD|SECRET|APIKEY|API_KEY|TOKEN|ACCOUNTKEY|ACCOUNT_KEY|"
    r"CONNECTIONSTRING|CONNECTION_STRING|MASTER_KEY|COOKIESECRET|SESSIONSECRET|"
    r"CLIENT_SECRET|PRIVATE_KEY|JAAS_CONFIG)$",
    re.IGNORECASE,
)
MUTABLE_REF = re.compile(r"^(?:HEAD|edge|latest|main|master|dev|develop|nightly)$", re.IGNORECASE)
IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
EVIDENCE_ID = re.compile(r"^[EI][0-9]+$")
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
URI_CONSUMER = re.compile(
    r"(?:URL|URI|DSN|CONNECTION[_ -]?STRING)",
    re.IGNORECASE,
)


class SchemaValidator:
    def __init__(self, schema):
        self.root = schema
        self.errors = []

    def validate(self, value):
        self.visit(value, self.root, "$")
        return self.errors

    def error(self, path, message):
        self.errors.append(
            {
                "code": "PLAN_SCHEMA",
                "path": path,
                "message": message,
                "evidence": None,
            }
        )

    def visit(self, value, schema, path):
        if schema is True:
            return
        if schema is False:
            self.error(path, "Value is forbidden by the plan schema.")
            return
        if not isinstance(schema, dict):
            return
        if "$ref" in schema:
            resolved = self.resolve(schema["$ref"])
            if resolved is None:
                self.error(path, f"Unresolved schema reference {schema['$ref']!r}.")
                return
            self.visit(value, resolved, path)
            return
        if "const" in schema and value != schema["const"]:
            self.error(path, f"Expected constant {schema['const']!r}.")
        if "enum" in schema and value not in schema["enum"]:
            self.error(path, f"Value must be one of {schema['enum']!r}.")

        expected_types = schema.get("type")
        if expected_types:
            expected_types = (
                expected_types if isinstance(expected_types, list) else [expected_types]
            )
            if not any(json_type_matches(value, item) for item in expected_types):
                self.error(path, f"Expected JSON type {expected_types!r}.")
                return

        if isinstance(value, dict):
            properties = schema.get("properties", {})
            for required in schema.get("required", []):
                if required not in value:
                    self.error(f"{path}.{required}", "Required property is missing.")
            additional = schema.get("additionalProperties", True)
            for key, nested in value.items():
                child_path = f"{path}.{key}"
                if key in properties:
                    self.visit(nested, properties[key], child_path)
                elif additional is False:
                    self.error(child_path, "Unknown property.")
                elif isinstance(additional, dict):
                    self.visit(nested, additional, child_path)
        elif isinstance(value, list):
            minimum = schema.get("minItems")
            if minimum is not None and len(value) < minimum:
                self.error(path, f"Expected at least {minimum} items.")
            if schema.get("uniqueItems"):
                canonical = [json.dumps(item, sort_keys=True) for item in value]
                if len(canonical) != len(set(canonical)):
                    self.error(path, "Array items must be unique.")
            item_schema = schema.get("items")
            if item_schema is not None:
                for index, nested in enumerate(value):
                    self.visit(nested, item_schema, f"{path}[{index}]")
        elif isinstance(value, str):
            minimum = schema.get("minLength")
            if minimum is not None and len(value) < minimum:
                self.error(path, f"String must contain at least {minimum} characters.")
            pattern = schema.get("pattern")
            if pattern and not re.search(pattern, value):
                self.error(path, f"String does not match {pattern!r}.")
        elif isinstance(value, int) and not isinstance(value, bool):
            minimum = schema.get("minimum")
            maximum = schema.get("maximum")
            if minimum is not None and value < minimum:
                self.error(path, f"Value must be at least {minimum}.")
            if maximum is not None and value > maximum:
                self.error(path, f"Value must be at most {maximum}.")

    def resolve(self, reference):
        if not reference.startswith("#/"):
            return None
        value = self.root
        for token in reference[2:].split("/"):
            token = token.replace("~1", "/").replace("~0", "~")
            if not isinstance(value, dict) or token not in value:
                return None
            value = value[token]
        return value


def json_type_matches(value, expected):
    return {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "null": value is None,
    }.get(expected, False)


class Validator:
    def __init__(self, plan, facts, contract):
        self.plan = plan
        self.facts = facts
        self.contract = contract
        self.errors = []
        self.parameters = {}
        self.resources = {}
        self.workloads = {}
        self.review_map = {}
        self.evidence = {
            item.get("id"): item
            for item in facts.get("evidence", [])
            if isinstance(item, dict) and item.get("id")
        }
        self.evidence_text = collect_evidence_text(self.facts)

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
        self.validate_shape()
        self.validate_contract()
        self.validate_source_and_profile()
        self.validate_evidence()
        self.validate_evidence_alignment()
        self.validate_parameters()
        self.validate_resources()
        self.validate_workloads()
        self.validate_operational_secrets()
        self.validate_connections()
        self.validate_connection_namespaces()
        self.validate_resolution()
        return self.errors

    def validate_contract(self):
        for resource_type, profile in self.contract.get("protocolProfiles", {}).items():
            required_names = set()
            for index, requirement in enumerate(profile.get("requiredClientSettings", [])):
                if not isinstance(requirement, str) or not requirement.strip():
                    self.error(
                        "CONTRACT_PROTOCOL_SETTING",
                        f"$.contract.protocolProfiles.{resource_type}.requiredClientSettings[{index}]",
                        "Protocol requirements must be non-empty strings.",
                    )
                elif MASKED_PLACEHOLDER.search(requirement):
                    self.error(
                        "CONTRACT_MASKED_PLACEHOLDER",
                        f"$.contract.protocolProfiles.{resource_type}.requiredClientSettings[{index}]",
                        "Protocol requirements must not contain masked or redacted placeholders.",
                    )
                else:
                    required_names.add(requirement.split("=", 1)[0])
            for group_index, group in enumerate(
                profile.get("requiredAnyClientSettings", [])
            ):
                if not isinstance(group, list) or not group:
                    self.error(
                        "CONTRACT_PROTOCOL_ALTERNATIVE",
                        f"$.contract.protocolProfiles.{resource_type}.requiredAnyClientSettings[{group_index}]",
                        "Protocol setting alternatives must be a non-empty array.",
                    )
                    continue
                for setting_index, setting_name in enumerate(group):
                    if (
                        not isinstance(setting_name, str)
                        or not setting_name.strip()
                        or MASKED_PLACEHOLDER.search(setting_name)
                    ):
                        self.error(
                            "CONTRACT_PROTOCOL_ALTERNATIVE",
                            f"$.contract.protocolProfiles.{resource_type}.requiredAnyClientSettings[{group_index}][{setting_index}]",
                            "Alternative protocol setting names must be non-empty and unmasked.",
                        )
            runtime_required = profile.get(
                "runtimeRequiredClientSettings",
                [],
            )
            if not isinstance(runtime_required, list):
                self.error(
                    "CONTRACT_PROTOCOL_RUNTIME_SETTING",
                    f"$.contract.protocolProfiles.{resource_type}.runtimeRequiredClientSettings",
                    "Runtime-required protocol settings must be an array.",
                )
                runtime_required = []
            seen_runtime = set()
            for index, setting_name in enumerate(runtime_required):
                runtime_path = (
                    f"$.contract.protocolProfiles.{resource_type}."
                    f"runtimeRequiredClientSettings[{index}]"
                )
                if (
                    not isinstance(setting_name, str)
                    or not setting_name.strip()
                    or MASKED_PLACEHOLDER.search(setting_name)
                ):
                    self.error(
                        "CONTRACT_PROTOCOL_RUNTIME_SETTING",
                        runtime_path,
                        "Runtime-required protocol settings must be non-empty and unmasked.",
                    )
                elif setting_name not in required_names:
                    self.error(
                        "CONTRACT_PROTOCOL_RUNTIME_SETTING",
                        runtime_path,
                        "A runtime-required setting must name a required client setting.",
                    )
                elif setting_name in seen_runtime:
                    self.error(
                        "CONTRACT_PROTOCOL_RUNTIME_SETTING",
                        runtime_path,
                        "Runtime-required protocol settings must be unique.",
                    )
                else:
                    seen_runtime.add(setting_name)
            for setting_name, transform in profile.get(
                "requiredTransforms",
                {},
            ).items():
                if setting_name not in required_names:
                    self.error(
                        "CONTRACT_PROTOCOL_TRANSFORM",
                        f"$.contract.protocolProfiles.{resource_type}.requiredTransforms.{setting_name}",
                        "A required transform must name a required client setting.",
                    )
                if (
                    not isinstance(transform, str)
                    or not transform
                    or MASKED_PLACEHOLDER.search(transform)
                ):
                    self.error(
                        "CONTRACT_PROTOCOL_TRANSFORM",
                        f"$.contract.protocolProfiles.{resource_type}.requiredTransforms.{setting_name}",
                        "Protocol transforms must be non-empty and unmasked.",
                    )

    def validate_shape(self):
        if not isinstance(self.plan, dict):
            self.error("PLAN_NOT_OBJECT", "$", "The plan must be a JSON object.")
            return
        missing = sorted(TOP_LEVEL_KEYS - set(self.plan))
        unknown = sorted(set(self.plan) - TOP_LEVEL_KEYS)
        for key in missing:
            self.error("TOP_LEVEL_MISSING", f"$.{key}", f"Required key '{key}' is missing.")
        for key in unknown:
            self.error("TOP_LEVEL_UNKNOWN", f"$.{key}", f"Unknown top-level key '{key}'.")
        if self.plan.get("schemaVersion") != SCHEMA_VERSION:
            self.error(
                "SCHEMA_VERSION",
                "$.schemaVersion",
                f"schemaVersion must be {SCHEMA_VERSION}.",
            )
        for key in (
            "reviewedEvidence",
            "parameters",
            "resources",
            "workloads",
            "connections",
            "ambiguities",
            "unsupported",
        ):
            if key in self.plan and not isinstance(self.plan[key], list):
                self.error("TYPE_ARRAY", f"$.{key}", f"{key} must be an array.")
                self.plan[key] = []

    def validate_source_and_profile(self):
        source = self.plan.get("source")
        if not isinstance(source, dict):
            self.error("SOURCE_REQUIRED", "$.source", "source must be an object.")
            return
        expected = self.facts.get("repository", {})
        comparisons = {
            "repositoryUrl": expected.get("repositoryUrl"),
            "commit": expected.get("commit"),
            "subdirectory": expected.get("sourceSubdirectory"),
            "factsDigest": self.facts.get("factsDigest"),
        }
        for key, value in comparisons.items():
            if source.get(key) != value:
                self.error(
                    "SOURCE_MISMATCH",
                    f"$.source.{key}",
                    f"Expected {value!r}, got {source.get(key)!r}.",
                )

        intent = self.facts.get("intent", {})
        if intent.get("unresolved"):
            self.error(
                "PROFILE_UNRESOLVED",
                "$.profile",
                f"Source facts have unresolved intent: {intent['unresolved']}.",
            )
        profile = self.plan.get("profile")
        if not isinstance(profile, dict):
            self.error("PROFILE_REQUIRED", "$.profile", "profile must be an object.")
            return
        if profile.get("name") != intent.get("profile"):
            self.error(
                "PROFILE_MISMATCH",
                "$.profile.name",
                f"Expected profile {intent.get('profile')!r}.",
            )
        if profile.get("exposure") != intent.get("exposure"):
            self.error(
                "EXPOSURE_MISMATCH",
                "$.profile.exposure",
                f"Expected exposure {intent.get('exposure')!r}.",
            )
        if profile.get("provider") != intent.get("provider"):
            self.error(
                "PROFILE_PROVIDER",
                "$.profile.provider",
                f"Expected provider {intent.get('provider')!r}.",
            )
        expected_profile_evidence = intent.get("profileEvidence")
        expected_exposure_evidence = intent.get("exposureEvidence")
        expected_provider_evidence = intent.get("providerEvidence")
        profile_evidence = profile.get("evidence", [])
        for evidence_id in (
            expected_profile_evidence,
            expected_exposure_evidence,
            expected_provider_evidence,
        ):
            if evidence_id and evidence_id not in profile_evidence:
                self.error(
                    "PROFILE_EVIDENCE",
                    "$.profile.evidence",
                    f"Profile must cite {evidence_id}.",
                )

    def validate_evidence(self):
        references = collect_evidence_references(self.plan)
        for evidence_id, path in references:
            if not EVIDENCE_ID.fullmatch(evidence_id):
                self.error(
                    "EVIDENCE_FORMAT",
                    path,
                    f"Invalid evidence ID {evidence_id!r}.",
                )
            elif evidence_id not in self.evidence:
                self.error(
                    "EVIDENCE_UNKNOWN",
                    path,
                    f"Evidence ID {evidence_id!r} is not present in source facts.",
                )

        reviews = self.plan.get("reviewedEvidence", [])
        review_map = {}
        for index, review in enumerate(reviews):
            path = f"$.reviewedEvidence[{index}]"
            if not isinstance(review, dict):
                self.error("REVIEW_OBJECT", path, "Evidence review must be an object.")
                continue
            evidence_id = review.get("evidenceId")
            if evidence_id in review_map:
                self.error(
                    "REVIEW_DUPLICATE",
                    f"{path}.evidenceId",
                    f"{evidence_id} was reviewed more than once.",
                )
            review_map[evidence_id] = review
            if review.get("disposition") not in {"included", "excluded", "defaulted"}:
                self.error(
                    "REVIEW_DISPOSITION",
                    f"{path}.disposition",
                    "Disposition must be included, excluded, or defaulted.",
                )
            if not isinstance(review.get("rationale"), str) or not review["rationale"].strip():
                self.error(
                    "REVIEW_RATIONALE",
                    f"{path}.rationale",
                    "Every evidence decision requires a rationale.",
                )

        self.review_map = review_map
        for evidence_id in self.facts.get("reviewRequiredEvidence", []):
            if evidence_id not in review_map:
                self.error(
                    "REVIEW_MISSING",
                    "$.reviewedEvidence",
                    f"Required evidence {evidence_id} has no disposition.",
                    evidence_id,
                )

        intent_ids = {
            self.facts.get("intent", {}).get("profileEvidence"),
            self.facts.get("intent", {}).get("exposureEvidence"),
            self.facts.get("intent", {}).get("providerEvidence"),
            *[
                role.get("evidence")
                for role in self.facts.get("intent", {}).get("roles", [])
            ],
        } - {None}
        for evidence_id in intent_ids:
            if review_map.get(evidence_id, {}).get("disposition") == "excluded":
                self.error(
                    "INTENT_EXCLUDED",
                    "$.reviewedEvidence",
                    f"User intent {evidence_id} cannot be excluded.",
                    evidence_id,
                )

        environment_by_evidence = {}
        for item in self.facts.get("environmentVariables", []):
            if set(item.get("parsers", [])) == {"compose"}:
                continue
            for evidence_id in item.get("evidence", []):
                environment_by_evidence[evidence_id] = item["name"]
        planned_env = {
            entry.get("name")
            for workload in self.plan.get("workloads", [])
            if isinstance(workload, dict)
            for entry in workload.get("environment", [])
            if isinstance(entry, dict)
        }
        for evidence_id, review in review_map.items():
            env_name = environment_by_evidence.get(evidence_id)
            if (
                env_name
                and review.get("disposition") == "included"
                and env_name not in planned_env
            ):
                self.error(
                    "REVIEW_INCLUDED_MISSING",
                    "$.reviewedEvidence",
                    f"{evidence_id} includes {env_name}, but no workload binds it.",
                    evidence_id,
                )

    def validate_parameters(self):
        for index, parameter in enumerate(self.plan.get("parameters", [])):
            path = f"$.parameters[{index}]"
            if not isinstance(parameter, dict):
                self.error("PARAMETER_OBJECT", path, "Parameter must be an object.")
                continue
            name = parameter.get("name")
            if not isinstance(name, str) or not IDENTIFIER.fullmatch(name):
                self.error("PARAMETER_NAME", f"{path}.name", "Invalid parameter name.")
                continue
            if name in self.parameters:
                self.error("DUPLICATE_ID", f"{path}.name", f"Duplicate parameter {name}.")
            self.parameters[name] = parameter
            if parameter.get("type") not in {"string", "int", "bool", "object", "array"}:
                self.error("PARAMETER_TYPE", f"{path}.type", "Unsupported parameter type.")
            if not isinstance(parameter.get("secure"), bool):
                self.error("PARAMETER_SECURE", f"{path}.secure", "secure must be boolean.")
            if parameter.get("secure") and "default" in parameter:
                self.error(
                    "PARAMETER_SECURE_DEFAULT",
                    f"{path}.default",
                    "Secure parameters must not have defaults.",
                )
            if SECRET_NAME.search(name) and not parameter.get("secure"):
                self.error(
                    "PARAMETER_SECRET_INSECURE",
                    f"{path}.secure",
                    f"Secret-like parameter {name} must be secure.",
                )
        environment = self.parameters.get("environment")
        if not environment or environment.get("type") != "string" or environment.get("secure"):
            self.error(
                "ENVIRONMENT_PARAMETER",
                "$.parameters",
                "A non-secure string parameter named environment is required.",
            )

    def validate_evidence_alignment(self):
        intent_ids = {
            value
            for key, value in self.facts.get("intent", {}).items()
            if key.endswith("Evidence") and isinstance(value, str)
        } | {
            role.get("evidence")
            for role in self.facts.get("intent", {}).get("roles", [])
            if role.get("evidence")
        }
        docker_by_path = {
            item.get("path"): item.get("evidence")
            for item in self.facts.get("dockerfiles", [])
        }
        candidates = self.facts.get("workloadCandidates", [])
        all_workload_evidence = {
            evidence_id
            for candidate in candidates
            for evidence_id in candidate.get("evidence", [])
        }
        env_evidence = {
            item.get("name"): set(item.get("evidence", []))
            for item in self.facts.get("environmentVariables", [])
        }
        port_evidence = {}
        for dockerfile in self.facts.get("dockerfiles", []):
            for port in dockerfile.get("exposedPorts", []):
                port_evidence.setdefault(port.get("port"), set()).add(
                    dockerfile.get("evidence")
                )
        for manifest in self.facts.get("compose", []):
            for service in manifest.get("services", []):
                for port in service.get("ports", []):
                    port_evidence.setdefault(port, set()).add(service.get("evidence"))

        for index, workload in enumerate(self.plan.get("workloads", [])):
            if not isinstance(workload, dict):
                continue
            path = f"$.workloads[{index}]"
            workload_evidence = set(workload.get("evidence", []))
            exact_candidates = [
                candidate
                for candidate in candidates
                if candidate.get("id") == workload.get("id")
            ]
            expected_workload_evidence = {
                evidence_id
                for candidate in exact_candidates
                for evidence_id in candidate.get("evidence", [])
            } or all_workload_evidence
            if (
                expected_workload_evidence
                and not workload_evidence & expected_workload_evidence
                and not workload_evidence & intent_ids
            ):
                self.error(
                    "WORKLOAD_EVIDENCE",
                    f"{path}.evidence",
                    "Workload evidence does not cite a discovered workload or explicit role.",
                )

            image = workload.get("image", {})
            if isinstance(image, dict) and image.get("kind") == "build":
                expected = docker_by_path.get(image.get("dockerfile"))
                if expected and expected not in image.get("evidence", []):
                    self.error(
                        "IMAGE_EVIDENCE",
                        f"{path}.image.evidence",
                        f"Build must cite Dockerfile evidence {expected}.",
                    )

            for port_index, port in enumerate(workload.get("ports", [])):
                discovered = port_evidence.get(port.get("containerPort"), set())
                cited = set(port.get("evidence", []))
                if discovered and not cited & discovered and not cited & intent_ids:
                    self.error(
                        "PORT_EVIDENCE",
                        f"{path}.ports[{port_index}].evidence",
                        "Port evidence does not cite a matching source listener.",
                    )

            for env_index, entry in enumerate(workload.get("environment", [])):
                name = entry.get("name")
                expected = env_evidence.get(name)
                if not expected:
                    continue
                cited = set(entry.get("evidence", [])) | set(
                    entry.get("binding", {}).get("evidence", [])
                )
                if not cited & expected and not cited & intent_ids:
                    self.error(
                        "ENV_EVIDENCE",
                        f"{path}.environment[{env_index}].evidence",
                        f"{name} does not cite its source read.",
                    )

    def validate_resources(self):
        contract_types = self.contract.get("resourceTypes", {})
        for index, resource in enumerate(self.plan.get("resources", [])):
            path = f"$.resources[{index}]"
            if not isinstance(resource, dict):
                self.error("RESOURCE_OBJECT", path, "Resource must be an object.")
                continue
            resource_id = resource.get("id")
            if not isinstance(resource_id, str) or not IDENTIFIER.fullmatch(resource_id):
                self.error("RESOURCE_ID", f"{path}.id", "Invalid resource ID.")
                continue
            if resource_id in self.resources:
                self.error("DUPLICATE_ID", f"{path}.id", f"Duplicate resource {resource_id}.")
            self.resources[resource_id] = resource
            full_type = f"{resource.get('type')}@{resource.get('apiVersion')}"
            contract_type = contract_types.get(full_type)
            if not contract_type:
                self.error(
                    "RESOURCE_TYPE_UNSUPPORTED",
                    f"{path}.type",
                    f"{full_type} is absent from the pinned contract.",
                )
                continue
            if resource.get("type", "").startswith("Radius.Compute/"):
                self.error(
                    "RESOURCE_COMPUTE_DUPLICATE",
                    f"{path}.type",
                    "Compute images, containers, and routes are represented by workloads and exposure.",
                )
            schema = contract_type.get("schema", {}).get("properties", {})
            properties = resource.get("properties")
            if not isinstance(properties, dict):
                self.error("RESOURCE_PROPERTIES", f"{path}.properties", "properties must be an object.")
                properties = {}
            required = {
                name
                for name, body in schema.items()
                if body.get("required") and name not in {"environment", "application"}
            }
            for name in sorted(required - set(properties)):
                self.error(
                    "PROPERTY_REQUIRED",
                    f"{path}.properties",
                    f"Required property {name} is missing.",
                )
            for name, binding in properties.items():
                property_path = f"{path}.properties.{name}"
                property_schema = schema.get(name)
                if not property_schema:
                    self.error(
                        "PROPERTY_UNKNOWN",
                        property_path,
                        f"{name} is absent from the pinned schema.",
                    )
                    continue
                if property_schema.get("readOnly"):
                    self.error(
                        "PROPERTY_READ_ONLY",
                        property_path,
                        f"{name} is read-only.",
                    )
                self.validate_binding(binding, property_path)
                if property_schema.get("sensitive"):
                    if not (
                        isinstance(binding, dict)
                        and binding.get("kind") == "secureParameter"
                        and self.parameters.get(binding.get("parameter"), {}).get("secure")
                    ):
                        self.error(
                            "PROPERTY_SENSITIVE",
                            property_path,
                            f"Sensitive property {name} requires a secure parameter.",
                        )

            name_binding = resource.get("name")
            self.validate_binding(name_binding, f"{path}.name")
            recipe = self.contract.get("azureRecipeMappings", {}).get(resource.get("type"))
            if (
                resource.get("type") != "Radius.Core/applications"
                and self.plan.get("profile", {}).get("provider") == "azure"
                and not recipe
            ):
                self.error(
                    "RECIPE_MISSING",
                    f"{path}.type",
                    f"No pinned Azure Recipe exists for {resource.get('type')}.",
                )
            if recipe and recipe.get("providerGlobalName"):
                parameter_name = (
                    name_binding.get("parameter")
                    if isinstance(name_binding, dict)
                    and name_binding.get("kind") == "parameter"
                    else None
                )
                parameter = self.parameters.get(parameter_name)
                if not parameter or parameter.get("secure") or "default" in parameter:
                    self.error(
                        "PROVIDER_NAME_PARAMETER",
                        f"{path}.name",
                        "Provider-global names require a non-secure parameter with no default.",
                    )

            valid_outputs = contract_outputs(resource.get("type"), self.contract)
            outputs = resource.get("outputsUsed")
            if not isinstance(outputs, list):
                self.error("OUTPUTS_ARRAY", f"{path}.outputsUsed", "outputsUsed must be an array.")
                outputs = []
            for output in outputs:
                if output not in valid_outputs:
                    self.error(
                        "OUTPUT_UNKNOWN",
                        f"{path}.outputsUsed",
                        f"Output {output!r} is absent from the pinned Recipe mapping.",
                    )

        applications = [
            resource
            for resource in self.resources.values()
            if resource.get("type") == "Radius.Core/applications"
        ]
        if len(applications) != 1:
            self.error(
                "APPLICATION_COUNT",
                "$.resources",
                f"Exactly one Radius.Core/applications resource is required; found {len(applications)}.",
            )

    def validate_workloads(self):
        dockerfiles = {
            item.get("path"): item
            for item in self.facts.get("dockerfiles", [])
        }
        for index, workload in enumerate(self.plan.get("workloads", [])):
            path = f"$.workloads[{index}]"
            if not isinstance(workload, dict):
                self.error("WORKLOAD_OBJECT", path, "Workload must be an object.")
                continue
            workload_id = workload.get("id")
            if not isinstance(workload_id, str) or not IDENTIFIER.fullmatch(workload_id):
                self.error("WORKLOAD_ID", f"{path}.id", "Invalid workload ID.")
                continue
            if workload_id in self.workloads:
                self.error("DUPLICATE_ID", f"{path}.id", f"Duplicate workload {workload_id}.")
            self.workloads[workload_id] = workload
            if workload.get("exposure") != self.plan.get("profile", {}).get("exposure"):
                self.error(
                    "WORKLOAD_EXPOSURE",
                    f"{path}.exposure",
                    "Workload exposure must match the selected profile.",
                )
            image = workload.get("image")
            if not isinstance(image, dict):
                self.error("IMAGE_REQUIRED", f"{path}.image", "image must be an object.")
            else:
                self.validate_image(image, f"{path}.image", dockerfiles)

            environment = workload.get("environment")
            if not isinstance(environment, list):
                self.error("ENV_ARRAY", f"{path}.environment", "environment must be an array.")
                environment = []
            seen = set()
            for env_index, entry in enumerate(environment):
                env_path = f"{path}.environment[{env_index}]"
                if not isinstance(entry, dict):
                    self.error("ENV_OBJECT", env_path, "Environment entry must be an object.")
                    continue
                name = entry.get("name")
                if name in seen:
                    self.error("ENV_DUPLICATE", f"{env_path}.name", f"Duplicate environment key {name}.")
                seen.add(name)
                binding = entry.get("binding")
                self.validate_binding(binding, f"{env_path}.binding")
                if SECRET_NAME.search(name or "") and not self.binding_is_secure(binding, environment[:env_index]):
                    self.error(
                        "ENV_SECRET_INSECURE",
                        f"{env_path}.binding",
                        f"{name} must come from a secure binding.",
                    )
                if isinstance(binding, dict) and binding.get("kind") == "runtimeExpansion":
                    for referenced in re.findall(r"\$\(([A-Z][A-Z0-9_]*)\)", binding.get("expression", "")):
                        prior = {
                            previous.get("name"): previous.get("binding")
                            for previous in environment[:env_index]
                            if isinstance(previous, dict)
                        }
                        if referenced not in prior:
                            self.error(
                                "ENV_EXPANSION_ORDER",
                                f"{env_path}.binding.expression",
                                f"{referenced} must appear earlier in the environment list.",
                            )

                if (
                    isinstance(binding, dict)
                    and binding.get("kind") == "literal"
                    and MASKED_PLACEHOLDER.search(str(binding.get("value", "")))
                ):
                    self.error(
                        "MASKED_PLACEHOLDER",
                        f"{env_path}.binding.value",
                        "Executable configuration must not contain a masked or redacted placeholder.",
                    )

            startup = "\n".join(
                str(value)
                for key in ("command", "args")
                for value in workload.get(key, [])
                if isinstance(value, str)
            )
            if MASKED_PLACEHOLDER.search(startup):
                self.error(
                    "MASKED_PLACEHOLDER",
                    path,
                    "Startup configuration must not contain masked or redacted placeholders.",
                )
            secure_environment = set()
            for env_index, entry in enumerate(environment):
                if not isinstance(entry, dict) or not entry.get("name"):
                    continue
                if self.binding_is_secure(
                    entry.get("binding"),
                    environment[:env_index],
                ):
                    secure_environment.add(entry["name"])
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

            for file_index, generated in enumerate(workload.get("generatedFiles", [])):
                file_path = f"{path}.generatedFiles[{file_index}]"
                if generated.get("mode") != "0600":
                    self.error(
                        "FILE_MODE",
                        f"{file_path}.mode",
                        "Generated credential-bearing files must use mode 0600.",
                    )
                if generated.get("lifetime") not in {"one-shot", "process"}:
                    self.error(
                        "FILE_LIFETIME",
                        f"{file_path}.lifetime",
                        "File lifetime must be one-shot or process.",
                    )
                if not str(generated.get("cleanup", "")).strip():
                    self.error(
                        "FILE_CLEANUP",
                        f"{file_path}.cleanup",
                        "Generated files require an explicit cleanup condition.",
                    )
                if not workload.get("command") and not workload.get("args"):
                    self.error(
                        "FILE_STARTUP",
                        file_path,
                        "Generated files require explicit startup command or arguments.",
                    )
                content_template = str(generated.get("contentTemplate", ""))
                if not content_template or content_template not in startup:
                    self.error(
                        "FILE_CONTENT_TEMPLATE",
                        f"{file_path}.contentTemplate",
                        "Generated-file contentTemplate must appear verbatim in startup logic.",
                    )
                available = {
                    entry.get("name")
                    for entry in environment
                    if isinstance(entry, dict)
                }
                referenced = {
                    match[0] or match[1]
                    for match in re.findall(
                        r"\$\{([A-Za-z_][A-Za-z0-9_]*)[^}]*\}|"
                        r"\$\(([A-Za-z_][A-Za-z0-9_]*)\)",
                        content_template,
                    )
                }
                missing = sorted(referenced - available)
                if missing:
                    self.error(
                        "FILE_ENV_CLOSURE",
                        f"{file_path}.contentTemplate",
                        f"Generated file references unbound variables: {missing}.",
                    )

    def validate_image(self, image, path, dockerfiles):
        kind = image.get("kind")
        if kind == "build":
            required = {"repositoryUrl", "ref", "context", "dockerfile", "platforms"}
            missing = sorted(required - set(image))
            if missing:
                self.error(
                    "IMAGE_BUILD_FIELDS",
                    path,
                    f"Source build is missing fields: {missing}.",
                )
            if image.get("repositoryUrl") != self.facts.get("repository", {}).get("repositoryUrl"):
                self.error(
                    "IMAGE_REPOSITORY",
                    f"{path}.repositoryUrl",
                    "Build repository must match source facts.",
                )
            if image.get("ref") != self.facts.get("repository", {}).get("commit"):
                self.error(
                    "IMAGE_REF",
                    f"{path}.ref",
                    "Source-built images must use the exact modeled commit.",
                )
            dockerfile = image.get("dockerfile")
            if dockerfile not in dockerfiles:
                self.error(
                    "DOCKERFILE_UNKNOWN",
                    f"{path}.dockerfile",
                    f"Dockerfile {dockerfile!r} was not found by source-fact extraction.",
                )
            platforms = image.get("platforms")
            if not isinstance(platforms, list) or not platforms:
                self.error(
                    "IMAGE_PLATFORMS",
                    f"{path}.platforms",
                    "Source builds require at least one target platform.",
                )
        elif kind == "published":
            value = image.get("image")
            if not isinstance(value, str) or not image_is_immutable(value):
                self.error(
                    "IMAGE_MUTABLE",
                    f"{path}.image",
                    "Published images require a digest or immutable version tag.",
                )
        else:
            self.error("IMAGE_KIND", f"{path}.kind", "Image kind must be build or published.")

    def validate_operational_secrets(self):
        groups = {}
        for item in self.facts.get("environmentVariables", []):
            if (
                not isinstance(item, dict)
                or not item.get("operationalSecretRole")
                or item.get("required") is False
                or "production" not in item.get("scopes", [])
            ):
                continue
            groups.setdefault(item["operationalSecretRole"], []).append(item)

        for role, candidates in sorted(groups.items()):
            matches = []
            for candidate in candidates:
                evidence = candidate.get("evidence", [])
                if not any(
                    self.review_map.get(evidence_id, {}).get("disposition")
                    == "included"
                    for evidence_id in evidence
                ):
                    continue
                for workload_id, workload in self.workloads.items():
                    environment = workload.get("environment", [])
                    entry = next(
                        (
                            item
                            for item in environment
                            if isinstance(item, dict)
                            and item.get("name") == candidate.get("name")
                        ),
                        None,
                    )
                    if entry and self.binding_is_secure(
                        entry.get("binding"),
                        environment,
                    ):
                        matches.append(
                            {
                                "name": candidate.get("name"),
                                "workload": workload_id,
                                "evidence": evidence,
                            }
                        )
            if not matches:
                self.error(
                    "OPERATIONAL_SECRET_MISSING",
                    "$.workloads",
                    f"Required production {role} configuration must be included through a secure source-native environment binding.",
                    [
                        {
                            "name": candidate.get("name"),
                            "evidence": candidate.get("evidence", []),
                            "dispositions": [
                                self.review_map.get(evidence_id, {}).get(
                                    "disposition"
                                )
                                for evidence_id in candidate.get("evidence", [])
                            ],
                        }
                        for candidate in candidates
                    ],
                )

    def validate_connections(self):
        seen = set()
        connected = set()
        consumed = set()
        for workload in self.workloads.values():
            for entry in workload.get("environment", []):
                if not isinstance(entry, dict):
                    continue
                binding = entry.get("binding")
                if isinstance(binding, dict) and binding.get("resource"):
                    consumed.add(binding["resource"])
                    resource = self.resources.get(binding["resource"], {})
                    output = (
                        binding.get("property")
                        if binding.get("kind") == "resourceProperty"
                        else f"secrets.{binding.get('key')}"
                        if binding.get("kind") == "managedSecret"
                        else None
                    )
                    if output and output not in resource.get("outputsUsed", []):
                        self.error(
                            "OUTPUT_NOT_DECLARED",
                            "$.workloads",
                            f"Binding consumes {binding['resource']}.{output}, but outputsUsed omits it.",
                        )
        for index, connection in enumerate(self.plan.get("connections", [])):
            path = f"$.connections[{index}]"
            if not isinstance(connection, dict):
                self.error("CONNECTION_OBJECT", path, "Connection must be an object.")
                continue
            source = connection.get("sourceResource")
            target = connection.get("targetWorkload")
            key = (target, connection.get("name"))
            if key in seen:
                self.error("CONNECTION_DUPLICATE", path, f"Duplicate connection {key}.")
            seen.add(key)
            if source not in self.resources:
                self.error(
                    "CONNECTION_SOURCE",
                    f"{path}.sourceResource",
                    f"Unknown resource {source!r}.",
                )
                continue
            if target not in self.workloads:
                self.error(
                    "CONNECTION_TARGET",
                    f"{path}.targetWorkload",
                    f"Unknown workload {target!r}.",
                )
            connected.add(source)
            if connection.get("disableDefaultEnvVars") is not True:
                self.error(
                    "CONNECTION_DEFAULT_ENV",
                    f"{path}.disableDefaultEnvVars",
                    "Explicit native bindings require disableDefaultEnvVars=true.",
                )
            resource_type = self.resources[source].get("type")
            protocol = self.contract.get("protocolProfiles", {}).get(resource_type)
            if not protocol:
                self.error(
                    "CONNECTION_PROTOCOL_PROFILE",
                    f"{path}.clientProfile",
                    f"No pinned client protocol exists for {resource_type}.",
                )
            else:
                if connection.get("clientProfile") != protocol.get("clientKind"):
                    self.error(
                        "CONNECTION_CLIENT_PROFILE",
                        f"{path}.clientProfile",
                        f"Expected client profile {protocol.get('clientKind')!r}.",
                    )
                settings = connection.get("settings")
                if not isinstance(settings, dict):
                    self.error(
                        "CONNECTION_SETTINGS",
                        f"{path}.settings",
                        "settings must be an object.",
                    )
                    settings = {}
                for setting_name, setting in settings.items():
                    setting_path = f"{path}.settings.{setting_name}"
                    if not isinstance(setting, dict):
                        self.error(
                            "CONNECTION_SETTING_OBJECT",
                            setting_path,
                            "Each client setting must declare a binding and concrete consumer.",
                        )
                        continue
                    binding = setting.get("binding")
                    self.validate_binding(binding, f"{setting_path}.binding")
                    if (
                        isinstance(binding, dict)
                        and binding.get("kind") == "literal"
                        and binding.get("value") == ""
                    ):
                        self.error(
                            "CONNECTION_SETTING_EMPTY",
                            f"{setting_path}.binding.value",
                            f"Client setting {setting_name} must not be an empty string.",
                        )
                    if (
                        isinstance(binding, dict)
                        and binding.get("kind") in {"resourceProperty", "managedSecret"}
                        and binding.get("resource") != source
                    ):
                        self.error(
                            "CONNECTION_SETTING_RESOURCE",
                            f"{setting_path}.binding.resource",
                            f"{setting_name} must use connection source {source!r}.",
                        )
                    if SECRET_NAME.search(setting_name) and not self.binding_is_secure(
                        binding,
                        self.workloads.get(target, {}).get("environment", []),
                    ):
                        self.error(
                            "CONNECTION_SETTING_INSECURE",
                            f"{setting_path}.binding",
                            f"Secret-like client setting {setting_name} requires a secure binding.",
                        )
                    self.validate_setting_consumer(
                        setting,
                        setting_path,
                        self.workloads.get(target),
                        setting_name,
                    )
                for requirement in protocol.get("requiredClientSettings", []):
                    if "=" in requirement:
                        key_name, expected = requirement.split("=", 1)
                        setting = settings.get(key_name)
                        if setting is None:
                            self.error(
                                "CONNECTION_SETTING_MISSING",
                                f"{path}.settings",
                                f"Required client setting {key_name!r} is missing.",
                            )
                        elif not binding_matches_expected(
                            setting.get("binding")
                            if isinstance(setting, dict)
                            else None,
                            expected,
                        ):
                            self.error(
                                "CONNECTION_SETTING_VALUE",
                                f"{path}.settings.{key_name}",
                                f"Expected {key_name}={expected}.",
                            )
                    elif requirement not in settings:
                        self.error(
                            "CONNECTION_SETTING_MISSING",
                            f"{path}.settings",
                            f"Required client setting {requirement!r} is missing.",
                        )
                for alternatives in protocol.get(
                    "requiredAnyClientSettings",
                    [],
                ):
                    if not any(name in settings for name in alternatives):
                        self.error(
                            "CONNECTION_SETTING_ALTERNATIVE",
                            f"{path}.settings",
                            f"One of these client settings is required: {alternatives}.",
                        )
                for setting_name in protocol.get(
                    "runtimeRequiredClientSettings",
                    [],
                ):
                    setting = settings.get(setting_name)
                    consumer = (
                        setting.get("consumer")
                        if isinstance(setting, dict)
                        else None
                    )
                    if (
                        isinstance(consumer, dict)
                        and consumer.get("kind") == "source"
                    ):
                        self.error(
                            "CONNECTION_SETTING_RUNTIME_REQUIRED",
                            f"{path}.settings.{setting_name}.consumer.kind",
                            f"Required client setting {setting_name!r} must be delivered to the runtime, not left to a source default.",
                        )
                for setting_name, expected_transform in protocol.get(
                    "requiredTransforms",
                    {},
                ).items():
                    setting = settings.get(setting_name)
                    actual_transform = (
                        setting.get("consumer", {}).get("transform")
                        if isinstance(setting, dict)
                        and isinstance(setting.get("consumer"), dict)
                        else None
                    )
                    if setting is not None and actual_transform != expected_transform:
                        self.error(
                            "CONNECTION_SETTING_TRANSFORM",
                            f"{path}.settings.{setting_name}.consumer.transform",
                            f"Expected runtime transform {expected_transform!r}.",
                        )

        backing = {
            resource_id
            for resource_id, resource in self.resources.items()
            if resource.get("type") != "Radius.Core/applications"
        }
        for resource_id in sorted(backing - connected):
            self.error(
                "RESOURCE_UNCONNECTED",
                "$.connections",
                f"Backing resource {resource_id} has no workload connection.",
            )
        for resource_id in sorted(backing - consumed):
            self.error(
                "RESOURCE_UNCONSUMED",
                "$.workloads",
                f"Backing resource {resource_id} is not used by workload runtime configuration.",
            )

    def validate_connection_namespaces(self):
        namespaces = [
            item
            for item in self.facts.get("environmentNamespaces", [])
            if isinstance(item, dict)
            and item.get("purpose") == "connection"
            and item.get("reviewRequired")
        ]
        connections = [
            item
            for item in self.plan.get("connections", [])
            if isinstance(item, dict)
        ]
        if not namespaces or not connections:
            return

        matches = []
        for namespace in namespaces:
            evidence_id = namespace.get("evidence")
            if self.review_map.get(evidence_id, {}).get("disposition") != "included":
                continue
            for connection in connections:
                settings = connection.get("settings")
                workload = self.workloads.get(connection.get("targetWorkload"))
                if not isinstance(settings, dict) or not settings or not workload:
                    continue
                instances = []
                valid = True
                for setting in settings.values():
                    consumer = (
                        setting.get("consumer")
                        if isinstance(setting, dict)
                        else None
                    )
                    if (
                        not isinstance(consumer, dict)
                        or consumer.get("kind") != "environment"
                    ):
                        valid = False
                        break
                    instance = environment_namespace_instance(
                        namespace,
                        consumer.get("target"),
                    )
                    if instance is None:
                        valid = False
                        break
                    instances.append(instance)
                if valid and len(set(instances)) == 1:
                    matches.append(
                        {
                            "evidence": evidence_id,
                            "prefix": namespace.get("prefix"),
                            "instance": instances[0],
                            "connection": connection.get("name"),
                            "targetWorkload": connection.get("targetWorkload"),
                        }
                    )

        if not matches:
            self.error(
                "PRIMARY_CONNECTION_NAMESPACE",
                "$.connections",
                "The source exposes a production connection namespace; include it and deliver every client setting through one consistent namespace instance.",
                [
                    {
                        "evidence": item.get("evidence"),
                        "prefix": item.get("prefix"),
                        "delimiter": item.get("delimiter"),
                        "disposition": self.review_map.get(
                            item.get("evidence"),
                            {},
                        ).get("disposition"),
                    }
                    for item in namespaces
                ],
            )

    def validate_setting_consumer(self, setting, path, workload, setting_name):
        consumer = setting.get("consumer")
        if not isinstance(consumer, dict):
            self.error(
                "CONNECTION_SETTING_CONSUMER",
                f"{path}.consumer",
                "Client settings require a concrete runtime consumer.",
            )
            return
        if not isinstance(workload, dict):
            return

        kind = consumer.get("kind")
        target = consumer.get("target")
        inputs = consumer.get("inputs")
        if not isinstance(inputs, list):
            inputs = []
        environment = {
            entry.get("name"): entry.get("binding")
            for entry in workload.get("environment", [])
            if isinstance(entry, dict) and entry.get("name")
        }
        missing_inputs = sorted(
            name for name in inputs if name not in environment
        )
        if missing_inputs:
            self.error(
                "CONNECTION_SETTING_CONSUMER_INPUT",
                f"{path}.consumer.inputs",
                f"Consumer inputs are not bound by the target workload: {missing_inputs}.",
            )

        binding = setting.get("binding")
        if kind == "environment":
            actual = environment.get(target)
            if actual is None:
                self.error(
                    "CONNECTION_SETTING_CONSUMER_ENV",
                    f"{path}.consumer.target",
                    f"Target workload does not bind environment variable {target!r}.",
                )
            elif not bindings_equivalent(binding, actual):
                self.error(
                    "CONNECTION_SETTING_CONSUMER_VALUE",
                    f"{path}.consumer.target",
                    f"Environment variable {target!r} does not deliver the declared setting binding.",
                )
            if inputs:
                self.error(
                    "CONNECTION_SETTING_CONSUMER_INPUT",
                    f"{path}.consumer.inputs",
                    "Direct environment consumers do not use intermediate inputs.",
                )
            if "locator" in consumer:
                self.error(
                    "CONNECTION_SETTING_CONSUMER_FIELDS",
                    f"{path}.consumer.locator",
                    "Direct environment consumers do not use a locator.",
                )
            if "transform" in consumer:
                self.error(
                    "CONNECTION_SETTING_CONSUMER_FIELDS",
                    f"{path}.consumer.transform",
                    "Direct environment consumers do not use a runtime transform.",
                )
            return

        if kind == "source":
            if target != "source":
                self.error(
                    "CONNECTION_SETTING_CONSUMER_SOURCE",
                    f"{path}.consumer.target",
                    "Source consumers must use target 'source'.",
                )
            if not isinstance(binding, dict) or binding.get("kind") != "literal":
                self.error(
                    "CONNECTION_SETTING_CONSUMER_SOURCE",
                    f"{path}.binding",
                    "A source-code default or constant must be represented by a literal binding.",
                )
            elif not any(
                normalize_setting_value(binding.get("value"))
                in self.evidence_text.get(evidence_id, "")
                for evidence_id in {
                    *setting.get("evidence", []),
                    *consumer.get("evidence", []),
                    *binding.get("evidence", []),
                }
            ):
                self.error(
                    "CONNECTION_SETTING_CONSUMER_SOURCE_EVIDENCE",
                    f"{path}.consumer.evidence",
                    "Source consumer evidence does not contain the declared literal value.",
                )
            if inputs:
                self.error(
                    "CONNECTION_SETTING_CONSUMER_INPUT",
                    f"{path}.consumer.inputs",
                    "Source consumers do not use workload environment inputs.",
                )
            if "locator" in consumer:
                self.error(
                    "CONNECTION_SETTING_CONSUMER_FIELDS",
                    f"{path}.consumer.locator",
                    "Source consumers do not use a locator.",
                )
            if "transform" in consumer:
                self.error(
                    "CONNECTION_SETTING_CONSUMER_FIELDS",
                    f"{path}.consumer.transform",
                    "Source consumers do not use a runtime transform.",
                )
            return

        startup = "\n".join(
            str(value)
            for key in ("command", "args")
            for value in workload.get(key, [])
            if isinstance(value, str)
        )
        transform = consumer.get("transform")
        destination = " ".join(
            value
            for value in (target, consumer.get("locator"))
            if isinstance(value, str)
        )
        if (
            kind in {"startupEnvironment", "generatedFile", "processStdin"}
            and SECRET_NAME.search(setting_name)
            and URI_CONSUMER.search(destination)
            and not isinstance(transform, str)
        ):
            self.error(
                "CONNECTION_SETTING_URL_TRANSFORM",
                f"{path}.consumer.transform",
                "Secret URL components require a declared runtime encoding transform before interpolation.",
            )
        if transform is not None and (
            not isinstance(transform, str) or transform not in startup
        ):
            self.error(
                "CONNECTION_SETTING_CONSUMER_TRANSFORM",
                f"{path}.consumer.transform",
                "The declared runtime transform is absent from startup logic.",
            )
        if kind == "startupEnvironment":
            assignment = (
                re.search(
                    rf"(?:^|[;\n])\s*(?:export\s+)?[\"']?{re.escape(target)}=[^\n]*",
                    startup,
                )
                if isinstance(target, str)
                else None
            )
            if assignment is None:
                self.error(
                    "CONNECTION_SETTING_CONSUMER_STARTUP",
                    f"{path}.consumer.target",
                    f"Startup does not assign runtime environment variable {target!r}.",
                )
            if "locator" in consumer:
                self.error(
                    "CONNECTION_SETTING_CONSUMER_FIELDS",
                    f"{path}.consumer.locator",
                    "Startup environment consumers do not use a locator.",
                )
            self.validate_indirect_setting_delivery(
                binding,
                inputs,
                environment,
                assignment.group(0) if assignment else "",
                path,
            )
            return

        if kind == "generatedFile":
            generated = next(
                (
                    item
                    for item in workload.get("generatedFiles", [])
                    if isinstance(item, dict) and item.get("path") == target
                ),
                None,
            )
            if generated is None:
                self.error(
                    "CONNECTION_SETTING_CONSUMER_FILE",
                    f"{path}.consumer.target",
                    f"Target workload does not generate file {target!r}.",
                )
                return
            locator = consumer.get("locator")
            content_template = str(generated.get("contentTemplate", ""))
            if not isinstance(locator, str) or locator not in content_template:
                self.error(
                    "CONNECTION_SETTING_CONSUMER_LOCATOR",
                    f"{path}.consumer.locator",
                    "Generated-file consumers require a locator present in contentTemplate.",
                )
            self.validate_indirect_setting_delivery(
                binding,
                inputs,
                environment,
                content_template,
                path,
            )
            return

        if kind == "processStdin":
            locator = consumer.get("locator")
            if not isinstance(target, str) or target not in startup:
                self.error(
                    "CONNECTION_SETTING_CONSUMER_STDIN",
                    f"{path}.consumer.target",
                    f"Startup does not consume configuration from {target!r}.",
                )
            if not isinstance(locator, str) or locator not in startup:
                self.error(
                    "CONNECTION_SETTING_CONSUMER_LOCATOR",
                    f"{path}.consumer.locator",
                    "Process-stdin consumers require a locator present in startup configuration.",
                )
            self.validate_indirect_setting_delivery(
                binding,
                inputs,
                environment,
                startup,
                path,
            )
            return

        self.error(
            "CONNECTION_SETTING_CONSUMER_KIND",
            f"{path}.consumer.kind",
            f"Unknown setting consumer kind {kind!r}.",
        )

    def validate_indirect_setting_delivery(
        self,
        binding,
        inputs,
        environment,
        surface,
        path,
    ):
        missing_references = sorted(
            name
            for name in inputs
            if not re.search(rf"\$(?:\{{{re.escape(name)}(?:[^}}]*)?\}}|{re.escape(name)}\b)", surface)
        )
        if missing_references:
            self.error(
                "CONNECTION_SETTING_CONSUMER_INPUT",
                f"{path}.consumer.inputs",
                f"Consumer does not reference declared inputs: {missing_references}.",
            )

        if isinstance(binding, dict) and binding.get("kind") == "literal":
            expected = normalize_setting_value(binding.get("value"))
            delivered_by_input = any(
                bindings_equivalent(binding, environment.get(name))
                for name in inputs
            )
            if expected not in surface and not delivered_by_input:
                self.error(
                    "CONNECTION_SETTING_CONSUMER_VALUE",
                    f"{path}.consumer",
                    "Consumer does not contain the declared literal setting.",
                )
            return

        if not any(
            bindings_equivalent(binding, environment.get(name))
            for name in inputs
        ):
            self.error(
                "CONNECTION_SETTING_CONSUMER_VALUE",
                f"{path}.consumer.inputs",
                "No declared input delivers the setting binding to the consumer.",
            )

    def validate_resolution(self):
        for index, ambiguity in enumerate(self.plan.get("ambiguities", [])):
            if ambiguity.get("status") != "resolved" or not ambiguity.get("resolution"):
                self.error(
                    "AMBIGUITY_OPEN",
                    f"$.ambiguities[{index}]",
                    "Every ambiguity must be resolved before Bicep generation.",
                )
        if self.plan.get("unsupported"):
            for index, item in enumerate(self.plan["unsupported"]):
                self.error(
                    "UNSUPPORTED_CAPABILITY",
                    f"$.unsupported[{index}]",
                    f"Unsupported capability: {item.get('capability')}.",
                    item.get("evidence"),
                )
        if self.plan.get("profile", {}).get("exposure") != "external":
            routes = [
                resource
                for resource in self.resources.values()
                if resource.get("type") == "Radius.Compute/routes"
            ]
            if routes:
                self.error(
                    "ROUTE_UNREQUESTED",
                    "$.resources",
                    "A route is forbidden unless exposure is external.",
                )

    def validate_binding(self, binding, path):
        if not isinstance(binding, dict):
            self.error("BINDING_OBJECT", path, "Binding must be an object.")
            return
        kind = binding.get("kind")
        fields = {
            "literal": {"kind", "value", "evidence"},
            "parameter": {"kind", "parameter", "evidence"},
            "secureParameter": {"kind", "parameter", "evidence"},
            "resourceProperty": {"kind", "resource", "property", "evidence"},
            "managedSecret": {"kind", "resource", "key", "evidence"},
            "runtimeExpansion": {"kind", "expression", "evidence"},
        }
        if kind in fields:
            unexpected = sorted(set(binding) - fields[kind])
            if unexpected:
                self.error(
                    "BINDING_FIELDS",
                    path,
                    f"{kind} binding has unrelated fields: {unexpected}.",
                )
        if kind == "literal":
            if "value" not in binding:
                self.error("BINDING_LITERAL", path, "Literal binding requires value.")
        elif kind in {"parameter", "secureParameter"}:
            parameter_name = binding.get("parameter")
            parameter = self.parameters.get(parameter_name)
            if not parameter:
                self.error(
                    "BINDING_PARAMETER",
                    path,
                    f"Unknown parameter {parameter_name!r}.",
                )
            elif kind == "secureParameter" and not parameter.get("secure"):
                self.error(
                    "BINDING_PARAMETER_SECURE",
                    path,
                    f"Parameter {parameter_name} is not secure.",
                )
            elif kind == "parameter" and parameter.get("secure"):
                self.error(
                    "BINDING_PARAMETER_KIND",
                    path,
                    f"Secure parameter {parameter_name} requires secureParameter binding.",
                )
        elif kind in {"resourceProperty", "managedSecret"}:
            resource_id = binding.get("resource")
            resource = self.resources.get(resource_id)
            if not resource:
                self.error("BINDING_RESOURCE", path, f"Unknown resource {resource_id!r}.")
                return
            outputs = contract_outputs(resource.get("type"), self.contract)
            if kind == "resourceProperty":
                property_name = binding.get("property")
                if property_name not in outputs:
                    self.error(
                        "BINDING_PROPERTY",
                        path,
                        f"Output property {property_name!r} is absent from the pinned Recipe.",
                    )
            else:
                key = binding.get("key")
                if f"secrets.{key}" not in outputs:
                    self.error(
                        "BINDING_SECRET_KEY",
                        path,
                        f"Managed-secret key {key!r} is absent from the pinned Recipe.",
                    )
        elif kind == "runtimeExpansion":
            if not isinstance(binding.get("expression"), str) or not binding["expression"]:
                self.error(
                    "BINDING_EXPANSION",
                    path,
                    "runtimeExpansion requires an expression.",
                )
        else:
            self.error("BINDING_KIND", path, f"Unknown binding kind {kind!r}.")

    def binding_is_secure(self, binding, prior_environment):
        if not isinstance(binding, dict):
            return False
        if binding.get("kind") == "secureParameter":
            return self.parameters.get(binding.get("parameter"), {}).get("secure") is True
        if binding.get("kind") == "managedSecret":
            return True
        if binding.get("kind") == "runtimeExpansion":
            prior = {
                item.get("name"): item.get("binding")
                for item in prior_environment
                if isinstance(item, dict)
            }
            references = re.findall(
                r"\$\(([A-Z][A-Z0-9_]*)\)",
                binding.get("expression", ""),
            )
            return bool(references) and all(
                self.binding_is_secure(prior.get(name), [])
                for name in references
            )
        return False


def binding_signature(binding):
    if not isinstance(binding, dict):
        return None
    kind = binding.get("kind")
    if kind == "literal":
        return kind, normalize_setting_value(binding.get("value"))
    if kind in {"parameter", "secureParameter"}:
        return kind, binding.get("parameter")
    if kind == "resourceProperty":
        return kind, binding.get("resource"), binding.get("property")
    if kind == "managedSecret":
        return kind, binding.get("resource"), binding.get("key")
    if kind == "runtimeExpansion":
        return kind, binding.get("expression")
    return None


def bindings_equivalent(left, right):
    return binding_signature(left) == binding_signature(right)


def binding_matches_expected(binding, expected):
    if not isinstance(binding, dict):
        return False
    if expected.lower().startswith("managedsecret:"):
        key = expected.split(":", 1)[1]
        return (
            binding.get("kind") == "managedSecret"
            and binding.get("key") == key
        )
    return (
        binding.get("kind") == "literal"
        and normalize_setting_value(binding.get("value"))
        == normalize_setting_value(expected)
    )


def normalize_setting_value(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    return str(value)


def environment_namespace_instance(namespace, target):
    prefix = namespace.get("prefix")
    if (
        not isinstance(prefix, str)
        or not isinstance(target, str)
        or not target.startswith(prefix)
    ):
        return None
    remainder = target[len(prefix):]
    if not remainder:
        return None
    delimiter = namespace.get("delimiter")
    if not delimiter:
        return ""
    separator = remainder.find(delimiter)
    if separator <= 0 or separator + len(delimiter) >= len(remainder):
        return None
    return remainder[:separator]


def contract_outputs(resource_type, contract):
    mapping = contract.get("azureRecipeMappings", {}).get(resource_type, {})
    outputs = mapping.get("outputs", {})
    result = set()
    for name, value in outputs.items():
        if isinstance(value, dict):
            result.update(f"{name}.{child}" for child in value)
        else:
            result.add(name)
    return result


def collect_evidence_text(value):
    result = {}

    def visit(item):
        if isinstance(item, dict):
            excerpt = item.get("excerpt")
            evidence_ids = []
            if isinstance(item.get("id"), str):
                evidence_ids.append(item["id"])
            if isinstance(item.get("evidence"), str):
                evidence_ids.append(item["evidence"])
            elif isinstance(item.get("evidence"), list):
                evidence_ids.extend(
                    evidence_id
                    for evidence_id in item["evidence"]
                    if isinstance(evidence_id, str)
                )
            if isinstance(excerpt, str):
                for evidence_id in evidence_ids:
                    result[evidence_id] = (
                        f"{result.get(evidence_id, '')}\n{excerpt}"
                    )
            for nested in item.values():
                visit(nested)
        elif isinstance(item, list):
            for nested in item:
                visit(nested)

    visit(value)
    return result


def collect_evidence_references(value):
    result = []

    def visit(item, path):
        if isinstance(item, dict):
            for key, nested in item.items():
                child = f"{path}.{key}"
                if key == "evidenceId" and isinstance(nested, str):
                    result.append((nested, child))
                elif key == "evidence" and isinstance(nested, list):
                    result.extend(
                        (evidence_id, f"{child}[{index}]")
                        for index, evidence_id in enumerate(nested)
                        if isinstance(evidence_id, str)
                    )
                else:
                    visit(nested, child)
        elif isinstance(item, list):
            for index, nested in enumerate(item):
                visit(nested, f"{path}[{index}]")

    visit(value, "$")
    return result


def image_is_immutable(value):
    if re.search(r"@sha256:[0-9a-f]{64}$", value, re.IGNORECASE):
        return True
    match = re.search(r":([^/:]+)$", value)
    return bool(match and not MUTABLE_REF.fullmatch(match.group(1)))


def load_json(path, label):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        raise SystemExit(f"{label} file not found: {path}")
    except json.JSONDecodeError as error:
        raise SystemExit(f"{label} is not valid JSON: {error}")


def main():
    parser = argparse.ArgumentParser(description="Validate a typed Radius application plan.")
    parser.add_argument("--facts", required=True)
    parser.add_argument("--plan", required=True)
    parser.add_argument(
        "--contract",
        default=str(Path(__file__).resolve().parent.parent / "assets" / "radius-contract.json"),
    )
    parser.add_argument(
        "--schema",
        default=str(Path(__file__).resolve().parent.parent / "assets" / "plan.schema.json"),
    )
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    facts = load_json(args.facts, "Source facts")
    plan = load_json(args.plan, "Plan")
    contract = load_json(args.contract, "Radius contract")
    schema = load_json(args.schema, "Plan schema")
    validator = Validator(plan, facts, contract)
    errors = SchemaValidator(schema).validate(plan) + validator.validate()
    report = {
        "schemaVersion": 1,
        "valid": not errors,
        "factsDigest": facts.get("factsDigest"),
        "contract": {
            "extension": contract.get("extension", {}).get("reference"),
            "manifestDigest": contract.get("extension", {}).get("manifestDigest"),
        },
        "counts": {"errors": len(errors)},
        "errors": errors,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(
        json.dumps(
            {
                "valid": report["valid"],
                "errors": len(errors),
                "output": str(output.resolve()),
            },
            sort_keys=True,
        )
    )
    raise SystemExit(0 if report["valid"] else 1)


if __name__ == "__main__":
    main()
