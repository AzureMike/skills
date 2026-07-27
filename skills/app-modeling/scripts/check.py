#!/usr/bin/env python3
"""Reject Radius models that compile but cannot deploy or run.

Reads the compiled ARM JSON, not the Bicep text. No regular expressions:
ARM expressions are scanned as a small formal language.

Every finding is a DENY, and every DENY is a theorem: a fact provable from
the compiled template plus assets/recipe-outputs.json. Judgment calls belong
to the model and to the prose in references/authoring.md, not here.

A new check is admitted only when (a) it is provable from the compiled ARM
plus recipe-outputs.json — never a heuristic about intent — and (b) an
observed model failure shows the defect actually occurs. This rule is what
keeps this file from regrowing the way the prose rules once did.
"""

import argparse
import hashlib
import json
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HEX = set("0123456789abcdef")
DENY, ALLOW = "DENY", "ALLOW"

# Workloads consume dependencies; nothing is expected to consume them.
WORKLOAD_KINDS = {
    "Radius.Core/applications",
    "Radius.Compute/containers",
    "Radius.Compute/routes",
}


def calls(text, name):
    """Yield (argument, index just past the closing paren) for name('arg')."""
    marker = name + "('"
    i = 0
    while True:
        i = text.find(marker, i)
        if i < 0:
            return
        start = i + len(marker)
        end = text.find("'", start)
        if end < 0:
            return
        yield text[start:end], end + 2
        i = end


def identifier(text, i):
    j = i
    while j < len(text) and (text[j].isalnum() or text[j] == "_"):
        j += 1
    return text[i:j], j


def property_reads(text):
    """Yield (symbol, property) for every reference('sym').properties.prop."""
    for symbol, tail in calls(text, "reference"):
        while text.startswith(".properties.", tail):
            name, tail = identifier(text, tail + len(".properties."))
            if not name:
                break
            yield symbol, name


def strings(node, path=""):
    if isinstance(node, dict):
        for key, value in node.items():
            yield from strings(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from strings(value, f"{path}[{index}]")
    elif isinstance(node, str):
        yield path, node


def expand(text, variables, depth=3):
    """Substitute variables('name') references so escaped text becomes visible."""
    marker = "variables('"
    for _ in range(depth):
        if marker not in text:
            break
        out, position = [], 0
        while True:
            start = text.find(marker, position)
            if start < 0:
                out.append(text[position:])
                break
            end = text.find("')", start + len(marker))
            if end < 0:
                out.append(text[position:])
                break
            out.append(text[position:start])
            out.append(str(variables.get(text[start + len(marker) : end], "")))
            position = end + 2
        text = "".join(out)
    return text


def immutable(ref):
    if len(ref) == 40 and all(c in HEX for c in ref.lower()):
        return True
    body = ref[1:] if ref.startswith("v") else ref
    return bool(body) and body.replace(".", "").isdigit()


def resolve(text, variables):
    """Return the literal a template string resolves to, or None if unprovable.

    Anything that cannot be resolved returns None and is left alone rather
    than guessed at.
    """
    if not isinstance(text, str):
        return None
    if not (text.startswith("[") and text.endswith("]")):
        return text
    body = expand(text[1:-1], variables)
    if body.startswith("'") and body.endswith("'"):
        return body[1:-1]
    return None if "(" in body else body


def containers_of(properties):
    if not isinstance(properties, dict):
        return {}
    found = properties.get("containers", {})
    # A malformed model may hand back an array; report it, do not crash on it.
    return found if isinstance(found, dict) else {}


def diagnostics(output):
    """Yield (rule, detail) for every compiler diagnostic.

    ``bicep build`` exits 0 on a warning, and an unknown property on an
    extension type is a warning, so the SARIF stream is the compiler's real
    verdict. Every result denies — errors, warnings, and linter rules alike
    (radius-project/radius#12450 requires that any warning fail validation).
    A non-empty stream that is not SARIF denies as a whole, so a build run
    without ``--diagnostics-format sarif`` cannot silently pass.
    """
    try:
        results = json.loads(output)["runs"][0].get("results", [])
    except (ValueError, KeyError, IndexError):
        if output.strip():
            yield "unparseable-diagnostics", output.strip().splitlines()[0]
        return
    for result in results:
        rule = result.get("ruleId", "")
        region = (
            result.get("locations", [{}])[0]
            .get("physicalLocation", {})
            .get("region", {})
        )
        where = f"line {region['startLine']}: " if "startLine" in region else ""
        yield rule, where + result.get("message", {}).get("text", "")


def check(arm, recipes, compiler_output=""):
    findings = []

    def report(code, path, message):
        findings.append({"code": code, "path": path, "message": message})

    for rule, detail in diagnostics(compiler_output):
        report(
            "compiler-diagnostic",
            rule,
            f"the compiler reported {rule}: {detail}",
        )

    resources = arm.get("resources", {})
    # Without `extension radius` the compiler falls back to classic ARM and
    # emits a resource array, so no symbolic name any other check reads exists.
    if not isinstance(resources, dict):
        report(
            "unresolved-extension",
            "resources",
            "the compiled template has no symbolic resources, so the Radius "
            "types never resolved; declare `extension radius`.",
        )
        resources = {}

    variables = arm.get("variables", {})
    kinds = {
        symbol: str(body.get("type", "")).split("@")[0]
        for symbol, body in resources.items()
    }
    known = recipes.get("types", {})

    def backing(kind):
        # Generated custom types deploy through a recipe too, so they carry
        # the same connection contract as the predefined types.
        return kind in known or kind.startswith("Radius.Resources/")

    # An application is the deployment scope every other resource joins, and
    # two of them silently split the model across scopes.
    applications = [s for s, k in kinds.items() if k == "Radius.Core/applications"]
    if resources and len(applications) != 1:
        report(
            "application-count",
            "resources",
            f"the model declares {len(applications)} Radius.Core/applications "
            f"resources ({sorted(applications) or 'none'}); declare exactly one.",
        )

    secure = {
        name
        for name, spec in arm.get("parameters", {}).items()
        if spec.get("type") == "securestring"
    }
    # The properties each resource sets in this template: reading one back is
    # fine, because the value is right there. Only a property the Recipe is
    # expected to populate must appear in recipe-outputs.json.
    set_in_template = {}
    # The keys each resource actually publishes: a Recipe's secret map for a
    # predefined type, the authored data map for a secret written here.
    secrets_by_symbol = {}
    for symbol, body in resources.items():
        properties = body.get("properties", {}).get("properties", {})
        if isinstance(properties, dict):
            set_in_template[symbol] = set(properties)
        kind = kinds.get(symbol, "")
        if kind == "Radius.Security/secrets":
            data = body.get("properties", {}).get("properties", {}).get("data", {})
            if isinstance(data, dict):
                secrets_by_symbol[symbol] = set(data)
        elif known.get(kind):
            secrets_by_symbol[symbol] = set(
                known[kind].get("outputs", {}).get("secrets", {})
            )
    used = set()

    for symbol, body in resources.items():
        properties = body.get("properties", {}).get("properties", {})

        # A property neither set in this template nor mapped by the pinned
        # Recipe resolves to null at deploy time.
        for path, text in strings(body):
            for target, name in property_reads(text):
                used.add(target)
                outputs = recipes.get("types", {}).get(kinds.get(target, ""), {})
                if not outputs:
                    continue
                allowed = (
                    set(outputs.get("outputs", {}))
                    | set_in_template.get(target, set())
                    | {"secrets"}
                )
                if name not in allowed:
                    report(
                        "unmapped-recipe-output",
                        f"{symbol}{path}",
                        f"reads {target}.properties.{name}, which the template "
                        "does not set and the pinned Recipe does not return "
                        f"(it returns {sorted(outputs.get('outputs', {})) or 'nothing'}), "
                        "so the value is null at deploy time.",
                    )

        # An authored secret must never restate a Recipe output, and a secret
        # composed with Bicep interpolation lands in deployment state.
        if kinds.get(symbol) == "Radius.Security/secrets":
            for path, text in strings(properties.get("data", {})):
                if next(calls(text, "reference"), None):
                    report(
                        "authored-secret-copies-output",
                        f"{symbol}.data{path}",
                        "authored secret copies a resource output; bind the "
                        "managed secret with secretKeyRef instead.",
                    )
                expanded = expand(text, variables)
                if ("format(" in expanded or "concat(" in expanded) and any(
                    p in secure for p, _ in calls(expanded, "parameters")
                ):
                    report(
                        "secret-composed-in-template",
                        f"{symbol}.data{path}",
                        "a secure parameter is interpolated into this authored "
                        "secret value, materializing it in deployment state; "
                        "bind the parts separately and compose at runtime.",
                    )

        # Source builds must pin an immutable revision. build.source lives on
        # containerImages, a resource of its own, never inside a container.
        if kinds.get(symbol) == "Radius.Compute/containerImages":
            build = properties.get("build")
            raw = build.get("source") if isinstance(build, dict) else None
            source = resolve(raw, variables)
            if isinstance(source, str):
                ref = parse_qs(urlparse(source).query).get("ref", [""])[0]
                if not immutable(ref):
                    report(
                        "mutable-build-source",
                        f"{symbol}.build.source",
                        f"build ref {ref or '(absent)'} is mutable, so the code "
                        "that gets built is not the code that was read; pin a "
                        "commit sha or a release tag.",
                    )

        # Consuming a resource without declaring a connection to it costs the
        # application-graph edge permanently, and any RBAC binding with it.
        # Bicep still orders the deployment, so nothing else reveals this.
        if kinds.get(symbol) == "Radius.Compute/containers":
            connections = properties.get("connections") or {}
            connected = {}
            for path, text in strings(connections):
                for target, _ in calls(text, "reference"):
                    connected[target] = path.lstrip(".").split(".")[0]
            elsewhere = {k: v for k, v in properties.items() if k != "connections"}
            consumed = {}
            for path, text in strings(elsewhere):
                for target, _ in calls(text, "reference"):
                    if backing(kinds.get(target, "")):
                        consumed.setdefault(target, path)
            for target, path in sorted(consumed.items()):
                if target in connected:
                    continue
                report(
                    "missing-connection",
                    f"{symbol}{path}",
                    f"consumes {target} but declares no connection to it; only a "
                    "connection creates the application-graph edge and injects "
                    f"CONNECTION_* variables. Add connections.{target}.source = "
                    f"{target}.id.",
                )

            # A hand-written CONNECTION_ variable without the connection that
            # names it delivers one variable of a set the application reads.
            declared = {name.upper() for name in connected.values()}
            for name, container in containers_of(properties).items():
                for key in container.get("env") or {}:
                    parts = key.split("_")
                    if len(parts) < 3 or parts[0] != "CONNECTION":
                        continue
                    if not any(
                        "_".join(parts[1:index]) in declared
                        for index in range(2, len(parts))
                    ):
                        report(
                            "orphaned-connection-variable",
                            f"{symbol}.containers.{name}.env.{key}",
                            f"{key} is written by hand but no connection named "
                            f"{parts[1].lower()!r} is declared, so the rest of the "
                            "set the application reads is never injected; declare "
                            "the connection instead of forging its variables.",
                        )

        for name, container in containers_of(properties).items():
            where = f"{symbol}.containers.{name}"

            # argv is world-readable inside the pod; secrets must not appear.
            # command carries the same exposure as args.
            for field in ("command", "args"):
                for index, argument in enumerate(container.get(field) or []):
                    if not isinstance(argument, str):
                        continue
                    argument = expand(argument, variables)
                    for parameter, _ in calls(argument, "parameters"):
                        if parameter in secure:
                            report(
                                "secret-in-process-args",
                                f"{where}.{field}[{index}]",
                                f"secure parameter {parameter!r} is passed on the "
                                "command line, exposing it in the pod spec "
                                "and process list; deliver it through env instead.",
                            )
                    for target, name_ in property_reads(argument):
                        if name_ == "secrets":
                            report(
                                "secret-in-process-args",
                                f"{where}.{field}[{index}]",
                                f"managed secret from {target} is passed on the "
                                "command line; deliver it through env "
                                "with secretKeyRef instead.",
                            )

            for key, entry in (container.get("env") or {}).items():
                if not isinstance(entry, dict):
                    continue

                # A secretKeyRef key is never checked by the compiler, so a
                # key the resource does not publish arrives empty at runtime.
                bound = entry.get("valueFrom", {}).get("secretKeyRef")
                if isinstance(bound, dict):
                    holder = bound.get("secretName")
                    wanted = bound.get("key")
                    if isinstance(holder, str) and isinstance(wanted, str):
                        for target, _ in calls(holder, "reference"):
                            published = secrets_by_symbol.get(target)
                            if published is not None and wanted not in published:
                                report(
                                    "unknown-secret-key",
                                    f"{where}.env.{key}",
                                    f"binds key {wanted!r} from {target}, which "
                                    f"publishes {sorted(published) or 'no secrets'}.",
                                )

                value = entry.get("value")
                if not isinstance(value, str):
                    continue
                # A composition hidden behind a var reaches deployment state
                # the same way a direct one does, so expand before judging.
                resolved = expand(value, variables)
                if "format(" in resolved or "concat(" in resolved:
                    for parameter, _ in calls(resolved, "parameters"):
                        if parameter in secure:
                            report(
                                "secret-composed-in-template",
                                f"{where}.env.{key}",
                                f"secure parameter {parameter!r} is interpolated "
                                "into a larger value, materializing it in "
                                "deployment state; bind the parts separately and "
                                "let the application compose them at runtime.",
                            )
                            break

    for path, text in strings(arm.get("resources", {})):
        for target, _ in calls(text, "reference"):
            used.add(target)

    # A declared resource nothing consumes is not a modeled dependency.
    for symbol, kind in kinds.items():
        if kind in WORKLOAD_KINDS:
            continue
        if symbol not in used:
            report(
                "unconsumed-resource",
                symbol,
                f"{kind} is declared but no workload consumes it; wire it or "
                "remove it.",
            )

    return findings


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("arm", type=Path)
    parser.add_argument(
        "--recipes",
        type=Path,
        default=Path(__file__).resolve().parent.parent
        / "assets"
        / "recipe-outputs.json",
    )
    parser.add_argument(
        "--diagnostics",
        type=Path,
        help="file holding the compiler's SARIF stderr; every diagnostic denies",
    )
    args = parser.parse_args()

    compiler_output = args.diagnostics.read_text() if args.diagnostics else ""
    try:
        arm = json.loads(args.arm.read_text())
    except (OSError, ValueError):
        arm = None

    if arm is None:
        # The build itself failed, so there is nothing to check; surface the
        # compiler's own diagnostics instead of a traceback.
        findings = [
            {
                "code": "compile-failed",
                "path": str(args.arm),
                "message": "the compiled ARM JSON is missing or unparseable, so "
                "the build failed; fix the compiler errors below and rebuild.",
            }
        ]
        for rule, detail in diagnostics(compiler_output):
            findings.append(
                {
                    "code": "compiler-diagnostic",
                    "path": rule,
                    "message": f"the compiler reported {rule}: {detail}",
                }
            )
    else:
        findings = check(
            arm, json.loads(args.recipes.read_text()), compiler_output
        )

    verdict = DENY if findings else ALLOW
    signature = hashlib.sha256(
        "\n".join(sorted(f"{f['code']}:{f['path']}" for f in findings)).encode()
    ).hexdigest()[:12]

    json.dump(
        {"verdict": verdict, "signature": signature, "findings": findings},
        sys.stdout,
        indent=2,
    )
    sys.stdout.write("\n")
    return 1 if verdict == DENY else 0


if __name__ == "__main__":
    raise SystemExit(main())
