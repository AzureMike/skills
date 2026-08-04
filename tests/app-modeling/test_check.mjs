import assert from "node:assert/strict";
import test from "node:test";
import {
  ALLOW,
  DENY,
  ERROR,
  validateCompiledModel,
} from "../../skills/app-modeling/scripts/check.mjs";

const CLEAN_SARIF = JSON.stringify({
  version: "2.1.0",
  runs: [{ tool: { driver: { name: "bicep" } }, results: [] }],
});

function arm(resources) {
  return JSON.stringify({ resources });
}

function application(extra = {}) {
  return {
    type: "Radius.Core/applications@2025-08-01-preview",
    properties: { properties: { environment: "test" } },
    ...extra,
  };
}

function containerResource(containers) {
  return {
    type: "Radius.Compute/containers@2025-08-01-preview",
    properties: { properties: { containers } },
  };
}

test("allows one unconditional application with clean diagnostics", () => {
  assert.deepEqual(
    validateCompiledModel(arm({ app: application() }), CLEAN_SARIF),
    { verdict: ALLOW, findings: [] },
  );
});

test("denies every compiler diagnostic and preserves its source location", () => {
  const diagnostics = JSON.stringify({
    version: "2.1.0",
    runs: [{
      results: [{
        ruleId: "no-unused-params",
        message: { text: "Parameter is declared but never used." },
        locations: [{
          physicalLocation: {
            artifactLocation: { uri: "app.bicep" },
            region: { startLine: 7 },
          },
        }],
      }],
    }],
  });
  assert.deepEqual(
    validateCompiledModel(arm({ app: application() }), diagnostics),
    {
      verdict: DENY,
      findings: [{
        code: "compiler-diagnostic",
        path: "app.bicep:7",
        message: "no-unused-params: Parameter is declared but never used.",
      }],
    },
  );
});

test("denies unresolved classic ARM resources", () => {
  const result = validateCompiledModel(
    JSON.stringify({ resources: [] }),
    CLEAN_SARIF,
  );
  assert.equal(result.verdict, DENY);
  assert.ok(result.findings.some(({ code }) => code === "unresolved-extension"));
});

test("denies zero applications", () => {
  const result = validateCompiledModel(arm({}), CLEAN_SARIF);
  assert.equal(result.verdict, DENY);
  assert.ok(result.findings.some(
    ({ code, path: findingPath }) =>
      code === "application-count" && findingPath === "resources",
  ));
});

test("denies multiple applications", () => {
  const result = validateCompiledModel(
    arm({ first: application(), second: application() }),
    CLEAN_SARIF,
  );
  assert.equal(result.verdict, DENY);
  assert.ok(result.findings.some(
    ({ code, path: findingPath }) =>
      code === "application-count" && findingPath === "resources",
  ));
});

test("denies a conditional application", () => {
  const result = validateCompiledModel(
    arm({ app: application({ condition: "[parameters('enabled')]" }) }),
    CLEAN_SARIF,
  );
  assert.equal(result.verdict, DENY);
  assert.ok(result.findings.some(
    ({ code, path: findingPath }) =>
      code === "application-count" && findingPath === "app",
  ));
});

test("leaves provider resource naming to the Recipe", () => {
  const result = validateCompiledModel(
    arm({
      app: application(),
      database: {
        type: "Radius.Data/mySqlDatabases@2025-08-01-preview",
        properties: { name: "mysql", properties: {} },
      },
    }),
    CLEAN_SARIF,
  );
  assert.equal(result.verdict, ALLOW);
});

test("rejects statically invalid Kubernetes container names", () => {
  for (const name of ["API", "-api", "api-", "api_worker", "a".repeat(64)]) {
    const result = validateCompiledModel(
      arm({
        app: application(),
        workload: containerResource({ [name]: { image: "example/api:latest" } }),
      }),
      CLEAN_SARIF,
    );
    assert.ok(result.findings.some(
      ({ code, path: findingPath }) =>
        code === "invalid-container-name" &&
        findingPath === `workload.containers.${name}`,
    ));
  }
});

test("allows valid and expression-backed Kubernetes container names", () => {
  for (const name of ["api", "api-worker-2", "a".repeat(63), "[variables('name')]"]) {
    const result = validateCompiledModel(
      arm({
        app: application(),
        workload: containerResource({ [name]: { image: "example/api:latest" } }),
      }),
      CLEAN_SARIF,
    );
    assert.equal(
      result.findings.some(({ code }) => code === "invalid-container-name"),
      false,
    );
  }
});

test("reports malformed diagnostics as a checker error", () => {
  assert.equal(
    validateCompiledModel(arm({ app: application() }), "").verdict,
    ERROR,
  );
});

test("reports missing compiled ARM as a model denial", () => {
  const result = validateCompiledModel("", CLEAN_SARIF);
  assert.equal(result.verdict, DENY);
  assert.ok(result.findings.some(({ code }) => code === "compile-failed"));
});
