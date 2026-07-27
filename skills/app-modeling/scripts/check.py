#!/usr/bin/env python3
"""Reject Radius models that compile but cannot deploy or run.

Reads the compiled ARM JSON, not the Bicep text. No regular expressions:
ARM expressions are scanned as a small formal language.

A check belongs here only when the bad model compiles cleanly. Anything the
Bicep compiler already rejects is its job, not this file's.
"""

import argparse
import hashlib
import json
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HEX = set("0123456789abcdef")
DENY, WARN, ALLOW = "DENY", "WARN", "ALLOW"

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


# Names a credential rather than a path, identifier, or filename.
CREDENTIAL_HINTS = ("PASSWORD", "PASSWD", "SECRET", "TOKEN", "APIKEY", "CREDENTIAL")
NOT_CREDENTIAL = ("_PATH", "_FILE", "_ID", "_NAME", "_DIR", "_URL", "_ENABLED")


def credential_name(key):
    upper = key.upper()
    if any(upper.endswith(tail) for tail in NOT_CREDENTIAL):
        return False
    return any(hint in upper for hint in CREDENTIAL_HINTS) or upper.endswith("_KEY")


# Radius names become Kubernetes object names, which are RFC 1123 labels.
NAME_SAFE = set("abcdefghijklmnopqrstuvwxyz0123456789-")


def arguments(text):
    """Split an ARM argument list on top-level commas, or None if malformed."""
    args, depth, quoted, current = [], 0, False, []
    for character in text:
        if character == "'":
            quoted = not quoted
        if not quoted:
            if character in "([":
                depth += 1
            elif character in ")]":
                depth -= 1
            elif character == "," and depth == 0:
                args.append("".join(current).strip())
                current = []
                continue
        current.append(character)
    args.append("".join(current).strip())
    return None if quoted or depth else args


def resolve(text, variables):
    """Return the literal a template string resolves to, or None if unprovable.

    A pinned revision is often written as a variable or interpolated, so the
    raw ARM text has to be evaluated before it can be judged. Anything that
    cannot be resolved returns None and is left alone rather than guessed at.
    """
    if not isinstance(text, str):
        return None
    if not (text.startswith("[") and text.endswith("]")):
        return text
    body = expand(text[1:-1], variables)
    if body.startswith("format(") and body.endswith(")"):
        args = arguments(body[len("format(") : -1])
        if not args:
            return None
        # A substituted variable arrives bare, a source literal arrives quoted,
        # and anything still holding a call cannot be resolved.
        parts = []
        for argument in args:
            if argument.startswith("'") and argument.endswith("'"):
                parts.append(argument[1:-1])
            elif argument and "(" not in argument:
                parts.append(argument)
            else:
                return None
        out = parts[0]
        for index, argument in enumerate(parts[1:]):
            out = out.replace("{%d}" % index, argument)
        return None if "{" in out else out
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
    """Yield (rule, detail) for each Bicep diagnostic worth failing on.

    Bicep reports three unrelated things in one stream. A ``BCP`` code is the
    type checker resolving the model against the schema. A rule naming
    ``secure`` or ``secret`` is the linter finding a credential that would be
    written into deployment state. Anything else is style, and style is not a
    modeling defect. Only the first two are yielded.

    ``bicep build --diagnostics-format sarif`` writes structured JSON, which is
    read when present; a plain-text stream is parsed as a fallback so a build
    run without the flag still reports.
    """
    try:
        results = json.loads(output)["runs"][0].get("results", [])
    except (ValueError, KeyError, IndexError):
        results = None

    if results is None:
        entries = []
        for line in output.splitlines():
            for severity in ("Error", "Warning"):
                marker = f" : {severity} "
                index = line.find(marker)
                if index < 0:
                    continue
                start = index + len(marker)
                end = start
                while end < len(line) and (line[end].isalnum() or line[end] in "-_"):
                    end += 1
                entries.append((line[start:end], line.strip()))
                break
    else:
        entries = []
        for result in results:
            rule = result.get("ruleId", "")
            region = (
                result.get("locations", [{}])[0]
                .get("physicalLocation", {})
                .get("region", {})
            )
            where = f"line {region['startLine']}: " if "startLine" in region else ""
            entries.append((rule, where + result.get("message", {}).get("text", "")))

    for rule, detail in entries:
        lowered = rule.lower()
        if rule.startswith("BCP") or "secure" in lowered or "secret" in lowered:
            yield rule, detail


def check(arm, recipes, compiler_output=""):
    findings = []

    def report(verdict, code, path, message):
        findings.append(
            {"verdict": verdict, "code": code, "path": path, "message": message}
        )

    # 0. bicep build exits 0 on a warning, and both an unresolved schema
    #    reference and a credential bound for deployment state are reported
    #    as warnings, so a zero exit code proves nothing on its own.
    for rule, detail in diagnostics(compiler_output):
        schema = rule.startswith("BCP")
        report(
            DENY,
            "compiler-diagnostic" if schema else "insecure-value-diagnostic",
            rule,
            (
                f"the compiler reported {rule}, so the shape did not resolve "
                f"against the schema: {detail}"
            )
            if schema
            else (
                f"the compiler reported {rule}, so a credential would be "
                f"written into deployment state: {detail}"
            ),
        )

    resources = arm.get("resources", {})
    # Without `extension radius` the compiler falls back to classic ARM and
    # emits a resource array, so no symbolic name any other check reads exists.
    if not isinstance(resources, dict):
        report(
            DENY,
            "unresolved-extension",
            "resources",
            "the compiled template has no symbolic resources, so the Radius "
            "types never resolved; declare `extension radius`.",
        )
        resources = {}

    kinds = {
        symbol: str(body.get("type", "")).split("@")[0]
        for symbol, body in resources.items()
    }

    # An application is the deployment scope every other resource joins, and
    # two of them silently split the model across scopes.
    applications = [s for s, k in kinds.items() if k == "Radius.Core/applications"]
    if resources and len(applications) != 1:
        report(
            DENY,
            "application-count",
            "resources",
            f"the model declares {len(applications)} Radius.Core/applications "
            f"resources ({sorted(applications) or 'none'}); declare exactly one.",
        )

    # A model with no backing service is either a genuinely standalone workload
    # or an application whose dependency was left for a human to configure.
    # Which one is a judgement, so this only raises the question.
    if resources and not any(k in recipes.get("types", {}) for k in kinds.values()):
        report(
            WARN,
            "no-backing-service",
            "resources",
            "the model declares no backing service; if the application exists "
            "to operate on a database, broker, cache, or store, that service is "
            "part of the model and enabling a mode that lets someone supply it "
            "by hand is not a substitute.",
        )

    # A route publishes the workload outside the cluster, which the repository
    # cannot ask for. containerPort plus rad run already serves development.
    for symbol, kind in sorted(kinds.items()):
        if kind == "Radius.Compute/routes":
            report(
                WARN,
                "external-ingress-declared",
                symbol,
                "declares external ingress; a containerPort is already "
                "reachable in-cluster and through rad run, so keep this only if "
                "the request asked to publish the application.",
            )
    secure = {
        name
        for name, spec in arm.get("parameters", {}).items()
        if spec.get("type") == "securestring"
    }
    secret_keys = {
        key
        for kind in set(kinds.values())
        for key in recipes.get("types", {})
        .get(kind, {})
        .get("outputs", {})
        .get("secrets", {})
    }
    # The keys each resource actually publishes: a Recipe's secret map for a
    # predefined type, the authored data map for a secret written here.
    secrets_by_symbol = {}
    for symbol, body in resources.items():
        kind = kinds.get(symbol, "")
        if kind == "Radius.Security/secrets":
            data = body.get("properties", {}).get("properties", {}).get("data", {})
            if isinstance(data, dict):
                secrets_by_symbol[symbol] = set(data)
        elif recipes.get("types", {}).get(kind):
            secrets_by_symbol[symbol] = set(
                recipes["types"][kind].get("outputs", {}).get("secrets", {})
            )
    used = set()

    for symbol, body in resources.items():
        properties = body.get("properties", {}).get("properties", {})

        # 11. A name the cluster cannot accept compiles cleanly and fails on
        #     apply, because Radius names become Kubernetes object names.
        name = resolve(body.get("properties", {}).get("name"), arm.get("variables", {}))
        if isinstance(name, str) and name:
            bad = sorted(set(name) - NAME_SAFE)
            if bad or len(name) > 63 or name[0] == "-" or name[-1] == "-":
                report(
                    WARN,
                    "unsafe-resource-name",
                    f"{symbol}.name",
                    f"name {name!r} is not a valid RFC 1123 label"
                    + (f" (disallowed {bad})" if bad else "")
                    + "; use lowercase letters, digits, and hyphens.",
                )

        # 1. Every read property must be mapped by the pinned Recipe.
        for path, text in strings(body):
            for target, name in property_reads(text):
                used.add(target)
                outputs = recipes.get("types", {}).get(kinds.get(target, ""), {})
                if not outputs:
                    continue
                allowed = outputs.get("outputs", {})
                if name not in allowed and name != "secrets":
                    report(
                        DENY,
                        "unmapped-recipe-output",
                        f"{symbol}{path}",
                        f"reads {target}.properties.{name}, but the pinned Recipe "
                        f"only returns {sorted(allowed) or 'nothing'}.",
                    )

        # 4. An authored secret must never restate a Recipe output.
        if kinds.get(symbol) == "Radius.Security/secrets":
            for path, text in strings(properties.get("data", {})):
                if next(calls(text, "reference"), None):
                    report(
                        DENY,
                        "authored-secret-copies-output",
                        f"{symbol}.data{path}",
                        "authored secret copies a resource output; bind the "
                        "managed secret with secretKeyRef instead.",
                    )

        # 5. Source builds must pin an immutable revision. build.source lives on
        #    containerImages, a resource of its own, never inside a container.
        if kinds.get(symbol) == "Radius.Compute/containerImages":
            build = properties.get("build")
            raw = build.get("source") if isinstance(build, dict) else None
            source = resolve(raw, arm.get("variables", {}))
            if isinstance(source, str):
                ref = parse_qs(urlparse(source).query).get("ref", [""])[0]
                if not immutable(ref):
                    report(
                        DENY,
                        "mutable-build-source",
                        f"{symbol}.build.source",
                        f"build ref {ref or '(absent)'} is mutable, so the code "
                        "that gets built is not the code that was read; pin a "
                        "commit sha or a release tag.",
                    )
            tag = resolve(properties.get("tag"), arm.get("variables", {}))
            if isinstance(tag, str) and not immutable(tag):
                report(
                    WARN,
                    "mutable-image-tag",
                    f"{symbol}.tag",
                    f"image tag {tag!r} does not name a revision, so a rebuild "
                    "overwrites it and a node may reuse the cached image; "
                    "prefer the built commit.",
                )

        # 13. Consuming a resource without declaring a connection to it costs the
        #     application-graph edge permanently, and any RBAC binding with it.
        #     Bicep still orders the deployment, so nothing else reveals this.
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
                    if kinds.get(target, "") in recipes.get("types", {}):
                        consumed.setdefault(target, path)
            for target, path in sorted(consumed.items()):
                if target in connected:
                    continue
                report(
                    DENY,
                    "missing-connection",
                    f"{symbol}{path}",
                    f"consumes {target} but declares no connection to it; only a "
                    "connection creates the application-graph edge and injects "
                    f"CONNECTION_* variables. Add connections.{target}.source = "
                    f"{target}.id.",
                )

            # 14. A hand-written CONNECTION_ variable without the connection that
            #     names it delivers one variable of a set the application reads.
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
                            DENY,
                            "orphaned-connection-variable",
                            f"{symbol}.containers.{name}.env.{key}",
                            f"{key} is written by hand but no connection named "
                            f"{parts[1].lower()!r} is declared, so the rest of the "
                            "set the application reads is never injected; declare "
                            "the connection instead of forging its variables.",
                        )

        for name, container in containers_of(properties).items():
            where = f"{symbol}.containers.{name}"

            # 7. A workload with no configuration rarely has its feature enabled.
            if not (container.get("env") or properties.get("connections")):
                report(
                    WARN,
                    "unconfigured-workload",
                    where,
                    "workload declares no environment configuration and no "
                    "connections; a profile whose primary feature is inert "
                    "until someone supplies configuration by hand is not a "
                    "running application. Select a profile that works, or "
                    "report that the repository has none.",
                )

            # 8. Kubernetes expands $(VAR) in args, never ${VAR} or bare names.
            declared = set(container.get("env") or {})
            for index, argument in enumerate(container.get("args") or []):
                if not isinstance(argument, str):
                    continue
                resolved = expand(argument, arm.get("variables", {}))
                named = sorted(name for name in declared if name in resolved)
                if named and "$(" not in resolved:
                    report(
                        WARN,
                        "unexpanded-argument-reference",
                        f"{where}.args[{index}]",
                        f"argument references environment variable(s) {named} but "
                        "Kubernetes only expands $(VAR) in args; prove the process "
                        "itself expands this or the literal text is passed through.",
                    )

            # 9. argv is world-readable inside the pod; secrets must not appear.
            #    command carries the same exposure as args.
            for field in ("command", "args"):
                for index, argument in enumerate(container.get(field) or []):
                    if not isinstance(argument, str):
                        continue
                    argument = expand(argument, arm.get("variables", {}))
                    for parameter, _ in calls(argument, "parameters"):
                        if parameter in secure:
                            report(
                                DENY,
                                "secret-in-process-args",
                                f"{where}.{field}[{index}]",
                                f"secure parameter {parameter!r} is passed on the "
                                "command line, exposing it in the pod spec "
                                "and process list; deliver it through env instead.",
                            )
                    for target, name in property_reads(argument):
                        if name == "secrets":
                            report(
                                DENY,
                                "secret-in-process-args",
                                f"{where}.{field}[{index}]",
                                f"managed secret from {target} is passed on the "
                                "command line; deliver it through env "
                                "with secretKeyRef instead.",
                            )

            for key, entry in (container.get("env") or {}).items():
                if not isinstance(entry, dict):
                    continue

                # 10. A secretKeyRef key is never checked by the compiler, so a
                #     key the resource does not publish arrives empty at runtime.
                bound = entry.get("valueFrom", {}).get("secretKeyRef")
                if isinstance(bound, dict):
                    holder = bound.get("secretName")
                    wanted = bound.get("key")
                    if isinstance(holder, str) and isinstance(wanted, str):
                        for target, _ in calls(holder, "reference"):
                            known = secrets_by_symbol.get(target)
                            if known is not None and wanted not in known:
                                report(
                                    DENY,
                                    "unknown-secret-key",
                                    f"{where}.env.{key}",
                                    f"binds key {wanted!r} from {target}, which "
                                    f"publishes {sorted(known) or 'no secrets'}.",
                                )

                value = entry.get("value")
                if not isinstance(value, str):
                    continue
                # A composition hidden behind a var reaches deployment state the
                # same way a direct one does, so resolve before judging.
                resolved = expand(value, arm.get("variables", {}))
                literal = resolve(value, arm.get("variables", {}))

                # 2. A managed secret must arrive via secretKeyRef.
                if isinstance(literal, str):
                    for secret in secret_keys:
                        if secret in literal:
                            report(
                                DENY,
                                "managed-secret-as-literal",
                                f"{where}.env.{key}",
                                f"literal {literal!r} names managed secret "
                                f"{secret!r}; bind it with secretKeyRef.",
                            )

                    # 12. A credential written as a literal is stored in the
                    #     template and in deployment state, where the schema
                    #     linter cannot see it because env is a free-form map.
                    if literal and credential_name(key):
                        report(
                            WARN,
                            "literal-credential-value",
                            f"{where}.env.{key}",
                            f"{key} carries the literal {literal!r}; if that is a "
                            "real credential, take it from a @secure() parameter "
                            "or bind it with secretKeyRef.",
                        )
                    continue

                # 3. Secrets composed in Bicep land in deployment state.
                composed = "format(" in resolved or "concat(" in resolved
                for parameter, _ in calls(resolved, "parameters"):
                    if parameter in secure and composed:
                        report(
                            WARN,
                            "secret-composed-in-template",
                            f"{where}.env.{key}",
                            f"secure parameter {parameter!r} is interpolated into "
                            "a larger value, materializing it in state; prefer a "
                            "managed connection string or runtime composition when "
                            "the contract offers one.",
                        )
                        break

    for path, text in strings(arm.get("resources", {})):
        for target, _ in calls(text, "reference"):
            used.add(target)

    # 6. A declared resource nothing consumes is not a modeled dependency.
    for symbol, kind in kinds.items():
        if kind in WORKLOAD_KINDS:
            continue
        if symbol not in used:
            report(
                DENY,
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
        help="file holding the compiler's stderr; any Error or Warning denies",
    )
    args = parser.parse_args()

    findings = check(
        json.loads(args.arm.read_text()),
        json.loads(args.recipes.read_text()),
        args.diagnostics.read_text() if args.diagnostics else "",
    )
    verdict = (
        DENY
        if any(f["verdict"] == DENY for f in findings)
        else WARN if findings else ALLOW
    )
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
