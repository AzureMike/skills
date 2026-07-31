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

Generate `.radius/app.bicep` from source evidence, then compile and check the
model. The detailed rules live in [authoring.md](references/authoring.md),
which includes a [worked example](references/authoring.md#worked-example).

## Prerequisites

This skill currently supports only repositories that already contain a Dockerfile for building the application image. Before doing anything else, confirm the target repository includes a Dockerfile for the application (repo root or the relevant service subdirectory; match `Dockerfile`, `Dockerfile.*`, or `*.Dockerfile`, case-insensitively).

If no Dockerfile is present, stop immediately. Do not generate `.radius/app.bicep` or `.radius/bicepconfig.json`, do not create or write to a branch, and do not commit or push. Return only this error:

> This repository does not contain a Dockerfile. The Radius app modeling skill currently supports only repositories that already include a Dockerfile for building the application image. Add a Dockerfile for the application service and run the skill again.

## Response and output

After the prerequisite passes:

1. Write only `.radius/app.bicep`, `.radius/bicepconfig.json`, and artifacts
   required by [custom-resource-types.md](references/custom-resource-types.md).
   Create the working branch first when it does not exist.
2. Commit the generated files and push when a remote is configured. If push
   authentication or authorization fails, report the committed branch and stop.
   Do not set up credentials.
3. Start the reply with a one-line introduction naming the application, then
   give a short summary of its modeled resources, profile choice, any
   `#disable-next-line` justification, compatibility risk, and unsupported
   component. Do not include raw source analysis or the full generated files.
4. Ask whether to open a pull request against the default branch. Never open it
   automatically. After confirmation, open a PR titled `Add Radius application
   definition` with body `Add the Radius application definition for
   <app-name>.`

## Workflow

After the [Prerequisites](#prerequisites) check:

1. Select one complete, runnable profile. Follow
   [profile selection](references/authoring.md#repository-exploration-and-profile-choice).
2. Start with source-owned manifests, Dockerfiles, build and startup scripts,
   then follow the configuration reads and client initialization they use.
   Inspect quickstarts and complete examples only when needed to choose or
   complete the profile. Keep a private evidence and trace note that follows the
   primary operation from input through workloads and dependencies to its
   observable result. Never put that note in Bicep.
3. Inventory each executable role and required service in that path. Extract its
   complete runtime contract: build, process, ports, configuration, secrets,
   storage, lifecycle, bootstrap, protocols, authentication, and feature flags.
   Explore migrations, seed data, identities, and protocol surfaces when the
   trace depends on them. Optional repository-wide extras are not dependencies.
4. Create or update `.radius/bicepconfig.json` first, then resolve every type,
   property, output, and managed secret against the exact target schema and
   Recipe. See [types and Recipes](references/authoring.md#type-and-recipe-resolution).
5. Prove a clean-checkout image path for every application workload. Build the
   application's code from its Dockerfile when possible, or report the
   packaging gap when the documented release-image exception does not apply.
6. Generate the model with the [file and naming
   rules](references/authoring.md#file-shape-and-naming), [runtime
   rules](references/authoring.md#runtime-configuration-and-lifecycle), and
   [connection rules](references/authoring.md#connections-and-secrets). Do not
   remove needed wiring to make validation pass.
7. Compile and check the whole model, fix every finding, and replay the private
   trace against the selected configuration. A process starting is not proof
   that the profile works.

## Compile and check

Use a fresh temporary directory so concurrent runs cannot read each other's
output:

```sh
out=$(mktemp -d)
bicep build .radius/app.bicep --diagnostics-format sarif --stdout \
  > "$out/app.json" 2> "$out/app.sarif" || true
node scripts/check.mjs "$out/app.json" --diagnostics "$out/app.sarif"
```

Run the checker even when `bicep build` exits nonzero. Every SARIF diagnostic,
including a warning, denies the model, and non-SARIF diagnostics output also
fails validation. `check.mjs` checks the compiled model for such things as
application shape, extensions, connections, secret bindings, Recipe outputs,
and build sources. Fix every `DENY` and rerun. If its stable `signature`
repeats after a repair, stop and report the finding. `checker-unusable` means
the validation service could not run, not that the model is valid or invalid.
Network, Recipe-pack compilation, and contract errors have that result.

The checker downloads and compiles the current Azure AKS Recipe Pack from
`radius-project/resource-types-contrib@main` with the repository's
`.radius/bicepconfig.json`. It has no bundled fallback or local contract
override, and it writes neither the pack nor its derived contract into the
repository. Its live boundary is the current upstream Azure `main`, not an
older, customized, AWS, or deployed Environment.

## Repairing an existing model

Repair an existing `.radius/app.bicep` in place when supplied deploy details
show an application-model or schema problem, including an unknown type,
property, API version, invalid reference, credential shape, or Bicep error. If
the deploy error and relevant logs are absent, request them before changing the
model.

Stop for infrastructure, Recipe, Environment, provider, or cluster failures:
editing `app.bicep` will not fix them. A pod that is not ready needs events and
logs to distinguish that class of failure from a model error in configuration,
listeners, credentials, or dependency wiring. For a model error, re-resolve the
exact schema and Recipe contract, repair the implicated resource and any other
clear rule violation, then rerun [Compile and check](#compile-and-check) and
the final checklist. Never remove required wiring as a repair. Return a brief
account of the change, including each collateral fix, and suggest redeploying;
stop after distinct repairs no longer make progress.

## Model shape and final checklist

The model contains exactly one application, uses `extension radius`, and keeps
provider modules, SKUs, regions, firewall or network policy, and Recipe output
mapping outside `app.bicep`. It adds the local custom-types extension only for
generated custom types, creates or updates `.radius/bicepconfig.json`, and
uses a parent configuration only as input. Routes appear only when public
exposure is requested.

Before returning, confirm that:

- the selected profile is complete, evidence-backed, and has a closed trace
- every workload and consumed backing service has its required runtime wiring
- types, properties, Recipe outputs, secret keys, and extension resolve to the
  exact target contract
- images build from a clean checkout or meet the documented immutable-image
  exception
- names, resource order, connections, secrets, persistent state, and lifecycle
  follow [authoring.md](references/authoring.md)
- the compile is warning-free, the checker returns `ALLOW`, and the trace
  reaches its observable result
