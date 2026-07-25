#!/usr/bin/env python3

"""Shared mechanics for the bounded app-modeling loop."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import signal
import shutil
import subprocess
import sys
import time
from typing import Any


SKILL_DIR = Path(__file__).resolve().parent.parent
AGENT_NAMES = (
    "radius-model-reviewer",
    "radius-model-auditor",
)
REQUIRED_CANDIDATE = (
    "source-facts.json",
    "requirements.json",
    "app.bicep",
    "bicepconfig.json",
)


def descendant_processes(root_pid: int) -> list[int]:
    try:
        result = subprocess.run(
            ["ps", "-eo", "pid=,ppid="],
            text=True,
            capture_output=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    children: dict[int, list[int]] = {}
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) != 2:
            continue
        try:
            pid, parent = map(int, fields)
        except ValueError:
            continue
        children.setdefault(parent, []).append(pid)
    descendants: list[int] = []
    pending = list(children.get(root_pid, []))
    while pending:
        pid = pending.pop()
        descendants.append(pid)
        pending.extend(children.get(pid, []))
    return descendants


def terminate_processes(
    process: subprocess.Popen[str],
    descendants: list[int],
    selected_signal: signal.Signals,
) -> None:
    try:
        os.killpg(process.pid, selected_signal)
    except OSError:
        pass
    for pid in reversed(descendants):
        try:
            os.kill(pid, selected_signal)
        except OSError:
            pass


def run_bounded(
    argv: list[str],
    *,
    cwd: Path,
    timeout: float,
) -> tuple[int, str, str, bool]:
    def text(
        current: str | bytes | None,
        fallback: str | bytes | None,
    ) -> str:
        value = current if current is not None else fallback
        return value.decode(errors="replace") if isinstance(value, bytes) else value or ""

    process = subprocess.Popen(
        argv,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=max(1, timeout))
        return process.returncode, stdout, stderr, False
    except subprocess.TimeoutExpired as first:
        descendants = descendant_processes(process.pid)
        terminate_processes(process, descendants, signal.SIGTERM)
        try:
            stdout, stderr = process.communicate(timeout=2)
        except subprocess.TimeoutExpired as second:
            terminate_processes(
                process,
                descendants + descendant_processes(process.pid),
                signal.SIGKILL,
            )
            try:
                stdout, stderr = process.communicate(timeout=2)
            except subprocess.TimeoutExpired as final:
                if process.stdout is not None:
                    process.stdout.close()
                if process.stderr is not None:
                    process.stderr.close()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                stdout = text(final.stdout, second.stdout or first.stdout)
                stderr = text(final.stderr, second.stderr or first.stderr)
        return 124, stdout, stderr, True


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def parse_final_text(stdout: str) -> str:
    final = ""
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "assistant.message":
            continue
        data = event.get("data") or {}
        content = data.get("content")
        if isinstance(content, str) and content.strip():
            final = content.strip()
    return final


def parse_json_object(text: str) -> dict[str, Any]:
    candidates = [text.strip()]
    candidates.extend(re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL))
    decoder = json.JSONDecoder()
    for candidate in candidates:
        for index, char in enumerate(candidate):
            if char != "{":
                continue
            try:
                value, _ = decoder.raw_decode(candidate[index:])
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                return value
    raise ValueError("agent response did not contain a JSON object")


def validate_review(value: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if value.get("verdict") not in {"accepted", "rejected", "needs_more_info"}:
        errors.append("invalid verdict")
    if not isinstance(value.get("summary"), str):
        errors.append("summary must be a string")
    findings = value.get("findings")
    if not isinstance(findings, list):
        errors.append("findings must be an array")
    else:
        required = {"code", "message", "source", "candidate", "correction"}
        for index, finding in enumerate(findings):
            if not isinstance(finding, dict) or not required <= finding.keys():
                errors.append(f"finding {index} is incomplete")
    if value.get("verdict") == "accepted" and findings:
        errors.append("accepted review cannot contain findings")
    return errors


def install_agents(target: Path) -> list[Path]:
    root_result = subprocess.run(
        ["git", "-C", str(target), "rev-parse", "--show-toplevel"],
        text=True,
        capture_output=True,
        check=False,
    )
    if root_result.returncode:
        raise RuntimeError(f"target is not in a Git worktree: {target}")
    destination = Path(root_result.stdout.strip()) / ".github" / "agents"
    destination.mkdir(parents=True, exist_ok=True)
    installed: list[Path] = []
    for name in AGENT_NAMES:
        source = SKILL_DIR / "agents" / f"{name}.agent.md"
        target_file = destination / f"{name}.md"
        if target_file.exists():
            raise RuntimeError(f"refusing to overwrite custom agent: {target_file}")
        shutil.copy2(source, target_file)
        installed.append(target_file)
    return installed


def remove_agents(paths: list[Path]) -> None:
    for path in paths:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def invoke(
    *,
    target: Path,
    run_dir: Path,
    label: str,
    agent: str,
    session_id: str,
    prompt: str,
    timeout: float,
    resume: bool = False,
    effort: str = "medium",
) -> dict[str, Any]:
    output = run_dir / "agents" / label
    output.mkdir(parents=True, exist_ok=True)
    argv = [
        "copilot",
        "-C",
        str(target),
        "--effort",
        effort,
        "--context",
        "default",
        "--output-format",
        "json",
        "--stream",
        "off",
        "--allow-all-tools",
        "--allow-all-paths",
        "--no-ask-user",
        "--no-auto-update",
        "--no-remote",
        "--no-remote-export",
        "--no-color",
    ]
    if resume:
        argv.append(f"--resume={session_id}")
    else:
        argv.extend(["--session-id", session_id, "--agent", agent])
    argv.extend(["-p", prompt])
    started = time.monotonic()
    status, stdout, stderr, timed_out = run_bounded(
        argv,
        cwd=target,
        timeout=timeout,
    )
    elapsed = round(time.monotonic() - started, 3)
    (output / "events.jsonl").write_text(stdout)
    (output / "stderr.txt").write_text(stderr)
    final_text = parse_final_text(stdout)
    (output / "final-response.txt").write_text(final_text + ("\n" if final_text else ""))
    metadata = {
        "agent": agent,
        "elapsedSeconds": elapsed,
        "label": label,
        "processExit": status,
        "sessionId": session_id,
        "timedOut": timed_out,
    }
    write_json(output / "invocation.json", metadata)
    metadata["finalText"] = final_text
    return metadata


def validate_candidate(
    candidate: Path,
    run_dir: Path,
    source_remote: str | None = None,
    source_commit: str | None = None,
    source_path: str | None = None,
    timeout: float = 60,
) -> dict[str, Any]:
    output = run_dir / "validation"
    output.mkdir(parents=True, exist_ok=True)
    argv = [
        sys.executable,
        str(SKILL_DIR / "scripts" / "validate_candidate.py"),
        "--app-bicep",
        str(candidate / "app.bicep"),
        "--bicepconfig",
        str(candidate / "bicepconfig.json"),
        "--requirements",
        str(candidate / "requirements.json"),
        "--output",
        str(output / "report.json"),
    ]
    if source_remote:
        argv.extend(["--source-remote", source_remote])
    if source_commit:
        argv.extend(["--source-commit", source_commit])
    if source_path:
        argv.extend(["--source-path", source_path])
    status, stdout, stderr, timed_out = run_bounded(
        argv,
        cwd=candidate,
        timeout=timeout,
    )
    (output / "stdout.txt").write_text(stdout)
    (output / "stderr.txt").write_text(stderr)
    try:
        report = json.loads((output / "report.json").read_text())
    except (OSError, json.JSONDecodeError):
        report = {
            "valid": False,
            "errors": [
                {
                    "code": (
                        "VALIDATOR_TIMEOUT"
                        if timed_out
                        else "VALIDATOR_FAILURE"
                    ),
                    "path": "$",
                    "message": f"validator exited {status} without a report",
                }
            ],
        }
    return report


def validate_handoff(candidate: Path) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    missing = [name for name in REQUIRED_CANDIDATE if not (candidate / name).is_file()]
    for name in missing:
        errors.append(
            {"code": "MISSING_HANDOFF", "path": name, "message": "required file is missing"}
        )
    if missing:
        return errors
    for name in ("source-facts.json", "requirements.json"):
        try:
            value = json.loads((candidate / name).read_text())
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(
                {"code": "INVALID_HANDOFF", "path": name, "message": str(exc)}
            )
            continue
        if not isinstance(value, dict):
            errors.append(
                {
                    "code": "INVALID_HANDOFF",
                    "path": name,
                    "message": "top level must be an object",
                }
            )
    facts_mtime = (candidate / "source-facts.json").stat().st_mtime_ns
    requirements_mtime = (candidate / "requirements.json").stat().st_mtime_ns
    bicep_mtime = min(
        (candidate / "app.bicep").stat().st_mtime_ns,
        (candidate / "bicepconfig.json").stat().st_mtime_ns,
    )
    if facts_mtime > requirements_mtime or requirements_mtime > bicep_mtime:
        errors.append(
            {
                "code": "HANDOFF_ORDER",
                "path": str(candidate),
                "message": "source facts and requirements must precede Bicep authoring",
            }
        )
    return errors


def remaining(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())
