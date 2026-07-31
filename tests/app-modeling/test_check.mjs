import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import {
  AZURE_RECIPE_PACK_URL,
  RecipeContractError,
  check,
  compileRecipePack,
  constrainedContract,
  contractFaults,
  fetchRecipePack,
  findBicepConfig,
  main,
  recipeContractFromPackArm,
  recipeSourceKey,
} from "../../skills/app-modeling/scripts/check.mjs";

const ROOT = path.dirname(fileURLToPath(import.meta.url));
const fixtures = JSON.parse(
  fs.readFileSync(path.join(ROOT, "fixtures", "checker-cases.json"), "utf8"),
);

for (const item of fixtures.cases) {
  test(`Python checker parity: ${item.name}`, () => {
    assert.deepEqual(
      check(item.arm, fixtures.recipes[item.recipe], item.compilerOutput),
      item.findings,
    );
  });
}

function resource(type, properties = {}) {
  return { type, properties: { properties } };
}

function recipePack(recipes) {
  return {
    resources: {
      pack: resource("Radius.Core/recipePacks@2025-08-01-preview", { recipes }),
    },
  };
}

test("normalizes compiled Recipe Pack ARM and omits output-less Recipes", () => {
  const contract = recipeContractFromPackArm(
    recipePack({
      "Radius.Data/postgreSqlDatabases": {
        source:
          "mcr.microsoft.com/bicep/avm/res/" +
          "db-for-postgre-sql/flexible-server:0.15.2",
        outputs: { host: "fqdn" },
      },
      "Radius.Compute/containers": {
        source: "ghcr.io/radius-project/kube-recipes/containers:latest",
      },
    }),
  );
  assert.deepEqual(contractFaults(contract), []);
  assert.deepEqual(
    contract.types["Radius.Data/postgreSqlDatabases"].reservedPrefixes.username,
    ["pg_"],
  );
  assert.equal(Object.hasOwn(contract.types, "Radius.Compute/containers"), false);
});

test("applies Azure restrictions only to exact normalized module sources", () => {
  assert.equal(
    recipeSourceKey(
      "mcr.microsoft.com/bicep/avm/res/sql/server@sha256:abc",
    ),
    "avm/res/sql/server",
  );
  assert.equal(
    Object.hasOwn(
      constrainedContract(
        "example.test/avm/res/sql/server-wrapper:1.0",
        { host: "fqdn" },
      ),
      "reserved",
    ),
    false,
  );
});

test("rejects conflicting case-insensitive Recipe definitions", () => {
  assert.throws(
    () =>
      recipeContractFromPackArm(
        recipePack({
          "Radius.Data/redisCaches": {
            source: "one",
            outputs: { host: "first" },
          },
          "radius.data/rediscaches": {
            source: "two",
            outputs: { host: "second" },
          },
        }),
      ),
    /conflicting Recipe definitions/u,
  );
});

test("fetches the fixed raw pack with bounded response size", async (context) => {
  const originalFetch = globalThis.fetch;
  context.after(() => {
    globalThis.fetch = originalFetch;
  });
  let requested;
  globalThis.fetch = async (url) => {
    requested = url;
    return new Response("extension radius");
  };
  assert.equal(await fetchRecipePack(), "extension radius");
  assert.equal(requested, AZURE_RECIPE_PACK_URL);

  globalThis.fetch = async () => new Response("too large");
  await assert.rejects(
    fetchRecipePack(AZURE_RECIPE_PACK_URL, 1_000, 3),
    (error) =>
      error instanceof RecipeContractError &&
      error.message.includes("3-byte download limit"),
  );
});

test("rejects invalid UTF-8 Recipe Pack downloads", async (context) => {
  const originalFetch = globalThis.fetch;
  context.after(() => {
    globalThis.fetch = originalFetch;
  });
  globalThis.fetch = async () =>
    new Response(Uint8Array.from([0xff]), {
      headers: { "content-type": "text/plain" },
    });
  await assert.rejects(
    fetchRecipePack(),
    (error) =>
      error instanceof RecipeContractError &&
      error.message.includes("valid UTF-8"),
  );
});

test("compiles beside the active Bicep configuration and cleans up", () => {
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), "checker-node-"));
  const previousBinary = process.env.BICEP_BINARY;
  try {
    const radius = path.join(temporary, ".radius");
    fs.mkdirSync(radius);
    const config = path.join(radius, "bicepconfig.json");
    fs.writeFileSync(config, "{}");
    const compiler = path.join(temporary, "fake-bicep.mjs");
    const compiled = recipePack({
      "Radius.Data/redisCaches": {
        source: "example.test/redis:1.0",
        outputs: { host: "hostname" },
      },
    });
    fs.writeFileSync(
      compiler,
      `#!/usr/bin/env node\nprocess.stdout.write(${JSON.stringify(
        JSON.stringify(compiled),
      )});\n`,
      { mode: 0o755 },
    );
    process.env.BICEP_BINARY = compiler;

    assert.equal(findBicepConfig(temporary), config);
    const contract = compileRecipePack("extension radius", config);
    assert.equal(
      contract.types["Radius.Data/redisCaches"].outputs.host,
      "hostname",
    );
    assert.deepEqual(
      fs.readdirSync(radius).filter((name) =>
        name.startsWith(".recipe-contract-"),
      ),
      [],
    );
  } finally {
    if (previousBinary === undefined) delete process.env.BICEP_BINARY;
    else process.env.BICEP_BINARY = previousBinary;
    fs.rmSync(temporary, { recursive: true, force: true });
  }
});

test("the removed --recipes option is rejected", async () => {
  const stdout = { value: "", write(text) { this.value += text; } };
  const stderr = { value: "", write(text) { this.value += text; } };
  const code = await main(
    ["arm.json", "--recipes", "contract.json", "--diagnostics", "app.sarif"],
    stdout,
    stderr,
  );
  assert.equal(code, 2);
  assert.match(stderr.value, /unrecognized arguments: --recipes/u);
  assert.equal(stdout.value, "");
});
