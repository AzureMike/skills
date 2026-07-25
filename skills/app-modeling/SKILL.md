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

After the prerequisite passes, do not inspect additional source or author
Bicep in this parent session. Run the packaged workflow exactly once:

```bash
python3 "<skill-directory>/scripts/run_evidence_loop.py" \
  --target "<absolute-application-directory>" \
  --request "<complete user request verbatim>"
```

The workflow first closes cited source facts without Radius context, then
selects the matching verified contract bundles mechanically. A separate writer
reconciles protocol, secret, composite, graph, and persistence requirements
before authoring. The parent compiles and validates the exact candidate, an
independent auditor reviews it against the source facts and contracts, and the
writer receives at most one finding-directed repair. The default internal
deadline is 360 seconds.

Treat the final JSON object as authoritative. On failure, report its reason and
artifact directory. Do not retry, inspect internal artifacts, edit the
repository, or fall back to manual authoring. Never delete the reported
artifact directory.

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
