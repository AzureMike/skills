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

1. **Read only what the Recipe returns.** `assets/recipe-outputs.json` lists every
   property each Recipe actually sets. A property declared in the type schema but
   absent from that list resolves to null at deploy time. PostgreSQL declares
   `port` and never sets it; use the provider's fixed `5432` instead.

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
   string whose format the source already accepts.

6. **Pin the build to an immutable ref.** `build.source` must carry
   `?ref=<40-char commit sha>`. Never `main`, `edge`, or `latest`. Set `tag` to
   that same commit.

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
    connection is the only thing that creates the application-graph edge, and
    the only thing that binds cloud RBAC. Bicep already orders the deployment,
    so nothing fails at deploy time to tell you it is missing — the topology is
    just permanently wrong. Radius injects `CONNECTION_<CONNECTION-NAME>_<PROPERTY-NAME>`
    into the container for each non-sensitive property of the connected
    resource. Sensitive values are redacted on read and are **not** injected:
    bind those with `secretKeyRef` against `<resource>.properties.secrets.name`.
    So a resource with a secret needs both the connection and the explicit
    binding. Never hand-write a `CONNECTION_*` variable for a resource you have
    not connected — the application reads the whole set, and forging one member
    of it supplies one and silently omits the rest.

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

## Verify

```sh
bicep build .radius/app.bicep --diagnostics-format sarif --stdout \
  > /tmp/app.json 2> /tmp/app.sarif
python3 scripts/check.py /tmp/app.json --diagnostics /tmp/app.sarif
```

`bicep build` exits 0 on a warning, so a silent exit code proves nothing on its
own. An unknown type, an unknown property, a missing required property, a
`@secure()` parameter carrying a default, and a secret reaching an output are
all warnings. Capture the diagnostics and pass them in.

`check.py` returns `ALLOW`, `WARN`, or `DENY` with a stable `signature`.

- `DENY` — fix it yourself and re-run.
- `WARN` — advisory. Decide using the source evidence and note the tradeoff in
  your summary. Never stop to ask.
- If the `signature` repeats after a repair attempt, the fix is not converging.
  Stop and report the finding.

Decide every ambiguity yourself. When several runnable profiles exist, pick the
one the source documents most completely, model it, and say which you chose and
why. When a component has no Radius type, model the rest and report the gap.
Never ask the user a modeling question; an unasked question answered from
evidence is the job. The pull request is the only confirmation to ask for.

Compiling proves the shape. Only these checks and the source evidence prove it
runs.
