#!/usr/bin/env python3

"""Run evidence -> author -> validation -> retained-reviewer loop."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shlex
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


def startup_dockerfiles(
    evidence: dict[str, Any],
    item: dict[str, Any],
    *,
    source_root: Path,
    source_path: str,
) -> list[Path]:
    facts = evidence.get("facts")
    names: set[str] = set()
    workload_name = item.get("workload")
    workloads = facts.get("workloads", []) if isinstance(facts, dict) else []
    for workload in workloads:
        if not isinstance(workload, dict) or workload_name not in {
            workload.get("name"),
            workload.get("workload"),
            workload.get("service"),
        }:
            continue
        build = workload.get("build")
        if isinstance(build, dict) and isinstance(build.get("dockerfile"), str):
            names.add(build["dockerfile"])
        citations = []
        for raw_citations in (
            build.get("citations") if isinstance(build, dict) else None,
            workload.get("citations"),
        ):
            if isinstance(raw_citations, str):
                citations.append(raw_citations)
            elif isinstance(raw_citations, list):
                citations.extend(raw_citations)
        for citation in citations:
            if not isinstance(citation, str):
                continue
            cited_name = re.sub(r":\d+(?:-\d+)?$", "", citation)
            if "dockerfile" in Path(cited_name).name.lower():
                names.add(cited_name)
    citations = item.get("citation")
    if isinstance(citations, str):
        citations = [citations]
    if isinstance(citations, list):
        for citation in citations:
            if not isinstance(citation, str):
                continue
            cited_name = re.sub(r":\d+(?:-\d+)?$", "", citation)
            if "dockerfile" in Path(cited_name).name.lower():
                names.add(cited_name)

    paths = []
    application_root = (
        source_root if source_path in {"", "."} else source_root / source_path
    )
    for name in sorted(names):
        for path in (application_root / name, source_root / name):
            resolved = path.resolve()
            try:
                resolved.relative_to(source_root.resolve())
            except ValueError:
                continue
            if resolved.is_file() and resolved not in paths:
                paths.append(resolved)
    return paths


def dockerfile_effective_process(
    dockerfile: Path,
) -> tuple[Any, set[str], set[str], int | None]:
    instructions: list[tuple[int, str, str]] = []
    buffer = ""
    start = 0
    for line_number, raw_line in enumerate(
        dockerfile.read_text(errors="replace").splitlines(),
        start=1,
    ):
        if not buffer:
            start = line_number
        stripped = raw_line.rstrip()
        continued = stripped.endswith("\\")
        buffer += stripped[:-1] + " " if continued else stripped
        if continued:
            continue
        match = re.match(r"^\s*([A-Za-z]+)\s+(.+?)\s*$", buffer)
        if match:
            instructions.append((start, match.group(1).upper(), match.group(2)))
        buffer = ""

    entrypoint: Any = None
    command: Any = None
    command_line: int | None = None
    for line_number, instruction, payload in instructions:
        if instruction == "FROM":
            entrypoint = None
            command = None
            command_line = None
            continue
        if instruction not in {"ENTRYPOINT", "CMD"}:
            continue
        try:
            value = json.loads(payload) if payload.startswith("[") else payload
        except json.JSONDecodeError:
            continue
        if instruction == "ENTRYPOINT":
            entrypoint = value
        else:
            command = value
            command_line = line_number

    if isinstance(entrypoint, list):
        process: Any = list(entrypoint)
        if isinstance(command, list):
            process.extend(command)
        elif isinstance(command, str) and command.strip():
            process.extend(["/bin/sh", "-c", command.strip()])
    elif isinstance(entrypoint, str) and entrypoint.strip():
        suffix = shell_process(command)
        process = " ".join(
            item for item in (entrypoint.strip(), suffix) if item
        )
    else:
        process = command

    tokens: list[str]
    if isinstance(process, list):
        tokens = [item for item in process if isinstance(item, str)]
    elif isinstance(process, str):
        try:
            tokens = shlex.split(process)
        except ValueError:
            tokens = []
    else:
        tokens = []

    config_paths: set[str] = set()
    artifacts: set[str] = set()
    if tokens and tokens[0].startswith("/"):
        artifacts.add(tokens[0])
    config_flags = {
        "-c",
        "-f",
        "--config",
        "--config-file",
        "--configuration",
        "--settings",
    }
    artifact_flags = {"-jar", "--jar"}
    command_interpreters = {"ash", "bash", "dash", "sh", "zsh"}
    executable = Path(tokens[0]).name if tokens else ""
    for index, token in enumerate(tokens):
        if (
            token in config_flags
            and index + 1 < len(tokens)
            and not (token == "-c" and executable in command_interpreters)
        ):
            path = tokens[index + 1]
            if path.startswith("/"):
                config_paths.add(path)
        elif any(
            token.startswith(prefix)
            for prefix in (
                "--config=",
                "--config-file=",
                "--configuration=",
                "--settings=",
            )
        ):
            path = token.split("=", 1)[1]
            if path.startswith("/"):
                config_paths.add(path)
        elif token in artifact_flags and index + 1 < len(tokens):
            artifacts.add(tokens[index + 1])
    return process, config_paths, artifacts, command_line


def same_process_path(path: str, process_path: str) -> bool:
    return path == process_path or (
        not process_path.startswith("/")
        and Path(path).name == Path(process_path).name
    )


def reconcile_source_startup_facts(
    evidence: dict[str, Any],
    *,
    source_root: Path,
    source_path: str,
) -> list[dict[str, str]]:
    facts = evidence.get("facts")
    workloads = facts.get("workloads", []) if isinstance(facts, dict) else []
    startup_files = facts.get("startupFiles") if isinstance(facts, dict) else None
    if not isinstance(workloads, list) or not isinstance(startup_files, list):
        return []

    changes: list[dict[str, str]] = []
    process_details: dict[str, tuple[Any, set[str], set[str], str]] = {}
    for workload in workloads:
        if not isinstance(workload, dict):
            continue
        name = str(
            workload.get(
                "name",
                workload.get("workload", workload.get("service", "")),
            )
        )
        if not name:
            continue
        dockerfiles = startup_dockerfiles(
            evidence,
            {
                "workload": name,
                "citation": workload.get("citations", []),
            },
            source_root=source_root,
            source_path=source_path,
        )
        if len(dockerfiles) != 1:
            continue
        dockerfile = dockerfiles[0]
        process, config_paths, artifacts, command_line = (
            dockerfile_effective_process(dockerfile)
        )
        if not process:
            continue
        try:
            relative = dockerfile.resolve().relative_to(source_root.resolve())
        except ValueError:
            continue
        citation = (
            f"{relative.as_posix()}:{command_line}"
            if command_line is not None
            else relative.as_posix()
        )
        prior = evidence_process(
            {"facts": {"workloads": [workload]}},
            name,
        )
        derived = shell_process(process)
        if derived and prior != derived:
            workload["process"] = process
            changes.append(
                {
                    "workload": name,
                    "property": "process",
                    "source": citation,
                }
            )
        process_details[name] = (process, config_paths, artifacts, citation)

    single_workload = next(
        (
            str(
                item.get(
                    "name",
                    item.get("workload", item.get("service", "")),
                )
            )
            for item in workloads
            if isinstance(item, dict)
        ),
        "",
    ) if len([item for item in workloads if isinstance(item, dict)]) == 1 else ""

    retained: list[Any] = []
    for item in startup_files:
        if not isinstance(item, dict):
            retained.append(item)
            continue
        item.pop("processArgument", None)
        if not isinstance(item.get("workload"), str) and single_workload:
            item["workload"] = single_workload
        name = item.get("workload")
        path = item.get("path")
        detail = process_details.get(name) if isinstance(name, str) else None
        if not detail or not isinstance(path, str):
            retained.append(item)
            continue
        _, config_paths, artifacts, citation = detail
        if any(same_process_path(path, artifact) for artifact in artifacts):
            changes.append(
                {
                    "workload": name,
                    "property": "startupFiles",
                    "removedArtifact": path,
                }
            )
            continue
        if path in config_paths:
            item["processArgument"] = True
            if not isinstance(item.get("citation"), str):
                item["citation"] = citation
        retained.append(item)
    startup_files[:] = retained

    known = {
        (item.get("workload"), item.get("path"))
        for item in startup_files
        if isinstance(item, dict)
    }
    for name, (_, config_paths, _, citation) in process_details.items():
        for path in sorted(config_paths):
            if (name, path) in known:
                continue
            item = {
                "workload": name,
                "path": path,
                "required": True,
                "delivery": "image",
                "presentInImage": True,
                "citation": citation,
                "processArgument": True,
            }
            if not image_provides_startup_file(
                evidence,
                item,
                source_root=source_root,
                source_path=source_path,
            ):
                item["delivery"] = "operatorConfig"
                item.pop("presentInImage")
            startup_files.append(item)
            changes.append(
                {
                    "workload": name,
                    "property": "startupFiles",
                    "addedConfig": path,
                }
            )
    return changes


def image_provides_startup_file(
    evidence: dict[str, Any],
    item: dict[str, Any],
    *,
    source_root: Path,
    source_path: str,
) -> bool:
    path = item.get("path")
    if not isinstance(path, str):
        return False
    for dockerfile in startup_dockerfiles(
        evidence,
        item,
        source_root=source_root,
        source_path=source_path,
    ):
        logical_source = re.sub(
            r"\\\r?\n\s*",
            " ",
            dockerfile.read_text(errors="replace"),
        )
        for line in logical_source.splitlines():
            match = re.match(r"^\s*(COPY|ADD)\s+(.+)$", line, re.IGNORECASE)
            if not match:
                continue
            payload = match.group(2).strip()
            try:
                if payload.startswith("["):
                    tokens = json.loads(payload)
                else:
                    tokens = shlex.split(payload, comments=True)
            except (json.JSONDecodeError, ValueError):
                continue
            tokens = [
                str(token)
                for token in tokens
                if not str(token).startswith("--")
            ]
            if len(tokens) < 2:
                continue
            destination = tokens[-1]
            sources = tokens[:-1]
            if destination == path:
                return True
            if destination.endswith("/") and any(
                destination + Path(source).name == path for source in sources
            ):
                return True
    return False


def startup_content_is_selected(
    item: dict[str, Any],
    *,
    source_root: Path,
    source_path: str,
) -> bool:
    if item.get("profileSelectedBy") not in {"request", "canonicalProduction"}:
        return False
    citations = item.get("contentCitation")
    if isinstance(citations, str):
        citations = [citations]
    if not isinstance(citations, list):
        return False
    application_root = (
        source_root if source_path in {"", "."} else source_root / source_path
    )
    for citation in citations:
        if not isinstance(citation, str):
            continue
        name = re.sub(r":\d+(?:-\d+)?$", "", citation)
        for path in (application_root / name, source_root / name):
            resolved = path.resolve()
            try:
                resolved.relative_to(source_root.resolve())
            except ValueError:
                continue
            if resolved.is_file():
                return True
    return False


def reconcile_startup_file_delivery(
    evidence: dict[str, Any],
    *,
    source_root: Path,
    source_path: str,
) -> list[dict[str, str]]:
    facts = evidence.get("facts")
    startup_files = facts.get("startupFiles", []) if isinstance(facts, dict) else []
    changes = []
    for item in startup_files:
        if not isinstance(item, dict) or item.get("delivery") != "image":
            continue
        image_proven = image_provides_startup_file(
            evidence,
            item,
            source_root=source_root,
            source_path=source_path,
        )
        content_selected = startup_content_is_selected(
            item,
            source_root=source_root,
            source_path=source_path,
        )
        if image_proven and content_selected:
            continue
        item["delivery"] = "operatorConfig"
        item.pop("presentInImage", None)
        item["mechanicalReason"] = (
            "required file lacks selected production content provenance"
            if image_proven
            else "selected Dockerfile does not COPY or ADD the required file"
        )
        changes.append(
            {
                "workload": str(item.get("workload", "")),
                "path": str(item.get("path", "")),
                "from": "image",
                "to": "operatorConfig",
            }
        )
    return changes


def workload_facts(
    evidence: dict[str, Any],
    name: str,
) -> dict[str, Any] | None:
    facts = evidence.get("facts")
    workloads = facts.get("workloads", []) if isinstance(facts, dict) else []
    matching = [
        item
        for item in workloads
        if isinstance(item, dict)
        and name
        in {
            str(item.get("name", "")),
            str(item.get("workload", "")),
            str(item.get("service", "")),
        }
    ]
    if matching:
        return matching[0]
    eligible = [item for item in workloads if isinstance(item, dict)]
    return eligible[0] if len(eligible) == 1 else None


def writable_directories(workload: dict[str, Any] | None) -> list[str]:
    values = workload.get("writablePaths", []) if isinstance(workload, dict) else []
    directories: list[str] = []
    for item in values:
        if not isinstance(item, dict):
            continue
        path = item.get("path")
        if (
            isinstance(path, str)
            and path.startswith("/")
            and item.get("kind") == "directory"
            and isinstance(item.get("writableBy"), str)
            and item["writableBy"].strip()
            and isinstance(item.get("citation"), str)
            and item["citation"].strip()
        ):
            directories.append(path.rstrip("/") or "/")
    return directories


def path_is_within(path: str, directory: str) -> bool:
    return path == directory or path.startswith(directory.rstrip("/") + "/")


def reconcile_operator_materialized_paths(
    evidence: dict[str, Any],
) -> list[dict[str, str]]:
    facts = evidence.get("facts")
    startup_files = facts.get("startupFiles", []) if isinstance(facts, dict) else []
    changes: list[dict[str, str]] = []
    stdin_candidates: dict[str, int] = {}
    for item in startup_files:
        if (
            isinstance(item, dict)
            and item.get("delivery") == "operatorConfig"
            and item.get("processArgument") is True
            and isinstance(item.get("workload"), str)
        ):
            workload_name = item["workload"]
            stdin_candidates[workload_name] = (
                stdin_candidates.get(workload_name, 0) + 1
            )
    for item in startup_files:
        if not isinstance(item, dict) or item.get("delivery") != "operatorConfig":
            continue
        item.pop("materializedPath", None)
        item.pop("transport", None)
        path = item.get("path")
        workload_name = item.get("workload")
        if not isinstance(path, str) or not isinstance(workload_name, str):
            continue
        directories = writable_directories(workload_facts(evidence, workload_name))
        if any(path_is_within(path, directory) for directory in directories):
            item["materializedPath"] = path
            item["transport"] = "file"
            continue
        directory = next((value for value in directories if value != "/"), None)
        if directory is None:
            if (
                item.get("processArgument") is not True
                or stdin_candidates.get(workload_name) != 1
            ):
                continue
            item["materializedPath"] = "/dev/stdin"
            item["transport"] = "stdin"
            changes.append(
                {
                    "workload": workload_name,
                    "sourcePath": path,
                    "materializedPath": "/dev/stdin",
                }
            )
            continue
        materialized_path = f"{directory}/{Path(path).name}"
        item["materializedPath"] = materialized_path
        item["transport"] = "file"
        changes.append(
            {
                "workload": workload_name,
                "sourcePath": path,
                "materializedPath": materialized_path,
            }
        )
    return changes


def candidate_provisions_path(source: str, path: str) -> bool:
    quoted_path = rf"['\"]?{re.escape(path)}['\"]?"
    patterns = (
        rf"\b(?:cat|printf)\b[\s\S]{{0,500}}?>\s*{quoted_path}",
        rf"\btee(?:\s+-[A-Za-z]+)*\s+{quoted_path}",
        rf"\b(?:cp|install)\b[^\n;]{{0,500}}\s+{quoted_path}",
    )
    return any(re.search(pattern, source) for pattern in patterns)


def candidate_streams_path(
    source: str,
    path: str,
    *,
    expected_process: str | None,
) -> bool:
    if path != "/dev/stdin" or not expected_process:
        return False
    required = (
        "trap forward_term TERM",
        "trap forward_int INT",
        'kill -TERM "$child"',
        'kill -INT "$child"',
        'if wait "$child"; then child_status=0; else child_status=$?; fi',
        'exit "$child_status"',
        expected_process,
    )
    for match in re.finditer(
        r"\bset -u;[\s\S]{0,4000}?exit \"\$child_status\"",
        source,
    ):
        command = match.group(0)
        if (
            re.search(
                rf"\|[\s\S]{{0,500}}{re.escape(path)}",
                command,
            )
            and all(fragment in command for fragment in required)
        ):
            return True
    return False


def validate_startup_input_closure(
    candidate: Path,
    evidence: dict[str, Any],
    *,
    source_root: Path | None = None,
    source_path: str = ".",
) -> list[dict[str, str]]:
    """Require every selected startup configuration file to be available."""

    source = (candidate / "app.bicep").read_text()
    facts = evidence.get("facts")
    startup_files = facts.get("startupFiles", []) if isinstance(facts, dict) else []
    errors: list[dict[str, str]] = []
    required_paths: set[str] = set()

    for index, item in enumerate(startup_files):
        if not isinstance(item, dict) or item.get("required") is False:
            continue
        source_startup_path = item.get("path")
        if (
            not isinstance(source_startup_path, str)
            or not source_startup_path.startswith("/")
        ):
            continue
        delivery = str(item.get("delivery", "")).strip()
        path = (
            item.get("materializedPath", source_startup_path)
            if delivery == "operatorConfig"
            else source_startup_path
        )
        if not isinstance(path, str) or not path.startswith("/"):
            continue
        required_paths.add(path)
        if delivery == "image" and item.get("presentInImage") is True:
            if source_root is not None and (
                image_provides_startup_file(
                    evidence,
                    item,
                    source_root=source_root,
                    source_path=source_path,
                )
                and startup_content_is_selected(
                    item,
                    source_root=source_root,
                    source_path=source_path,
                )
            ):
                continue
            errors.append(
                {
                    "code": "STARTUP_IMAGE_PROOF",
                    "path": f"$.sourceFacts.startupFiles[{index}]",
                    "message": (
                        f"Independent evidence claims {source_startup_path} is "
                        "image-provided, "
                        "but the selected Dockerfile and production-content "
                        "citations do not prove that claim."
                    ),
                }
            )
            continue
        if delivery == "operatorConfig":
            workload_name = str(item.get("workload", ""))
            if item.get("transport") == "stdin":
                expected_process = evidence_process(
                    evidence,
                    workload_name or None,
                )
                if expected_process:
                    expected_process = substitute_process_path(
                        expected_process,
                        source_startup_path,
                        path,
                    )
                if candidate_streams_path(
                    source,
                    path,
                    expected_process=expected_process,
                ):
                    continue
                errors.append(
                    {
                        "code": "STARTUP_INPUT_STREAM",
                        "path": f"$.sourceFacts.startupFiles[{index}]",
                        "message": (
                            f"Operator configuration is not streamed to {path} "
                            "before the selected process starts."
                        ),
                    }
                )
                continue
            directories = writable_directories(
                workload_facts(evidence, workload_name)
            )
            if not any(path_is_within(path, directory) for directory in directories):
                errors.append(
                    {
                        "code": "STARTUP_INPUT_WRITABILITY",
                        "path": f"$.sourceFacts.startupFiles[{index}]",
                        "message": (
                            f"Operator configuration cannot be materialized at {path}; "
                            "independent evidence does not prove it writable by the "
                            "selected runtime user."
                        ),
                    }
                )
                continue
        if not candidate_provisions_path(source, path):
            errors.append(
                {
                    "code": "STARTUP_INPUT",
                    "path": f"$.sourceFacts.startupFiles[{index}]",
                    "message": (
                        f"Selected startup requires {path}, but the candidate "
                        "neither creates it before exec nor cites it as present "
                        "in the immutable image."
                    ),
                }
            )
            continue
        if delivery == "operatorConfig" and not (
            "@secure()" in source
            and "Radius.Security/secrets@" in source
            and "secretKeyRef:" in source
        ):
            errors.append(
                {
                    "code": "STARTUP_INPUT_SECURITY",
                    "path": f"$.sourceFacts.startupFiles[{index}]",
                    "message": (
                        f"Operator-supplied startup configuration for {path} "
                        "must enter through a secure parameter and secretKeyRef."
                    ),
                }
            )

    referenced_paths = set(
        re.findall(
            r"(?<![A-Za-z0-9_.-])"
            r"(/[A-Za-z0-9_./-]+\.(?:ya?ml|json|toml|conf|ini))\b",
            source,
            flags=re.IGNORECASE,
        )
    )
    for path in sorted(referenced_paths - required_paths):
        if candidate_provisions_path(source, path):
            continue
        errors.append(
            {
                "code": "UNPROVEN_STARTUP_INPUT",
                "path": "$.app.bicep",
                "message": (
                    f"Candidate references startup configuration {path}, but "
                    "independent evidence does not prove it is image-provided "
                    "and the candidate does not create it."
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
    source_root: Path,
    source_path: str,
    timeout: float,
) -> dict[str, Any]:
    report = validate_candidate(
        candidate,
        run_dir,
        source_remote=remote,
        source_commit=commit,
        source_path=source_path,
        timeout=timeout,
    )
    report["errors"].extend(validate_evidence_candidate(candidate, evidence))
    report["errors"].extend(
        validate_startup_input_closure(
            candidate,
            evidence,
            source_root=source_root,
            source_path=source_path,
        )
    )
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
        workloads = facts.get("workloads")
        if not isinstance(workloads, list) or not workloads:
            errors.append("facts.workloads must contain the selected workloads")
        else:
            for index, workload in enumerate(workloads):
                if not isinstance(workload, dict):
                    errors.append(f"facts.workloads[{index}] must be an object")
                    continue
                name = str(
                    workload.get(
                        "name",
                        workload.get("workload", workload.get("service", "")),
                    )
                )
                scoped = {"facts": {"workloads": [workload]}}
                if not evidence_process(scoped, name or None):
                    errors.append(
                        f"facts.workloads[{index}].process must be a shell "
                        "command, argv, or command/args object"
                    )
        startup_files = facts.get("startupFiles")
        if not isinstance(startup_files, list):
            errors.append(
                "facts.startupFiles must list required startup files or be []"
            )
        else:
            for index, item in enumerate(startup_files):
                if not isinstance(item, dict):
                    errors.append(f"facts.startupFiles[{index}] must be an object")
                    continue
                if not isinstance(item.get("workload"), str):
                    errors.append(
                        f"facts.startupFiles[{index}].workload must be a string"
                    )
                path = item.get("path")
                if not isinstance(path, str) or not path.startswith("/"):
                    errors.append(
                        f"facts.startupFiles[{index}].path must be absolute"
                    )
                if item.get("required") is not True:
                    errors.append(
                        f"facts.startupFiles[{index}].required must be true"
                    )
                if item.get("delivery") not in {
                    "image",
                    "runtimeGenerated",
                    "operatorConfig",
                }:
                    errors.append(
                        f"facts.startupFiles[{index}].delivery is invalid"
                    )
                if not isinstance(item.get("citation"), str):
                    errors.append(
                        f"facts.startupFiles[{index}].citation must be a string"
                    )
                if (
                    item.get("delivery") == "image"
                    and item.get("presentInImage") is not True
                ):
                    errors.append(
                        f"facts.startupFiles[{index}] lacks image-presence proof"
                    )
                needs_operator_directory = (
                    (
                        item.get("delivery") == "operatorConfig"
                        and item.get("processArgument") is not True
                    )
                    or (
                        item.get("delivery") == "image"
                        and item.get("profileSelectedBy")
                        not in {"request", "canonicalProduction"}
                        and item.get("processArgument") is not True
                    )
                )
                if needs_operator_directory:
                    workload = workload_facts(
                        value,
                        str(item.get("workload", "")),
                    )
                    if not writable_directories(workload):
                        errors.append(
                            f"facts.workloads for startupFiles[{index}] must cite "
                            "at least one directory writable by the runtime user"
                        )
    if not isinstance(value.get("blockers"), list):
        errors.append("evidence blockers must be an array")
    return errors


def prepare_evidence(
    value: dict[str, Any],
    *,
    source_root: Path,
    source_path: str,
) -> tuple[dict[str, Any], list[str], list[dict[str, str]]]:
    evidence = normalize_evidence(value)
    changes = reconcile_source_startup_facts(
        evidence,
        source_root=source_root,
        source_path=source_path,
    )
    return evidence, validate_evidence(evidence), changes


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
        source_defaults = dependency.get("sourceDefaults")
        client_tuple = dependency.get("supportedClientTuple")
        endpoint_composition = str(
            dependency.get("endpointComposition", "")
        ).lower()
        if (
            isinstance(source_defaults, dict)
            and isinstance(client_tuple, dict)
            and source_defaults.get("port") is not None
            and source_defaults.get("port") == client_tuple.get("port")
            and (
                "fixed port" in endpoint_composition
                or "hardcoded port" in endpoint_composition
            )
        ):
            fixed_ports.setdefault(kind, set()).add(
                str(source_defaults["port"])
            )

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
    env_keys_to_remove: set[str] = set()
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
            and delivery.get("kind") == "env"
            and isinstance(delivery.get("key"), str)
        ):
            env_keys_to_remove.add(delivery["key"])
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
    if env_keys_to_remove:
        source = source_path.read_text()
        lines = source.splitlines(keepends=True)

        def block_end(start: int, limit: int) -> int | None:
            depth = 0
            for index in range(start, limit):
                depth += lines[index].count("{") - lines[index].count("}")
                if index > start and depth == 0:
                    return index
            return None

        index = 0
        removed = []
        while index < len(lines):
            if not re.match(r"^\s*env\s*:\s*\{", lines[index]):
                index += 1
                continue
            env_end = block_end(index, len(lines))
            if env_end is None:
                break
            cursor = index + 1
            while cursor < env_end:
                key_match = re.match(
                    r"^\s*['\"]?([A-Za-z_][A-Za-z0-9_]*)['\"]?\s*:\s*\{",
                    lines[cursor],
                )
                if not key_match or key_match.group(1) not in env_keys_to_remove:
                    cursor += 1
                    continue
                entry_end = block_end(cursor, env_end + 1)
                if entry_end is None:
                    cursor += 1
                    continue
                removed.append(key_match.group(1))
                del lines[cursor : entry_end + 1]
                env_end -= entry_end - cursor + 1
            index = env_end + 1
        if removed:
            source_path.write_text("".join(lines))
            for key in sorted(set(removed)):
                changes.append({"removedInventedEnv": key})
    if changes:
        write_json(requirements_path, requirements)
    return changes


def reconcile_nested_connections(candidate: Path) -> list[dict[str, str]]:
    source_path = candidate / "app.bicep"
    lines = source_path.read_text().splitlines(keepends=True)
    changes: list[dict[str, str]] = []

    def block_end(start: int, limit: int) -> int | None:
        depth = 0
        for index in range(start, limit):
            depth += lines[index].count("{") - lines[index].count("}")
            if index > start and depth == 0:
                return index
        return None

    resources = [
        (index, match.group(1))
        for index, line in enumerate(lines)
        if (
            match := re.search(
                r"\bresource\s+([A-Za-z_][A-Za-z0-9_]*)\s+"
                r"'Radius\.Compute/containers@[^']+'\s*=\s*\{",
                line,
            )
        )
    ]
    for resource_start, symbol in reversed(resources):
        resource_end = block_end(resource_start, len(lines))
        if resource_end is None:
            continue
        containers_index = next(
            (
                index
                for index in range(resource_start + 1, resource_end)
                if re.match(r"^(\s*)containers\s*:\s*\{\s*$", lines[index])
            ),
            None,
        )
        if containers_index is None:
            continue
        containers_indent = re.match(r"^(\s*)", lines[containers_index]).group(1)
        containers_end = block_end(containers_index, resource_end + 1)
        if containers_end is None:
            continue
        if any(
            re.match(
                rf"^{re.escape(containers_indent)}connections\s*:\s*\{{\s*$",
                lines[index],
            )
            for index in range(resource_start + 1, resource_end)
            if not containers_index <= index <= containers_end
        ):
            continue
        nested_index = next(
            (
                index
                for index in range(containers_index + 1, containers_end)
                if re.match(
                    rf"^{re.escape(containers_indent)}  "
                    r"connections\s*:\s*\{\s*$",
                    lines[index],
                )
            ),
            None,
        )
        if nested_index is None:
            continue
        nested_end = block_end(nested_index, containers_end + 1)
        if nested_end is None:
            continue
        block = [
            line[2:] if line.startswith("  ") else line
            for line in lines[nested_index : nested_end + 1]
        ]
        del lines[nested_index : nested_end + 1]
        containers_end -= nested_end - nested_index + 1
        lines[containers_end + 1 : containers_end + 1] = block
        changes.append({"resourceSymbol": symbol, "property": "connections"})

    if changes:
        source_path.write_text("".join(lines))
    return list(reversed(changes))


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
    image = next(iter(images))
    direct_pattern = re.compile(
        rf"(?m)^(\s*image\s*:\s*)'"
        rf"{re.escape(image)}@sha256:[0-9a-fA-F]{{64}}'(\s*)$"
    )
    source, direct_count = direct_pattern.subn(
        rf"\1'{image}'\2",
        source,
    )
    direct_changes = (
        [{"image": image, "removedUnverifiedDigest": "true"}]
        if direct_count
        else []
    )
    if direct_count:
        source_path.write_text(source)

    declarations = list(
        re.finditer(
            r"(?m)^resource\s+([A-Za-z_][A-Za-z0-9_]*)\s+"
            r"'Radius\.Compute/containerImages@[^']+'\s*=\s*\{",
            source,
        )
    )
    if len(declarations) != 1:
        return direct_changes
    declaration = declarations[0]
    symbol = declaration.group(1)
    reference = f"{symbol}.properties.imageReference"
    if reference not in source:
        return direct_changes
    depth = 0
    end = None
    for index in range(declaration.end() - 1, len(source)):
        depth += (source[index] == "{") - (source[index] == "}")
        if depth == 0:
            end = index + 1
            break
    if end is None:
        return direct_changes
    while end < len(source) and source[end] in "\r\n":
        end += 1
    rewritten = source[: declaration.start()] + source[end:]
    rewritten = rewritten.replace(reference, f"'{image}'")
    if reference in rewritten:
        return direct_changes
    source_path.write_text(rewritten)
    return direct_changes + [{"resourceSymbol": symbol, "image": image}]


def upper_snake(value: str) -> str:
    separated = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", value)
    return re.sub(r"[^A-Za-z0-9]+", "_", separated).strip("_").upper()


def shell_process(value: Any) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if not isinstance(value, list) or not value:
        return None
    if not all(isinstance(item, str) and item for item in value):
        return None
    return " ".join(
        item
        if re.fullmatch(r"\$(?:[A-Za-z_][A-Za-z0-9_]*|\{[A-Za-z_][A-Za-z0-9_]*\})", item)
        else shlex.quote(item)
        for item in value
    )


def evidence_process(
    evidence: dict[str, Any],
    workload: str | None = None,
) -> str | None:
    def normalize(value: Any) -> str | None:
        direct = shell_process(value)
        if direct:
            return direct
        if not isinstance(value, dict):
            return None
        for command_key, args_key in (
            ("command", "args"),
            ("entrypoint", "cmd"),
        ):
            command = shell_process(value.get(command_key))
            args = shell_process(value.get(args_key))
            process = " ".join(item for item in (command, args) if item)
            if process:
                return process
        return None

    def find(value: Any) -> str | None:
        if isinstance(value, dict):
            process = normalize(value.get("process"))
            if process:
                return process
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

    facts = evidence.get("facts")
    workloads = facts.get("workloads") if isinstance(facts, dict) else None
    if isinstance(workloads, dict):
        workloads = [workloads]
    if isinstance(workloads, list):
        eligible = [item for item in workloads if isinstance(item, dict)]
        if workload:
            matching = [
                item
                for item in eligible
                if workload
                in {
                    str(item.get("name", "")),
                    str(item.get("workload", "")),
                    str(item.get("service", "")),
                }
            ]
            if matching:
                return find(matching[0])
            if len(eligible) != 1:
                return None
        if eligible:
            found = find(eligible[0])
            if found:
                return found
    found = find(evidence)
    if found:
        return found
    return None


def bicep_single_quoted(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("'", "\\'")
        .replace("${", r"\${")
    )


def insert_runtime_command(
    source: str,
    *,
    composite_key: str,
    secret_key: str,
    command: str,
) -> str | None:
    def find_environment_key(
        lines: list[str],
        names: tuple[str, ...],
    ) -> tuple[int, int, str] | None:
        for env_index, line in enumerate(lines):
            match = re.match(r"^(\s*)env\s*:\s*\{\s*$", line)
            if not match:
                continue
            env_indent = match.group(1)
            key_indent = env_indent + "  "
            depth = 0
            env_end = None
            for index in range(env_index, len(lines)):
                depth += lines[index].count("{") - lines[index].count("}")
                if index > env_index and depth == 0:
                    env_end = index
                    break
            if env_end is None:
                continue
            for name in names:
                pattern = re.compile(
                    rf"^{re.escape(key_indent)}(?:['\"])?"
                    rf"{re.escape(name)}(?:['\"])?\s*:\s*\{{\s*$"
                )
                key_index = next(
                    (
                        index
                        for index in range(env_index + 1, env_end)
                        if pattern.match(lines[index])
                    ),
                    None,
                )
                if key_index is not None:
                    return env_index, key_index, env_indent
        return None

    lines = source.splitlines(keepends=True)
    located = find_environment_key(lines, (secret_key, composite_key))
    if located is None:
        return None
    env_index, key_index, env_indent = located
    if secret_key != composite_key:
        key_match = re.match(
            r"^(\s*)(?:['\"])?[^:'\"]+(?:['\"])?(\s*:\s*\{\s*)$",
            lines[key_index],
        )
        if key_match is None:
            return None
        lines[key_index] = (
            f"{key_match.group(1)}{secret_key}{key_match.group(2)}"
        )

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
    located = find_environment_key(lines, (secret_key,))
    if located is None:
        return None
    env_index, _, env_indent = located
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


def decode_bicep_single_quoted(value: str) -> str:
    decoded: list[str] = []
    index = 0
    while index < len(value):
        if value[index] == "\\" and index + 1 < len(value):
            index += 1
        decoded.append(value[index])
        index += 1
    return "".join(decoded)


def candidate_runtime_command(source: str, environment_key: str) -> str | None:
    lines = source.splitlines(keepends=True)
    key_index = next(
        (
            index
            for index, line in enumerate(lines)
            if re.match(
                rf"^\s*(?:['\"])?{re.escape(environment_key)}(?:['\"])?"
                r"\s*:\s*\{\s*$",
                line,
            )
        ),
        None,
    )
    if key_index is None:
        return None
    env_index = next(
        (
            index
            for index in range(key_index - 1, -1, -1)
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
    args_index = next(
        (
            index
            for index in range(env_index - 1, container_index, -1)
            if re.match(rf"^{re.escape(env_indent)}args\s*:\s*\[", lines[index])
        ),
        None,
    )
    if args_index is None:
        return None
    depth = 0
    args_end = args_index
    while args_end < env_index:
        depth += lines[args_end].count("[") - lines[args_end].count("]")
        if depth == 0:
            break
        args_end += 1
    if depth != 0:
        return None
    payload = "".join(lines[args_index + 1 : args_end])
    match = re.fullmatch(r"\s*'((?:\\.|[^'])*)'\s*", payload)
    if not match:
        return None
    return decode_bicep_single_quoted(match.group(1))


def secret_key_ref_environment_keys(source: str) -> set[str]:
    lines = source.splitlines()
    keys: set[str] = set()
    for env_index, line in enumerate(lines):
        match = re.match(r"^(\s*)env\s*:\s*\{\s*$", line)
        if not match:
            continue
        env_indent = match.group(1)
        key_indent = env_indent + "  "
        depth = 0
        env_end = None
        for index in range(env_index, len(lines)):
            depth += lines[index].count("{") - lines[index].count("}")
            if index > env_index and depth == 0:
                env_end = index
                break
        if env_end is None:
            continue
        index = env_index + 1
        while index < env_end:
            key_match = re.match(
                rf"^{re.escape(key_indent)}(?:'([^']+)'|\"([^\"]+)\"|"
                r"([A-Za-z_][A-Za-z0-9_]*))\s*:\s*\{\s*$",
                lines[index],
            )
            if not key_match:
                index += 1
                continue
            key_depth = 0
            key_end = index
            while key_end < env_end:
                key_depth += (
                    lines[key_end].count("{") - lines[key_end].count("}")
                )
                if key_end > index and key_depth == 0:
                    break
                key_end += 1
            block = "\n".join(lines[index : key_end + 1])
            if "secretKeyRef:" in block:
                keys.add(next(item for item in key_match.groups() if item))
            index = key_end + 1
    return keys


def container_environment_keys(source: str) -> set[str]:
    lines = source.splitlines()
    keys: set[str] = set()
    for env_index, line in enumerate(lines):
        match = re.match(r"^(\s*)env\s*:\s*\{\s*$", line)
        if not match:
            continue
        key_indent = match.group(1) + "  "
        depth = 0
        env_end = None
        for index in range(env_index, len(lines)):
            depth += lines[index].count("{") - lines[index].count("}")
            if index > env_index and depth == 0:
                env_end = index
                break
        if env_end is None:
            continue
        for line in lines[env_index + 1 : env_end]:
            key_match = re.match(
                rf"^{re.escape(key_indent)}(?:'([^']+)'|\"([^\"]+)\"|"
                r"([A-Za-z_][A-Za-z0-9_]*))\s*:\s*\{\s*$",
                line,
            )
            if key_match:
                keys.add(next(item for item in key_match.groups() if item))
    return keys


def substitute_process_path(
    process: str,
    source_path: str,
    materialized_path: str,
) -> str | None:
    path = re.escape(source_path)
    pattern = re.compile(
        rf"(?<![A-Za-z0-9_./-])(?P<quote>['\"]?){path}(?P=quote)"
        r"(?![A-Za-z0-9_./-])"
    )
    rewritten, count = pattern.subn(shlex.quote(materialized_path), process)
    return rewritten if count == 1 else None


def reconcile_operator_startup_files(
    candidate: Path,
    evidence: dict[str, Any],
) -> list[dict[str, str]]:
    facts = evidence.get("facts")
    startup_files = facts.get("startupFiles", []) if isinstance(facts, dict) else []
    source_path = candidate / "app.bicep"
    source = source_path.read_text()
    available_keys = secret_key_ref_environment_keys(source)
    changes: list[dict[str, str]] = []

    for item in startup_files:
        if (
            not isinstance(item, dict)
            or item.get("required") is False
            or item.get("delivery") != "operatorConfig"
        ):
            continue
        path = item.get("path")
        workload = item.get("workload")
        materialized_path = item.get("materializedPath", path)
        if (
            not isinstance(path, str)
            or not path.startswith("/")
            or not isinstance(materialized_path, str)
            or not materialized_path.startswith("/")
            or candidate_provisions_path(source, materialized_path)
        ):
            continue
        process = evidence_process(
            evidence,
            workload if isinstance(workload, str) else None,
        )
        if not process:
            continue
        process = substitute_process_path(process, path, materialized_path)
        if not process:
            continue

        tokens = {
            token
            for value in (Path(path).stem, workload)
            if isinstance(value, str)
            for token in upper_snake(value).split("_")
            if token
        }
        ranked = sorted(
            (
                (
                    sum(token in upper_snake(key).split("_") for token in tokens)
                    + (1 if "CONFIG" in upper_snake(key).split("_") else 0),
                    key,
                )
                for key in available_keys
            ),
            reverse=True,
        )
        if not ranked:
            continue
        best_score = ranked[0][0]
        best = [key for score, key in ranked if score == best_score]
        if len(best) != 1 or (best_score == 0 and len(available_keys) != 1):
            continue
        environment_key = best[0]
        existing = candidate_runtime_command(source, environment_key)
        if item.get("transport") == "stdin":
            base_command = (
                existing if existing and process in existing else process
            )
            command = (
                'set -u; child=""; forwarded=0; '
                'forward_term() { forwarded=1; if [ -n "$child" ]; then '
                'kill -TERM "$child" 2>/dev/null || true; fi; }; '
                'forward_int() { forwarded=1; if [ -n "$child" ]; then '
                'kill -INT "$child" 2>/dev/null || true; fi; }; '
                "trap forward_term TERM; trap forward_int INT; "
                f"printf '%s' \"${environment_key}\" | {base_command} & "
                'child=$!; while :; do forwarded=0; '
                'if wait "$child"; then child_status=0; '
                'else child_status=$?; fi; '
                'if [ "$forwarded" -eq 0 ]; then '
                'exit "$child_status"; fi; done'
            )
        else:
            base_command = (
                existing if existing and process in existing else f"exec {process}"
            )
            command = (
                f"umask 077; printf '%s' \"${environment_key}\" > "
                f"{shlex.quote(materialized_path)}"
                f" && {base_command}"
            )
        rewritten = insert_runtime_command(
            source,
            composite_key=environment_key,
            secret_key=environment_key,
            command=command,
        )
        if rewritten is None:
            continue
        source = rewritten
        changes.append(
            {
                "workload": str(workload or ""),
                "path": materialized_path,
                "secretEnvironment": environment_key,
            }
        )
    if changes:
        source_path.write_text(source)
    return changes


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


def selected_dependency_fact(
    evidence: dict[str, Any],
    client_kind: str,
) -> dict[str, Any] | None:
    facts = evidence.get("facts")
    dependencies = facts.get("dependencies", []) if isinstance(facts, dict) else []
    if isinstance(dependencies, dict):
        dependencies = [dependencies]
    matching = [
        item
        for item in dependencies
        if isinstance(item, dict)
        and str(item.get("kind", "")).strip().lower() == client_kind.lower()
    ]
    return matching[0] if len(matching) == 1 else None


def source_runtime_encoder(
    evidence: dict[str, Any],
    *,
    source_root: Path,
    source_path: str,
) -> tuple[str, str, str] | None:
    facts = evidence.get("facts")
    workloads = facts.get("workloads", []) if isinstance(facts, dict) else []
    if isinstance(workloads, dict):
        workloads = [workloads]
    eligible = [item for item in workloads if isinstance(item, dict)]
    if len(eligible) != 1:
        return None
    workload = eligible[0]
    name = str(
        workload.get(
            "name",
            workload.get("workload", workload.get("service", "")),
        )
    )
    dockerfiles = startup_dockerfiles(
        evidence,
        {
            "workload": name,
            "citation": workload.get("citations", []),
        },
        source_root=source_root,
        source_path=source_path,
    )
    if len(dockerfiles) != 1:
        return None
    dockerfile = dockerfiles[0]
    logical_source = re.sub(
        r"\\\r?\n\s*",
        " ",
        dockerfile.read_text(errors="replace"),
    )
    from_lines = list(
        re.finditer(
            r"(?im)^\s*FROM\s+(?:(?:--\S+)\s+)*\S+.*$",
            logical_source,
        )
    )
    if not from_lines:
        return None
    final_stage = logical_source[from_lines[-1].start() :]
    install = re.search(
        r"(?im)^\s*RUN\s+.*\b(?:apk\s+add|apt(?:-get)?\s+install|"
        r"dnf\s+install|yum\s+install)\b[^\n]{0,1200}"
        r"\b(python3|python|curl)\b",
        final_stage,
    )
    if not install:
        return None
    try:
        relative = dockerfile.resolve().relative_to(source_root.resolve())
    except ValueError:
        return None
    binary = install.group(1)
    kind = "python" if binary.startswith("python") else binary
    return binary, kind, relative.as_posix()


def reconcile_runtime_uris(
    candidate: Path,
    authoring_contract: dict[str, Any],
    evidence: dict[str, Any],
    *,
    source_root: Path,
    source_path: str,
) -> list[dict[str, Any]]:
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
    runtime_encoder = source_runtime_encoder(
        evidence,
        source_root=source_root,
        source_path=source_path,
    )
    if runtime_encoder is None:
        return []
    encoder_binary, encoder_kind, dockerfile = runtime_encoder
    environment_keys = container_environment_keys(source)
    secret_keys = secret_key_ref_environment_keys(source)
    process = evidence_process(evidence)
    if not process:
        return []

    dependencies = [
        item
        for item in requirements.get("dependencies", [])
        if isinstance(item, dict)
    ]
    if len(dependencies) != 1:
        return []

    changes: list[dict[str, Any]] = []
    for dependency in dependencies:
        symbol = dependency.get("resourceSymbol")
        qualified_type = resource_types.get(symbol)
        protocol = (
            authoring_contract.get("bundles", {})
            .get(qualified_type, {})
            .get("protocol")
            or {}
        )
        runtime_uri = protocol.get("runtimeUri")
        if not isinstance(runtime_uri, dict):
            continue
        client_kind = runtime_uri.get("clientKind", protocol.get("clientKind"))
        if not isinstance(client_kind, str):
            continue
        source_dependency = selected_dependency_fact(evidence, client_kind)
        if source_dependency is None:
            continue
        components = runtime_uri.get("components")
        template = runtime_uri.get("format")
        encoded_components = runtime_uri.get("percentEncode", [])
        suffixes = runtime_uri.get("settingSuffixes", [])
        allowed_schemes = runtime_uri.get("schemes", [])
        if (
            not isinstance(components, list)
            or not components
            or not all(isinstance(item, str) and item for item in components)
            or not isinstance(template, str)
            or not template
            or not isinstance(encoded_components, list)
            or not all(item in components for item in encoded_components)
            or not isinstance(suffixes, list)
            or not all(isinstance(item, str) and item for item in suffixes)
            or not isinstance(allowed_schemes, list)
            or not all(
                isinstance(item, str) and item for item in allowed_schemes
            )
        ):
            continue
        placeholders = re.findall(r"<([A-Za-z][A-Za-z0-9]*)>", template)
        if [item for item in placeholders if item != "scheme"] != components:
            continue

        settings = {
            str(item.get("name", "")).partition("=")[0]: item
            for item in dependency.get("settings", [])
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        }
        source_text = json.dumps(source_dependency, sort_keys=True)
        source_keys = {
            key
            for key in re.findall(r"\b[A-Z][A-Z0-9_]{2,}\b", source_text)
            if any(key.endswith(suffix) for suffix in suffixes)
        }
        ledger_keys = {
            delivery["key"]
            for setting in settings.values()
            for delivery in [setting.get("delivery")]
            if isinstance(delivery, dict)
            and delivery.get("kind") == "runtimeConfig"
            and isinstance(delivery.get("key"), str)
            and any(delivery["key"].endswith(suffix) for suffix in suffixes)
        }
        candidates = ledger_keys & source_keys
        if not candidates and len(source_keys) == 1:
            candidates = source_keys
        if len(candidates) != 1:
            continue
        composite_key = next(iter(candidates))
        uri_fact = source_dependency.get("runtimeUri")
        declared_scheme = (
            uri_fact.get("scheme") if isinstance(uri_fact, dict) else None
        )
        if declared_scheme in allowed_schemes:
            source_schemes = {declared_scheme}
        else:
            source_schemes = {
                scheme
                for scheme in re.findall(
                    r"\b([A-Za-z][A-Za-z0-9+.-]*)://",
                    source_text,
                )
                if scheme in allowed_schemes
            }
        if len(source_schemes) != 1:
            continue
        scheme = next(iter(source_schemes))
        if scheme not in allowed_schemes:
            continue

        arguments: list[str] = []
        secret_key = None
        component_keys: dict[str, str] = {}
        binding = protocol.get("binding") or {}
        for component in components:
            if component == "port":
                port = binding.get("portLiteral", binding.get("port"))
                if port is None:
                    arguments = []
                    break
                arguments.append(shlex.quote(str(port)))
                continue
            setting = settings.get(component)
            delivery = setting.get("delivery") if isinstance(setting, dict) else None
            key = delivery.get("key") if isinstance(delivery, dict) else None
            if not isinstance(key, str) or key not in environment_keys:
                arguments = []
                break
            if component == "password":
                if delivery.get("kind") != "secretKeyRef" or key not in secret_keys:
                    arguments = []
                    break
                secret_key = key
            component_keys[component] = key
            arguments.append(f'"${key}"')
        if len(arguments) != len(components) or secret_key is None:
            continue

        existing = candidate_runtime_command(source, secret_key)
        expands_secret = bool(
            existing
            and re.search(
                rf"\$(?:\{{{re.escape(secret_key)}\}}|"
                rf"{re.escape(secret_key)}\b)",
                existing,
            )
        )
        base_command = (
            f"exec {process}"
            if expands_secret or not existing or process not in existing
            else existing
        )
        if encoder_kind == "python":
            python_template = template.replace("<scheme>", scheme)
            for index, component in enumerate(components):
                python_template = python_template.replace(
                    f"<{component}>",
                    "{" + str(index) + "}",
                )
            encoded_indexes = tuple(
                index
                for index, component in enumerate(components)
                if component in encoded_components
            )
            python_code = (
                "import sys; from urllib.parse import quote; "
                "values=sys.argv[1:]; "
                f"encoded=[quote(value, safe=\"\") if index in "
                f"{encoded_indexes!r} else value "
                "for index, value in enumerate(values)]; "
                f"print({python_template!r}.format(*encoded))"
            )
            command = (
                f'export {composite_key}="$({encoder_binary} -c '
                f"{shlex.quote(python_code)} {' '.join(arguments)})\"; "
                f"{base_command}"
            )
        elif encoder_kind == "curl":
            uri_expression = template.replace("<scheme>", scheme)
            encoded_assignments: list[str] = []
            for component in components:
                if component == "port":
                    value = str(
                        binding.get("portLiteral", binding.get("port"))
                    )
                else:
                    key = component_keys[component]
                    if component in encoded_components:
                        encoded_key = f"uri_{component}"
                        encoded_assignments.append(
                            f'{encoded_key}=$(uri_encode "${key}") '
                            "|| exit $?"
                        )
                        value = f"${encoded_key}"
                    else:
                        value = f"${key}"
                uri_expression = uri_expression.replace(
                    f"<{component}>",
                    value,
                )
            command = (
                "uri_encode() { "
                "encoded=$(printf '%s' \"$1\" | curl -Gs "
                "--unix-socket /dev/null -o /dev/null "
                "-w '%{url_effective}' --data-urlencode 'x@-' "
                "http://localhost/ || true); "
                "case \"$encoded\" in "
                "'http://localhost/?x='*) ;; *) return 1 ;; esac; "
                r"encoded=${encoded#*\?x=}; "
                'while [ "${encoded#*+}" != "$encoded" ]; do '
                'encoded="${encoded%%+*}%20${encoded#*+}"; done; '
                "printf '%s' \"$encoded\"; }; "
                f"{'; '.join(encoded_assignments)}; "
                f'export {composite_key}="{uri_expression}"; '
                f"{base_command}"
            )
        else:
            continue
        rewritten = insert_runtime_command(
            source,
            composite_key=composite_key,
            secret_key=secret_key,
            command=command,
        )
        if rewritten is None:
            continue
        source = rewritten
        for requirement in protocol.get("requiredClientSettings", []):
            name, separator, value = str(requirement).partition("=")
            if not separator or name in components:
                continue
            setting = settings.get(name)
            if not isinstance(setting, dict):
                continue
            setting["delivery"] = {
                "kind": "runtimeConfig",
                "key": composite_key,
                "value": requirement,
            }
        changes.append(
            {
                "resourceSymbol": str(symbol),
                "setting": composite_key,
                "format": template,
                "encoder": encoder_binary,
                "encoderSource": dockerfile,
                "componentKeys": component_keys,
            }
        )
    if changes:
        app_path.write_text(source)
        write_json(requirements_path, requirements)
    return changes


def reconcile_exported_runtime_settings(
    candidate: Path,
    authoring_contract: dict[str, Any],
) -> list[dict[str, Any]]:
    requirements_path = candidate / "requirements.json"
    source_path = candidate / "app.bicep"
    requirements = json.loads(requirements_path.read_text())
    source = source_path.read_text()
    resource_types = {
        symbol: resource_type.split("@", 1)[0]
        for symbol, resource_type in re.findall(
            r"\bresource\s+([A-Za-z_][A-Za-z0-9_]*)\s+'([^']+)'",
            source,
        )
    }
    changes: list[dict[str, Any]] = []
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
        groups: dict[str, list[dict[str, Any]]] = {}
        for setting in dependency.get("settings", []):
            if not isinstance(setting, dict):
                continue
            delivery = setting.get("delivery")
            if (
                isinstance(delivery, dict)
                and delivery.get("kind") == "env"
                and isinstance(delivery.get("key"), str)
            ):
                groups.setdefault(delivery["key"], []).append(setting)
        for key, settings in groups.items():
            if len(settings) < 2:
                continue
            export = re.search(
                rf"(?m)^.*\bexport\s+{re.escape(key)}\s*=.*$",
                source,
            )
            if not export:
                continue
            line = export.group(0)
            port_setting = next(
                (
                    setting
                    for setting in settings
                    if str(setting.get("name", "")).partition("=")[0] == "port"
                ),
                None,
            )
            binding = protocol.get("binding") or {}
            port = binding.get("portLiteral", binding.get("port"))
            port_inserted = False
            if port_setting is not None and port is not None and str(port) not in line:
                rewritten, count = re.subn(
                    r"(@[^/:\\'\"\s?;]+)(/)",
                    rf"\1:{port}\2",
                    line,
                    count=1,
                )
                if count != 1:
                    continue
                source = source[: export.start()] + rewritten + source[export.end() :]
                line = rewritten
                port_inserted = True
            if port_setting is not None and port is not None and str(port) not in line:
                continue
            for setting in settings:
                delivery = setting["delivery"]
                delivery["kind"] = "runtimeConfig"
                changes.append(
                    {
                        "resourceSymbol": symbol,
                        "setting": setting.get("name"),
                        "key": key,
                        "portInserted": (
                            port_inserted
                            and setting is port_setting
                        ),
                    }
                )
    if changes:
        source_path.write_text(source)
        write_json(requirements_path, requirements)
    return changes


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
    parser.add_argument("--deadline-seconds", type=float, default=360)
    parser.add_argument("--evidence-timeout", type=float, default=70)
    parser.add_argument("--author-timeout", type=float, default=100)
    parser.add_argument("--review-timeout", type=float, default=45)
    parser.add_argument("--repair-timeout", type=float, default=60)
    parser.add_argument("--final-review-timeout", type=float, default=30)
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
        "phaseBudgets": {
            "evidence": args.evidence_timeout,
            "author": args.author_timeout,
            "review": args.review_timeout,
            "repair": args.repair_timeout,
            "finalReview": args.final_review_timeout,
        },
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
            evidence, evidence_errors, source_startup_changes = prepare_evidence(
                parse_json_object(evidence_invocation["finalText"]),
                source_root=repository_root,
                source_path=source_path,
            )
        except ValueError as exc:
            evidence = {}
            evidence_errors = [str(exc)]
            source_startup_changes = []
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
                    "containing workloads, startupFiles, dependencies, route, "
                    "and persistentPaths. Preserve all already closed citations."
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
                evidence, evidence_errors, source_startup_changes = (
                    prepare_evidence(
                        parse_json_object(evidence_retry["finalText"]),
                        source_root=repository_root,
                        source_path=source_path,
                    )
                )
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
                    evidence, evidence_errors, source_startup_changes = (
                        prepare_evidence(
                            parse_json_object(
                                evidence_format_retry["finalText"]
                            ),
                            source_root=repository_root,
                            source_path=source_path,
                        )
                    )
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
        if source_startup_changes:
            status["sourceStartupChanges"] = source_startup_changes
        startup_file_changes = reconcile_startup_file_delivery(
            evidence,
            source_root=repository_root,
            source_path=source_path,
        )
        startup_path_changes = reconcile_operator_materialized_paths(evidence)
        if startup_file_changes:
            status["startupFileChanges"] = startup_file_changes
        if startup_path_changes:
            status["startupPathChanges"] = startup_path_changes
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
                "connectionShapeChanges": [],
                "runtimeCompositeChanges": [],
                "runtimeUriChanges": [],
                "startupFileChanges": [],
                "exportedRuntimeChanges": [],
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
            reconciliation["connectionShapeChanges"] = (
                reconcile_nested_connections(candidate)
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
            reconciliation["runtimeUriChanges"] = reconcile_runtime_uris(
                candidate,
                authoring_contract,
                evidence,
                source_root=repository_root,
                source_path=source_path,
            )
            reconciliation["startupFileChanges"] = (
                reconcile_operator_startup_files(candidate, evidence)
            )
            reconciliation["exportedRuntimeChanges"] = (
                reconcile_exported_runtime_settings(
                    candidate,
                    authoring_contract,
                )
            )
            reconciliation["secretCompositeEnvChanges"] = (
                reconcile_stale_secret_composite_env(candidate)
            )
        write_json(run_dir / "reconciliation-1.json", reconciliation)
        status["reconciliation"] = reconciliation
        validation = (
            {"valid": False, "errors": handoff_errors}
            if handoff_errors
            else validate_all(
                candidate,
                run_dir,
                evidence,
                remote=remote,
                commit=commit,
                source_root=repository_root,
                source_path=source_path,
                timeout=remaining(deadline),
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
            reconciliation["connectionShapeChanges"] = (
                reconcile_nested_connections(candidate)
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
            reconciliation["runtimeUriChanges"] = reconcile_runtime_uris(
                candidate,
                authoring_contract,
                evidence,
                source_root=repository_root,
                source_path=source_path,
            )
            reconciliation["startupFileChanges"] = (
                reconcile_operator_startup_files(candidate, evidence)
            )
            reconciliation["exportedRuntimeChanges"] = (
                reconcile_exported_runtime_settings(
                    candidate,
                    authoring_contract,
                )
            )
            reconciliation["secretCompositeEnvChanges"] = (
                reconcile_stale_secret_composite_env(candidate)
            )
            write_json(run_dir / "reconciliation-2.json", reconciliation)
            status["reconciliation"] = reconciliation
            validation = validate_all(
                candidate,
                run_dir,
                evidence,
                remote=remote,
                commit=commit,
                source_root=repository_root,
                source_path=source_path,
                timeout=remaining(deadline),
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
