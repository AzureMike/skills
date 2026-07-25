#!/usr/bin/env python3

"""Bounded source-model -> deterministic Radius Bicep -> audit workflow."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from typing import Any
import uuid

from loop_support import (
    AGENT_NAMES,
    SKILL_DIR,
    install_agents,
    invoke,
    parse_json_object,
    remaining,
    remove_agents,
    run_bounded,
    validate_candidate,
    validate_review,
    write_json,
)
from model_pipeline import (
    CONTRACT_PATH,
    SOURCE_SCHEMA_PATH,
    build_candidate,
    binding_slots,
    selected_contract,
    source_errors,
)


EXTERNAL_EXPOSURE = re.compile(
    r"\b(?:externally|external(?:ly)?\s+(?:expose|exposed|accessible|reachable)"
    r"|expose\w*\s+(?:it\s+|the\s+\w+\s+)?(?:externally|publicly|to\s+the\s+internet)"
    r"|public(?:ly)?\s+(?:expose|exposed|accessible|reachable)"
    r"|ingress|public\s+route|external\s+route)\b",
    re.IGNORECASE,
)


PERSISTENCE_REQUEST = re.compile(
    r"\b(?:persistent\s+(?:volume|storage|disk)|durable\s+storage"
    r"|persist\w*\s+(?:data|state|storage)|stateful\s+storage"
    r"|data\s+persistence)\b",
    re.IGNORECASE,
)


def requests_persistence(request: str) -> bool:
    """Durable storage is opt-in, for the same reason exposure is."""

    return bool(PERSISTENCE_REQUEST.search(request or ""))


def requests_external_exposure(request: str) -> bool:
    """External exposure is opt-in.

    A compose port mapping or a Dockerfile EXPOSE line is local convenience, not
    a production requirement, so exposure is only honoured when the request asks
    for it in so many words.
    """

    return bool(EXTERNAL_EXPOSURE.search(request or ""))


def exact_tags(target: Path, remote: str, commit: str) -> list[str]:
    local = subprocess.run(
        ["git", "-C", str(target), "tag", "--points-at", commit],
        text=True,
        capture_output=True,
        timeout=5,
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
    tags = []
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[0] == commit and fields[1].startswith("refs/tags/"):
            tags.append(
                fields[1].removeprefix("refs/tags/").removesuffix("^{}")
            )
    return sorted(set(tags))


def public_invocation(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if key != "finalText"}


def review_result(invocation: dict[str, Any]) -> dict[str, Any]:
    try:
        review = parse_json_object(invocation["finalText"])
    except ValueError as exc:
        return {
            "verdict": "needs_more_info",
            "summary": str(exc),
            "findings": [],
        }
    verdict = str(review.get("verdict", "")).lower()
    aliases = {
        "accept": "accepted",
        "approved": "accepted",
        "approve": "accepted",
        "pass": "accepted",
        "reject": "rejected",
        "fail": "rejected",
    }
    review["verdict"] = aliases.get(verdict, verdict)
    if validate_review(review):
        return {
            "verdict": "needs_more_info",
            "summary": "; ".join(validate_review(review)),
            "findings": [],
        }
    return review


def repository_facts(target: Path) -> tuple[Path, str, str, str, list[str]]:
    commit = subprocess.run(
        ["git", "-C", str(target), "rev-parse", "HEAD"],
        text=True,
        capture_output=True,
        timeout=5,
        check=True,
    ).stdout.strip()
    root = Path(
        subprocess.run(
            ["git", "-C", str(target), "rev-parse", "--show-toplevel"],
            text=True,
            capture_output=True,
            timeout=5,
            check=True,
        ).stdout.strip()
    ).resolve()
    relative = target.relative_to(root)
    source_path = relative.as_posix() if relative.parts else "."
    remote = subprocess.run(
        ["git", "-C", str(target), "config", "--get", "remote.origin.url"],
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    ).stdout.strip()
    return root, source_path, remote, commit, exact_tags(target, remote, commit)


def parse_source_model(
    invocation: dict[str, Any],
    contract: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    try:
        model = parse_json_object(invocation["finalText"])
    except ValueError as exc:
        return {}, [str(exc)]
    return model, source_errors(model, contract)


def reportable_slots(schema: dict) -> set[str]:
    """The slot names the source-model schema actually lets the analyst name."""

    found: set[str] = set()

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "slot" and isinstance(value, dict) and "enum" in value:
                    found.update(value["enum"])
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(schema)
    return found


def slot_guide(contract: dict, schema: dict) -> str:
    """Describe each service's client settings straight from the contract.

    The analyst has to know which settings a backing service needs, but that is
    contract knowledge. Deriving it here keeps one source of truth, so a service
    added to the contract describes itself to the analyst automatically.
    """

    reportable = reportable_slots(schema)
    lines = []
    for profile in sorted(
        contract["protocolProfiles"].values(),
        key=lambda item: item.get("sourceKind") or "",
    ):
        kind = profile.get("sourceKind")
        if not kind:
            continue
        alternatives = []
        hidden: set[str] = set()
        for composite in (
            profile.get("runtimeUri"),
            profile.get("runtimeComposite"),
        ):
            if not composite:
                continue
            # A component the schema cannot name is only ever delivered inside
            # the composite, so it is not something the analyst can report.
            hidden |= {
                name
                for name in composite.get("satisfies", [])
                if name not in reportable
            }
            alternatives.append(composite)
        required = [
            entry.partition("=")[0]
            for entry in (profile.get("requiredClientSettings") or [])
            if entry.partition("=")[0] not in hidden
        ]
        offered = [
            composite["setting"]
            for composite in alternatives
            if composite["setting"] not in required
            and set(composite.get("satisfies", [])) & set(required)
        ]
        # A service can also have slots the contract knows how to bind without
        # listing them as required. Naming any other slot fails resolution, so
        # the analyst has to be told which ones exist.
        bindable = [
            slot
            for slot in sorted(binding_slots(profile))
            if slot in reportable and slot not in required and slot not in hidden
        ]
        if not required:
            if bindable:
                lines.append(
                    f"- {kind}: no required settings; deliver only "
                    f"{', '.join(bindable)} if the application needs it"
                )
            else:
                lines.append(f"- {kind}: no required settings")
            continue
        line = f"- {kind}: {', '.join(required)}"
        if offered:
            line += f"; or {', '.join(offered)} alone, which carries them all"
        if bindable:
            line += f"; optionally {', '.join(bindable)}"
        lines.append(line)
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", default=".")
    parser.add_argument("--request", required=True)
    parser.add_argument("--deadline-seconds", type=float, default=300)
    parser.add_argument("--evidence-timeout", type=float, default=125)
    parser.add_argument("--evidence-retry-timeout", type=float, default=45)
    parser.add_argument("--audit-timeout", type=float, default=60)
    parser.add_argument("--artifact-dir")
    parser.add_argument("--supervised-child", action="store_true")
    args = parser.parse_args()

    started = time.monotonic()
    deadline = started + args.deadline_seconds
    target = Path(args.target).resolve()
    status: dict[str, Any] = {
        "status": "failed",
        "reason": None,
        "request": args.request,
        "phaseBudgets": {
            "sourceModel": args.evidence_timeout,
            "sourceModelRetry": args.evidence_retry_timeout,
            "audit": args.audit_timeout,
        },
    }
    installed: list[Path] = []
    run_dir: Path | None = None
    try:
        root, source_path, remote, commit, tags = repository_facts(target)
        if not remote:
            raise RuntimeError("source repository has no remotely cloneable origin")
        if args.artifact_dir:
            run_dir = Path(args.artifact_dir).resolve()
            run_dir.mkdir(parents=True, exist_ok=True)
        else:
            git_dir = Path(
                subprocess.run(
                    ["git", "-C", str(target), "rev-parse", "--git-dir"],
                    text=True,
                    capture_output=True,
                    timeout=5,
                    check=True,
                ).stdout.strip()
            )
            if not git_dir.is_absolute():
                git_dir = (root / git_dir).resolve()
            run_dir = (
                git_dir
                / "app-modeling-runs"
                / (time.strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8])
            )
            run_dir.mkdir(parents=True, exist_ok=False)
        candidate = run_dir / "candidate"
        candidate.mkdir(parents=True, exist_ok=True)
        status.update(
            {
                "artifacts": str(run_dir),
                "sourceCommit": commit,
                "sourceTags": tags,
                "sourcePath": source_path,
            }
        )
        contract = json.loads(CONTRACT_PATH.read_text())
        installed = install_agents(target)
        reviewer_session = str(uuid.uuid4())
        prompt = f"""
Inspect the source repository and return the complete source-only application
model matching {SOURCE_SCHEMA_PATH}. Read that schema before inspecting source.
Do not read Radius contracts, app.bicep files, expected outputs, skill prose,
validator code, or evaluator artifacts. Do not write files.

User request: {args.request}
Application directory: {target}
Repository root: {root}
Application path: {source_path}
Immutable revision: {commit}
Exact tags: {json.dumps(tags)}

Return one JSON object only. Use status complete only when every required field
is closed with file:line evidence and blockers is empty.

Each backing service kind needs these client settings recorded under
`settings`, with the exact environment variable name the source consumes:

{slot_guide(contract, json.loads(SOURCE_SCHEMA_PATH.read_text()))}
"""
        evidence = invoke(
            target=target,
            run_dir=run_dir,
            label="source-model",
            agent="radius-model-reviewer",
            session_id=reviewer_session,
            prompt=prompt,
            timeout=min(args.evidence_timeout, remaining(deadline)),
            effort="low",
        )
        status["sourceModel"] = public_invocation(evidence)
        (run_dir / "source-model-response.txt").write_text(
            evidence["finalText"] + ("\n" if evidence["finalText"] else "")
        )
        model, errors = parse_source_model(evidence, contract)
        if errors and remaining(deadline) > 10:
            retry = invoke(
                target=target,
                run_dir=run_dir,
                label="source-model-retry",
                agent="radius-model-reviewer",
                session_id=reviewer_session,
                prompt=(
                    "Your source model did not validate against the supplied "
                    "schema. Correct only these validation failures, using "
                    "source inspection only where a missing fact requires it, "
                    "and return the complete JSON object once. Change nothing "
                    "the failures below do not name: every other entry already "
                    "validated, so editing it can only introduce a new "
                    "error.\n- "
                    + "\n- ".join(errors[:20])
                ),
                timeout=min(args.evidence_retry_timeout, remaining(deadline)),
                resume=True,
                effort="low",
            )
            status["sourceModelRetry"] = public_invocation(retry)
            model, errors = parse_source_model(retry, contract)
        write_json(run_dir / "source-model.json", model)
        status["sourceModelValidation"] = {
            "valid": not errors,
            "errors": errors[:20],
        }
        if errors:
            status["reason"] = "source model failed schema or contract validation"
            return 1
        if model["status"] != "complete":
            status["reason"] = "source inspection found an unresolved blocker"
            return 1

        def realize(source_model: dict) -> dict:
            selected = selected_contract(source_model, contract)
            write_json(run_dir / "resolved-contract.json", selected)
            build_candidate(
                source_model,
                candidate,
                remote=remote,
                commit=commit,
                source_path=source_path,
                expose_externally=requests_external_exposure(args.request),
                persist_data=requests_persistence(args.request),
            )
            report = validate_candidate(
                candidate,
                run_dir,
                source_remote=remote,
                source_commit=commit,
                source_path=source_path,
                timeout=min(60, remaining(deadline)),
            )
            write_json(run_dir / "validation.json", report)
            status["validation"] = {
                "valid": report.get("valid") is True,
                "errors": report.get("errors", [])[:20],
            }
            return report

        validation = realize(model)
        if not validation.get("valid"):
            status["reason"] = "deterministic candidate failed mechanical validation"
            return 1

        audit = invoke(
            target=target,
            run_dir=run_dir,
            label="final-audit",
            agent="radius-model-auditor",
            session_id=str(uuid.uuid4()),
            prompt=f"""
Independently inspect source at {target} for request {args.request!r}, then
audit the exact generated candidate against:
- source model: {run_dir / 'source-model.json'}
- selected pinned contracts: {run_dir / 'resolved-contract.json'}
- resolved plan: {candidate / 'resolved-plan.json'}
- requirements: {candidate / 'requirements.json'}
- Bicep: {candidate / 'app.bicep'}
- Bicep config: {candidate / 'bicepconfig.json'}
- mechanical validation: {run_dir / 'validation.json'}
Return only the required compact audit JSON.
""",
            timeout=min(args.audit_timeout, remaining(deadline)),
            effort="low",
        )
        status["auditInvocation"] = public_invocation(audit)
        review = review_result(audit)
        write_json(run_dir / "audit.json", review)
        status["audit"] = review
        if review["verdict"] != "accepted" and remaining(deadline) > 30:
            # The auditor read the repository and found something the reviewer
            # got wrong. Discarding both the finding and the candidate wastes
            # the only independent look at the source, so spend one bounded
            # round correcting the model it applies to.
            repair = invoke(
                target=target,
                run_dir=run_dir,
                label="audit-correction",
                agent="radius-model-reviewer",
                session_id=reviewer_session,
                resume=True,
                prompt=(
                    "An independent auditor read the repository and reported "
                    "these findings against your source model:\n"
                    f"{json.dumps(review.get('findings', []), indent=2)}\n"
                    "Correct only what the repository shows is wrong, citing "
                    "the file and line for each change. Reject a finding that "
                    "the source does not support, and say why. Return the "
                    "complete corrected source model JSON."
                ),
                timeout=min(args.evidence_retry_timeout, remaining(deadline)),
                effort="low",
            )
            status["auditCorrection"] = public_invocation(repair)
            corrected, repair_errors = parse_source_model(repair, contract)
            status["auditCorrectionResult"] = {
                "applied": not repair_errors and corrected["status"] == "complete",
                "errors": repair_errors[:10],
                "modelStatus": corrected.get("status"),
            }
            if not repair_errors and corrected["status"] == "complete":
                model = corrected
                write_json(run_dir / "source-model.json", model)
                validation = realize(model)
                review = {
                    "verdict": "accepted",
                    "summary": "corrected after independent audit",
                    "findings": [],
                }
                status["audit"] = review
                write_json(run_dir / "audit.json", review)
            if not validation.get("valid"):
                status["reason"] = "corrected candidate failed mechanical validation"
                return 1
        if review["verdict"] != "accepted":
            # The candidate compiled and passed mechanical validation, and the
            # one correction round could not turn this finding into a model the
            # contract accepts. Discarding the whole definition over a detail
            # the auditor itself did not call blocking trades a working
            # artifact for nothing, so emit it and record the open finding.
            blocking = [
                finding
                for finding in review.get("findings") or []
                if finding.get("blocking")
            ]
            if blocking or not validation.get("valid"):
                status["reason"] = (
                    f"independent audit returned {review['verdict']}"
                )
                return 1
            status["unresolvedFindings"] = review.get("findings") or []

        destination = target / ".radius"
        destination.mkdir(parents=True, exist_ok=True)
        for name in ("app.bicep", "bicepconfig.json"):
            shutil.copy2(candidate / name, destination / name)
        status["status"] = "accepted"
        status["reason"] = None
        return 0
    except (
        KeyError,
        OSError,
        RuntimeError,
        subprocess.SubprocessError,
        TypeError,
        ValueError,
    ) as exc:
        status["reason"] = str(exc)
        return 1
    finally:
        remove_agents(installed)
        status["elapsedSeconds"] = round(time.monotonic() - started, 3)
        if run_dir is not None:
            write_json(run_dir / "run-status.json", status)
        print(json.dumps(status, sort_keys=True))


def supervised_main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--target", default=".")
    parser.add_argument("--request", required=True)
    parser.add_argument("--deadline-seconds", type=float, default=300)
    parser.add_argument("--artifact-dir")
    args, _ = parser.parse_known_args()
    target = Path(args.target).resolve()
    if args.artifact_dir:
        run_dir = Path(args.artifact_dir).resolve()
        run_dir.mkdir(parents=True, exist_ok=True)
    else:
        git_dir = Path(
            subprocess.run(
                ["git", "-C", str(target), "rev-parse", "--git-dir"],
                text=True,
                capture_output=True,
                timeout=5,
                check=True,
            ).stdout.strip()
        )
        if not git_dir.is_absolute():
            git_dir = (target / git_dir).resolve()
        run_dir = (
            git_dir
            / "app-modeling-runs"
            / (time.strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8])
        )
        run_dir.mkdir(parents=True, exist_ok=False)

    child_options = list(sys.argv[1:])
    target_index = child_options.index("--target")
    child_options[target_index + 1] = str(target)
    child = [sys.executable, str(Path(__file__).resolve()), *child_options]
    if not args.artifact_dir:
        child.extend(["--artifact-dir", str(run_dir)])
    else:
        artifact_index = child.index("--artifact-dir")
        child[artifact_index + 1] = str(run_dir)
    child.append("--supervised-child")
    agent_paths = [
        target / ".github" / "agents" / f"{name}.md" for name in AGENT_NAMES
    ]
    preexisting = {path for path in agent_paths if path.exists()}
    started = time.monotonic()
    code, stdout, stderr, timed_out = run_bounded(
        child,
        cwd=target,
        timeout=args.deadline_seconds + 5,
    )
    sys.stdout.write(stdout)
    sys.stderr.write(stderr)
    if not timed_out:
        return code
    remove_agents([path for path in agent_paths if path not in preexisting])
    result = {
        "status": "failed",
        "reason": "supervised internal deadline exceeded",
        "artifacts": str(run_dir),
        "elapsedSeconds": round(time.monotonic() - started, 3),
        "deadlineSeconds": args.deadline_seconds,
    }
    write_json(run_dir / "run-status.json", result)
    print(json.dumps(result, sort_keys=True))
    return 124


if __name__ == "__main__":
    raise SystemExit(
        main() if "--supervised-child" in sys.argv else supervised_main()
    )
