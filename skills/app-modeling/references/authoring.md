# Authoring a Radius app.bicep

Write `.radius/app.bicep` and `.radius/bicepconfig.json`. Touch no other file.

The Bicep compiler already enforces resource shapes: property names, object maps
versus arrays, and required fields. Do not memorize those. Compile, read the
errors, and fix them. This file covers only what a clean compile cannot prove.

## Shape

```bicep
extension radius

param environment string

@secure()
param dbPassword string

resource app 'Radius.Core/applications@2025-08-01-preview' = {
  name: 'todo-list-app'
  properties: { environment: environment }
}

resource db 'Radius.Data/mySqlDatabases@2025-08-01-preview' = {
  name: 'mysql'
  properties: {
    environment: environment
    application: app.id
    database: 'todos'
    username: 'myadmin'
    password: dbPassword
  }
}

resource cache 'Radius.Data/redisCaches@2025-08-01-preview' = {
  name: 'cache'
  properties: {
    environment: environment
    application: app.id
  }
}

resource image 'Radius.Compute/containerImages@2025-08-01-preview' = {
  name: 'todo-image'
  properties: {
    environment: environment
    application: app.id
    tag: '5568077e0b1d1e2c3f4a5b6c7d8e9f0a1b2c3d4e'
    build: {
      source: 'git::https://github.com/org/repo.git?ref=5568077e0b1d1e2c3f4a5b6c7d8e9f0a1b2c3d4e'
    }
  }
}

resource web 'Radius.Compute/containers@2025-08-01-preview' = {
  name: 'todo-list-app'
  properties: {
    environment: environment
    application: app.id
    containers: {
      todo: {
        image: image.properties.imageReference
        ports: { web: { containerPort: 3000 } }
        env: {
          MYSQL_HOST: { value: db.properties.host }
          MYSQL_PASSWORD: { value: dbPassword }
          CACHE_URL: {
            valueFrom: {
              secretKeyRef: {
                secretName: cache.properties.secrets.name
                key: 'url'
              }
            }
          }
        }
      }
    }
    connections: {
      db: { source: db.id }
      cache: { source: cache.id }
    }
  }
}
```

Symbolic names are camelCase; `name` values are kebab-case, and a `name` becomes
a Kubernetes object name, so it must be lowercase letters, digits, and hyphens.

This example shows shape only. Derive every resource name, source URL, tag,
credential, port, and literal value from the repository being modeled.

## Rules the compiler cannot check

1. **Read only what the Recipe returns or the template sets.** Reading back a
   property this file itself sets on a resource (`db.properties.database` when
   you wrote `database: 'todos'`) is fine — the value is right there. A property
   the Recipe is expected to populate must appear in
   `assets/recipe-outputs.json`, which lists every property each Recipe actually
   sets; one declared in the type schema but absent from that list resolves to
   null at deploy time. PostgreSQL declares `port` and never sets it; use the
   provider's fixed `5432` instead.

2. **Bind managed secrets by reference.** When `recipe-outputs.json` lists a
   `secrets` entry, reach it through
   `valueFrom.secretKeyRef` with `secretName: <resource>.properties.secrets.name`
   and that exact key. Never write a placeholder string such as
   `'managedSecret:connectionString'`, and never read the key as a property.

3. **Pass a developer-supplied credential straight through.** A `@secure()`
   parameter goes to the resource property and to `env.value`. Radius encrypts
   and injects it. Do not author a secret resource to wrap a value you already
   hold as a parameter.

4. **Never author a secret that restates a Recipe output.** `Radius.Security/secrets`
   is for genuine application secrets and config files, or a schema-required
   `secretName`. It is not an adapter for an output shape you wanted.

5. **Do not interpolate a secret into a larger value.** Bicep composes at deploy
   time, which writes the combined value into deployment state. Bind the parts
   separately and let the application compose them, or bind a managed connection
   string whose format the source already accepts. When the application composes
   at runtime: a credential embedded in a URL must be URL-encoded and shell
   expansion is not encoding, and Kubernetes `$(VAR)` expansion sees only
   environment variables declared earlier in the map.

6. **Pin the build to an immutable ref.** `build.source` must carry
   `?ref=<40-char commit sha>`. Never `main`, `edge`, or `latest`. Set `tag` to
   that same commit. When the Dockerfile is not at the repo root, the context is
   `git::https://github.com/<org>/<repo>.git//<subdir>?ref=<sha>`, and
   `build.dockerfile` names a Dockerfile not called `Dockerfile`. If the build
   needs git metadata (BuildKit git contexts omit `.git`), set
   `build.args.BUILDKIT_CONTEXT_KEEP_GIT_DIR: '1'`.

7. **Model only what the selected startup path requires.** An installed package,
   optional extra, test fixture, or example is not evidence of a dependency.
   Every resource you declare must be consumed by a workload.

8. **Never delete required wiring to make compilation pass.** If a needed
   property, secret, or Recipe is missing, report the gap and stop.

9. **Wire the whole dependency contract, not just the host.** A `host` output is
   one field of a tuple the client needs: endpoint format, port, protocol
   version, TLS mode, auth mechanism, username, and secret key. Read the type's
   schema description for the provider's fixed values. Azure Event Hubs, for
   example, serves Kafka at `<host>.servicebus.windows.net:9093` over `SASL_SSL`
   with mechanism `PLAIN` and username `$ConnectionString`.

10. **Declare a connection for every resource a workload consumes.** A
    connection records the application-graph relationship this skill requires
    for every consumed backing resource. A direct resource reference already
    creates a deployment dependency edge and orders the resources; it is not a
    substitute for that relationship, and nothing fails at deploy time to tell
    you the connection is missing — the topology is just permanently wrong.
    By default Radius injects `CONNECTION_<CONNECTION-NAME>_<PROPERTY-NAME>`
    into the container for each non-sensitive property of the connected
    resource; `disableDefaultEnvVars` on the connection entry suppresses that
    injection. Connection-driven cloud RBAC applies only for supported IAM
    relationship kinds, not every portable-resource connection. Sensitive
    values are redacted on read and are **not** injected: bind those with
    `secretKeyRef` against `<resource>.properties.secrets.name`. So a resource
    with a secret needs both the connection and the explicit binding. Never
    hand-write a `CONNECTION_*` variable for a resource you have not connected
    — the application reads the whole set, and forging one member of it
    supplies one and silently omits the rest.

11. **`command` replaces the image ENTRYPOINT and `args` replaces its CMD.**
    Setting one and not the other silently drops the rest of the original
    command. Read the Dockerfile and keep the image's defaults unless the
    selected profile requires an override.

12. **`containerPort` publishes a port; it does not make the process listen.**
    If the application binds a port or address from configuration, set that
    configuration too, and make the two agree.

13. **Write env values in the representation the source parses.** Most parsers
    treat any non-empty string as true, so `'false'` enables the feature it was
    meant to disable. Omit the variable instead.

14. **Match lifecycle to the role.** Run-to-completion jobs need `restartPolicy`
    of `'OnFailure'` or `'Never'`, and anything that keeps state needs writable
    and persistent paths modeled.

15. **Do not declare a route unless the request asked to publish the
    application.** `containerPort` already makes the port reachable inside the
    cluster, and `rad run` port-forwards it to the developer. A
    `Radius.Compute/routes` resource publishes the workload outside the cluster,
    which is a deployment decision the repository cannot tell you. Having a
    browser interface is not the test — almost every one of these applications
    has one. The absence of a route is the normal shape of a model.

16. **Build application code from its Dockerfile; reserve published images for
    third-party components.** Match a backing service by wire protocol rather
    than package name, so MariaDB maps to MySQL and Valkey to Redis, but only
    when the client's protocol version, TLS, and auth match the Recipe endpoint.

17. **Starting is not working.** A container that boots into a login screen,
    placeholder config, or empty pipeline is not modeled. The selected profile's
    primary feature has to be reachable without manual setup.

18. **Model the service the application exists to operate on.** A UI for Kafka
    needs a Kafka cluster; a SQL client needs a database; a pipeline needs its
    broker. Turning on a mode that lets a human supply those coordinates later —
    a dynamic-config flag, a setup wizard, an admin form, a mounted config file
    someone still has to write — is the opposite of modeling the dependency. If
    a type in the catalog fits, declare it and wire it. If none fits, generate a
    custom type. Declaring none, and leaving the workload to be configured by
    hand, is the one option that is always wrong.

19. **Set explicit `build.platforms` unless the Dockerfile proves cross-builds.**
    Do not assume the Recipe's multi-platform default or QEMU emulation. A
    Dockerfile that runs target-architecture binaries in its build stages with
    no `BUILDPLATFORM`/`TARGETARCH` strategy builds one platform; set that one
    explicitly (for example `['linux/amd64']`).

20. **Deliver an external config file by mounting an authored secret.** When an
    unmodified image needs a config file, author it into
    `Radius.Security/secrets` `data` and mount it, rather than assuming the
    image has a shell to generate it at startup:

    ```bicep
    resource runtimeConfig 'Radius.Security/secrets@2025-08-01-preview' = {
      name: 'runtime-config'
      properties: {
        environment: environment
        application: app.id
        data: {
          #disable-next-line use-secure-value-for-secure-inputs
          'app.yaml': { value: '<complete config file content>' }
        }
      }
    }
    ```

    The container mounts it with `volumeMounts` (`volumeName`/`mountPath`), a
    `volumes` entry with `secretName: runtimeConfig.name`, and `args` pointing
    the process at the mounted path. The `#disable-next-line` directive is
    allowed **only** when the value is
    genuinely non-credential config content — the compile must otherwise be
    warning-free, and a real credential in the file body belongs in env via a
    `@secure()` parameter or `secretKeyRef`, referenced from the file when the
    format supports it. Generate config at startup instead only when the image
    verifiably contains the shell and tools and the destination is writable.

21. **Generated Bicep carries no commentary.** No explanatory comments and no
    `@description` decorators. The one exception is a functional
    `#disable-next-line` directive per rule 20.

22. **Choose an unsupported service version deterministically.** The type's
    version enum is the Recipe's contract. When the source pins a version the
    type does not offer, set the highest supported version that does not exceed
    the source's. When every supported version is newer, use the lowest one only
    when the repository proves that its protocol, TLS, and authentication remain
    compatible (rule 16); otherwise the predefined type does not fit, so
    generate a custom type (rule 18). Name either substitution in the summary,
    flagging the newer-server case as a compatibility risk. Do not pick a
    version by taste — two runs over the same repository must produce the same
    model.

23. **Never use a provider-reserved admin username.** A backing resource's
    `username` becomes the cloud database's admin login, and providers reject
    reserved names: Azure refuses `root`, `admin`, `administrator`, `guest`,
    `public`, and `azure_superuser`; PostgreSQL additionally refuses
    `postgres`, `azuresu`, `azure_pg_admin`, and any name starting with
    `pg_`; SQL Server refuses `sa` and other fixed logins.
    `recipe-outputs.json` carries each type's exact names and reserved
    prefixes. The common cases are exactly what a source derives — MySQL's
    `root`, PostgreSQL's default `postgres` — so substitute a neutral admin
    name such as `myadmin`, and pass the resource property (or the same
    literal) to the application's user variable so the two cannot drift.

24. **Give backing resources application-scoped names.** The Radius resource
    `name` becomes the cloud resource's name, and many of those are globally
    scoped — Service Bus namespaces, Redis Enterprise clusters, and
    flexible-server DNS labels. A bare service name (`redis`, `rabbitmq`,
    `mysql`) collides with any other deployment that picked the same one and
    fails with NameInUse. Prefix the application name (`<app>-redis`,
    `<app>-mysql`), keeping the kebab-case rule.

25. **Bind a published connection string; never compose one.** When the
    application reads a full connection string or URL and
    `recipe-outputs.json` lists one as a managed secret (`url`,
    `connectionString`), bind that exact key with `secretKeyRef`. A string
    composed from `host` and `port` omits the credential the provider
    requires — Redis rejects it with NOAUTH — and invites reads of properties
    the Recipe never sets (PostgreSQL `port`, rule 1). Composing one that
    embeds a secret also violates rule 5.

## Verify

Run the compile and check from step 5 of [SKILL.md](../SKILL.md#workflow). The
verdict is binary: `ALLOW` or `DENY`, with a stable `signature`. Every finding
is a provable defect — fix it and re-run; there is nothing to adjudicate. Every
compiler diagnostic in the SARIF denies, warnings included, so the compile must
be warning-free (the only sanctioned suppression is rule 20's
`#disable-next-line` for a non-credential config file). If the `signature`
repeats after a repair attempt, the fix is not converging — stop and report the
finding.

Decide every ambiguity yourself. When several runnable profiles exist, pick the
one the source documents most completely, model it, and say which you chose and
why. When a component has no Radius type, model the rest and report the gap.
Never ask the user a modeling question; an unasked question answered from
evidence is the job. The pull request is the only confirmation to ask for.

Compiling proves the shape. Only these checks and the source evidence prove it
runs.
