---
name: app-modeling
description: >
  Analyze a source code repository and generate a Radius application
  definition (.radius/app.bicep) that models the app's compute and backing
  services as Radius resource types. Use for: creating, generating, or
  updating a Radius application definition or app.bicep; modeling or
  onboarding an app or repo to Radius; determining which Radius resource
  types an app needs; repairing or fixing an app.bicep that failed to
  deploy because of a modeling or schema error. Do not use for: authoring
  generic or Azure Bicep unrelated to Radius, or deploying or running an
  already-modeled app. Resolves the configured Radius schemas and the
  application's runtime contract to produce validated, deployable output.
---

# Radius Application Modeling

Generate `.radius/app.bicep` from a source repository, compile it, and check the
compiled output for the defects a clean compile cannot rule out.

The Bicep compiler owns resource shape. Read its errors instead of memorizing
property names. This skill covers the three things it does not know: what a
Recipe actually returns, what the application actually reads, and which of those
two disagree.

## Prerequisites

This skill supports only repositories that already contain a Dockerfile for the
application image (repo root or the relevant service subdirectory; match
`Dockerfile`, `Dockerfile.*`, or `*.Dockerfile`, case-insensitively).

If no Dockerfile is present, stop before writing anything and return only:

> This repository does not contain a Dockerfile. The Radius app modeling skill
> currently supports only repositories that already include a Dockerfile for
> building the application image. Add a Dockerfile for the application service
> and run the skill again.

## Workflow

1. **Inventory the running application.** Read the Dockerfile, entrypoint,
   compose and Helm manifests, and the configuration the source actually reads —
   environment variables, CLI flags, and config files alike. Treat web, worker,
   producer, consumer, migration, and scheduler roles separately.

2. **Select one runnable deployment profile.** Model a backing service only when
   the selected startup path requires it. An import, optional extra, adapter,
   test fixture, or example elsewhere in the repository is not evidence. When
   several profiles are runnable, take the one the source documents most
   completely. When the request names a profile, backend, or version, treat that
   as the acceptance criteria and report the gap rather than falling back to a
   default the request did not ask for.

3. **Map each selected service** to a type from the allow-list below, reading
   its schema for property names and sensitivity. When nothing fits, generate a
   custom type per
   [custom-resource-types.md](references/custom-resource-types.md) instead of
   substituting an unrelated one.

4. **Write `.radius/bicepconfig.json`** first, then `.radius/app.bicep`
   following [authoring.md](references/authoring.md).

5. **Compile and check.**

   ```sh
   out=$(mktemp -d)
   bicep build .radius/app.bicep --diagnostics-format sarif --stdout \
     > "$out/app.json" 2> "$out/app.sarif"
   python3 scripts/check.py "$out/app.json" --diagnostics "$out/app.sarif"
   ```

   Use a fresh directory rather than fixed `/tmp` paths: two runs sharing
   `/tmp/app.json` would check each other's output.

   `bicep build` exits 0 on a warning, and both an unknown type or property and
   a credential headed for deployment state are reported as warnings, so the
   diagnostics have to be captured and passed in. `--diagnostics` is therefore
   required: without it a run could pass a model the compiler already objected
   to. Every diagnostic in the SARIF is a failure — the compile must be
   warning-free, and output that is not SARIF at all is itself a failure.
   `check.py` reads the compiled ARM JSON and returns `ALLOW` or `DENY` with a
   stable `signature`. Every finding is a provable defect: repair it and re-run.
   The `signature` covers the findings themselves, not their wording, so fixing
   any one of them moves it — if it repeats after a repair, the fix is not
   converging, so stop and report it. A `checker-unusable` finding means the
   checker could not run at all rather than that the model is wrong.

Decide every modeling ambiguity from the evidence. The pull request in
[Response](#response) is the only confirmation to ask for.

## Resource types

`Radius.Core/applications@2025-08-01-preview` is built into the `radius`
extension and has no schema file. Do not use `Applications.Core/applications`.

Everything else comes from `radius-project/resource-types-contrib`. Derive the
schema path from the type name rather than hardcoding it: the category is the
segment after `Radius.`, and the file is
`<Category>/<typeName>/<typeName>.yaml` — so `Radius.Data/mySqlDatabases` reads
from `Data/mySqlDatabases/mySqlDatabases.yaml`.

This is the allow-list of predefined types this skill emits when one fits:

| Need | Resource Type |
|---|---|
| Container images (build from Dockerfile) | `Radius.Compute/containerImages` |
| Containers | `Radius.Compute/containers` |
| MySQL | `Radius.Data/mySqlDatabases` |
| PostgreSQL | `Radius.Data/postgreSqlDatabases` |
| Neo4j | `Radius.Data/neo4jDatabases` |
| MongoDB | `Radius.Data/mongoDatabases` |
| Redis (cache) | `Radius.Data/redisCaches` |
| SQL Server | `Radius.Data/sqlServerDatabases` |
| Kafka (event streaming) | `Radius.Messaging/kafka` |
| RabbitMQ (message queue) | `Radius.Messaging/rabbitMQ` |
| AI model endpoint | `Radius.AI/models` |
| AI search | `Radius.AI/search` |
| Object storage | `Radius.Storage/objectStorage` |
| Persistent storage | `Radius.Compute/persistentVolumes` |
| Secrets | `Radius.Security/secrets` |

`Radius.Compute/routes` also exists, for publishing a workload outside the
cluster. Do not reach for it: a `containerPort` is already reachable in-cluster
and through `rad run`. Declare a route only when the request asks to publish the
application.

[recipe-outputs.json](assets/recipe-outputs.json) records what each of these
Recipes actually sets, which is not the same as what the schema declares.

Do not invent properties on these types and do not substitute one for another.
When a backing service the application genuinely needs has no matching type
above, neither stop nor force an ill-fitting one: generate a custom type under
the `Radius.Resources` namespace, following
[custom-resource-types.md](references/custom-resource-types.md), which is
authoritative for the schema, extension, recipe, and recipe-pack flow. That
scope is Azure for now — report a dependency that Azure cannot provision.

## bicepconfig.json

`app.bicep` cannot compile without a `bicepconfig.json` that resolves the
`radius` extension. Always write `.radius/bicepconfig.json` alongside
`app.bicep`, and never a config outside `.radius/`.

If one already exists there, correct what `app.bicep` needs and preserve the
rest. If a parent directory holds the config that would otherwise apply, seed
from its compatible settings and leave the parent file alone. With nothing to
carry forward, write:

```json
{
  "experimentalFeaturesEnabled": {
    "extensibility": true
  },
  "extensions": {
    "radius": "br:biceptypes.azurecr.io/radius:latest"
  }
}
```

Prefer an immutable reference when the user, an existing config, or the target
Environment supplies one. `radius:latest` drifts, and a clean compile against a
mutable artifact that disagrees with the deployed contract is not validation.

Declare `extension radius` in the Bicep. It covers every predefined namespace,
so per-namespace or per-type extensions are wrong. When the app uses a generated
`Radius.Resources/*` type, also declare the local custom-types extension
published into `.radius/` (see
[custom-resource-types.md](references/custom-resource-types.md)). Every alias
must resolve through `.radius/bicepconfig.json`.

## Response

1. Write `.radius/app.bicep` and `.radius/bicepconfig.json` to the current
   working branch, creating the branch first if it does not exist. Touch no
   other file.
2. Commit both, and push when a remote is configured.
3. Reply with a one-line intro naming the app, then a short summary of the
   resources identified, any judgment call worth flagging (profile choice, a
   `#disable-next-line` justification), and any unsupported component. Keep
   the raw analysis and file contents out of it.
4. Ask whether to open a pull request against the default branch. Do not open it
   without confirmation. On confirmation, use the title
   `Add Radius application definition` and the body
   `Add .radius/app.bicep and .radius/bicepconfig.json for <app-name>.`

## Repairing an existing app.bicep

When a deploy fails on a modeling or schema error — unknown type or API version,
missing property, invalid reference, wrong credential shape, or a parse error —
repair the file in place instead of regenerating it. This needs the deploy error
and any relevant logs; ask for them if they weren't provided.

First confirm the failure comes from the model at all. A recipe, Environment,
provider, or cluster failure will not be fixed by editing `app.bicep`, and a pod
that never becomes ready is not enough to tell those apart — read the events and
logs. Then re-resolve the implicated type against its schema and Recipe, apply
the fix under the same rules as authoring, and run the compile and check from
step 5 across the whole file, since one change can ripple to references
elsewhere. Report any collateral fixes made along the way.

If the same error recurs, the previous fix was insufficient and a different one
is needed. After a couple of distinct attempts fail, stop and surface the
problem rather than looping.
