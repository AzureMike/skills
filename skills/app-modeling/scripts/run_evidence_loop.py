#!/usr/bin/env python3

"""Run evidence -> author -> validation -> retained-reviewer loop."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
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
    if not isinstance(dependencies, list):
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
    if isinstance(persistent_paths, list) and persistent_paths:
        selected.add("Radius.Compute/persistentVolumes")
    route = facts.get("route")
    if isinstance(route, dict) and route.get("required") is True:
        selected.add("Radius.Compute/routes")
    workloads = facts.get("workloads")
    if isinstance(workloads, dict):
        workloads = [workloads]
    if not isinstance(workloads, list):
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
                "literal rendered in container command, args, or native config"
            ),
            "secretKeyRef": "secret environment input",
            "sourceDefault": "only an unmodified source default",
        },
        "bundles": bundles,
    }


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


def normalize_evidence(value: dict[str, Any]) -> dict[str, Any]:
    if str(value.get("status", "")).startswith("source_facts_complete"):
        value["status"] = "complete"
    if (
        value.get("status") in {"ready", "complete", "completed"}
        and not isinstance(value.get("blockers"), list)
    ):
        value["blockers"] = []
    facts = value.get("facts")
    if isinstance(facts, dict):
        dependencies = facts.get("dependencies")
        if isinstance(dependencies, list):
            for dependency in dependencies:
                if not isinstance(dependency, dict) or dependency.get("kind"):
                    continue
                candidate = str(dependency.get("type", "")).strip().lower()
                if candidate in DEPENDENCY_AUTHORING_TYPES:
                    dependency["kind"] = candidate
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
    if not isinstance(value.get("blockers"), list):
        errors.append("evidence blockers must be an array")
    return errors


def reconcile_requirements(
    candidate: Path, authoring_contract: dict[str, Any]
) -> dict[str, Any]:
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
        required_settings = [
            setting.partition("=")
            for setting in (
                protocol.get("requiredClientSettings", [])
                + protocol.get("runtimeRequiredClientSettings", [])
            )
        ]
        for setting in dependency.get("settings", []):
            if not isinstance(setting, dict):
                continue
            name = setting.get("name")
            if not isinstance(name, str) or "*" not in name:
                continue
            pattern = "^" + re.sub(r"(?:\\\*)+", ".+", re.escape(name)) + "$"
            matches = []
            delivery_kind = (setting.get("delivery") or {}).get("kind")
            for required_name, _, required_value in required_settings:
                if not re.fullmatch(pattern, required_name):
                    continue
                is_secret = required_value.startswith("managedSecret:")
                if delivery_kind == "secretKeyRef" and not is_secret:
                    continue
                if delivery_kind != "secretKeyRef" and is_secret:
                    continue
                matches.append(required_name)
            if len(matches) == 1:
                setting["name"] = matches[0]
                changes.append(
                    {
                        "resourceSymbol": resource_symbol,
                        "from": name,
                        "to": matches[0],
                    }
                )
    if changes:
        write_json(requirements_path, requirements)
    return {"changes": changes}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", default=".")
    parser.add_argument("--request", required=True)
    parser.add_argument("--deadline-seconds", type=float, default=430)
    parser.add_argument("--evidence-timeout", type=float, default=195)
    parser.add_argument("--author-timeout", type=float, default=135)
    parser.add_argument("--review-timeout", type=float, default=45)
    parser.add_argument("--repair-timeout", type=float, default=40)
    parser.add_argument("--final-review-timeout", type=float, default=45)
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
    if args.artifact_dir:
        run_dir = Path(args.artifact_dir).resolve()
        run_dir.mkdir(parents=True, exist_ok=True)
    else:
        run_dir = Path(tempfile.mkdtemp(prefix="app-modeling-")).resolve()
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
        "request": args.request,
    }
    common = f"""
User acceptance request: {args.request}
Source repository: {target}
Git repository root: {repository_root}
Application path within remote: {source_path}
Remote: {remote}
Immutable source revision: {commit}
Skill directory: {SKILL_DIR}
Contract query: {SKILL_DIR / 'scripts' / 'contract_query.py'}
Verified contract: {SKILL_DIR / 'assets' / 'radius-contract.json'}
Candidate directory: {candidate}
Expected/golden application definitions are unavailable.
"""
    try:
        installed = install_agents(target)
        evidence_invocation = invoke(
            target=target,
            run_dir=run_dir,
            label="reviewer-evidence",
            agent="radius-model-reviewer",
            session_id=reviewer_session,
            prompt=(
                "Independently derive source facts, select the profile required by "
                "the user request, then resolve only that profile's exact Radius "
                "contracts. Return compact JSON with status, facts, and blockers. "
                "Do not write files.\n" + common
            ),
            timeout=min(args.evidence_timeout, remaining(deadline)),
            effort="low",
        )
        status["evidence"] = public_invocation(evidence_invocation)
        if evidence_invocation["processExit"] != 0:
            status["reason"] = "evidence reviewer failed"
            return 1
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
        if evidence_errors:
            evidence_retry = invoke(
                target=target,
                run_dir=run_dir,
                label="reviewer-evidence-retry",
                agent="radius-model-reviewer",
                session_id=reviewer_session,
                prompt=(
                    "Your evidence JSON was malformed or incomplete: "
                    + "; ".join(evidence_errors)
                    + ". Do not use tools. Restate the completed evidence as one "
                    "compact valid JSON object with status, facts, and blockers."
                ),
                timeout=min(35, remaining(deadline)),
                resume=True,
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
            if evidence_errors:
                status["reason"] = "; ".join(evidence_errors)
                return 1
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
        writer_events = (
            run_dir / "agents" / "writer-initial" / "events.jsonl"
        )
        candidate_files = list(candidate.iterdir())
        if (
            author_invocation["processExit"] != 0
            and writer_events.is_file()
            and writer_events.stat().st_size == 0
            and not candidate_files
            and remaining(deadline) > 30
        ):
            writer_session = str(uuid.uuid4())
            author_invocation = invoke(
                target=target,
                run_dir=run_dir,
                label="writer-infra-retry",
                agent="radius-model-writer",
                session_id=writer_session,
                prompt=(
                    "The prior writer process produced no events or files. Author "
                    "the four files directly under the Candidate directory from "
                    f"{run_dir / 'reviewer-evidence.json'}, "
                    f"{run_dir / 'authoring-contract.json'}, and "
                    f"{SKILL_DIR / 'schemas' / 'requirements.schema.json'}. "
                    "Return immediately after writing.\n" + common
                ),
                timeout=min(args.author_timeout, remaining(deadline)),
                effort="low",
            )
            status["authorRetry"] = public_invocation(author_invocation)
        if author_invocation["processExit"] != 0:
            status["reason"] = "author failed"
            return 1
        handoff_errors = validate_handoff(candidate)
        shutil.copytree(candidate, run_dir / "candidate-initial")
        reconciliation = (
            {"changes": []}
            if handoff_errors
            else reconcile_requirements(candidate, authoring_contract)
        )
        write_json(run_dir / "reconciliation-1.json", reconciliation)
        validation = (
            {"valid": False, "errors": handoff_errors}
            if handoff_errors
            else validate_candidate(
                candidate,
                run_dir,
                source_remote=remote,
                source_commit=commit,
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
        try:
            review = normalize_review(parse_json_object(review_invocation["finalText"]))
            review_errors = validate_review(review)
        except ValueError as exc:
            review = {"verdict": "needs_more_info", "summary": str(exc), "findings": []}
            review_errors = [str(exc)]
        if review_errors:
            review = {
                "verdict": "needs_more_info",
                "summary": "; ".join(review_errors),
                "findings": [],
            }
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
            try:
                review = normalize_review(
                    parse_json_object(retry_invocation["finalText"])
                )
                review_errors = validate_review(review)
            except ValueError as exc:
                review = {
                    "verdict": "needs_more_info",
                    "summary": str(exc),
                    "findings": [],
                }
                review_errors = [str(exc)]
            if review_errors:
                review["verdict"] = "needs_more_info"
                review["summary"] = "; ".join(review_errors)
            write_json(run_dir / "review-retry.json", review)

        if not validation.get("valid") or review.get("verdict") == "rejected":
            repair_invocation = invoke(
                target=target,
                run_dir=run_dir,
                label="writer-repair",
                agent="radius-model-writer",
                session_id=writer_session,
                prompt=f"""
Repair every item in {run_dir / 'validation-1.json'} and
{run_dir / 'review-1.json'} once. Preserve all cited source behavior. Reconcile
security, composite values, persistence, process semantics, and graph impact
together. Do not read validator source or rescan the repository.
""",
                timeout=min(args.repair_timeout, remaining(deadline)),
                resume=True,
                effort="low",
            )
            status["repair"] = public_invocation(repair_invocation)
            if repair_invocation["processExit"] != 0:
                status["reason"] = "repair failed"
                return 1
            shutil.copytree(candidate, run_dir / "candidate-repaired")
            reconciliation = reconcile_requirements(candidate, authoring_contract)
            write_json(run_dir / "reconciliation-2.json", reconciliation)
            validation = validate_candidate(
                candidate,
                run_dir,
                source_remote=remote,
                source_commit=commit,
                source_path=source_path,
            )
            write_json(run_dir / "validation-2.json", validation)
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
                resume=True,
                effort="low",
            )
            status["finalReview"] = public_invocation(final_invocation)
            try:
                review = normalize_review(parse_json_object(final_invocation["finalText"]))
                review_errors = validate_review(review)
            except ValueError as exc:
                review = {
                    "verdict": "needs_more_info",
                    "summary": str(exc),
                    "findings": [],
                }
                review_errors = [str(exc)]
            if review_errors:
                review["verdict"] = "needs_more_info"
                review["summary"] = "; ".join(review_errors)
            write_json(run_dir / "review-2.json", review)

        if remaining(deadline) <= 0:
            status["reason"] = "internal deadline exceeded"
            return 1
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
        print(json.dumps(status, sort_keys=True))


if __name__ == "__main__":
    raise SystemExit(main())
