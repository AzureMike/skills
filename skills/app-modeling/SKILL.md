---
name: app-modeling
description: >
  Analyze a source repository and generate a validated Radius application
  definition at .radius/app.bicep. Use when creating or updating app.bicep,
  onboarding an application to Radius, or selecting Radius compute and backing
  resource types from application source. Do not use for unrelated Azure
  Bicep or for deploying an existing model.
---

# Radius application modeling

Generate `.radius/app.bicep` and `.radius/bicepconfig.json` from repository
evidence. Do not use an existing app definition, evaluator output, golden file,
or bundled sample answer as generation evidence.

## Prerequisite

Confirm that the application has a clean-checkout Dockerfile at the repository
root or applicable service directory. Match `Dockerfile`, `Dockerfile.*`, or
`*.Dockerfile` case-insensitively.

If none exists, create no files and return:

> This repository does not contain a Dockerfile. The Radius app modeling skill
> currently supports only repositories that already include a Dockerfile for
> building the application service.

## Generate

Work directly in the current session. Do not invoke nested agents or
`run_evidence_loop.py`. Keep evidence and validation artifacts under
`<git-dir>/app-modeling-run/`; write only the final two files under `.radius/`.

### 1. Close source facts

Before inspecting Radius contracts or writing Bicep, select the minimum
production profile and write `source-facts.json`. Cite exact files and lines
for:

- production workloads, clean-checkout Dockerfile contexts or an exact
  source-corresponding first-party release image;
- entrypoints, arguments, listeners, routes, required configuration keys,
  parser/default behavior, and secrets;
- mandatory backing dependencies and their app-supported endpoint, port,
  protocol, TLS, authentication, and composite-value settings; and
- application paths whose data must persist across workload replacement.

Separate source defaults and development implementations from configurable
client capabilities. Exclude optional adapters, admin/debug services, proxies,
live reload, migrations, and optional persistence features. A development
server version or plaintext example is not a production constraint when cited
source supports the provider tuple. Conversely, never enable an optional
source feature merely to justify a Radius resource.

Use the repository's complete documented application profile, not merely the
Dockerfile's unset fallback behavior. When a canonical manifest combines the
production-buildable workload with a first-class backing service consumed by
that production code, retain the backing service and its native settings while
excluding development-only companion workloads individually. Do not replace it
with a local fallback database solely because its selector is unset in the
image. A user-facing HTTP workload requires a route unless source proves it is
internal-only.

Prove one usable primary feature path for every workload. A UI, worker,
producer, consumer, or API that exists to operate on a database, broker,
storage service, or model is not runnable merely because its process starts:
model at least one supported dependency instance and every required native
setting. Source fact closure is invalid if removing a declared dependency makes
the application's primary feature useless.

Inspect source in this order: Dockerfile and README; canonical deployment
manifests; configuration binding and client construction; then release metadata
only when packaging requires it. Stay bounded to four batched inspections. Do
not build the application, browse the network, or search outside the repository.

### 2. Resolve Radius contracts

Only after `source-facts.json` exists, map mandatory dependencies to the
smallest matching Radius type set. Query each selected type; do not enumerate
types or infer properties:

```bash
python3 "<skill-directory>/scripts/contract_query.py" bundle "<qualified-type>"
```

Also query the base application, container, image, secret, route, or persistent
volume types actually needed. Treat each returned schema, Recipe mapping,
managed-secret key, protocol profile, and extension reference as authoritative.
Recipe metadata describes the Environment implementation and outputs; it is
not an application resource property.

Never run `contract_query.py list` or `--help`, inspect the full contract JSON,
or query unused types. Use at most one `bundle` call per selected type. Resolve
release provenance locally with `git tag --points-at HEAD` and cited repository
release workflows; never call GitHub or a registry.

Mechanically reconcile every dependency as one tuple: native key, endpoint,
literal port, protocol, TLS, auth mechanism, username, secret, composite
grammar, connection edge, and persistence. Write the result to
`requirements.json` using `schemas/requirements.schema.json` before Bicep.

### 3. Author once

Write a fresh `.radius/app.bicep` and `.radius/bicepconfig.json`.

- Declare `extension radius`, `param environment string`, one application, and
  bind every resource to `environment` and the application ID.
- Build usable application Dockerfiles from the immutable checked-out source.
  If a Dockerfile requires an absent generated artifact, use only a verified
  exact first-party release image and exact Git tag for the same revision. A
  release tag is acceptable; never invent a digest or short-commit image tag.
- Configure every dependency through exact source-native settings plus a Radius
  connection with generic projection disabled when the source does not consume
  it.
- Put developer-supplied credentials through `@secure()` parameters and an
  authored Radius secret when a container consumes them. Bind Recipe-generated
  credentials directly from the verified managed secret and key.
- Construct secret-bearing composite values only at runtime. Preserve literal
  shell values such as Kafka's `$ConnectionString` while expanding only the
  secret environment variable. A container `env.value` never expands another
  environment variable: override the source image's command with `/bin/sh -c`,
  export the composite there, then `exec` the exact original entrypoint.
- Emit a persistent volume only for a source-cited path required by the
  selected profile. Omit optional schema inputs, including provider versions,
  when source evidence does not select them.

### 4. Validate and repair at most once

Run `scripts/validate_candidate.py` with the two `.radius` files,
`requirements.json`, source remote, commit, and application path. Treat every
Bicep diagnostic, including warnings, as failure. If validation fails, make one
finding-directed repair without changing validator-clean source bindings, then
run it once more.

Finally compare the exact validated files against `source-facts.json` and the
queried bundles. Reject missing or extra workloads/dependencies, invented
settings, changed process/listener behavior, insecure secrets, incomplete
protocol tuples, optional persistence, and missing graph edges. On any
remaining mismatch, remove the two output files and report the retained
artifact directory and blocker.

## Invariants

- Model production workloads only. Exclude development proxies, admin tools,
  live reload, tests, migrations, and optional adapters unless source proves
  they are required by the selected runnable profile.
- Preserve source-native ports, configuration names and values, protocols,
  authentication, secrets, routes, writable persistence, entrypoints, and
  dependency relationships.
- Build application images from an immutable clean-checkout source, or use an
  exact immutable first-party release image only when the source Dockerfile
  cannot build without an externally generated artifact.
- Use only bundled verified Radius types and properties.
- Bind every workload dependency with both its native runtime configuration
  and a Radius connection; disable generic projection when source does not
  consume it.
- Pass developer-supplied credentials through `@secure()` parameters. When a
  container consumes one, store it in an authored `Radius.Security/secrets`
  resource and use `valueFrom.secretKeyRef`. Bind Recipe-generated credentials
  directly from their verified managed secret name and key.
- Treat every Bicep diagnostic, including warnings, as failure.

## Response

On success, state that the two `.radius` files were created and briefly list
the modeled workloads, backing resources, routes, secrets, and persistence.
Do not commit, push, deploy, or open a pull request unless separately requested.
