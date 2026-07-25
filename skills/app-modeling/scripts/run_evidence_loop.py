#!/usr/bin/env python3

"""Run evidence -> author -> validation -> retained-reviewer loop."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import time
from typing import Any
import uuid

from loop_support import (
    SKILL_DIR,
    install_agents,
    invoke,
    parse_json_object,
    remaining,
    remove_agents,
    validate_candidate,
    validate_handoff,
    validate_review,
    write_json,
)

BASE_AUTHORING_TYPES = (
    "Radius.Core/applications",
    "Radius.Compute/containerImages",
    "Radius.Compute/containers",
    "Radius.Security/secrets",
)

DEPENDENCY_AUTHORING_TYPES = {
    "mysql": "Radius.Data/mySqlDatabases",
    "postgresql": "Radius.Data/postgreSqlDatabases",
    "sql-server": "Radius.Data/sqlServerDatabases",
    "mongodb": "Radius.Data/mongoDatabases",
    "neo4j": "Radius.Data/neo4jDatabases",
    "redis": "Radius.Data/redisCaches",
    "kafka": "Radius.Messaging/kafka",
    "rabbitmq": "Radius.Messaging/rabbitMQ",
    "ai-model": "Radius.AI/models",
    "ai-search": "Radius.AI/search",
    "object-storage": "Radius.Storage/objectStorage",
}


def skill_fingerprint() -> str:
    digest = hashlib.sha256()
    for path in sorted(SKILL_DIR.rglob("*")):
        if not path.is_file() or path.suffix not in {".json", ".md", ".py"}:
            continue
        digest.update(path.relative_to(SKILL_DIR).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def receipt_path(
    git_dir: Path,
    *,
    commit: str,
    source_path: str,
    request: str,
) -> Path:
    key = json.dumps(
        {
            "commit": commit,
            "request": request,
            "skill": skill_fingerprint(),
            "sourcePath": source_path,
        },
        sort_keys=True,
    ).encode()
    return (
        git_dir
        / "app-modeling-runs"
        / "receipts"
        / f"{hashlib.sha256(key).hexdigest()}.json"
    )


def exact_tags(target: Path, remote: str, commit: str) -> list[str]:
    local = subprocess.run(
        ["git", "-C", str(target), "tag", "--points-at", commit],
        text=True,
        capture_output=True,
        check=False,
    ).stdout.splitlines()
    if local or not remote:
        return sorted(set(local))
    try:
        result = subprocess.run(
            ["git", "ls-remote", "--tags", remote],
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return []
    if result.returncode:
        return []
    tags = []
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) != 2 or fields[0] != commit:
            continue
        reference = fields[1]
        if not reference.startswith("refs/tags/"):
            continue
        tags.append(reference.removeprefix("refs/tags/").removesuffix("^{}"))
    return sorted(set(tags))


def public_invocation(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if key != "finalText"}


def walk_strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from walk_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk_strings(item)


def source_selected_types(evidence: dict[str, Any]) -> set[str]:
    selected: set[str] = set()
    facts = evidence.get("facts")
    if not isinstance(facts, dict):
        return selected

    dependencies = facts.get("dependencies")
    if isinstance(dependencies, dict):
        dependencies = [dependencies]
    elif isinstance(dependencies, list):
        dependencies = list(dependencies)
    else:
        dependencies = []
    singular = facts.get("dependency")
    if isinstance(singular, dict):
        dependencies.append(singular)
    for dependency in dependencies:
        if not isinstance(dependency, dict):
            continue
        kind = str(dependency.get("kind", "")).strip().lower()
        qualified_type = DEPENDENCY_AUTHORING_TYPES.get(kind)
        if qualified_type:
            selected.add(qualified_type)

    persistent_paths = facts.get("persistentPaths")
    if isinstance(persistent_paths, list) and any(
        isinstance(item, str)
        or (
            isinstance(item, dict)
            and (
                item.get("required") is True
                or (
                    "required" not in item
                    and not item.get("requiredWhen")
                )
            )
        )
        for item in persistent_paths
    ):
        selected.add("Radius.Compute/persistentVolumes")
    route = facts.get("route")
    if isinstance(route, dict) and route.get("required") is True:
        selected.add("Radius.Compute/routes")
    workloads = facts.get("workloads")
    if isinstance(workloads, dict):
        workloads = [workloads]
    elif isinstance(workloads, list):
        workloads = list(workloads)
    else:
        workloads = []
    workload = facts.get("workload")
    if isinstance(workload, dict):
        workloads.append(workload)
    if any(
        isinstance(item, dict)
        and (
            item.get("externalRouteRequired") is True
            or item.get("routeRequired") is True
            or (
                isinstance(item.get("route"), dict)
                and item["route"].get("required") is True
            )
        )
        for item in workloads
    ):
        selected.add("Radius.Compute/routes")
    routes = facts.get("routes")
    if isinstance(routes, list) and routes:
        selected.add("Radius.Compute/routes")
    return selected


def build_authoring_contract(evidence: dict[str, Any]) -> dict[str, Any]:
    contract = json.loads((SKILL_DIR / "assets" / "radius-contract.json").read_text())
    selected = set(BASE_AUTHORING_TYPES) | source_selected_types(evidence)
    available = {
        name.split("@", 1)[0]: name for name in contract["resourceTypes"]
    }
    for value in walk_strings(evidence):
        for qualified_type in available:
            if qualified_type in value:
                selected.add(qualified_type)
    bundles = {}
    for qualified_type in sorted(selected):
        key = available[qualified_type]
        bundles[qualified_type] = {
            "type": contract["resourceTypes"][key],
            "recipe": contract["azureRecipeMappings"].get(qualified_type),
            "protocol": contract["protocolProfiles"].get(qualified_type),
        }
        recipe = bundles[qualified_type]["recipe"] or {}
        if recipe:
            recipe["authoringUsage"] = (
                "This is immutable Environment Recipe provenance and output "
                "mapping, not an emittable resource property. The target "
                "environment selects its registered Recipe. Never add a "
                "`recipe` property unless the exact resource schema exposes it."
            )
        secret_outputs = recipe.get("outputs", {}).get("secrets", {})
        if secret_outputs:
            bundles[qualified_type]["containerSecretBindings"] = [
                {
                    "radiusKey": radius_key,
                    "providerManagedKey": provider_key,
                    "secretNameExpression": (
                        "<resourceSymbol>.properties.secrets.name"
                    ),
                    "secretKeyRefProperty": "secretName",
                    "secretKeyRefKey": radius_key,
                }
                for radius_key, provider_key in sorted(secret_outputs.items())
            ]
    return {
        "extension": contract["extension"],
        "policies": contract["policies"],
        "requirementsRules": {
            "settingName": "exact text before '=' in each required profile setting",
            "runtimeConfig": (
                "literal rendered in container env, command, args, or native config"
            ),
            "secretKeyRef": "secret environment input",
            "sourceDefault": "only an unmodified source default",
        },
        "secretPolicy": {
            "developerSupplied": {
                "input": "@secure() parameter",
                "containerStorage": "Radius.Security/secrets.data",
                "containerBinding": "valueFrom.secretKeyRef",
                "reuse": (
                    "The same secure parameter may also bind a backing resource's "
                    "sensitive input."
                ),
            },
            "recipeGenerated": {
                "containerBinding": "valueFrom.secretKeyRef",
                "secretName": "<resource>.properties.secrets.name",
                "key": "exact Radius key from the verified Recipe output",
            },
            "forbidden": [
                "secret-like container env.value",
                "Bicep interpolation of credentials",
                "copying a Recipe-generated secret into an authored secret",
            ],
        },
        "bundles": bundles,
    }


def validate_evidence_candidate(
    candidate: Path,
    evidence: dict[str, Any],
) -> list[dict[str, str]]:
    source = (candidate / "app.bicep").read_text()
    declarations = re.findall(
        r"\bresource\s+([A-Za-z_][A-Za-z0-9_]*)\s+'([^']+)'",
        source,
    )
    by_type: dict[str, list[str]] = {}
    for symbol, resource_type in declarations:
        by_type.setdefault(resource_type.split("@", 1)[0], []).append(symbol)

    errors: list[dict[str, str]] = []
    for qualified_type in sorted(source_selected_types(evidence)):
        symbols = by_type.get(qualified_type, [])
        if not symbols:
            errors.append(
                {
                    "code": "SOURCE_SELECTED_TYPE",
                    "path": "$.sourceFacts",
                    "message": (
                        f"Closed source facts require {qualified_type}, but the "
                        "candidate does not emit that Radius type."
                    ),
                }
            )
            continue
        if qualified_type.startswith(
            ("Radius.Data/", "Radius.Messaging/", "Radius.AI/", "Radius.Storage/")
        ):
            for symbol in symbols:
                if not re.search(
                    rf"\bsource\s*:\s*{re.escape(symbol)}\.id\b",
                    source,
                ):
                    errors.append(
                        {
                            "code": "SOURCE_DEPENDENCY_CONNECTION",
                            "path": f"$.resources.{symbol}",
                            "message": (
                                f"Source-selected dependency {symbol!r} is not "
                                "connected to an application workload."
                            ),
                        }
                    )
    return errors


def validate_all(
    candidate: Path,
    run_dir: Path,
    evidence: dict[str, Any],
    *,
    remote: str,
    commit: str,
    source_path: str,
) -> dict[str, Any]:
    report = validate_candidate(
        candidate,
        run_dir,
        source_remote=remote,
        source_commit=commit,
        source_path=source_path,
    )
    report["errors"].extend(validate_evidence_candidate(candidate, evidence))
    report["valid"] = not report["errors"]
    return report


def normalize_review(value: dict[str, Any]) -> dict[str, Any]:
    if "verdict" in value:
        verdict = str(value.get("verdict", "")).lower()
        if verdict in {"accept", "accepted", "approve", "approved", "pass", "passed"}:
            value["verdict"] = "accepted"
        elif verdict in {"reject", "rejected", "fail", "failed"}:
            value["verdict"] = "rejected"
        elif verdict not in {"needs_more_info"}:
            value["verdict"] = "needs_more_info"
        if not isinstance(value.get("findings"), list):
            value["findings"] = []
        normalized_findings = []
        for finding in value["findings"]:
            if not isinstance(finding, dict):
                finding = {"message": str(finding)}
            normalized_findings.append(
                {
                    "code": str(finding.get("code", "INDEPENDENT_REVIEW")),
                    "message": str(
                        finding.get("message", "Independent review failed.")
                    ),
                    "source": str(
                        finding.get("source", finding.get("evidence", ""))
                    ),
                    "candidate": str(
                        finding.get("candidate", finding.get("path", ""))
                    ),
                    "correction": str(
                        finding.get(
                            "correction",
                            "Resolve the cited source or contract mismatch.",
                        )
                    ),
                }
            )
        value["findings"] = normalized_findings
        if value["verdict"] == "accepted" and normalized_findings:
            value["verdict"] = "rejected"
        if not isinstance(value.get("summary"), str):
            value["summary"] = f"Independent auditor returned {value['verdict']}."
        return value
    status = str(value.get("status", "")).lower()
    blockers = value.get("blockers")
    if not isinstance(blockers, list):
        blockers = []
    if status in {"accept", "accepted", "complete", "completed", "pass", "passed"}:
        verdict = "accepted" if not blockers else "rejected"
    elif status in {"reject", "rejected", "fail", "failed", "conflict"}:
        verdict = "rejected"
    else:
        verdict = "needs_more_info"
    findings = []
    for blocker in blockers:
        if not isinstance(blocker, dict):
            continue
        findings.append(
            {
                "code": str(blocker.get("code", "INDEPENDENT_REVIEW")),
                "message": str(blocker.get("message", "Independent review failed.")),
                "source": str(blocker.get("source", "independent evidence")),
                "candidate": str(blocker.get("path", blocker.get("candidate", ""))),
                "correction": str(
                    blocker.get(
                        "correction",
                        "Resolve the cited source or contract mismatch.",
                    )
                ),
            }
        )
    return {
        "verdict": verdict,
        "summary": str(value.get("summary", f"Independent review returned {status}.")),
        "findings": findings,
    }


def completed_review(invocation: dict[str, Any]) -> dict[str, Any]:
    if invocation.get("processExit") != 0:
        return {
            "verdict": "needs_more_info",
            "summary": (
                f"auditor process exited {invocation.get('processExit')} "
                "without clean completion"
            ),
            "findings": [],
        }
    try:
        review = normalize_review(parse_json_object(invocation["finalText"]))
        errors = validate_review(review)
    except (KeyError, ValueError) as exc:
        errors = [str(exc)]
        review = {}
    if errors:
        return {
            "verdict": "needs_more_info",
            "summary": "; ".join(errors),
            "findings": [],
        }
    return review


def normalize_evidence(value: dict[str, Any]) -> dict[str, Any]:
    def normalize_named_items(
        raw: Any,
        *,
        object_markers: set[str],
    ) -> list[dict[str, Any]] | None:
        if isinstance(raw, list):
            return [item for item in raw if isinstance(item, dict)]
        if not isinstance(raw, dict):
            return None
        if object_markers.intersection(raw):
            return [raw]
        if raw and all(isinstance(item, dict) for item in raw.values()):
            return [
                {"name": name, **item} if not item.get("name") else item
                for name, item in raw.items()
            ]
        return None

    if str(value.get("status", "")).startswith("source_facts_complete"):
        value["status"] = "complete"
    if (
        value.get("status") in {"ready", "complete", "completed"}
        and not isinstance(value.get("blockers"), list)
    ):
        value["blockers"] = []
    facts = value.get("facts")
    if isinstance(facts, dict):
        workloads = normalize_named_items(
            facts.get("workloads"),
            object_markers={
                "name",
                "image",
                "build",
                "process",
                "listener",
                "nativeSettings",
            },
        )
        if workloads is None:
            for alias in (
                "workload",
                "application",
                "productionWorkload",
                "selectedWorkload",
            ):
                workloads = normalize_named_items(
                    facts.get(alias),
                    object_markers={
                        "name",
                        "image",
                        "build",
                        "process",
                        "listener",
                        "nativeSettings",
                    },
                )
                if workloads is not None:
                    break
        if workloads is not None:
            facts["workloads"] = workloads

        dependencies = normalize_named_items(
            facts.get("dependencies"),
            object_markers={
                "kind",
                "type",
                "sourceVersion",
                "clientLibrary",
                "supportedOverrides",
            },
        )
        if dependencies is None:
            dependencies = []
            dependency = normalize_named_items(
                facts.get("dependency"),
                object_markers={
                    "kind",
                    "type",
                    "sourceVersion",
                    "clientLibrary",
                    "supportedOverrides",
                },
            )
            if dependency:
                dependencies.extend(dependency)
            for alias in ("database", "broker", "cache", "storage", "model", "search"):
                item = facts.get(alias)
                if isinstance(item, dict) and item.get("kind"):
                    dependencies.append(item)
        if dependencies:
            facts["dependencies"] = dependencies
        if not isinstance(facts.get("route"), dict):
            application = facts.get("application")
            if isinstance(application, dict) and isinstance(
                application.get("route"), dict
            ):
                facts["route"] = application["route"]
        dependencies = facts.get("dependencies")
        if isinstance(dependencies, list):
            for dependency in dependencies:
                if not isinstance(dependency, dict):
                    continue
                candidate = str(
                    dependency.get("kind", dependency.get("type", ""))
                ).strip().lower()
                if candidate in DEPENDENCY_AUTHORING_TYPES:
                    dependency["kind"] = candidate
                if (
                    not dependency.get("versionScope")
                    and "development" in str(
                        dependency.get("versionEvidence", "")
                    ).lower()
                ):
                    dependency["versionScope"] = "developmentImplementation"
        blockers = value.get("blockers")
        if isinstance(blockers, list) and source_selected_types(value):
            retained = []
            for blocker in blockers:
                if isinstance(blocker, dict):
                    text = " ".join(
                        str(blocker.get(key, ""))
                        for key in ("code", "message")
                    ).lower()
                else:
                    text = str(blocker).lower()
                contract_pending = (
                    "radius" in text
                    and "contract" in text
                    and any(
                        marker in text
                        for marker in ("pending", "unresolved", "not resolved")
                    )
                )
                if not contract_pending:
                    retained.append(blocker)
            value["blockers"] = retained
            if not retained and value.get("status") in {
                "blocked",
                "needs_more_info",
                "conflict",
            }:
                value["status"] = "complete"
        if value.get("status") not in {
            "ready",
            "complete",
            "completed",
            "blocked",
            "needs_more_info",
            "conflict",
        }:
            blockers = value.get("blockers")
            workloads = facts.get("workloads")
            if isinstance(blockers, list) and blockers:
                value["status"] = "blocked"
            elif (
                isinstance(blockers, list)
                and not blockers
                and isinstance(workloads, list)
                and workloads
            ):
                value["status"] = "complete"
    return value


def validate_evidence(value: dict[str, Any]) -> list[str]:
    errors = []
    if value.get("status") not in {
        "ready",
        "complete",
        "completed",
        "blocked",
        "needs_more_info",
        "conflict",
    }:
        errors.append("invalid or missing evidence status")
    if not isinstance(value.get("facts"), dict):
        errors.append("evidence facts must be an object")
    else:
        facts = value["facts"]
        if not isinstance(facts.get("workloads"), list) or not facts["workloads"]:
            errors.append("facts.workloads must contain the selected workloads")
    if not isinstance(value.get("blockers"), list):
        errors.append("evidence blockers must be an array")
    return errors


def reconcile_requirements(
    candidate: Path, authoring_contract: dict[str, Any]
) -> dict[str, Any]:
    aliases = {
        "host": {"host", "hostname"},
        "port": {"port"},
        "database": {"database", "db"},
        "username": {"username", "user"},
        "password": {"password", "passwd", "pwd"},
        "endpoint": {"endpoint", "url", "uri"},
        "apikey": {"apikey", "key"},
        "accountname": {"accountname", "account"},
        "container": {"container", "bucket"},
    }

    def tokens(value: Any) -> set[str]:
        text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(value))
        return {
            item
            for item in re.split(r"[^A-Za-z0-9]+", text.lower())
            if item
        }

    requirements_path = candidate / "requirements.json"
    app_path = candidate / "app.bicep"
    requirements = json.loads(requirements_path.read_text())
    source = app_path.read_text()
    resource_types = {
        symbol: resource_type.split("@", 1)[0]
        for symbol, resource_type in re.findall(
            r"\bresource\s+([A-Za-z_][A-Za-z0-9_]*)\s+'([^']+)'",
            source,
        )
    }
    changes = []
    for dependency in requirements.get("dependencies", []):
        if not isinstance(dependency, dict):
            continue
        resource_symbol = dependency.get("resourceSymbol")
        qualified_type = resource_types.get(resource_symbol)
        bundle = authoring_contract.get("bundles", {}).get(qualified_type, {})
        protocol = bundle.get("protocol") or {}
        binding = protocol.get("binding") or {}
        required_settings = [
            setting.partition("=")
            for setting in (
                protocol.get("requiredClientSettings", [])
                + protocol.get("runtimeRequiredClientSettings", [])
            )
        ]
        required_names = {name for name, _, _ in required_settings}
        for setting in dependency.get("settings", []):
            if not isinstance(setting, dict):
                continue
            name = setting.get("name")
            if not isinstance(name, str):
                continue
            bare_name = name.partition("=")[0]
            if bare_name in required_names and bare_name != name:
                setting["name"] = bare_name
                changes.append(
                    {
                        "resourceSymbol": resource_symbol,
                        "from": name,
                        "to": bare_name,
                    }
                )
                name = bare_name
            delivery_kind = (setting.get("delivery") or {}).get("kind")
            delivery = setting.get("delivery") or {}
            if name in required_names:
                required_value = next(
                    value
                    for required_name, _, value in required_settings
                    if required_name == name
                )
                if (
                    required_value == ""
                    and delivery_kind == "runtimeConfig"
                    and delivery.get("key")
                ):
                    delivery["kind"] = "env"
                    delivery_kind = "env"
                    changes.append(
                        {
                            "resourceSymbol": resource_symbol,
                            "setting": name,
                            "fromKind": "runtimeConfig",
                            "toKind": "env",
                        }
                    )
                if (
                    required_value == ""
                    and delivery_kind == "env"
                    and "value" in delivery
                ):
                    removed = delivery.pop("value")
                    changes.append(
                        {
                            "resourceSymbol": resource_symbol,
                            "setting": name,
                            "removedDynamicValue": removed,
                        }
                    )
                continue
            matches = []
            if "*" in name:
                pattern = "^" + re.sub(r"(?:\\\*)+", ".+", re.escape(name)) + "$"
                for required_name, _, required_value in required_settings:
                    if not re.fullmatch(pattern, required_name):
                        continue
                    is_managed_secret = required_value.startswith("managedSecret:")
                    if delivery_kind == "secretKeyRef" and not is_managed_secret:
                        continue
                    if delivery_kind != "secretKeyRef" and is_managed_secret:
                        continue
                    matches.append(required_name)
            setting_tokens = tokens(delivery.get("key", name))
            port_literal = binding.get("portLiteral", binding.get("port"))
            if (
                "port" in required_names
                and delivery_kind == "sourceDefault"
                and port_literal is not None
                and str(delivery.get("value")) == str(port_literal)
            ):
                matches.append("port")
            for binding_name, target in binding.items():
                if not binding_name.endswith(("Input", "Property", "Secret")):
                    continue
                target_name = str(target)
                normalized_target = re.sub(r"[^a-z0-9]", "", target_name.lower())
                if target_name not in required_names:
                    continue
                target_aliases = aliases.get(normalized_target, {target_name.lower()})
                if setting_tokens & target_aliases:
                    matches.append(target_name)
            if delivery_kind == "secretKeyRef":
                secret_candidates = [
                    required_name
                    for required_name, _, required_value in required_settings
                    if required_value.startswith("managedSecret:")
                    or re.search(
                        r"(?:password|secret|key)$",
                        required_name,
                        re.IGNORECASE,
                    )
                ]
                if len(secret_candidates) == 1:
                    matches.extend(secret_candidates)
            matches = sorted(set(matches))
            if len(matches) == 1:
                setting["name"] = matches[0]
                changes.append(
                    {
                        "resourceSymbol": resource_symbol,
                        "from": name,
                        "to": matches[0],
                    }
                )
            resolved_name = setting.get("name")
            required_value = next(
                (
                    value
                    for required_name, _, value in required_settings
                    if required_name == resolved_name
                ),
                None,
            )
            if (
                required_value == ""
                and delivery_kind == "runtimeConfig"
                and delivery.get("key")
            ):
                delivery["kind"] = "env"
                delivery_kind = "env"
                changes.append(
                    {
                        "resourceSymbol": resource_symbol,
                        "setting": str(resolved_name),
                        "fromKind": "runtimeConfig",
                        "toKind": "env",
                    }
                )
            if (
                required_value == ""
                and delivery_kind == "env"
                and "value" in delivery
            ):
                removed = delivery.pop("value")
                changes.append(
                    {
                        "resourceSymbol": resource_symbol,
                        "setting": str(resolved_name),
                        "removedDynamicValue": removed,
                    }
                )
    if changes:
        write_json(requirements_path, requirements)

    config_changes = []
    config_path = candidate / "bicepconfig.json"
    try:
        config = json.loads(config_path.read_text())
    except (OSError, json.JSONDecodeError):
        config = None
    if isinstance(config, dict):
        experimental = config.setdefault("experimentalFeaturesEnabled", {})
        if isinstance(experimental, dict) and experimental.get("extensibility") is not True:
            experimental["extensibility"] = True
            config_changes.append("experimentalFeaturesEnabled.extensibility")
        extensions = config.setdefault("extensions", {})
        extension_ref = authoring_contract["extension"]["reference"]
        if isinstance(extensions, dict) and extensions.get("radius") != extension_ref:
            extensions["radius"] = extension_ref
            config_changes.append("extensions.radius")
        if config_changes:
            write_json(config_path, config)
    return {
        "requirementChanges": changes,
        "bicepConfigChanges": config_changes,
    }


def remove_resource_property(
    source: str,
    *,
    qualified_type: str,
    property_name: str,
) -> tuple[str, bool]:
    lines = source.splitlines(keepends=True)
    start = next(
        (
            index
            for index, line in enumerate(lines)
            if re.search(
                rf"\bresource\s+[A-Za-z_][A-Za-z0-9_]*\s+'"
                rf"{re.escape(qualified_type)}@[^']+'\s*=\s*\{{",
                line,
            )
        ),
        None,
    )
    if start is None:
        return source, False
    depth = 0
    end = None
    for index in range(start, len(lines)):
        depth += lines[index].count("{") - lines[index].count("}")
        if index > start and depth == 0:
            end = index
            break
    if end is None:
        return source, False
    property_index = next(
        (
            index
            for index in range(start + 1, end)
            if re.match(
                rf"^\s+{re.escape(property_name)}\s*:",
                lines[index],
            )
        ),
        None,
    )
    if property_index is None:
        return source, False
    del lines[property_index]
    return "".join(lines), True


def reconcile_optional_versions(
    candidate: Path,
    evidence: dict[str, Any],
) -> list[dict[str, str]]:
    facts = evidence.get("facts") if isinstance(evidence, dict) else None
    if not isinstance(facts, dict):
        return []
    dependencies = facts.get("dependencies")
    if not isinstance(dependencies, list):
        return []
    source_path = candidate / "app.bicep"
    source = source_path.read_text()
    changes: list[dict[str, str]] = []
    for dependency in dependencies:
        if (
            not isinstance(dependency, dict)
            or dependency.get("versionScope") != "developmentImplementation"
        ):
            continue
        qualified_type = DEPENDENCY_AUTHORING_TYPES.get(
            str(dependency.get("kind", "")).lower()
        )
        if not qualified_type:
            continue
        source, removed = remove_resource_property(
            source,
            qualified_type=qualified_type,
            property_name="version",
        )
        if removed:
            changes.append(
                {
                    "resourceType": qualified_type,
                    "property": "version",
                }
            )
    if changes:
        source_path.write_text(source)
    return changes


def reconcile_fixed_source_ports(
    candidate: Path,
    evidence: dict[str, Any],
    authoring_contract: dict[str, Any],
) -> list[dict[str, Any]]:
    facts = evidence.get("facts") if isinstance(evidence, dict) else None
    source_dependencies = facts.get("dependencies") if isinstance(facts, dict) else None
    if not isinstance(source_dependencies, list):
        return []

    fixed_ports: dict[str, set[str]] = {}
    for dependency in source_dependencies:
        if not isinstance(dependency, dict):
            continue
        kind = str(dependency.get("kind", "")).lower()
        overrides = dependency.get("supportedOverrides")
        if isinstance(overrides, dict):
            overrides = list(overrides.values())
        if not isinstance(overrides, list):
            continue
        for override in overrides:
            if (
                isinstance(override, dict)
                and str(override.get("field", "")).lower() == "port"
                and override.get("configurable") is False
                and override.get("default") is not None
            ):
                fixed_ports.setdefault(kind, set()).add(str(override["default"]))

    source_path = candidate / "app.bicep"
    resource_types = {
        symbol: resource_type.split("@", 1)[0]
        for symbol, resource_type in re.findall(
            r"\bresource\s+([A-Za-z_][A-Za-z0-9_]*)\s+'([^']+)'",
            source_path.read_text(),
        )
    }
    type_kinds = {
        qualified_type: kind
        for kind, qualified_type in DEPENDENCY_AUTHORING_TYPES.items()
    }
    requirements_path = candidate / "requirements.json"
    requirements = json.loads(requirements_path.read_text())
    changes: list[dict[str, Any]] = []
    for dependency in requirements.get("dependencies", []):
        if not isinstance(dependency, dict):
            continue
        symbol = dependency.get("resourceSymbol")
        qualified_type = resource_types.get(symbol)
        kind = type_kinds.get(qualified_type)
        ports = fixed_ports.get(kind or "", set())
        binding = (
            authoring_contract.get("bundles", {})
            .get(qualified_type, {})
            .get("protocol", {})
            .get("binding", {})
        )
        contract_port = binding.get("portLiteral", binding.get("port"))
        if (
            len(ports) != 1
            or contract_port is None
            or str(contract_port) not in ports
        ):
            continue
        setting = next(
            (
                item
                for item in dependency.get("settings", [])
                if isinstance(item, dict)
                and str(item.get("name", "")).partition("=")[0] == "port"
            ),
            None,
        )
        if not isinstance(setting, dict):
            continue
        expected = int(contract_port) if str(contract_port).isdigit() else contract_port
        delivery = setting.get("delivery")
        if (
            isinstance(delivery, dict)
            and delivery.get("kind") == "sourceDefault"
            and delivery.get("value") == expected
        ):
            continue
        setting["delivery"] = {"kind": "sourceDefault", "value": expected}
        changes.append(
            {
                "resourceSymbol": symbol,
                "setting": "port",
                "value": expected,
            }
        )
    if changes:
        write_json(requirements_path, requirements)
    return changes


def reconcile_bicep_expressions(candidate: Path) -> list[dict[str, str]]:
    source_path = candidate / "app.bicep"
    source = source_path.read_text()
    pattern = re.compile(
        r"(?m)^(\s*value\s*:\s*)'\$\{"
        r"([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+)"
        r"\}'(\s*)$"
    )
    changes: list[dict[str, str]] = []

    def replace(match: re.Match[str]) -> str:
        changes.append(
            {
                "from": f"'${{{match.group(2)}}}'",
                "to": match.group(2),
            }
        )
        return f"{match.group(1)}{match.group(2)}{match.group(3)}"

    rewritten = pattern.sub(replace, source)
    if changes:
        source_path.write_text(rewritten)
    return changes


def reconcile_published_images(
    candidate: Path,
    evidence: dict[str, Any],
) -> list[dict[str, str]]:
    facts = evidence.get("facts") if isinstance(evidence, dict) else None
    if not isinstance(facts, dict):
        return []
    images: set[str] = set()
    build = facts.get("build")
    if isinstance(build, dict) and build.get("mode") == "publishedRelease":
        image = build.get("image")
        if isinstance(image, str) and image:
            images.add(image)
    workloads = facts.get("workloads")
    if isinstance(workloads, list):
        for workload in workloads:
            if not isinstance(workload, dict):
                continue
            workload_build = workload.get("build")
            if (
                isinstance(workload_build, dict)
                and workload_build.get("mode") == "publishedRelease"
            ):
                image = workload_build.get("image", workload.get("image"))
                if isinstance(image, str) and image:
                    images.add(image)
    if len(images) != 1:
        return []

    source_path = candidate / "app.bicep"
    source = source_path.read_text()
    declarations = list(
        re.finditer(
            r"(?m)^resource\s+([A-Za-z_][A-Za-z0-9_]*)\s+"
            r"'Radius\.Compute/containerImages@[^']+'\s*=\s*\{",
            source,
        )
    )
    if len(declarations) != 1:
        return []
    declaration = declarations[0]
    symbol = declaration.group(1)
    reference = f"{symbol}.properties.imageReference"
    if reference not in source:
        return []
    depth = 0
    end = None
    for index in range(declaration.end() - 1, len(source)):
        depth += (source[index] == "{") - (source[index] == "}")
        if depth == 0:
            end = index + 1
            break
    if end is None:
        return []
    while end < len(source) and source[end] in "\r\n":
        end += 1
    image = next(iter(images))
    rewritten = source[: declaration.start()] + source[end:]
    rewritten = rewritten.replace(reference, f"'{image}'")
    if reference in rewritten:
        return []
    source_path.write_text(rewritten)
    return [{"resourceSymbol": symbol, "image": image}]


def upper_snake(value: str) -> str:
    separated = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", value)
    return re.sub(r"[^A-Za-z0-9]+", "_", separated).strip("_").upper()


def evidence_process(evidence: dict[str, Any]) -> str | None:
    def find(value: Any) -> str | None:
        if isinstance(value, dict):
            process = value.get("process")
            if isinstance(process, str) and process.strip():
                return process.strip()
            if isinstance(process, dict):
                command = process.get("command")
                if isinstance(command, str) and command.strip():
                    return command.strip()
            for item in value.values():
                found = find(item)
                if found:
                    return found
        elif isinstance(value, list):
            for item in value:
                found = find(item)
                if found:
                    return found
        return None

    found = find(evidence)
    if found:
        return found
    return None


def bicep_single_quoted(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def insert_runtime_command(
    source: str,
    *,
    composite_key: str,
    secret_key: str,
    command: str,
) -> str | None:
    key_pattern = re.compile(
        rf"(?m)^(\s*){re.escape(composite_key)}(\s*:\s*\{{\s*)$"
    )
    match = key_pattern.search(source)
    if match:
        renamed = source[: match.start()] + (
            f"{match.group(1)}{secret_key}{match.group(2)}"
        ) + source[match.end() :]
    elif re.search(
        rf"(?m)^\s*{re.escape(secret_key)}\s*:\s*\{{\s*$",
        source,
    ):
        renamed = source
    else:
        return None
    lines = renamed.splitlines(keepends=True)
    secret_index = next(
        (
            index
            for index, line in enumerate(lines)
            if re.match(rf"^\s*{re.escape(secret_key)}\s*:\s*\{{\s*$", line)
        ),
        None,
    )
    if secret_index is None:
        return None
    env_index = next(
        (
            index
            for index in range(secret_index - 1, -1, -1)
            if re.match(r"^\s*env\s*:\s*\{\s*$", lines[index])
        ),
        None,
    )
    if env_index is None:
        return None
    env_indent = re.match(r"^(\s*)", lines[env_index]).group(1)
    container_indent = env_indent[:-2] if len(env_indent) >= 2 else ""
    container_index = next(
        (
            index
            for index in range(env_index - 1, -1, -1)
            if re.match(
                rf"^{re.escape(container_indent)}(?:"
                r"[A-Za-z_][A-Za-z0-9_-]*|'[^']+'|\"[^\"]+\""
                r")\s*:\s*\{\s*$",
                lines[index],
            )
        ),
        None,
    )
    if container_index is None:
        return None
    ranges: list[tuple[int, int]] = []
    index = container_index + 1
    property_pattern = re.compile(
        rf"^{re.escape(env_indent)}(?:command|args)\s*:\s*\["
    )
    while index < env_index:
        if not property_pattern.match(lines[index]):
            index += 1
            continue
        depth = 0
        end = index
        while end < env_index:
            depth += lines[end].count("[") - lines[end].count("]")
            if depth == 0:
                break
            end += 1
        if depth != 0:
            return None
        ranges.append((index, end + 1))
        index = end + 1
    for start, end in reversed(ranges):
        del lines[start:end]
    secret_index = next(
        (
            index
            for index, line in enumerate(lines)
            if re.match(rf"^\s*{re.escape(secret_key)}\s*:\s*\{{\s*$", line)
        ),
        None,
    )
    if secret_index is None:
        return None
    env_index = next(
        (
            index
            for index in range(secret_index - 1, -1, -1)
            if re.match(r"^\s*env\s*:\s*\{\s*$", lines[index])
        ),
        None,
    )
    if env_index is None:
        return None
    encoded = bicep_single_quoted(command)
    block = (
        f"{env_indent}command: [\n"
        f"{env_indent}  '/bin/sh'\n"
        f"{env_indent}  '-c'\n"
        f"{env_indent}]\n"
        f"{env_indent}args: [\n"
        f"{env_indent}  '{encoded}'\n"
        f"{env_indent}]\n"
    )
    lines.insert(env_index, block)
    return "".join(lines)


def reconcile_stale_secret_composite_env(
    candidate: Path,
) -> list[dict[str, str]]:
    requirements = json.loads((candidate / "requirements.json").read_text())
    runtime_keys = {
        delivery.get("key")
        for dependency in requirements.get("dependencies", [])
        if isinstance(dependency, dict)
        for setting in dependency.get("settings", [])
        if isinstance(setting, dict)
        for delivery in [setting.get("delivery")]
        if isinstance(delivery, dict)
        and delivery.get("kind") == "runtimeConfig"
        and isinstance(delivery.get("key"), str)
    }
    secret_keys = {
        item.get("key")
        for item in requirements.get("secretEnvironment", [])
        if isinstance(item, dict) and isinstance(item.get("key"), str)
    }
    if not runtime_keys or not secret_keys:
        return []

    source_path = candidate / "app.bicep"
    lines = source_path.read_text().splitlines(keepends=True)
    removals: list[tuple[int, int, str, str]] = []
    for runtime_key in sorted(runtime_keys):
        key_pattern = re.compile(
            rf"^(\s*)(?:'|\")?{re.escape(runtime_key)}(?:'|\")?"
            r"\s*:\s*\{\s*$"
        )
        for key_index, line in enumerate(lines):
            match = key_pattern.match(line)
            if not match:
                continue
            key_indent = match.group(1)
            env_indent = key_indent[:-2]
            env_index = next(
                (
                    index
                    for index in range(key_index - 1, -1, -1)
                    if re.match(
                        rf"^{re.escape(env_indent)}env\s*:\s*\{{\s*$",
                        lines[index],
                    )
                ),
                None,
            )
            if env_index is None:
                continue
            depth = 0
            env_end = None
            for index in range(env_index, len(lines)):
                depth += lines[index].count("{") - lines[index].count("}")
                if index > env_index and depth == 0:
                    env_end = index
                    break
            if env_end is None or key_index >= env_end:
                continue
            depth = 0
            key_end = None
            for index in range(key_index, env_end + 1):
                depth += lines[index].count("{") - lines[index].count("}")
                if index > key_index and depth == 0:
                    key_end = index + 1
                    break
            if key_end is None:
                continue
            key_text = "".join(lines[key_index:key_end])
            if not re.search(r"(?m)^\s*value\s*:", key_text):
                continue
            container_indent = env_indent[:-2]
            container_index = next(
                (
                    index
                    for index in range(env_index - 1, -1, -1)
                    if re.match(
                        rf"^{re.escape(container_indent)}(?:"
                        r"[A-Za-z_][A-Za-z0-9_-]*|'[^']+'|\"[^\"]+\""
                        r")\s*:\s*\{\s*$",
                        lines[index],
                    )
                ),
                None,
            )
            if container_index is None:
                continue
            depth = 0
            container_end = None
            for index in range(container_index, len(lines)):
                depth += lines[index].count("{") - lines[index].count("}")
                if index > container_index and depth == 0:
                    container_end = index + 1
                    break
            if container_end is None:
                continue
            container_text = "".join(lines[container_index:container_end])
            if not re.search(
                rf"\bexport\s+{re.escape(runtime_key)}\s*=",
                container_text,
            ):
                continue
            secret_key = next(
                (
                    key
                    for key in sorted(secret_keys)
                    if re.search(
                        rf"\$(?:\{{{re.escape(key)}\}}|{re.escape(key)}\b)",
                        key_text,
                    )
                    and re.search(
                        rf"\$(?:\{{{re.escape(key)}\}}|{re.escape(key)}\b)",
                        container_text,
                    )
                ),
                None,
            )
            if secret_key:
                removals.append(
                    (key_index, key_end, runtime_key, secret_key)
                )
    for start, end, _, _ in sorted(removals, reverse=True):
        del lines[start:end]
    if removals:
        source_path.write_text("".join(lines))
    return [
        {"runtimeKey": runtime_key, "secretEnvironment": secret_key}
        for _, _, runtime_key, secret_key in removals
    ]


def reconcile_runtime_composites(
    candidate: Path,
    authoring_contract: dict[str, Any],
    evidence: dict[str, Any],
) -> list[dict[str, str]]:
    requirements_path = candidate / "requirements.json"
    source_path = candidate / "app.bicep"
    requirements = json.loads(requirements_path.read_text())
    source = source_path.read_text()
    process = evidence_process(evidence)
    if not process:
        return []
    resource_types = {
        symbol: resource_type.split("@", 1)[0]
        for symbol, resource_type in re.findall(
            r"\bresource\s+([A-Za-z_][A-Za-z0-9_]*)\s+'([^']+)'",
            source,
        )
    }
    changes: list[dict[str, str]] = []
    for dependency in requirements.get("dependencies", []):
        if not isinstance(dependency, dict):
            continue
        symbol = dependency.get("resourceSymbol")
        qualified_type = resource_types.get(symbol)
        protocol = (
            authoring_contract.get("bundles", {})
            .get(qualified_type, {})
            .get("protocol")
            or {}
        )
        composite = protocol.get("runtimeComposite")
        if not isinstance(composite, dict):
            continue
        username = composite.get("username")
        managed_secret = composite.get("managedSecret")
        template = composite.get("format")
        if not all(isinstance(item, str) and item for item in (
            username,
            managed_secret,
            template,
        )):
            continue
        required = [
            item.partition("=")
            for item in protocol.get("requiredClientSettings", [])
        ]
        username_name = next(
            (name for name, _, value in required if value == username),
            None,
        )
        secret_name = next(
            (
                name
                for name, _, value in required
                if value == f"managedSecret:{managed_secret}"
            ),
            None,
        )
        settings = {
            item.get("name"): item
            for item in dependency.get("settings", [])
            if isinstance(item, dict)
        }
        username_setting = settings.get(username_name)
        secret_setting = settings.get(secret_name)
        if not isinstance(username_setting, dict) or not isinstance(
            secret_setting, dict
        ):
            continue
        username_delivery = username_setting.get("delivery") or {}
        secret_delivery = secret_setting.get("delivery") or {}
        exported_setting = re.search(
            r"\bexport\s+([A-Z][A-Z0-9_]+)\s*=",
            source,
        )
        composite_key = (
            username_delivery.get("key")
            or (exported_setting.group(1) if exported_setting else None)
            or secret_delivery.get("key")
        )
        if not isinstance(composite_key, str) or not composite_key:
            continue
        existing_secret_env = secret_delivery.get("key")
        secret_env = (
            existing_secret_env
            if isinstance(existing_secret_env, str)
            and existing_secret_env
            and existing_secret_env != composite_key
            else f"RADIUS_{upper_snake(str(symbol))}_{upper_snake(managed_secret)}"
        )
        runtime_value = (
            template.replace("<username>", username)
            .replace("<password>", f"${secret_env}")
            .replace('"', '\\"')
            .replace(username, f"\\{username}")
        )
        shell_command = (
            f'export {composite_key}="{runtime_value}"; exec {process}'
        )
        rewritten = insert_runtime_command(
            source,
            composite_key=composite_key,
            secret_key=secret_env,
            command=shell_command,
        )
        if rewritten is None:
            continue
        source = rewritten
        username_setting["delivery"] = {
            "kind": "runtimeConfig",
            "key": composite_key,
            "value": username,
        }
        secret_setting["delivery"] = {
            "kind": "secretKeyRef",
            "key": secret_env,
            "secretKey": managed_secret,
        }
        secret_environment = [
            item
            for item in requirements.get("secretEnvironment", [])
            if isinstance(item, dict)
            and item.get("key") not in {composite_key, secret_env}
        ]
        secret_environment.append({"key": secret_env})
        requirements["secretEnvironment"] = secret_environment
        changes.append(
            {
                "resourceSymbol": str(symbol),
                "setting": str(composite.get("setting")),
                "secretEnvironment": secret_env,
            }
        )
    if changes:
        source_path.write_text(source)
        write_json(requirements_path, requirements)
    return changes


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", default=".")
    parser.add_argument("--request", required=True)
    parser.add_argument("--deadline-seconds", type=float, default=390)
    parser.add_argument("--evidence-timeout", type=float, default=100)
    parser.add_argument("--author-timeout", type=float, default=200)
    parser.add_argument("--review-timeout", type=float, default=100)
    parser.add_argument("--repair-timeout", type=float, default=90)
    parser.add_argument("--final-review-timeout", type=float, default=100)
    parser.add_argument("--artifact-dir")
    args = parser.parse_args()

    started = time.monotonic()
    deadline = started + args.deadline_seconds
    target = Path(args.target).resolve()
    commit_process = subprocess.run(
        ["git", "-C", str(target), "rev-parse", "HEAD"],
        text=True,
        capture_output=True,
        check=False,
    )
    if commit_process.returncode:
        print(json.dumps({"status": "failed", "reason": "target is not in Git"}))
        return 1
    commit = commit_process.stdout.strip()
    repository_root = Path(
        subprocess.run(
            ["git", "-C", str(target), "rev-parse", "--show-toplevel"],
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
    ).resolve()
    relative_target = target.relative_to(repository_root)
    source_path = relative_target.as_posix() if relative_target.parts else "."
    remote = subprocess.run(
        ["git", "-C", str(target), "config", "--get", "remote.origin.url"],
        text=True,
        capture_output=True,
        check=False,
    ).stdout.strip()
    tags = exact_tags(target, remote, commit)
    receipt: Path | None = None
    if args.artifact_dir:
        run_dir = Path(args.artifact_dir).resolve()
        run_dir.mkdir(parents=True, exist_ok=True)
    else:
        git_dir_value = subprocess.run(
            ["git", "-C", str(target), "rev-parse", "--git-dir"],
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        git_dir = Path(git_dir_value)
        if not git_dir.is_absolute():
            git_dir = (repository_root / git_dir).resolve()
        receipt = receipt_path(
            git_dir,
            commit=commit,
            source_path=source_path,
            request=args.request,
        )
        if receipt.is_file():
            try:
                cached = json.loads(receipt.read_text())
            except (OSError, json.JSONDecodeError):
                cached = None
            if isinstance(cached, dict) and (
                cached.get("status") != "accepted"
                or all(
                    (target / ".radius" / name).is_file()
                    for name in ("app.bicep", "bicepconfig.json")
                )
            ):
                cached["receiptReused"] = True
                print(json.dumps(cached, sort_keys=True))
                return 0 if cached.get("status") == "accepted" else 1
        run_dir = (
            git_dir
            / "app-modeling-runs"
            / (time.strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8])
        ).resolve()
        run_dir.mkdir(parents=True, exist_ok=False)
    candidate = run_dir / "candidate"
    candidate.mkdir(parents=True, exist_ok=True)
    installed: list[Path] = []
    reviewer_session = str(uuid.uuid4())
    writer_session = str(uuid.uuid4())
    auditor_session = str(uuid.uuid4())
    status: dict[str, Any] = {
        "status": "failed",
        "reason": None,
        "artifacts": str(run_dir),
        "sourceCommit": commit,
        "sourceTags": tags,
        "request": args.request,
    }
    common = f"""
User acceptance request: {args.request}
Source repository: {target}
Git repository root: {repository_root}
Application path within remote: {source_path}
Remote: {remote}
Immutable source revision: {commit}
Exact Git tags at revision: {json.dumps(tags)}
Skill directory: {SKILL_DIR}
Contract query: {SKILL_DIR / 'scripts' / 'contract_query.py'}
Verified contract: {SKILL_DIR / 'assets' / 'radius-contract.json'}
Candidate directory: {candidate}
Expected/golden application definitions are unavailable.
"""
    try:
        installed = install_agents(target)
        evidence_prompt = (
            "Independently derive cited source facts and select the profile "
            "required by the user request. Do not inspect or select Radius "
            "types; the parent resolves contracts after source facts close. "
            "Return compact JSON with status, facts, and blockers. Do not "
            "write files.\n" + common
        )
        evidence_invocation = invoke(
            target=target,
            run_dir=run_dir,
            label="reviewer-evidence",
            agent="radius-model-reviewer",
            session_id=reviewer_session,
            prompt=evidence_prompt,
            timeout=min(args.evidence_timeout, remaining(deadline)),
            effort="low",
        )
        status["evidence"] = public_invocation(evidence_invocation)
        (run_dir / "reviewer-evidence.txt").write_text(
            evidence_invocation["finalText"] + "\n"
        )
        try:
            evidence = normalize_evidence(
                parse_json_object(evidence_invocation["finalText"])
            )
            evidence_errors = validate_evidence(evidence)
        except ValueError as exc:
            evidence = {}
            evidence_errors = [str(exc)]
        if evidence_invocation["processExit"] != 0 and not evidence_errors:
            status["evidenceRecoveredFromExit"] = evidence_invocation["processExit"]
        evidence_blocked = evidence.get("status") in {
            "blocked",
            "needs_more_info",
            "conflict",
        }
        if evidence_errors or evidence_blocked:
            infrastructure_retry = bool(
                evidence_errors
                and (
                    evidence_invocation["timedOut"]
                    or evidence_invocation["processExit"] != 0
                )
            )
            if infrastructure_retry:
                reviewer_session = str(uuid.uuid4())
                retry_prompt = (
                    "The prior source-review process ended without a valid "
                    "handoff. Perform the source review once in this fresh "
                    "session and return only the compact evidence JSON.\n"
                    + evidence_prompt
                )
            elif evidence_errors:
                retry_prompt = (
                    "Your evidence JSON was malformed or incomplete: "
                    + "; ".join(evidence_errors)
                    + ". Do not use tools. Restate the completed evidence as one "
                    "compact valid JSON object with status, blockers, and facts "
                    "containing workloads, dependencies, route, and "
                    "persistentPaths. Preserve all already closed citations."
                )
            else:
                retry_prompt = (
                    "Recheck only the reported source blockers once. Use tools "
                    "when needed to inspect the exact source revision and Git "
                    "tags supplied in the original prompt; do not broadly "
                    "rescan. Contract selection is not a source blocker. "
                    "Preserve genuine packaging or runtime blockers; otherwise "
                    "return the closed profile as one compact JSON object with "
                    "status, facts, and blockers."
                )
            evidence_retry = invoke(
                target=target,
                run_dir=run_dir,
                label=(
                    "reviewer-evidence-infra-retry"
                    if infrastructure_retry
                    else "reviewer-evidence-retry"
                ),
                agent="radius-model-reviewer",
                session_id=reviewer_session,
                prompt=retry_prompt,
                timeout=min(
                    args.evidence_timeout if infrastructure_retry else 40,
                    remaining(deadline),
                ),
                resume=not infrastructure_retry,
                effort="low",
            )
            status["evidenceRetry"] = public_invocation(evidence_retry)
            try:
                evidence = normalize_evidence(
                    parse_json_object(evidence_retry["finalText"])
                )
                evidence_errors = validate_evidence(evidence)
            except ValueError as exc:
                evidence_errors = [str(exc)]
            if (
                infrastructure_retry
                and evidence_errors
                and evidence_retry["processExit"] == 0
                and remaining(deadline) > 10
            ):
                evidence_format_retry = invoke(
                    target=target,
                    run_dir=run_dir,
                    label="reviewer-evidence-format-retry",
                    agent="radius-model-reviewer",
                    session_id=reviewer_session,
                    prompt=(
                        "Your completed evidence handoff was malformed: "
                        + "; ".join(evidence_errors)
                        + ". Do not use tools or change the facts. Restate it "
                        "once as one compact valid JSON object with status, "
                        "facts, and blockers."
                    ),
                    timeout=min(30, remaining(deadline)),
                    resume=True,
                    effort="low",
                )
                status["evidenceFormatRetry"] = public_invocation(
                    evidence_format_retry
                )
                try:
                    evidence = normalize_evidence(
                        parse_json_object(evidence_format_retry["finalText"])
                    )
                    evidence_errors = validate_evidence(evidence)
                except ValueError as exc:
                    evidence_errors = [str(exc)]
            if evidence_retry["processExit"] != 0 and not evidence_errors:
                status["evidenceRetryRecoveredFromExit"] = evidence_retry[
                    "processExit"
                ]
            if evidence_errors:
                status["evidenceResult"] = {
                    "status": evidence.get("status"),
                    "blockers": evidence.get("blockers", [])[:8],
                    "errors": evidence_errors,
                }
                status["reason"] = "; ".join(evidence_errors)
                return 1
        status["evidenceResult"] = {
            "status": evidence.get("status"),
            "blockers": evidence.get("blockers", [])[:8],
            "errors": evidence_errors,
        }
        write_json(run_dir / "reviewer-evidence.json", evidence)
        if evidence.get("status") in {"blocked", "needs_more_info", "conflict"}:
            status["reason"] = "independent evidence could not close the requested profile"
            return 1
        authoring_contract = build_authoring_contract(evidence)
        write_json(run_dir / "authoring-contract.json", authoring_contract)

        author_invocation = invoke(
            target=target,
            run_dir=run_dir,
            label="writer-initial",
            agent="radius-model-writer",
            session_id=writer_session,
            prompt=(
                "Author the complete four-file candidate from the independent "
                "evidence report. Do not broadly rescan source or skill prose. "
                "Write the four files directly under Candidate directory (no "
                "nested .radius), then return without compiling or self-review. "
                f"Evidence: {run_dir / 'reviewer-evidence.json'}\n"
                f"Authoring contract: {run_dir / 'authoring-contract.json'}\n"
                "Requirements schema: "
                f"{SKILL_DIR / 'schemas' / 'requirements.schema.json'}\n"
                + common
            ),
            timeout=min(args.author_timeout, remaining(deadline)),
            effort="low",
        )
        status["author"] = public_invocation(author_invocation)
        candidate_files = list(candidate.iterdir())
        handoff_errors = validate_handoff(candidate)
        if (
            handoff_errors
            and remaining(deadline) > 30
        ):
            if candidate_files:
                shutil.copytree(candidate, run_dir / "candidate-incomplete")
            writer_session = str(uuid.uuid4())
            author_invocation = invoke(
                target=target,
                run_dir=run_dir,
                label="writer-handoff-retry",
                agent="radius-model-writer",
                session_id=writer_session,
                prompt=(
                    "The prior writer process ended without a complete valid "
                    "four-file handoff. Replace or complete the four files "
                    "directly under the Candidate directory from "
                    f"{run_dir / 'reviewer-evidence.json'}, "
                    f"{run_dir / 'authoring-contract.json'}, and "
                    f"{SKILL_DIR / 'schemas' / 'requirements.schema.json'}. "
                    "Return immediately after writing.\n" + common
                ),
                timeout=min(args.author_timeout, remaining(deadline)),
                effort="low",
            )
            status["authorRetry"] = public_invocation(author_invocation)
            handoff_errors = validate_handoff(candidate)
        if author_invocation["processExit"] != 0 and handoff_errors:
            status["authorHandoffErrors"] = handoff_errors[:8]
            status["reason"] = "author failed"
            return 1
        if author_invocation["processExit"] != 0:
            status["authorRecoveredFromExit"] = author_invocation["processExit"]
        shutil.copytree(candidate, run_dir / "candidate-initial")
        reconciliation = (
            {
                "requirementChanges": [],
                "bicepConfigChanges": [],
                "sourceDefaultChanges": [],
                "runtimeCompositeChanges": [],
                "secretCompositeEnvChanges": [],
                "optionalVersionChanges": [],
                "publishedImageChanges": [],
                "bicepExpressionChanges": [],
            }
            if handoff_errors
            else reconcile_requirements(candidate, authoring_contract)
        )
        if not handoff_errors:
            reconciliation["sourceDefaultChanges"] = reconcile_fixed_source_ports(
                candidate,
                evidence,
                authoring_contract,
            )
            reconciliation["optionalVersionChanges"] = (
                reconcile_optional_versions(candidate, evidence)
            )
            reconciliation["publishedImageChanges"] = (
                reconcile_published_images(candidate, evidence)
            )
            reconciliation["bicepExpressionChanges"] = (
                reconcile_bicep_expressions(candidate)
            )
            reconciliation["runtimeCompositeChanges"] = (
                reconcile_runtime_composites(
                    candidate,
                    authoring_contract,
                    evidence,
                )
            )
            reconciliation["secretCompositeEnvChanges"] = (
                reconcile_stale_secret_composite_env(candidate)
            )
        write_json(run_dir / "reconciliation-1.json", reconciliation)
        validation = (
            {"valid": False, "errors": handoff_errors}
            if handoff_errors
            else validate_all(
                candidate,
                run_dir,
                evidence,
                remote=remote,
                commit=commit,
                source_path=source_path,
            )
        )
        write_json(run_dir / "validation-1.json", validation)

        review_invocation = invoke(
            target=target,
            run_dir=run_dir,
            label="auditor-candidate",
            agent="radius-model-auditor",
            session_id=auditor_session,
            prompt=f"""
Audit the candidate against {run_dir / 'reviewer-evidence.json'},
{run_dir / 'authoring-contract.json'}, the user request, and the following:
{candidate / 'source-facts.json'}, {candidate / 'requirements.json'},
{candidate / 'app.bicep'}, {candidate / 'bicepconfig.json'}, and
{run_dir / 'validation-1.json'}. Return only the required audit JSON.
""",
            timeout=min(args.review_timeout, remaining(deadline)),
            effort="low",
        )
        status["review"] = public_invocation(review_invocation)
        review = completed_review(review_invocation)
        write_json(run_dir / "review-1.json", review)

        if validation.get("valid") and review.get("verdict") == "needs_more_info":
            retry_invocation = invoke(
                target=target,
                run_dir=run_dir,
                label="auditor-retry",
                agent="radius-model-auditor",
                session_id=str(uuid.uuid4()),
                prompt=f"""
Return a compact audit JSON for {candidate / 'app.bicep'} using only
{run_dir / 'reviewer-evidence.json'}, {run_dir / 'authoring-contract.json'},
{candidate / 'source-facts.json'}, {candidate / 'requirements.json'},
{candidate / 'bicepconfig.json'}, and {run_dir / 'validation-1.json'}.
""",
                timeout=min(args.final_review_timeout, remaining(deadline)),
                effort="low",
            )
            status["reviewRetry"] = public_invocation(retry_invocation)
            review = completed_review(retry_invocation)
            write_json(run_dir / "review-retry.json", review)

        if not validation.get("valid") or review.get("verdict") == "rejected":
            repair_session = str(uuid.uuid4())
            repair_invocation = invoke(
                target=target,
                run_dir=run_dir,
                label="writer-repair",
                agent="radius-model-writer",
                session_id=repair_session,
                prompt=f"""
Repair the existing four-file candidate in {candidate} once using
{run_dir / 'reviewer-evidence.json'}, {run_dir / 'authoring-contract.json'},
{run_dir / 'validation-1.json'}, and {run_dir / 'review-1.json'}.
Change only fields required by those findings; preserve every validator-clean
source, secret, protocol, persistence, process, and graph binding. Reconcile
the full tuple only when a cited finding changes its representation. Do not
read validator source or rescan the repository. Return after updating the
candidate files.
""",
                timeout=min(args.repair_timeout, remaining(deadline)),
                effort="low",
            )
            status["repair"] = public_invocation(repair_invocation)
            shutil.copytree(candidate, run_dir / "candidate-repaired")
            reconciliation = reconcile_requirements(candidate, authoring_contract)
            reconciliation["sourceDefaultChanges"] = reconcile_fixed_source_ports(
                candidate,
                evidence,
                authoring_contract,
            )
            reconciliation["optionalVersionChanges"] = (
                reconcile_optional_versions(candidate, evidence)
            )
            reconciliation["publishedImageChanges"] = (
                reconcile_published_images(candidate, evidence)
            )
            reconciliation["bicepExpressionChanges"] = (
                reconcile_bicep_expressions(candidate)
            )
            reconciliation["runtimeCompositeChanges"] = (
                reconcile_runtime_composites(
                    candidate,
                    authoring_contract,
                    evidence,
                )
            )
            reconciliation["secretCompositeEnvChanges"] = (
                reconcile_stale_secret_composite_env(candidate)
            )
            write_json(run_dir / "reconciliation-2.json", reconciliation)
            validation = validate_all(
                candidate,
                run_dir,
                evidence,
                remote=remote,
                commit=commit,
                source_path=source_path,
            )
            write_json(run_dir / "validation-2.json", validation)
            if repair_invocation["processExit"] != 0 and validation.get("valid"):
                status["repairRecoveredFromExit"] = repair_invocation[
                    "processExit"
                ]
            auditor_session = str(uuid.uuid4())
            final_invocation = invoke(
                target=target,
                run_dir=run_dir,
                label="auditor-final",
                agent="radius-model-auditor",
                session_id=auditor_session,
                prompt=f"""
Audit the repaired {candidate / 'app.bicep'},
{candidate / 'bicepconfig.json'}, {candidate / 'source-facts.json'}, and
{candidate / 'requirements.json'} against
{run_dir / 'reviewer-evidence.json'}, {run_dir / 'authoring-contract.json'},
and {run_dir / 'validation-2.json'}. Return only compact audit JSON.
""",
                timeout=min(args.final_review_timeout, remaining(deadline)),
                effort="low",
            )
            status["finalReview"] = public_invocation(final_invocation)
            review = completed_review(final_invocation)
            write_json(run_dir / "review-2.json", review)

        if remaining(deadline) <= 0:
            status["reason"] = "internal deadline exceeded"
            return 1
        status["validation"] = {
            "valid": validation.get("valid") is True,
            "errors": validation.get("errors", [])[:8],
        }
        status["audit"] = {
            "verdict": review.get("verdict"),
            "summary": review.get("summary"),
            "findings": review.get("findings", [])[:8],
        }
        if not validation.get("valid"):
            status["reason"] = "mechanical validation rejected the candidate"
            return 1
        if review.get("verdict") != "accepted":
            status["reason"] = f"independent review returned {review.get('verdict')}"
            return 1

        destination = target / ".radius"
        destination.mkdir(parents=True, exist_ok=True)
        for name in ("app.bicep", "bicepconfig.json"):
            shutil.copy2(candidate / name, destination / name)
        status["status"] = "accepted"
        return 0
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        status["reason"] = str(exc)
        return 1
    finally:
        remove_agents(installed)
        status["elapsedSeconds"] = round(time.monotonic() - started, 3)
        write_json(run_dir / "run-status.json", status)
        if receipt is not None:
            write_json(receipt, status)
        print(json.dumps(status, sort_keys=True))


if __name__ == "__main__":
    raise SystemExit(main())
