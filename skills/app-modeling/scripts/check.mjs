// Keep this file identical in radius-project/skills and radius-project/ai-extensions.

export const ALLOW = "ALLOW";
export const DENY = "DENY";
export const ERROR = "ERROR";
export const APP_KIND = "Radius.Core/applications";
export const CONTAINER_KIND = "Radius.Compute/containers";

function isObject(value) {
    return value !== null && typeof value === "object" && !Array.isArray(value);
}

function propertiesOf(resource) {
    if (!isObject(resource?.properties)) return {};
    return isObject(resource.properties.properties)
        ? resource.properties.properties
        : {};
}

function compilerFindingPath(result, rule) {
    const physical = result?.locations?.[0]?.physicalLocation;
    const uri = physical?.artifactLocation?.uri;
    const line = physical?.region?.startLine;
    if (typeof uri === "string" && uri) return line ? `${uri}:${line}` : uri;
    return rule || "compiler";
}

function checkerUnusable(message) {
    return [{ code: "checker-unusable", path: "diagnostics", message }];
}

export function compilerFindings(output) {
    let document;
    try {
        document = JSON.parse(String(output));
    } catch {
        return checkerUnusable("Bicep did not return readable SARIF diagnostics.");
    }
    if (!isObject(document) || !Array.isArray(document.runs)) {
        return checkerUnusable("Bicep diagnostics do not contain a SARIF runs array.");
    }

    const findings = [];
    for (const run of document.runs) {
        if (!isObject(run)) return checkerUnusable("A Bicep SARIF run is not an object.");
        const results = run.results ?? [];
        if (!Array.isArray(results)) {
            return checkerUnusable("A Bicep SARIF run has a non-array results value.");
        }
        for (const result of results) {
            if (!isObject(result)) {
                return checkerUnusable("A Bicep SARIF result is not an object.");
            }
            const rule = typeof result.ruleId === "string" && result.ruleId
                ? result.ruleId
                : "compiler-diagnostic";
            const text = typeof result.message?.text === "string"
                ? result.message.text
                : typeof result.message?.markdown === "string"
                    ? result.message.markdown
                    : "Bicep reported a diagnostic without a message.";
            findings.push({
                code: "compiler-diagnostic",
                path: compilerFindingPath(result, rule),
                message: `${rule}: ${text}`,
            });
        }
    }
    return findings;
}

function resourceKind(resource) {
    if (!isObject(resource) || typeof resource.type !== "string") return "";
    return resource.type.split("@", 1)[0];
}

export function containerNameFindings(arm) {
    if (!isObject(arm) || !isObject(arm.resources)) return [];
    const kubernetesName = /^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$/u;
    const findings = [];
    for (const [symbol, resource] of Object.entries(arm.resources)) {
        if (resourceKind(resource) !== CONTAINER_KIND) continue;
        const containers = propertiesOf(resource).containers;
        if (!isObject(containers)) continue;
        for (const name of Object.keys(containers)) {
            if (
                name.startsWith("[") ||
                (name.length <= 63 && kubernetesName.test(name))
            ) {
                continue;
            }
            findings.push({
                code: "invalid-container-name",
                path: `${symbol}.containers.${name}`,
                message: `${JSON.stringify(name)} is not a valid Kubernetes container name; use a lowercase RFC 1123 label of at most 63 characters.`,
            });
        }
    }
    return findings;
}

export function structuralFindings(arm) {
    if (!isObject(arm) || !isObject(arm.resources)) {
        return [{
            code: "unresolved-extension",
            path: "resources",
            message: "The compiled template has no symbolic resource object, so the Radius extension did not resolve the model.",
        }];
    }

    const findings = [];
    const applications = Object.entries(arm.resources)
        .filter(([, resource]) => resourceKind(resource) === APP_KIND);

    for (const [symbol, resource] of applications) {
        const modifier = ["condition", "copy"].find((key) => Object.hasOwn(resource, key));
        if (modifier) {
            findings.push({
                code: "application-count",
                path: symbol,
                message: `The ${APP_KIND} resource uses ${modifier}, so the deployment does not always contain exactly one application.`,
            });
        }
    }

    if (applications.length !== 1) {
        findings.push({
            code: "application-count",
            path: "resources",
            message: `The model declares ${applications.length} ${APP_KIND} resources; it must declare exactly one.`,
        });
    }
    return findings;
}

export function makeResult(findings) {
    const unique = [];
    const seen = new Set();
    for (const finding of findings) {
        const key = JSON.stringify([finding.code, finding.path, finding.message]);
        if (seen.has(key)) continue;
        seen.add(key);
        unique.push(finding);
    }
    const verdict = unique.some(({ code }) => code === "checker-unusable")
        ? ERROR
        : unique.length > 0
            ? DENY
            : ALLOW;
    return { verdict, findings: unique };
}

export function validateCompiledModel(armOutput, diagnosticsOutput) {
    const diagnostics = compilerFindings(diagnosticsOutput);
    if (diagnostics.some(({ code }) => code === "checker-unusable")) {
        return makeResult(diagnostics);
    }

    let arm;
    try {
        arm = JSON.parse(String(armOutput));
    } catch {
        return makeResult([
            {
                code: "compile-failed",
                path: "app.bicep",
                message: "Bicep did not produce readable ARM JSON.",
            },
            ...diagnostics,
        ]);
    }
    return makeResult([
        ...diagnostics,
        ...structuralFindings(arm),
        ...containerNameFindings(arm),
    ]);
}
