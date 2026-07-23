---
name: app-modeling
description: >
  Analyze a source repository and generate or repair a Radius application
  definition in .radius/app.bicep. Use when modeling an app for Radius,
  choosing Radius resource types, or fixing schema and runtime wiring in an
  existing app.bicep. Do not use for generic Bicep, deployment, or unrelated
  infrastructure work.
---

# Radius application modeling

Generate a source-faithful Radius model through three checked artifacts:
temporary source facts, a temporary typed plan, and the final two `.radius`
files. The facts and plan never belong in the application repository.

Compilation is necessary, but it isn't the semantic test. A result also fails
when a required workload, resource, setting, secret, graph edge, protocol
field, source pin, or false-valued option is missing, or when an unrequested
resource or route appears.

## Boundaries

- Support repositories that contain a Dockerfile for every modeled
  application workload. If no usable Dockerfile exists, write nothing and
  return:

  > This repository does not contain a Dockerfile. The Radius app modeling skill currently supports only repositories that already include a Dockerfile for building the application image. Add a Dockerfile for the application service and run the skill again.

- Work from the requested source directory and exact checked-out commit.
- Write temporary files under a newly created system temporary directory,
  outside the Git worktree. Keep its absolute path for every command.
- Modify only `.radius/app.bicep` and `.radius/bicepconfig.json`.
- Don't deploy, create cloud resources, commit, push, or open a pull request
  unless the request explicitly asks for that separate action.
- Don't emit a partial model when required intent, source evidence, contract
  data, or validation is unresolved.
- The bundled contract supports the pinned Azure Recipe profile. Select
  `azure` when that profile is requested or implied. Stop on a different
  provider unless an exact bundled contract supports it.
- External exposure is opt-in. Use `none` unless the request or pinned source
  deployment profile explicitly requires an external route.

## Fixed inputs

Resolve this skill's directory, then use only these bundled files:

- `scripts/collect_source_facts.py`
- `scripts/validate_plan.py`
- `scripts/validate_output.py`
- `assets/plan.schema.json`
- `assets/radius-contract.json`
- `assets/radius-validation-extension.tgz`

The contract pins:

- `br:biceptypes.azurecr.io/radius:0.60.0-rc1`
- Radius API version `2025-08-01-preview`
- exact resource schemas and sensitivity
- exact Azure Recipe outputs and managed-secret keys
- provider-global naming rules
- client protocol profiles

Don't fetch `main`, use `latest`, substitute an older extension, or infer an
output from a similar type.

Treat the three scripts as black-box tools. Read the plan schema, contract,
source facts, and structured validation reports, but don't inspect validator
or extractor source while modeling an application.

## Workflow

### 1. Resolve scope and intent

Record:

- Git repository root and current commit
- selected source directory
- canonical repository URL
- one deployment profile
- provider
- exposure (`none`, `internal`, or `external`)
- every explicit workload role

The request is binding intent. Preserve named backend types, roles, workload
counts, native configuration keys, protocol values, connection names, and
false-valued options when the pinned source supports them.

If the repository remote is unavailable but the request names
`owner/repository`, use `https://github.com/owner/repository.git`. Never invent
a repository URL. If no profile is supplied, collect broad facts, inspect the
primary runnable configuration, select one complete profile, and rerun fact
collection with that profile. Leave materially different profiles unresolved
rather than combining them.

### 2. Collect verified source facts

Create one temporary directory with `mktemp -d`, then run:

```text
python3 <skill-dir>/scripts/collect_source_facts.py \
  --repo-root <absolute-git-root> \
  --source-root <absolute-selected-source-dir> \
  --repository-url <canonical-repository-url> \
  --profile <selected-profile> \
  --provider azure \
  --exposure <none|internal|external> \
  [--role <role> ...] \
  --output <temporary-dir>/source-facts.json
```

The extractor records Dockerfiles, root Compose services, dependencies,
direct and declarative environment reads, dynamic environment namespaces,
workload candidates, profile-focused source lines, and bounded source or
documentation blocks. Evidence IDs point to exact files and lines.
Files listed in `scan.readWarnings` weren't read. Don't restore, reconstruct,
or bypass access controls for them; continue only when they can't affect the
selected Dockerfile or runtime contract, otherwise stop and report the gap.

Treat `profileMatches`, `profileBlocks`, dependency hints, nested Compose
files, examples, and documentation as investigation leads. Confirm important
choices in production source. A package, optional adapter, test fixture, or
example doesn't make a service mandatory.

Review every ID in `reviewRequiredEvidence`:

- `included`: represented by a plan resource, workload, or native setting
- `excluded`: outside the selected production profile, with a source-backed
  reason
- `defaulted`: intentionally satisfied by a verified image or source default

Alternatives such as `*_FILE`, local development proxies, admin tools, and
optional databases need explicit dispositions. Don't silently drop them.
Required production operational secrets, including master keys and session,
cookie, JWT, signing, or encryption secrets, can't be excluded or defaulted:
bind each independent role from a secure parameter or managed secret.
When `environmentNamespaces` describes a generated key grammar, use that
source-native grammar and its exact delimiter rather than replacing it with a
different connection variable. A production namespace classified as
`purpose: connection` is the application's usable client connection, not an
optional metadata store: include it, choose one arbitrary instance ID, and
deliver every backing-service client setting through that same instance.

### 3. Write and validate the typed plan

Write `<temporary-dir>/plan.json` against `assets/plan.schema.json`. Use the
exact source commit, subdirectory, repository URL, and `factsDigest`.

The plan must contain:

- the selected profile and all three intent evidence IDs
- a disposition for every required evidence ID
- every parameter, including required provider-global names
- exactly one `Radius.Core/applications` resource
- every mandatory backing resource with exact type, API version, properties,
  outputs used, and evidence
- every executable workload role, immutable image/build, command, arguments,
  ports, native environment bindings, generated files, and exposure
- every backing-resource connection and complete client settings
- resolved ambiguities and no unsupported required capability

In the plan, `resource.properties` contains only type-specific Recipe inputs.
Don't put the standard Radius `environment` or `application` graph links
there; render those links in Bicep. `outputsUsed` contains only exact values
listed under that Recipe's `outputs` and consumed by a binding. Never list a
resource ID, connection name, schema field, or secret container name as a
Recipe output.

Use resource IDs as final Bicep symbolic names and workload IDs as final
container keys. Each nontrivial object and binding must cite real source or
intent evidence. An existing `app.bicep` or golden file is only a lead.

Binding kinds are:

- `literal`
- `parameter`
- `secureParameter`
- `resourceProperty`
- `managedSecret`
- `runtimeExpansion`

Every connection setting contains that typed `binding` plus one concrete
`consumer`. Use `environment` for a direct source-native variable, `source`
only for a source-backed constant or default, `startupEnvironment` for a
variable securely assembled by the startup script, and `generatedFile` for a
source-native config or bootstrap file. Use `processStdin` when the process
reads generated configuration directly from standard input. File and stdin
consumers need a `locator` that names the setting in the generated content.
`source` can't satisfy a setting named by the contract's
`runtimeRequiredClientSettings`; bind that value through environment,
startup, generated-file, or stdin delivery even when the source default is
the same.
When the protocol contract declares a runtime transform, copy its exact marker
to `consumer.transform` and implement it in startup logic. Declare every
intermediate workload environment variable in `consumer.inputs`. A setting
recorded only as connection metadata is invalid.
When a password or other secret becomes one component of a URL, URI, DSN, or
connection string, percent-encode it at runtime, declare the encoding function
or marker in `consumer.transform`, and use the transformed value in the final
source-native setting. A raw secret interpolation is invalid.

For every backing service, read both its `azureRecipeMappings` and
`protocolProfiles` entries from the bundled contract. Close the full client
tuple: subresource, endpoint transform, port, protocol, TLS, authentication,
secret source, and final source-native format. A provider connection string
isn't a client URL unless the source accepts that exact grammar.

For each workload, prove the image process, listener, native configuration,
dependency use, writable paths, and requested feature. A port declaration
doesn't configure the listener. Preserve the image entrypoint unless a
source-backed override is required, and remember that `command` replaces the
entrypoint while `args` replaces the default command.

Treat startup as incomplete when the requested feature still needs manual
setup. Database clients need a complete usable connection, message pipelines
need every requested producer and consumer role, model gateways need a usable
model route, and storage-backed services need both the remote filesystem and
any required account or folder bootstrap.

Before the first plan validation:

- copy `clientProfile`, setting names, punctuation, fixed values, alternatives,
  and transforms exactly from the selected protocol profile
- keep image `context` relative to `source.subdirectory`; don't duplicate the
  same path in both fields
- make generated-file templates appear verbatim in startup logic, bind every
  referenced variable, and use locators that occur in the template
- check that every `outputsUsed` value is both a pinned Recipe output and
  consumed by the plan

Run:

```text
python3 <skill-dir>/scripts/validate_plan.py \
  --facts <temporary-dir>/source-facts.json \
  --plan <temporary-dir>/plan.json \
  --contract <skill-dir>/assets/radius-contract.json \
  --output <temporary-dir>/plan-validation.json
```

Don't write Bicep until `valid` is `true`. Repair the plan at most twice.
Use only the structured validation report to repair all diagnostics in one
pass. Don't inspect validator source or the extension bundle, and don't create
probe plans or alternate binding variants. Repeated diagnostics or missing
contract data stop the run.

### 4. Render candidate files in temporary storage

Write temporary `app.bicep` and `bicepconfig.json`, not the repository copies.

`bicepconfig.json` must contain:

```json
{
  "experimentalFeaturesEnabled": {
    "extensibility": true
  },
  "extensions": {
    "radius": "br:biceptypes.azurecr.io/radius:0.60.0-rc1"
  }
}
```

Render only the validated plan:

- `extension radius`
- plan parameters, with `@secure()` on every secure input
- the application and planned backing resources
- one image resource per distinct source build
- all planned workloads, ports, environment bindings, files, and connections
- a route only for explicit external exposure

Every application and workload resource uses `environment`, and every backing
and compute resource references the application. Every provider-global
resource name uses its required, non-defaulted string parameter.

For a source build, use the exact repository, source subdirectory, build
context, Dockerfile, platform, and commit from the plan. The canonical form is
`git::<repository>.git//<subdirectory>?ref=<40-character-commit>`; omit the
`//<subdirectory>` part only when the effective context is the repository
root. Never use a branch, `HEAD`, or an unrelated release tag.

Explicitly bind every source-native runtime input. A `connections` block
records the graph edge but doesn't create arbitrary app variables. Set
`disableDefaultEnvVars: true` when native bindings are planned.

Developer-supplied credentials use one `@secure()` parameter for both the
sensitive resource input and direct container `env.value`. Recipe-generated
credentials use `valueFrom.secretKeyRef` with the exact
`<resource>.properties.secrets.name` and contract key. Never copy a Recipe
secret through an authored secret or interpolate a credential-bearing URL in
Bicep.

Runtime-generated secret files require a writable path, `umask 077` or
`chmod 0600`, explicit signal/exit cleanup, and a process lifecycle matching
the source. Confirm that the image contains every shell and executable used.
Escape literal client syntax such as Kafka's `$ConnectionString` so a shell
doesn't treat it as an environment variable.

Keep secrets out of process argument lists when the source supports
environment, import, or file-based delivery. Never emit masked placeholders
such as `******`, `<password>`, `REDACTED`, or `TODO` as executable
configuration.

Use an authored `Radius.Security/secrets` resource only for a genuine
application secret/configuration file or when an exact resource schema
requires `secretName`. It must not copy or combine a Recipe-generated secret.
When a credential-bearing URL or config can't be delivered as one exact
managed secret, bind source-native parts and compose it at runtime with safe
encoding; fail closed if neither path exists.

Preserve exact Boolean semantics. A string `'false'` is wrong when source uses
JavaScript truthiness such as `Boolean(value)`. Emit the representation proved
by the pinned parser.

Don't use `any()`, diagnostic suppression, mutable refs, explanatory Bicep
comments, unrequested resources, or convenience properties absent from the
contract.

### 5. Validate the candidate

Use a new empty output directory for each attempt:

```text
python3 <skill-dir>/scripts/validate_output.py \
  --plan <temporary-dir>/plan.json \
  --app-bicep <temporary-dir>/candidate/app.bicep \
  --bicepconfig <temporary-dir>/candidate/bicepconfig.json \
  --output-dir <temporary-dir>/output-validation-1
```

The validator compiles with the bundled extension, parses complete SARIF, and
fails every warning and error, including
`use-secure-value-for-secure-inputs`. It also checks plan closure, parameters,
resource and workload sets, names and properties, source paths and refs,
ports, native environment bindings, managed-secret keys, connections, routes,
startup-variable closure, generated-file safety, and mutable or suppressed
configuration.

Repair the candidate at most twice. Use a new output directory each time.
Use only the structured validation report and repair all diagnostics in one
pass; don't inspect validator source or probe alternate plans. Don't delete
required behavior to clear a diagnostic, and stop when the same diagnostic
fingerprint repeats.

### 6. Deliver only a passing result

After both validators report `valid: true`:

1. Create `.radius` if needed.
2. Copy the passing temporary files to `.radius/app.bicep` and
   `.radius/bicepconfig.json`.
3. Stage exactly those two files when the repository is on a branch.
4. Report the modeled workloads, backing resources, and validation outcome
   briefly.

If validation can't pass, leave the repository unchanged and surface the
complete structured diagnostics and the unresolved source or contract gap.

## Repairing an existing model

For a schema or modeling failure in an existing `.radius/app.bicep`, run the
same facts, plan, and output gates. Preserve correct existing intent, but
don't patch around a missing source, Recipe, or client contract. An
infrastructure provisioning failure belongs outside this skill.
