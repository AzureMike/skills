# Custom resource types (generated on demand)

Use this when the application genuinely needs a backing service that has no
matching type in the allow-list in [Resource types](../SKILL.md#resource-types).
Rather than forcing an ill-fitting predefined type or stopping, generate a custom
type so the application can still be modeled and deployed.

Custom types are generated as part of modeling. Do not ask whether to generate
one — decide from the source's actual dependency. The initial scope is backing
services Radius can provision on **Azure**. If the service is not provisionable
on Azure, do not invent a type: report the unsupported dependency and stop for
that resource.

Every generated artifact lives in `.radius/`, beside `app.bicep` and
`bicepconfig.json`. Publishing an extension or a recipe to a registry is an OCI
push, not a git push.

Author every artifact from the templates below: copy the skeleton and fill only
the marked `<placeholders>`. The surrounding structure, resource types, API
versions, and wiring keys are fixed.

## When to generate a custom type

All three must hold:

- The application requires the backing service to function.
- No allow-list type fits. Do not stretch one to cover a different service.
- The service is provisionable on Azure.

Otherwise report the gap instead of generating anything.

## Namespace and naming

- Custom types always use the `Radius.Resources` namespace. Do not invent other
  namespaces and do not extend the predefined `Radius.*` ones.
- The type name is a lowerCamelCase plural noun, for example
  `Radius.Resources/azureServiceBusNamespaces`.
- Declare only the properties the application actually reads or writes, plus the
  base properties (`environment`, `application`) and the read-only outputs the
  application consumes. Do not invent properties nothing uses.

## 1. Author the schema: `.radius/custom-types.yaml`

The manifest shape is fixed; only the type name and `properties` vary. Model it
on an existing type such as `Data/mySqlDatabases/mySqlDatabases.yaml` in
`resource-types-contrib`.

```yaml
namespace: Radius.Resources
types:
  <typeNamePlural>:
    description: |
      <one-line description, plus a short app.bicep usage example>
    apiVersions:
      '2025-08-01-preview':
        schema:
          type: object
          properties:
            environment:
              type: string
              description: "(Required) The Radius Environment ID."
            application:
              type: string
              description: "(Optional) The Radius Application ID."
            <inputProperty>:
              type: string            # or integer / boolean
              description: "(Required|Optional) ..."
            <secretInput>:
              type: string
              x-radius-sensitive: true
              description: "(Required) ..."
            <readOnlyProperty>:
              type: string
              readOnly: true
              description: "Mapped from the recipe's <moduleOutput> output."
          required: [environment, <required inputs>]
```

- Developer inputs are plain typed properties. Use `enum: [...]` for a fixed set.
- Mark every sensitive input or output `x-radius-sensitive: true`.
- Read-only outputs set `readOnly: true` and are populated by the recipe. Never
  list them in `required`.
- One manifest may declare several types under `types:`.

A property declared `readOnly` but never mapped in step 4 compiles cleanly and
arrives null at deploy time. This is the same defect
[recipe-outputs.json](../assets/recipe-outputs.json) exists to prevent for
predefined types, and for a custom type the mapping is the only record of it.

## 2. Publish the extension locally

```sh
rad bicep publish-extension --from-file .radius/custom-types.yaml \
  --target .radius/custom-types.tgz
```

`--target` is a local file path, not an OCI reference, so the extension ships
beside `app.bicep` and needs no registry, no cluster, and no network.

## 3. Wire the extension into `.radius/bicepconfig.json`

Add an alias for the local tgz beside the existing `radius` alias, and keep
`radius`:

```json
{
  "experimentalFeaturesEnabled": { "extensibility": true },
  "extensions": {
    "radius": "br:biceptypes.azurecr.io/radius:<pinned-version>",
    "customTypes": "./custom-types.tgz"
  }
}
```

In `app.bicep`, declare `extension customTypes` in addition to
`extension radius`. The generated extension enforces the schema authored in step
1, so a misspelled property is a compile error naming the property it expected.

## 4. Provide a recipe

The recipe provisions the resource; the recipe pack in step 5 points at it
through a `source`. Prefer AVM.

### 4a. AVM path (preferred, exact match only)

An Azure Verified Module needs no authoring and no publishing. Reference it
directly as the recipe pack `source`, pinned to a version:

```
source: 'mcr.microsoft.com/bicep/avm/res/<service>/<resource>:<x.y.z>'
```

Use an AVM module only when a maintained module matches the required resource
exactly. A loose or approximate match is not acceptable — author a recipe
instead.

### 4b. Authored recipe (fallback): `.radius/<type>-recipe.bicep`

A recipe takes a single `context` object and returns a `result` object with
exactly three maps. Model it on an existing recipe such as
`Data/mySqlDatabases/recipes/kubernetes/bicep/kubernetes-mysql.bicep`.

```bicep
@description('Information about what resource is calling this Recipe. Generated by Radius.')
param context object

var <input> = context.resource.properties.<inputProperty>

// ...provision the Azure resource(s) here...

output result object = {
  resources: [
    '<provisioned resource id>'
  ]
  values: {
    <readOnlyProperty>: <...>
  }
  secrets: {
    <sensitiveProperty>: <...>
  }
}
```

Publish it to the container registry for the repository being modeled:

```sh
rad bicep publish --file .radius/<type>-recipe.bicep \
  --target br:ghcr.io/<owner>/<repo>/<recipe>:<tag>
```

- `<owner>/<repo>` is the repository being modeled.
- Pin an immutable `<tag>`. Never publish `latest`.
- The push needs a prior registry login with push permission. If it is not
  authorized, stop and report it rather than guessing credentials.

## 5. Author the recipe pack: `.radius/custom-recipe-pack.bicep`

The pack registers the recipe for the type. Its `recipes` map is keyed by the
full type name. Model it on `recipepack/azure/aks-recipepack.bicep`.

```bicep
extension radius

resource pack 'Radius.Core/recipePacks@2025-08-01-preview' = {
  name: '<pack-name>'
  properties: {
    recipes: {
      'Radius.Resources/<typeNamePlural>': {
        kind: 'bicep'
        source: '<MCR AVM path from 4a, or ghcr path from 4b>'
        parameters: {
          name: '{{context.resource.name}}'
          <moduleParam>: '{{context.resource.properties.<inputProperty>}}'
        }
        outputs: {
          <readOnlyProperty>: '<moduleOutputName>'
          secrets: {
            <sensitiveProperty>: '<moduleSecretOutputName>'
          }
        }
      }
    }
  }
}
```

- `parameters` values use Radius `{{context.resource...}}` templating, not Bicep
  expressions. This is how developer inputs reach the module.
- `outputs` maps recipe outputs to the type's `readOnly` property names.
  Non-secret outputs sit at the top level, sensitive ones under `secrets`. This
  shape is deliberately different from an authored recipe's own return value in
  4b, which is `output result object = { resources, values, secrets }`.
- An authored recipe already returns `values`/`secrets` keyed by the type's
  property names, so its mapping is usually the identity and can be omitted. An
  AVM module needs a real mapping because its output names differ, for example
  `host: 'fqdn'`.
- Never inline a per-type recipe in `app.bicep`. Registering the pack on the
  Environment is a deployment concern; modeling only writes the file.

## 6. Reference the type in `app.bicep`

Use `Radius.Resources/<typeNamePlural>@2025-08-01-preview` and wire it to the
workloads like any other backing service. Connections, environment projection,
and secret handling follow [authoring.md](authoring.md).

A connection injects `CONNECTION_<CONNECTION-NAME>_<PROPERTY-NAME>` for the
connected resource's properties, and a generated type gets that behavior for
free, so hand-written env vars carrying the same values are duplication.

## Artifacts (all in `.radius/`)

- `custom-types.yaml` — the schema, namespace `Radius.Resources`.
- `custom-types.tgz` — the locally published Bicep extension.
- `bicepconfig.json` — updated to alias that extension.
- `custom-recipe-pack.bicep` — the `Radius.Core/recipePacks` mapping.
- `<type>-recipe.bicep` — only when 4a was not used.

## Verify

Everything through step 6 runs offline. Only the 4b registry push and the deploy
touch anything outside `.radius/`.

- `app.bicep` compiles with both the `radius` and custom-types extensions, and
  the compile plus `check.py` run in [authoring.md](authoring.md) still applies
  unchanged — a custom resource nothing consumes is reported the same way, and
  a workload that consumes a `Radius.Resources/*` resource without declaring a
  connection to it is denied exactly like a predefined backing service.
- Every artifact above exists in `.radius/`.
- The pack `source` resolves: a pinned MCR AVM path, or a GHCR path that was
  actually published.
- `parameters` cover the module's required inputs, and `outputs` map every
  `readOnly` property of the type, sensitive ones under `secrets`.
- The type is Azure-provisionable. A non-Azure need was reported, not invented.
