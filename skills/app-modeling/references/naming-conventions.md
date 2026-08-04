# Naming Conventions

| Element | Convention | Example |
|---|---|---|
| Bicep symbolic name | camelCase, descriptive | `todoApp`, `mysqlDb`, `todoContainer` |
| Data store symbolic name | `<engine>` + role suffix, camelCase | `mysqlDb`, `postgresDb`, `redisCache` |
| Secret symbolic name | `<engine>Secret` or `appSecrets`, camelCase | `appSecrets` |
| Resource `name` property | kebab-case, matches app/repo name | `'todo-list-app'`, `'my-database'` |
| Connection keys | lowercase, engine + role | `mysqldb`, `postgresdb`, `rediscache` |
| Application name | kebab-case, matches repository name | `'todo-list-app'` |
| Container keys (in `containers` map) | lowercase RFC 1123 label, at most 63 characters | `todo`, `frontend`, `api-worker` |
| Port keys (in `ports` map) | camelCase, describes the protocol/use | `web`, `http`, `grpc` |
| Volume keys (in `volumes` map) | camelCase, describes the data | `data`, `cache`, `secrets` |

## Rules

- Bicep symbolic names (left side of `=`) are always camelCase
- Resource `name` properties (string values) are always kebab-case
- Container keys are lowercase RFC 1123 labels of at most 63 characters; map keys inside `ports` and `volumes` are camelCase, and `connections` keys are lowercase (engine + role)
- Never use spaces or underscores; use hyphens only where the applicable convention permits them
- Explicit deployment-contract names and parameters take precedence over defaults. Preserve a documented resource-name parameter when a target Environment Recipe or verification couples it to a provider resource with naming or uniqueness constraints.

## Provider resource names

`app.bicep` names the Radius resource, not the underlying provider resource. Azure Recipes that need globally unique names derive them with a service prefix and `context.azure.resourceNameHash`; do not append `uniqueString(environment)` or encode provider naming rules in the Radius resource name.