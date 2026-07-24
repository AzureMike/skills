#!/usr/bin/env python3

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys


SKILL_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONTRACT = SKILL_DIR / "assets" / "radius-contract.json"


def load_contract(path):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise SystemExit(f"contract error: {error}") from error


def qualified_type(contract, name):
    if "@" in name:
        if name in contract["resourceTypes"]:
            return name
        raise SystemExit(f"unsupported type: {name}")

    matches = [
        key for key in contract["resourceTypes"]
        if key.split("@", 1)[0] == name
    ]
    if len(matches) != 1:
        raise SystemExit(f"unsupported or ambiguous type: {name}")
    return matches[0]


def emit(value):
    json.dump(value, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")


def verify_provenance(contract, contrib_dir):
    commit = contract["generatedFrom"]["resourceTypesContribCommit"]
    expected = contract["generatedFrom"]["manifestSha256"]
    paths = {}
    for source_path, digest in expected.items():
        prefix = "refs/resource-types-contrib/"
        if source_path.startswith(prefix):
            paths[source_path[len(prefix):]] = digest

    recipe_path = contract["generatedFrom"]["recipePack"].removeprefix(
        "refs/resource-types-contrib/"
    )
    paths[recipe_path] = contract["generatedFrom"]["recipePackSha256"]

    failures = []
    for path, expected_digest in sorted(paths.items()):
        result = subprocess.run(
            ["git", "-C", str(contrib_dir), "show", f"{commit}:{path}"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if result.returncode:
            failures.append({"path": path, "error": result.stderr.decode().strip()})
            continue
        actual = hashlib.sha256(result.stdout).hexdigest()
        if actual != expected_digest:
            failures.append(
                {"path": path, "expected": expected_digest, "actual": actual}
            )

    emit({"valid": not failures, "commit": commit, "failures": failures})
    return 0 if not failures else 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("list")

    type_parser = subparsers.add_parser("type")
    type_parser.add_argument("name")

    recipe_parser = subparsers.add_parser("recipe")
    recipe_parser.add_argument("name")

    protocol_parser = subparsers.add_parser("protocol")
    protocol_parser.add_argument("name")

    bundle_parser = subparsers.add_parser("bundle")
    bundle_parser.add_argument("name")

    subparsers.add_parser("provenance")

    verify_parser = subparsers.add_parser("verify-provenance")
    verify_parser.add_argument("--contrib-dir", type=Path, required=True)

    args = parser.parse_args()
    contract = load_contract(args.contract)

    if args.command == "list":
        emit(
            {
                "extension": contract["extension"]["reference"],
                "types": sorted(contract["resourceTypes"]),
            }
        )
        return 0

    if args.command == "type":
        key = qualified_type(contract, args.name)
        emit(contract["resourceTypes"][key])
        return 0

    if args.command == "recipe":
        name = args.name.split("@", 1)[0]
        if name not in contract["azureRecipeMappings"]:
            raise SystemExit(f"no verified Azure Recipe mapping: {name}")
        emit(contract["azureRecipeMappings"][name])
        return 0

    if args.command == "protocol":
        name = args.name.split("@", 1)[0]
        if name not in contract["protocolProfiles"]:
            raise SystemExit(f"no verified protocol profile: {name}")
        emit(contract["protocolProfiles"][name])
        return 0

    if args.command == "bundle":
        key = qualified_type(contract, args.name)
        name = key.split("@", 1)[0]
        emit(
            {
                "type": contract["resourceTypes"][key],
                "recipe": contract["azureRecipeMappings"].get(name),
                "protocol": contract["protocolProfiles"].get(name),
            }
        )
        return 0

    if args.command == "provenance":
        emit(
            {
                "generatedFrom": contract["generatedFrom"],
                "extension": contract["extension"],
            }
        )
        return 0

    return verify_provenance(contract, args.contrib_dir)


if __name__ == "__main__":
    raise SystemExit(main())
