import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import {
  AZURE_RECIPE_PACK_URL,
  RESOURCE_TYPES_CONTRIB_COMMIT,
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

test("shared checker fixture uses the current Recipe contract shape", () => {
  assert.deepEqual(contractFaults(fixtures.recipes[0]), []);
});

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
  assert.deepEqual(contract.recipeTypes, [
    "Radius.Compute/containers",
    "Radius.Data/postgreSqlDatabases",
  ]);
});

test("rejects a Recipe contract that loses output-less type membership", () => {
  assert.match(
    contractFaults({
      types: {
        "Radius.Data/postgreSqlDatabases": {
          source: "example.test/postgres:1",
          outputs: { host: "hostname" },
        },
      },
    }).join("\n"),
    /recipeTypes/u,
  );
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
  assert.match(AZURE_RECIPE_PACK_URL, new RegExp(RESOURCE_TYPES_CONTRIB_COMMIT, "u"));
  assert.doesNotMatch(AZURE_RECIPE_PACK_URL, /\/main\//u);

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

function arm(resources, parameters = {}) {
  return { parameters, resources };
}

function appResource() {
  return resource("Radius.Core/applications@2025-08-01-preview", {
    environment: "[parameters('environment')]",
  });
}

function containerResource(properties) {
  return resource("Radius.Compute/containers@2025-08-01-preview", {
    application: "[reference('app').id]",
    ...properties,
  });
}

function findingCodes(findings) {
  return findings.map(({ code }) => code);
}

test("rejects static container names Kubernetes cannot create", () => {
  const findings = check(
    arm({
      app: appResource(),
      workload: containerResource({
        containers: {
          volumeOwner: {
            image: "busybox@sha256:abc",
            initContainer: true,
          },
        },
      }),
    }),
    { types: {}, recipeTypes: [] },
  );
  assert.equal(findingCodes(findings).includes("invalid-container-name"), true);
});

test("allows lowercase RFC 1123 container names", () => {
  const findings = check(
    arm({
      app: appResource(),
      workload: containerResource({
        containers: {
          "volume-owner": {
            image: "busybox@sha256:abc",
            initContainer: true,
          },
        },
      }),
    }),
    { types: {}, recipeTypes: [] },
  );
  assert.equal(findingCodes(findings).includes("invalid-container-name"), false);
});

test("source builds allow an independent tag and default platforms", () => {
  const imageKind = "Radius.Compute/containerImages@2025-08-01-preview";
  const findings = check(
    arm({
      app: appResource(),
      image: resource(imageKind, {
        tag: "latest",
        build: {
          source:
            "git::https://github.com/example/app.git" +
            "?ref=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        },
      }),
      workload: containerResource({
        containers: {
          web: { image: "[reference('image').properties.imageReference]" },
        },
      }),
    }),
    { types: {}, recipeTypes: [] },
  );
  assert.equal(findingCodes(findings).includes("mutable-build-source"), false);
});

test("allows runtime interpolation from an authored secret-backed env", () => {
  const findings = check(
    arm({
      app: appResource(),
      config: resource("Radius.Security/secrets@2025-08-01-preview", {
        data: { token: { value: "placeholder" } },
      }),
      workload: containerResource({
        containers: {
          web: {
            image: "example.test/app@sha256:abc",
            env: {
              TOKEN: {
                valueFrom: {
                  secretKeyRef: {
                    secretName: "[reference('config').name]",
                    key: "token",
                  },
                },
              },
              CONFIG: { value: "prefix $(TOKEN)" },
            },
          },
        },
      }),
    }),
    { types: {}, recipeTypes: [] },
  );
  assert.equal(
    findingCodes(findings).includes("unresolvable-runtime-interpolation"),
    false,
  );
});

test("does not infer authored env ordering from a mutable container Recipe", () => {
  const findings = check(
    arm({
      app: appResource(),
      workload: containerResource({
        containers: {
          web: {
            image: "example.test/app@sha256:abc",
            env: {
              ALPHA: { value: "$(ZULU)" },
              ZULU: { value: "value" },
            },
          },
        },
      }),
    }),
    { types: {}, recipeTypes: [] },
  );
  assert.equal(
    findingCodes(findings).includes("unresolvable-runtime-interpolation"),
    false,
  );
});

test("allows runtime interpolation from an earlier authored plain env", () => {
  const findings = check(
    arm({
      app: appResource(),
      workload: containerResource({
        containers: {
          web: {
            image: "example.test/app@sha256:abc",
            env: {
              ALPHA: { value: "value" },
              ZULU: { value: "$(ALPHA)" },
            },
          },
        },
      }),
    }),
    { types: {}, recipeTypes: [] },
  );
  assert.equal(
    findingCodes(findings).includes("unresolvable-runtime-interpolation"),
    false,
  );
});

test("leaves unknown image-provided runtime env references alone", () => {
  const findings = check(
    arm({
      app: appResource(),
      workload: containerResource({
        containers: {
          web: {
            image: "example.test/app@sha256:abc",
            env: { CONFIG: { value: "prefix $(IMAGE_DEFAULT)" } },
          },
        },
      }),
    }),
    { types: {}, recipeTypes: [] },
  );
  assert.equal(
    findingCodes(findings).includes("unresolvable-runtime-interpolation"),
    false,
  );
});

test("does not infer connection env ordering from a mutable container Recipe", () => {
  const databaseKind = "Radius.Data/postgreSqlDatabases";
  const findings = check(
    arm({
      app: appResource(),
      database: resource(`${databaseKind}@2025-08-01-preview`, {}),
      workload: containerResource({
        containers: {
          web: {
            image: "example.test/app@sha256:abc",
            env: {
              DATABASE_URL: {
                value: "postgres://user@$(CONNECTION_DATABASE_HOST)/app",
              },
            },
          },
        },
        connections: {
          database: { source: "[reference('database').id]" },
        },
      }),
    }),
    {
      types: {
        [databaseKind]: {
          source: "example.test/postgres:1",
          outputs: { host: "hostname" },
        },
      },
      recipeTypes: [databaseKind],
    },
  );
  assert.equal(
    findingCodes(findings).includes("unresolvable-runtime-interpolation"),
    false,
  );
});

test("allows direct resource-property wiring without runtime interpolation", () => {
  const databaseKind = "Radius.Data/postgreSqlDatabases";
  const findings = check(
    arm({
      app: appResource(),
      database: resource(`${databaseKind}@2025-08-01-preview`, {}),
      workload: containerResource({
        containers: {
          web: {
            image: "example.test/app@sha256:abc",
            env: {
              DATABASE_HOST: {
                value: "[reference('database').properties.host]",
              },
            },
          },
        },
        connections: {
          database: { source: "[reference('database').id]" },
        },
      }),
    }),
    {
      types: {
        [databaseKind]: {
          source: "example.test/postgres:1",
          outputs: { host: "hostname" },
        },
      },
      recipeTypes: [databaseKind],
    },
  );
  assert.equal(
    findingCodes(findings).includes("unresolvable-runtime-interpolation"),
    false,
  );
});

test("denies interpolation when connection env injection is disabled", () => {
  const databaseKind = "Radius.Data/postgreSqlDatabases";
  const findings = check(
    arm({
      app: appResource(),
      database: resource(`${databaseKind}@2025-08-01-preview`, {}),
      workload: containerResource({
        containers: {
          web: {
            image: "example.test/app@sha256:abc",
            env: {
              DATABASE_HOST: { value: "$(CONNECTION_DATABASE_HOST)" },
            },
          },
        },
        connections: {
          database: {
            source: "[reference('database').id]",
            disableDefaultEnvVars: true,
          },
        },
      }),
    }),
    {
      types: {
        [databaseKind]: {
          source: "example.test/postgres:1",
          outputs: { host: "hostname" },
        },
      },
      recipeTypes: [databaseKind],
    },
  );
  assert.ok(
    findings.some(
      ({ code, message }) =>
        code === "unresolvable-runtime-interpolation" &&
        message.includes("disables default environment-variable injection"),
    ),
  );
});

test("allows an explicitly authored variable when connection injection is disabled", () => {
  const databaseKind = "Radius.Data/postgreSqlDatabases";
  const findings = check(
    arm({
      app: appResource(),
      database: resource(`${databaseKind}@2025-08-01-preview`, {}),
      workload: containerResource({
        containers: {
          web: {
            image: "example.test/app@sha256:abc",
            env: {
              CONNECTION_DATABASE_HOST: { value: "database.internal" },
              DATABASE_HOST: { value: "$(CONNECTION_DATABASE_HOST)" },
            },
          },
        },
        connections: {
          database: {
            source: "[reference('database').id]",
            disableDefaultEnvVars: true,
          },
        },
      }),
    }),
    {
      types: {
        [databaseKind]: {
          source: "example.test/postgres:1",
          outputs: { host: "hostname" },
        },
      },
      recipeTypes: [databaseKind],
    },
  );
  assert.equal(
    findingCodes(findings).includes("unresolvable-runtime-interpolation"),
    false,
  );
});

test("denies interpolation from a managed connection secret variable", () => {
  const databaseKind = "Radius.Data/postgreSqlDatabases";
  const findings = check(
    arm({
      app: appResource(),
      database: resource(`${databaseKind}@2025-08-01-preview`, {}),
      workload: containerResource({
        containers: {
          web: {
            image: "example.test/app@sha256:abc",
            env: {
              DATABASE_URL: {
                value:
                  "postgres://user:$(CONNECTION_DATABASE_PASSWORD)@database/app",
              },
            },
          },
        },
        connections: {
          database: { source: "[reference('database').id]" },
        },
      }),
    }),
    {
      types: {
        [databaseKind]: {
          source: "example.test/postgres:1",
          outputs: {
            host: "hostname",
            secrets: { password: "administratorPassword" },
          },
        },
      },
      recipeTypes: [databaseKind],
    },
  );
  assert.ok(
    findings.some(
      ({ code, message }) =>
        code === "unresolvable-runtime-interpolation" &&
        message.includes("do not inject secret outputs"),
    ),
  );
});

test("allows a secure parameter that may already be URL encoded", () => {
  const findings = check(
    arm(
      {
        app: appResource(),
        workload: containerResource({
          containers: {
            web: {
              image: "example.test/app@sha256:abc",
              env: {
                PASSWORD: { value: "[parameters('password')]" },
                DATABASE_URL: {
                  value: "postgres://user:$(PASSWORD)@database/app",
                },
              },
            },
          },
        }),
      },
      {
        environment: { type: "string" },
        password: { type: "securestring" },
      },
    ),
    { types: {}, recipeTypes: [] },
  );
  assert.equal(
    findingCodes(findings).includes("unencoded-secret-in-url"),
    false,
  );
});

test("does not treat an at-sign outside URL authority as userinfo", () => {
  const findings = check(
    arm(
      {
        app: appResource(),
        workload: containerResource({
          containers: {
            web: {
              image: "example.test/app@sha256:abc",
              env: {
                PASSWORD: { value: "[parameters('password')]" },
                CALLBACK_URL: {
                  value: "https://example.test/path/$(PASSWORD)@callback",
                },
              },
            },
          },
        }),
      },
      {
        environment: { type: "string" },
        password: { type: "securestring" },
      },
    ),
    { types: {}, recipeTypes: [] },
  );
  assert.equal(findingCodes(findings).includes("unencoded-secret-in-url"), false);
});

test("allows a complete URL bound directly through secretKeyRef", () => {
  const findings = check(
    arm({
      app: appResource(),
      config: resource("Radius.Security/secrets@2025-08-01-preview", {
        data: { url: { value: "placeholder" } },
      }),
      workload: containerResource({
        containers: {
          web: {
            image: "example.test/app@sha256:abc",
            env: {
              DATABASE_URL: {
                valueFrom: {
                  secretKeyRef: {
                    secretName: "[reference('config').name]",
                    key: "url",
                  },
                },
              },
            },
          },
        },
      }),
    }),
    { types: {}, recipeTypes: [] },
  );
  assert.equal(findingCodes(findings).includes("unencoded-secret-in-url"), false);
});

test("requires a connection for an output-less persistent-volume Recipe", () => {
  const volumeKind = "Radius.Compute/persistentVolumes";
  const model = arm({
    app: appResource(),
    volume: resource(`${volumeKind}@2025-08-01-preview`, {}),
    workload: containerResource({
      containers: { web: { image: "example.test/app@sha256:abc" } },
      volumes: {
        data: {
          persistentVolume: { resourceId: "[reference('volume').id]" },
        },
      },
    }),
  });
  const recipes = { types: {}, recipeTypes: [volumeKind] };
  const findings = check(model, recipes);
  assert.ok(
    findings.some(
      ({ code, path: findingPath }) =>
        code === "missing-connection" &&
        findingPath.includes("persistentVolume.resourceId"),
    ),
  );

  model.resources.workload.properties.properties.connections = {
    data: { source: "[reference('volume').id]" },
  };
  assert.equal(
    findingCodes(check(model, recipes)).includes("missing-connection"),
    false,
  );
});
