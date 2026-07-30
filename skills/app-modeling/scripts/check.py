#!/usr/bin/env python3
"""Validate compiled Radius models against the skill contract.

Rules read only compiled ARM and recipe-outputs.json. They report deploy or
runtime defects and ARM-checkable policy violations. Decisions that need
source, image, profile, or intent stay in authoring.md.

A finding may be a defect or a policy violation in a model that would
deploy and run, but it must be proven from these inputs. A fact a rule
cannot prove — an unresolved expression, an equivalent compiled form, a
type the contract does not cover — is skipped, never guessed at.
"""

import argparse
import hashlib
import json
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HEX = set("0123456789abcdef")
DENY, ALLOW = "DENY", "ALLOW"

APP_KIND = "Radius.Core/applications"
CONTAINER_KIND = "Radius.Compute/containers"
IMAGE_KIND = "Radius.Compute/containerImages"
SECRET_KIND = "Radius.Security/secrets"

# Nothing needs to consume a workload.
WORKLOAD_KINDS = {
    APP_KIND,
    CONTAINER_KIND,
    "Radius.Compute/routes",
}

SECURE_TYPES = {"securestring", "secureobject"}


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


def expand(text, variables, limit=None):
    """Expand known variable chains and preserve unknown names."""
    marker = "variables('"
    for _ in range(len(variables) + 1 if limit is None else limit):
        if marker not in text:
            break
        out, position, substituted = [], 0, False
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
            name = text[start + len(marker) : end]
            if name in variables:
                out.append(str(variables[name]))
                substituted = True
            else:
                out.append(text[start : end + 2])
            position = end + 2
        text = "".join(out)
        if not substituted:
            break
    return text


def commit_sha(ref):
    """Return whether ref is a full hexadecimal commit SHA."""
    return (
        isinstance(ref, str)
        and len(ref) == 40
        and all(c in HEX for c in ref.lower())
    )


def immutable(ref):
    """Whether a git ref is provably pinned: a commit SHA or a version tag."""
    if commit_sha(ref):
        return True
    body = ref[1:] if ref.startswith("v") else ref
    return bool(body) and body.replace(".", "").isdigit()


def unwrap_expression(text):
    """Strip nested ARM expression brackets."""
    if not isinstance(text, str):
        return ""
    while text.startswith("[") and text.endswith("]") and len(text) >= 2:
        text = text[1:-1]
    return text


def expression_body(text, variables):
    """Expand an ARM expression and return its unbracketed body."""
    if not isinstance(text, str):
        return ""
    return unwrap_expression(expand(unwrap_expression(text), variables))


def resolve(text, variables):
    """Return a resolved string literal, or None when it can't be proved."""
    if not isinstance(text, str):
        return None
    if not (text.startswith("[") and text.endswith("]")):
        return text
    # An expanded variable that was itself an expression keeps its brackets.
    body = unwrap_expression(expand(text[1:-1], variables))
    if body.startswith("'") and body.endswith("'"):
        return body[1:-1]
    return None if "(" in body else body


def mapping(value):
    """The value as an object, or an empty one if it is anything else."""
    return value if isinstance(value, dict) else {}


ENVELOPE = ".properties.properties"


def normalized(path):
    """Remove the compiled Radius property envelope from a path."""
    for prefix in (ENVELOPE, ".properties"):
        if not path.startswith(prefix):
            continue
        rest = path[len(prefix) :]
        if rest == "" or rest[0] in ".[":
            return rest
    return path


def properties_of(body):
    """Return a compiled Radius resource's inner properties."""
    if not isinstance(body, dict):
        return {}
    outer = body.get("properties")
    if not isinstance(outer, dict):
        return {}
    inner = outer.get("properties")
    return inner if isinstance(inner, dict) else {}


def containers_of(properties):
    """Return the valid container entries in a resource."""
    if not isinstance(properties, dict):
        return {}
    found = properties.get("containers")
    if not isinstance(found, dict):
        return {}
    return {
        name: body for name, body in found.items() if isinstance(body, dict)
    }


def source_line(result):
    """The line a SARIF result points at, or None when it carries no region."""
    try:
        return result["locations"][0]["physicalLocation"]["region"]["startLine"]
    except (KeyError, IndexError, TypeError):
        return None


def diagnostics(output):
    """Yield every SARIF diagnostic and fail closed on malformed output."""
    try:
        runs = json.loads(output)["runs"]
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        runs = None
    if not isinstance(runs, list) or not runs:
        if output.strip():
            yield "unparseable-diagnostics", output.strip().splitlines()[0]
        return
    for run in runs:
        # SARIF makes results optional; an absent list is a clean run.
        results = run.get("results", []) if isinstance(run, dict) else None
        if not isinstance(results, list):
            yield "unparseable-diagnostics", str(run)[:120]
            continue
        for result in results:
            if not isinstance(result, dict):
                yield "unparseable-diagnostics", str(result)[:120]
                continue
            rule = str(result.get("ruleId", ""))
            message = result.get("message")
            text = message.get("text", "") if isinstance(message, dict) else ""
            line = source_line(result)
            yield rule, (f"line {line}: " if line else "") + str(text)


class Model:
    """Facts shared by the validation rules."""

    def __init__(self, arm, recipes, compiler_output=""):
        arm, recipes = mapping(arm), mapping(recipes)
        self.compiler_output = compiler_output
        self.contracts = mapping(recipes.get("types"))
        self.variables = mapping(arm.get("variables"))

        declared = arm.get("resources", {})
        # An unresolved Radius extension emits a classic ARM resource array.
        self.resolved = isinstance(declared, dict)
        self.resources = declared if self.resolved else {}

        self.kinds = {
            symbol: str(body.get("type", "")).split("@")[0]
            if isinstance(body, dict)
            else ""
            for symbol, body in self.resources.items()
        }
        self.secure = {
            name
            for name, spec in mapping(arm.get("parameters")).items()
            if isinstance(spec, dict)
            and str(spec.get("type", "")).lower() in SECURE_TYPES
        }
        # Recipe checks allow properties authored in this template.
        self.authored = {
            symbol: set(self.properties(symbol)) for symbol in self.resources
        }
        # Recipes publish managed keys; authored secrets publish data keys.
        self.published = {}
        for symbol, kind in self.kinds.items():
            if kind == SECRET_KIND:
                data = self.properties(symbol).get("data")
                if isinstance(data, dict):
                    self.published[symbol] = set(data)
            elif self.contracts.get(kind):
                outputs = mapping(mapping(self.contracts[kind]).get("outputs"))
                self.published[symbol] = set(mapping(outputs.get("secrets")))
        # Any reference counts as use, including a connection.
        self.referenced = {
            target
            for _, text in strings(arm.get("resources", {}))
            for target, _ in calls(text, "reference")
        }

    def kind(self, symbol):
        return self.kinds.get(symbol, "")

    def properties(self, symbol):
        return properties_of(self.resources.get(symbol))

    def contract(self, symbol):
        """The pinned Recipe's output contract for a symbol, or None."""
        return mapping(self.contracts.get(self.kind(symbol))) or None

    def recipe_backed(self, symbol):
        return self.kind(symbol) in self.contracts or self.kind(symbol).startswith(
            "Radius.Resources/"
        )

    def of_kind(self, kind):
        """Yield (symbol, properties) for each resource of one type, in order."""
        for symbol in sorted(self.resources):
            if self.kind(symbol) == kind:
                yield symbol, self.properties(symbol)

    def texts(self, symbol):
        """Yield each string and its author-facing resource path."""
        for path, text in strings(self.resources.get(symbol)):
            yield f"{symbol}{normalized(path)}", text

    def containers(self):
        """Yield (path, container) for every container the model declares."""
        for symbol, properties in self.of_kind(CONTAINER_KIND):
            for name, container in containers_of(properties).items():
                yield f"{symbol}.containers.{name}", container


def composes(text):
    """Whether an expression builds a larger string out of its arguments."""
    return "format(" in text or "concat(" in text


def env_of(container):
    """The container's environment block, or an empty one if it is malformed."""
    found = container.get("env")
    return found if isinstance(found, dict) else {}


def secret_binding(entry):
    """The (holder, key) a structured env entry binds, or None if it binds none."""
    if not isinstance(entry, dict):
        return None
    source = entry.get("valueFrom")
    bound = source.get("secretKeyRef") if isinstance(source, dict) else None
    if not isinstance(bound, dict):
        return None
    holder, wanted = bound.get("secretName"), bound.get("key")
    if isinstance(holder, str) and isinstance(wanted, str):
        return holder, wanted
    return None


def connections_of(properties):
    """Map each connected symbol to the name of the connection that reaches it."""
    connected = {}
    for path, text in strings(properties.get("connections") or {}):
        for target, _ in calls(text, "reference"):
            connected[target] = path.lstrip(".").split(".")[0]
    return connected


def iter_secret_bindings(model):
    """Yield path, target, key, and expression for each secret binding."""
    for where, container in model.containers():
        for key, entry in env_of(container).items():
            bound = secret_binding(entry)
            if not bound:
                continue
            holder, wanted = bound
            expr = expression_body(holder, model.variables)
            at = f"{where}.env.{key}"
            for target, _ in calls(expr, "reference"):
                yield at, target, wanted, expr


def has_managed_secrets(model, symbol):
    """Return whether the pinned Recipe publishes managed secrets."""
    contract = model.contract(symbol)
    if not contract:
        return False
    return isinstance(mapping(contract.get("outputs")).get("secrets"), dict)


def reference_reads(text, symbol):
    """Classify what an expression reads from one referenced resource.

    Returns (properties, bare_name, unknown): the .properties names read,
    whether the resource's own name is read, and whether any use of the
    reference could not be classified. Judged by what is read, never by
    matching one expression form.
    """
    properties, bare_name, unknown = set(), False, False
    for target, tail in calls(text, "reference"):
        if target != symbol:
            continue
        if text.startswith(".properties.", tail):
            name, _ = identifier(text, tail + len(".properties."))
            if name:
                properties.add(name)
            else:
                unknown = True
        elif text.startswith(".name", tail):
            after = tail + len(".name")
            if after < len(text) and (text[after].isalnum() or text[after] == "_"):
                unknown = True
            else:
                bare_name = True
        else:
            unknown = True
    return properties, bare_name, unknown


def check_compiler_diagnostics(model, report):
    for rule, detail in diagnostics(model.compiler_output):
        report("compiler-diagnostic", rule, f"the compiler reported {rule}: {detail}")


def check_extension_resolved(model, report):
    if not model.resolved:
        report(
            "unresolved-extension",
            "resources",
            "the compiled template has no symbolic resources, so the Radius "
            "types never resolved; declare `extension radius`.",
        )


def check_application_count(model, report):
    if not model.resolved:
        return
    applications = [s for s in sorted(model.resources) if model.kind(s) == APP_KIND]
    # Loops and conditions make the deployed count unknown.
    for symbol in applications:
        body = model.resources.get(symbol)
        modifier = next(
            (k for k in ("copy", "condition") if isinstance(body, dict) and k in body),
            None,
        )
        if modifier:
            report(
                "application-count",
                symbol,
                f"the {APP_KIND} resource carries `{modifier}`, so the number "
                "deployed is not the one resource every other resource scopes "
                "to; declare exactly one application unconditionally.",
            )
            return
    if len(applications) != 1:
        report(
            "application-count",
            "resources",
            f"the model declares {len(applications)} {APP_KIND} resources "
            f"({applications or 'none'}); declare exactly one.",
        )


def check_resource_shapes(model, report):
    """Reject shapes that would make later rules skip a resource."""
    if not model.resolved:
        return
    for symbol in sorted(model.resources):
        body = model.resources.get(symbol)
        if not isinstance(body, dict):
            report(
                "malformed-resource",
                symbol,
                f"the resource is {type(body).__name__}, not an object, so no "
                "check can read it; declare it as a resource.",
            )
            continue
        outer = body.get("properties")
        inner = outer.get("properties") if isinstance(outer, dict) else None
        if not isinstance(inner, dict):
            report(
                "malformed-resource",
                f"{symbol}.properties",
                "the resource declares no readable properties object, so every "
                "check below it would pass without testing anything; give it a "
                "`properties` body.",
            )
            continue
        containers = inner.get("containers")
        if containers is not None and not isinstance(containers, dict):
            report(
                "malformed-resource",
                f"{symbol}.containers",
                f"`containers` is {type(containers).__name__}, not an object, "
                "so the container checks cannot read it; declare containers as "
                "a named map.",
            )
            continue
        for name, container in sorted(mapping(containers).items()):
            if not isinstance(container, dict):
                report(
                    "malformed-resource",
                    f"{symbol}.containers.{name}",
                    f"the container is {type(container).__name__}, not an "
                    "object, so its image and environment go unchecked.",
                )


def check_recipe_outputs(model, report):
    # Unmapped Recipe outputs resolve to null.
    for symbol in sorted(model.resources):
        for path, text in model.texts(symbol):
            for target, name in property_reads(text):
                contract = model.contract(target)
                if not contract:
                    continue
                outputs = mapping(contract.get("outputs"))
                allowed = set(outputs) | model.authored.get(target, set()) | {"secrets"}
                if name not in allowed:
                    report(
                        "unmapped-recipe-output",
                        path,
                        f"reads {target}.properties.{name}, which the template "
                        "does not set and the pinned Recipe does not return "
                        f"(it returns {sorted(outputs) or 'nothing'}), so the "
                        "value is null at deploy time.",
                    )


def check_authored_secrets(model, report):
    # Authored secrets can't copy Recipe outputs or compose secure parameters.
    for symbol, properties in model.of_kind(SECRET_KIND):
        for path, text in strings(properties.get("data") or {}):
            if next(calls(text, "reference"), None):
                report(
                    "authored-secret-copies-output",
                    f"{symbol}.data{path}",
                    "authored secret copies a resource output; bind the managed "
                    "secret with secretKeyRef instead.",
                )
            expanded = expand(text, model.variables)
            exposed = secure_parameters(model, expanded)
            if composes(expanded) and exposed:
                report(
                    "secret-composed-in-template",
                    f"{symbol}.data{path}",
                    f"secure parameter {names(exposed)} is interpolated into this "
                    "authored secret value, materializing it in deployment state; "
                    "bind the parts separately and compose at runtime.",
                )


def check_build_source(model, report):
    # A provably mutable ref denies; anything unprovable is left alone.
    # The full pinning discipline (GitHub form, tag == sha) is authoring
    # rule 6 prose, not a theorem this file can prove.
    for symbol, properties in model.of_kind(IMAGE_KIND):
        build = properties.get("build")
        if not isinstance(build, dict):
            continue
        source = resolve(build.get("source"), model.variables)
        if not isinstance(source, str):
            continue
        try:
            ref = parse_qs(urlparse(source).query).get("ref", [""])[0]
        except ValueError:
            continue
        if not immutable(ref):
            report(
                "mutable-build-source",
                f"{symbol}.build.source",
                f"build ref {ref or '(absent)'} is mutable, so the code that "
                "gets built is not the code that was read; pin a commit sha "
                "or a release tag.",
            )


def check_connections(model, report):
    # Each consumed Recipe resource needs a connection.
    for symbol, properties in model.of_kind(CONTAINER_KIND):
        connected = connections_of(properties)
        consumed = {}
        for path, text in model.texts(symbol):
            if path.startswith(f"{symbol}.connections"):
                continue
            for target, _ in calls(text, "reference"):
                if model.recipe_backed(target):
                    consumed.setdefault(target, path)
        for target, path in sorted(consumed.items()):
            if target in connected:
                continue
            report(
                "missing-connection",
                path,
                f"consumes {target} but declares no connection to it; local "
                "authoring requires a connection for the application-graph "
                f"relationship (add connections.<name>.source = {target}.id). "
                "A direct reference only orders deployment; it does not create "
                "that relationship. Default CONNECTION_* injection is "
                "connection-driven and may be suppressed with "
                "disableDefaultEnvVars.",
            )


def check_connection_variables(model, report):
    # A CONNECTION_* variable must belong to a declared connection.
    for symbol, properties in model.of_kind(CONTAINER_KIND):
        declared = {name.upper() for name in connections_of(properties).values()}
        for name, container in containers_of(properties).items():
            for key in env_of(container):
                parts = key.split("_") if isinstance(key, str) else []
                if len(parts) < 3 or parts[0] != "CONNECTION":
                    continue
                if any(
                    "_".join(parts[1:index]) in declared
                    for index in range(2, len(parts))
                ):
                    continue
                report(
                    "orphaned-connection-variable",
                    f"{symbol}.containers.{name}.env.{key}",
                    f"{key} is written by hand but no connection named "
                    f"{parts[1].lower()!r} is declared, so the rest of the set "
                    "the application reads is never injected; declare the "
                    "connection instead of forging its variables.",
                )


def check_process_arguments(model, report):
    # Process arguments are visible in the pod spec and process list.
    for where, container in model.containers():
        for field in ("command", "args"):
            arguments = container.get(field)
            if not isinstance(arguments, list):
                continue
            for index, argument in enumerate(arguments):
                if not isinstance(argument, str):
                    continue
                at = f"{where}.{field}[{index}]"
                argument = expand(argument, model.variables)
                exposed = secure_parameters(model, argument)
                if exposed:
                    report(
                        "secret-in-process-args",
                        at,
                        f"secure parameter {names(exposed)} is passed on the "
                        "command line, exposing it in the pod spec and process "
                        "list; deliver it through env instead.",
                    )
                holders = sorted(
                    {t for t, read in property_reads(argument) if read == "secrets"}
                )
                if holders:
                    report(
                        "secret-in-process-args",
                        at,
                        f"managed secret from {names(holders)} is passed on the "
                        "command line; deliver it through env with secretKeyRef "
                        "instead.",
                    )


def check_secret_bindings(model, report):
    # The compiler doesn't check secretName values or secretKeyRef keys.
    for at, target, wanted, expr in iter_secret_bindings(model):
        properties, bare_name, unknown = reference_reads(expr, target)
        wrong = None
        if has_managed_secrets(model, target):
            # The managed secret's name is published only under
            # .properties.secrets; any other read names a secret nothing
            # in the model or contract creates.
            if "secrets" not in properties and (properties or bare_name):
                read = [f".properties.{p}" for p in sorted(properties)]
                if bare_name:
                    read.append(".name")
                wrong = (
                    f"secretName reads {names(read)} from {target}, not the "
                    "managed secret name it publishes at "
                    f"reference('{target}').properties.secrets.name."
                )
        elif model.kind(target) == SECRET_KIND:
            if properties and not bare_name:
                wrong = (
                    f"secretName reads properties of authored secret "
                    f"{target}; its Kubernetes secret is named by "
                    f"reference('{target}').name."
                )
        # An expression that also uses the reference some unclassifiable
        # way is skipped, never guessed at.
        if wrong and not unknown:
            report("wrong-secret-name-path", at, wrong)
        published = model.published.get(target)
        if published is not None and wanted not in published:
            report(
                "unknown-secret-key",
                at,
                f"binds key {wanted!r} from {target}, which publishes "
                f"{sorted(published) or 'no secrets'}.",
            )


def check_composed_secrets(model, report):
    # Template-time composition writes the secret into deployment state.
    for where, container in model.containers():
        for key, entry in env_of(container).items():
            value = entry.get("value") if isinstance(entry, dict) else None
            if not isinstance(value, str):
                continue
            composed = expand(value, model.variables)
            exposed = secure_parameters(model, composed)
            if composes(composed) and exposed:
                report(
                    "secret-composed-in-template",
                    f"{where}.env.{key}",
                    f"secure parameter {names(exposed)} is interpolated into a "
                    "larger value, materializing it in deployment state; bind "
                    "the parts separately and let the application compose them "
                    "at runtime.",
                )


def check_unconsumed_resources(model, report):
    for symbol in sorted(model.resources):
        kind = model.kind(symbol)
        if kind in WORKLOAD_KINDS or symbol in model.referenced:
            continue
        report(
            "unconsumed-resource",
            symbol,
            f"{kind} is declared but no workload consumes it; wire it or "
            "remove it.",
        )


def secure_parameters(model, text):
    """The secure parameters an expression reads, in a stable order."""
    return sorted({p for p, _ in calls(text, "parameters") if p in model.secure})


def names(items):
    return ", ".join(repr(item) for item in items)


# Report structural failures before rules that assume valid resources.
RULES = (
    check_compiler_diagnostics,
    check_extension_resolved,
    check_resource_shapes,
    check_application_count,
    check_recipe_outputs,
    check_authored_secrets,
    check_build_source,
    check_connections,
    check_connection_variables,
    check_process_arguments,
    check_secret_bindings,
    check_composed_secrets,
    check_unconsumed_resources,
)


def check(arm, recipes, compiler_output=""):
    """Run every rule against one model and collect what they report."""
    findings = []
    seen = set()

    def report(code, path, message):
        # Duplicate findings must not change the repair signature.
        if (code, path, message) in seen:
            return
        seen.add((code, path, message))
        findings.append({"code": code, "path": path, "message": message})

    model = Model(arm, recipes, compiler_output)
    for rule in RULES:
        rule(model, report)
    return findings


def signature_of(findings):
    """Hash finding codes and paths so repair loops can detect progress."""
    keys = sorted(f"{f['code']}:{f['path']}" for f in findings)
    return hashlib.sha256("\n".join(keys).encode()).hexdigest()[:12]


def make_result(findings):
    return {
        "verdict": DENY if findings else ALLOW,
        "signature": signature_of(findings),
        "findings": findings,
    }


def load(path):
    """Parse a JSON object from a file, or None when it is missing or malformed."""
    try:
        parsed = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def contract_faults(recipes):
    """Return faults that would disable Recipe checks."""
    types = recipes.get("types")
    if not isinstance(types, dict) or not types:
        return ["`types` is missing, empty, or not an object"]
    faults = []
    for kind, contract in sorted(types.items()):
        if not isinstance(contract, dict):
            faults.append(f"{kind} is not an object")
            continue
        outputs = contract.get("outputs")
        if not isinstance(outputs, dict) or not outputs:
            faults.append(f"{kind}.outputs is missing, empty, or not an object")
            continue
        for name, value in sorted(outputs.items()):
            # Every mapped output must name a real Recipe value.
            if isinstance(value, dict):
                faults += [
                    f"{kind}.outputs.{name}.{key} is not a non-empty string"
                    for key, nested in sorted(value.items())
                    if not (isinstance(nested, str) and nested)
                ]
            elif not (isinstance(value, str) and value):
                faults.append(f"{kind}.outputs.{name} is not a non-empty string")
    return faults


def unusable(path, reason):
    """Return a failure for an unusable checker input."""
    return [{"code": "checker-unusable", "path": str(path), "message": reason}]


def findings_for(arm_path, recipes_path, diagnostics_path):
    """Check the inputs, then return model findings."""
    try:
        compiler_output = diagnostics_path.read_text()
    except (OSError, UnicodeError):
        # Missing diagnostics can't count as a clean build.
        return unusable(
            diagnostics_path,
            "the compiler diagnostics could not be read, so a warning-free "
            "build cannot be established; re-run bicep build with "
            "--diagnostics-format sarif and pass the file it writes.",
        )

    recipes = load(recipes_path)
    if recipes is None:
        # Recipe checks can't run without their contract.
        return unusable(
            recipes_path,
            "the Recipe output contract is missing or unparseable, so the "
            "model cannot be checked; restore assets/recipe-outputs.json.",
        )
    faults = contract_faults(recipes)
    if faults:
        return unusable(
            recipes_path,
            "the Recipe output contract is malformed, so the checks that read "
            f"it would pass without testing anything ({'; '.join(faults[:5])}); "
            "restore assets/recipe-outputs.json.",
        )

    arm = load(arm_path)
    if arm is None:
        # Preserve compiler errors when no ARM output exists.
        findings = [
            {
                "code": "compile-failed",
                "path": str(arm_path),
                "message": "the compiled ARM JSON is missing or unparseable, so "
                "the build failed; fix the compiler errors below and rebuild.",
            }
        ]
        findings += [
            {
                "code": "compiler-diagnostic",
                "path": rule,
                "message": f"the compiler reported {rule}: {detail}",
            }
            for rule, detail in diagnostics(compiler_output)
        ]
        return findings

    return check(arm, recipes, compiler_output)


def main(argv=None):
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
        required=True,
        help="file holding the compiler's SARIF stderr; every diagnostic denies",
    )
    args = parser.parse_args(argv)

    result = make_result(findings_for(args.arm, args.recipes, args.diagnostics))
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 1 if result["verdict"] == DENY else 0


if __name__ == "__main__":
    raise SystemExit(main())
