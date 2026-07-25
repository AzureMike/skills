#!/usr/bin/env python3

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys


SKILL_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONTRACT = SKILL_DIR / "assets" / "radius-contract.json"
SECRET_ENV = re.compile(
    r"(?:^|_)(?:PASSWORD|PASSWD|SECRET|TOKEN|API_?KEY|PRIVATE_?KEY|"
    r"CONNECTION_?STRING|CREDENTIAL)(?:$|_)",
    re.IGNORECASE,
)
IMMUTABLE_REF = re.compile(
    r"^(?:[0-9a-f]{40}|v?\d+\.\d+(?:\.\d+)?(?:[-+][A-Za-z0-9.-]+)?)$",
    re.IGNORECASE,
)


def emit(report, output):
    text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if output:
        output.write_text(text)
    sys.stdout.write(text)


def error(errors, code, path, message):
    errors.append({"code": code, "path": path, "message": message})


def load_json(path, label):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"{label} error: {exc}") from exc


def resolve_static(value, variables):
    if isinstance(value, dict):
        return {key: resolve_static(item, variables) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve_static(item, variables) for item in value]
    if not isinstance(value, str):
        return value
    variable = re.fullmatch(r"\[variables\('([^']+)'\)\]", value)
    if variable:
        return resolve_static(variables.get(variable.group(1)), variables)
    formatted = re.fullmatch(
        r"\[format\('([^']*)',\s*variables\('([^']+)'\)\)\]",
        value,
    )
    if formatted:
        argument = resolve_static(variables.get(formatted.group(2)), variables)
        return formatted.group(1).replace("{0}", str(argument))
    return value


def normalize_resources(template):
    resources = template.get("resources", {})
    variables = template.get("variables", {})
    if isinstance(resources, dict):
        return [
            {
                "symbol": symbol,
                "type": body.get("type"),
                "name": resolve_static(
                    body.get("properties", {}).get("name"),
                    variables,
                ),
                "properties": resolve_static(
                    body.get("properties", {}).get("properties", {}),
                    variables,
                ),
            }
            for symbol, body in resources.items()
        ]
    return []


def referenced_symbol(value):
    if not isinstance(value, str):
        return None
    match = re.search(r"reference\('([^']+)'\)", value)
    return match.group(1) if match else None


def validate_ref(errors, path, source):
    if not isinstance(source, str) or "?ref=" not in source:
        error(errors, "IMMUTABLE_SOURCE_REF", path, "Git build source must include ?ref=.")
        return None
    ref = source.rsplit("?ref=", 1)[1].split("&", 1)[0]
    if not IMMUTABLE_REF.fullmatch(ref):
        error(
            errors,
            "IMMUTABLE_SOURCE_REF",
            path,
            f"Git ref {ref!r} is not a full commit SHA or immutable release tag.",
        )
        return None
    return ref


def git_remote_prefixes(remote):
    normalized = remote.strip().rstrip("/")
    if normalized.endswith(".git"):
        repositories = {normalized, normalized[:-4]}
    else:
        repositories = {normalized, normalized + ".git"}
    return tuple(
        f"git::{repository}//"
        for repository in sorted(repositories, key=len, reverse=True)
    )


def validate_template(
    template,
    contract,
    errors,
    source_remote=None,
    source_commit=None,
    source_path=None,
):
    allowed = set(contract.get("resourceTypes", {}))
    environment_parameter = template.get("parameters", {}).get("environment")
    if not isinstance(environment_parameter, dict) or environment_parameter.get(
        "type"
    ) != "string":
        error(
            errors,
            "ENVIRONMENT_PARAMETER",
            "$.parameters.environment",
            "Radius applications must declare `param environment string` for "
            "automatic Radius CLI binding.",
        )
    secure_parameters = {
        name
        for name, body in template.get("parameters", {}).items()
        if body.get("type") in {"securestring", "secureObject"}
    }
    for resource in normalize_resources(template):
        symbol = resource["symbol"]
        resource_type = resource["type"]
        path = f"$.resources.{symbol}"
        if resource_type not in allowed:
            error(
                errors,
                "UNVERIFIED_RESOURCE_TYPE",
                f"{path}.type",
                f"{resource_type!r} is absent from the bundled verified contract.",
            )

        properties = resource["properties"]
        schema_properties = (
            contract.get("resourceTypes", {})
            .get(resource_type, {})
            .get("schema", {})
            .get("properties", {})
        )
        if (
            "environment" in schema_properties
            and properties.get("environment") != "[parameters('environment')]"
        ):
            error(
                errors,
                "ENVIRONMENT_BINDING",
                f"{path}.properties.environment",
                "Radius resource environment must bind directly to the "
                "`environment` parameter.",
            )
        qualified_type = (resource_type or "").split("@", 1)[0]
        recipe = contract.get("azureRecipeMappings", {}).get(qualified_type, {})
        if recipe.get("providerGlobalName"):
            name = resource.get("name")
            parameter_ref = (
                re.fullmatch(r"\[parameters\('([^']+)'\)\]", name)
                if isinstance(name, str)
                else None
            )
            parameter = (
                template.get("parameters", {}).get(parameter_ref.group(1), {})
                if parameter_ref
                else {}
            )
            if not parameter_ref or "defaultValue" in parameter:
                error(
                    errors,
                    "GLOBAL_NAME_PARAMETER",
                    f"{path}.name",
                    f"{qualified_type} uses a provider-global Recipe name and "
                    "must bind resource name directly from a required Bicep "
                    "parameter with no default.",
                )
        if resource_type == "Radius.Compute/containerImages@2025-08-01-preview":
            build = properties.get("build", {})
            build_source = build.get("source")
            source_ref = validate_ref(
                errors,
                f"{path}.properties.build.source",
                build_source,
            )
            if not isinstance(build_source, str) or not re.match(
                r"^git::(?:https://|ssh://|git@)", build_source
            ):
                error(
                    errors,
                    "REMOTE_BUILD_SOURCE",
                    f"{path}.properties.build.source",
                    "Source-built images must use a remotely cloneable Git source; "
                    "workstation paths are not deployable by the stock Recipe.",
                )
            if source_remote and isinstance(build_source, str):
                expected_prefixes = git_remote_prefixes(source_remote)
                matched_prefix = next(
                    (
                        prefix
                        for prefix in expected_prefixes
                        if build_source.startswith(prefix)
                    ),
                    None,
                )
                if not matched_prefix:
                    error(
                        errors,
                        "SOURCE_REMOTE",
                        f"{path}.properties.build.source",
                        "Expected the checked-out source repository prefix "
                        f"(accepted forms: {', '.join(repr(item) for item in expected_prefixes)}).",
                    )
                elif source_path and source_path != ".":
                    source_locator = build_source.split("?ref=", 1)[0]
                    build_path = source_locator.removeprefix(matched_prefix)
                    if not (
                        build_path == source_path
                        or build_path.startswith(source_path.rstrip("/") + "/")
                    ):
                        error(
                            errors,
                            "SOURCE_PATH",
                            f"{path}.properties.build.source",
                            "Build source must stay within the selected "
                            f"repository application path {source_path!r}.",
                        )
            if source_commit and source_ref and source_ref != source_commit:
                error(
                    errors,
                    "SOURCE_COMMIT",
                    f"{path}.properties.build.source",
                    f"Expected immutable source revision {source_commit!r}.",
                )
            if (
                contract.get("policies", {}).get("containerImageTagRequired")
                and not properties.get("tag")
            ):
                error(
                    errors,
                    "CONTAINER_IMAGE_TAG",
                    f"{path}.properties.tag",
                    "The verified containerImages Recipe requires an explicit immutable tag.",
                )
            elif source_ref and properties.get("tag") != source_ref:
                error(
                    errors,
                    "CONTAINER_IMAGE_TAG",
                    f"{path}.properties.tag",
                    "The image tag must equal the immutable Git source ref.",
                )

        if resource_type != "Radius.Compute/containers@2025-08-01-preview":
            continue

        env_values = []
        for container in properties.get("containers", {}).values():
            env_values.extend(container.get("env", {}).values())
            for name, body in container.get("env", {}).items():
                value = body.get("value")
                if value is None or not SECRET_ENV.search(name):
                    continue
                # A secret may reach a container from a managed secret, or as a
                # secure parameter the deployer supplied - including composed
                # into a connection URI, which is the only shape some clients
                # accept. What must never happen is a secret written into the
                # definition as a plain literal.
                carries_secure_input = isinstance(value, str) and any(
                    f"parameters('{parameter}')" in value
                    for parameter in secure_parameters
                )
                if not carries_secure_input:
                    error(
                        errors,
                        "SECRET_ENV_VALUE",
                        f"{path}.properties.containers.env.{name}",
                        "A secret-like setting must come from a managed secret "
                        "or a secure parameter, never a literal value.",
                    )

        for container_name, container in properties.get("containers", {}).items():
            argv = list(container.get("command") or []) + list(container.get("args") or [])
            for position, item in enumerate(argv):
                if not isinstance(item, str):
                    continue
                for parameter in secure_parameters:
                    if f"parameters('{parameter}')" in item:
                        error(
                            errors,
                            "SECRET_IN_PROCESS_ARGUMENTS",
                            f"{path}.properties.containers.{container_name}"
                            f".command[{position}]",
                            "A secure parameter must not be expanded into process "
                            "arguments, where it is visible in the process table.",
                        )

        for name, connection in properties.get("connections", {}).items():
            source_symbol = referenced_symbol(connection.get("source"))
            has_explicit_binding = source_symbol and any(
                f"reference('{source_symbol}')" in json.dumps(value)
                for value in env_values
            )
            if (
                has_explicit_binding
                and connection.get("disableDefaultEnvVars") is not True
            ):
                error(
                    errors,
                    "CONNECTION_DEFAULT_ENV",
                    f"{path}.properties.connections.{name}.disableDefaultEnvVars",
                    "An explicit native binding to the connection source requires "
                    "disableDefaultEnvVars=true.",
                )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--app-bicep", type=Path, required=True)
    parser.add_argument("--bicepconfig", type=Path, required=True)
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    parser.add_argument("--requirements", type=Path)
    parser.add_argument("--source-remote")
    parser.add_argument("--source-commit")
    parser.add_argument("--source-path")
    parser.add_argument("--bicep", default="bicep")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    contract = load_json(args.contract, "contract")
    config = load_json(args.bicepconfig, "bicepconfig")
    errors = []

    expected_extension = contract.get("extension", {}).get("reference")
    actual_extension = config.get("extensions", {}).get("radius")
    if actual_extension != expected_extension:
        error(
            errors,
            "EXTENSION_REF",
            "$.bicepconfig.extensions.radius",
            f"Expected {expected_extension!r}, got {actual_extension!r}.",
        )
    if config.get("experimentalFeaturesEnabled", {}).get("extensibility") is not True:
        error(
            errors,
            "EXTENSIBILITY_DISABLED",
            "$.bicepconfig.experimentalFeaturesEnabled.extensibility",
            "The Radius extension requires extensibility=true.",
        )

    source = args.app_bicep.read_text()
    if re.search(r"\bany\s*\(", source):
        error(errors, "SCHEMA_ESCAPE", "$.appBicep", "any() is forbidden.")

    result = subprocess.run(
        [args.bicep, "build", args.app_bicep.name, "--stdout"],
        cwd=args.app_bicep.parent,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode:
        error(
            errors,
            "BICEP_BUILD",
            "$.compile",
            f"Bicep build exited with status {result.returncode}.",
        )
    if result.stderr.strip():
        error(errors, "BICEP_DIAGNOSTIC", "$.compile", result.stderr.strip())

    template = None
    if result.stdout.strip():
        try:
            template = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            error(errors, "BICEP_OUTPUT", "$.compile", f"Invalid build JSON: {exc}")
    if template:
        validate_template(
            template,
            contract,
            errors,
            source_remote=args.source_remote,
            source_commit=args.source_commit,
            source_path=args.source_path,
        )

    report = {"valid": not errors, "errors": errors}
    emit(report, args.output)
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
