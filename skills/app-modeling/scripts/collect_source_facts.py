#!/usr/bin/env python3

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tomllib
from urllib.parse import urlsplit, urlunsplit


SCHEMA_VERSION = 1
GENERATOR_VERSION = "1.4.0"
MAX_FILE_BYTES = 1024 * 1024
MAX_FILES = 12000
MAX_EVIDENCE_PER_ENV = 2
READ_WARNINGS = {}

SKIP_DIRS = {
    ".git",
    ".github",
    ".gradle",
    ".idea",
    ".next",
    ".nuxt",
    ".terraform",
    ".venv",
    ".vscode",
    "__pycache__",
    "bin",
    "build",
    "cookbook",
    "coverage",
    "dist",
    "env",
    "node_modules",
    "obj",
    "out",
    "target",
    "vendor",
    "venv",
}
NON_PRODUCTION_SEGMENTS = {
    "__tests__",
    "docs",
    "documentation",
    "e2e",
    "example",
    "examples",
    "fixtures",
    "spec",
    "specs",
    "test",
    "testdata",
    "tests",
}
SOURCE_EXTENSIONS = {
    ".c",
    ".cc",
    ".cpp",
    ".cs",
    ".go",
    ".h",
    ".hpp",
    ".java",
    ".js",
    ".jsx",
    ".kt",
    ".mjs",
    ".php",
    ".py",
    ".rb",
    ".rs",
    ".scala",
    ".sh",
    ".ts",
    ".tsx",
}
CONFIG_NAMES = {
    ".env",
    ".env.example",
    "compose.yaml",
    "compose.yml",
    "docker-compose.yaml",
    "docker-compose.yml",
    "helmfile.yaml",
    "helmfile.yml",
}
CONFIG_SUFFIXES = {".json", ".toml", ".yaml", ".yml"}
DOCUMENTATION_SUFFIXES = {".md", ".mdx", ".rst"}
COMPOSE_NAMES = {
    "compose.yaml",
    "compose.yml",
    "docker-compose.yaml",
    "docker-compose.yml",
}

ENV_PATTERNS = [
    ("node-dot", re.compile(r"\bprocess\.env\.([A-Za-z_][A-Za-z0-9_]*)"), None),
    (
        "node-index",
        re.compile(r"\bprocess\.env\[\s*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]\s*\]"),
        None,
    ),
    (
        "python-environ",
        re.compile(r"\bos\.environ\[\s*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]\s*\]"),
        True,
    ),
    (
        "python-get",
        re.compile(r"\bos\.environ\.get\(\s*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]"),
        None,
    ),
    (
        "getenv",
        re.compile(
            r"\b(?:os\.)?(?:Getenv|LookupEnv|getenv)\(\s*['\"]"
            r"([A-Za-z_][A-Za-z0-9_]*)['\"]"
        ),
        None,
    ),
    (
        "java",
        re.compile(r"\bSystem\.getenv\(\s*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]"),
        None,
    ),
    (
        "dotnet",
        re.compile(
            r"\bEnvironment\.GetEnvironmentVariable\(\s*['\"]"
            r"([A-Za-z_][A-Za-z0-9_]*)['\"]"
        ),
        None,
    ),
    (
        "ruby",
        re.compile(r"\bENV(?:\.fetch\(|\[)\s*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]"),
        None,
    ),
    (
        "rust",
        re.compile(r"\b(?:std::)?env::var\(\s*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]"),
        None,
    ),
    (
        "config-os-environ",
        re.compile(r"\bos\.environ/([A-Za-z_][A-Za-z0-9_]*)"),
        True,
    ),
    (
        "config-helper",
        re.compile(
            r"\b(?:get_secret_str|get_env|getPrefixedEnvVar)\(\s*['\"]"
            r"([A-Za-z_][A-Za-z0-9_]*)['\"]"
        ),
        None,
    ),
    (
        "declarative-env",
        re.compile(
            r"\b(?:envVar|env_var|environmentVariable|environment_variable)"
            r"\s*[:=]\s*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]"
        ),
        None,
    ),
    (
        "bound-env",
        re.compile(
            r"\b(?:BindEnv|bindEnv|bind_env)\s*\([^,\n]+,\s*['\"]"
            r"([A-Za-z_][A-Za-z0-9_]*)['\"]"
        ),
        None,
    ),
]
NODE_DESTRUCTURE = re.compile(
    r"(?:const|let|var)\s*\{([^{}]{1,500})\}\s*=\s*process\.env\b",
    re.MULTILINE,
)

CLIENT_HINTS = [
    (re.compile(r"(^|[-_/])(mysql2?|mariadb)([-_/]|$)", re.I), "mysql"),
    (re.compile(r"(^|[-_/])(pg|postgres|psycopg|npgsql)([-_/]|$)", re.I), "postgresql"),
    (re.compile(r"(^|[-_/])redis([-_/]|$)", re.I), "redis"),
    (re.compile(r"(^|[-_/])(mongo|mongodb)([-_/]|$)", re.I), "mongodb"),
    (re.compile(r"(^|[-_/])(mssql|tedious|sqlserver)([-_/]|$)", re.I), "sqlserver"),
    (re.compile(r"(azure.*search|search.*documents)", re.I), "search"),
    (re.compile(r"(openai|azure.*ai)", re.I), "ai-model"),
    (re.compile(r"(kafka|sarama)", re.I), "kafka"),
    (re.compile(r"(amqp|rabbit)", re.I), "amqp"),
    (re.compile(r"(azure.*storage.*blob|azblob|s3|object.*storage)", re.I), "object-storage"),
]

PROFILE_ENV_TOKENS = {
    "ai-model": {"AZURE_", "OPENAI_"},
    "amqp": {"AMQP", "RABBIT", "SERVICEBUS"},
    "kafka": {"EVENTHUB", "KAFKA", "SASL"},
    "mongodb": {"MONGO", "MONGODB"},
    "mysql": {"MYSQL"},
    "object-storage": {"AZURE_STORAGE", "AZ_", "BLOB", "S3", "STORAGE"},
    "postgresql": {"DATABASE_URL", "PG", "POSTGRES"},
    "redis": {"REDIS"},
    "search": {"SEARCH"},
    "sqlserver": {"MSSQL", "SQLSERVER"},
}
PROFILE_ALIASES = {
    "ai-model": {"ai model", "openai"},
    "amqp": {"amqp", "rabbitmq", "service bus"},
    "kafka": {"event hubs", "kafka"},
    "mongodb": {"mongo", "mongodb"},
    "mysql": {"mysql"},
    "object-storage": {"blob storage", "object storage", "s3"},
    "postgresql": {"postgres", "postgresql"},
    "redis": {"redis"},
    "search": {"search"},
    "sqlserver": {"mssql", "sql server", "sqlserver"},
}
PROFILE_PRIORITY_PATTERNS = {
    "ai-model": re.compile(
        r"^(?:AZURE_API_(?:BASE|KEY|VERSION)|OPENAI_API_(?:BASE|KEY|VERSION))$"
    ),
    "amqp": re.compile(r"^(?:AMQP|RABBITMQ|SERVICEBUS)_"),
    "kafka": re.compile(r"^(?:KAFKA_|EVENTHUB)"),
    "mongodb": re.compile(r"^(?:MONGO|MONGODB)"),
    "mysql": re.compile(r"^MYSQL_"),
    "object-storage": re.compile(r"^(?:AZURE_STORAGE|AZURE_BLOB|BLOB_|S3_|STORAGE_)"),
    "postgresql": re.compile(r"^(?:PG|POSTGRES|DATABASE_URL)"),
    "redis": re.compile(r"^(?:CONNECTION_)?REDIS"),
    "search": re.compile(r"^(?:CONNECTION_)?SEARCH"),
    "sqlserver": re.compile(r"^(?:MSSQL|SQLSERVER)"),
}
PROFILE_LINE_PATTERNS = {
    "ai-model": re.compile(r"\b(?:azure[_ -]?(?:openai|ai)|openai|api[_ -]?(?:base|key|version)|model)\b", re.I),
    "amqp": re.compile(r"\b(?:amqp[_-]?(?:0[_-]?9|1)?|rabbitmq|service[_ -]?bus)\b", re.I),
    "kafka": re.compile(r"\b(?:kafka|bootstrap[_ -]?servers?|sasl|jaas)\b", re.I),
    "mongodb": re.compile(r"\b(?:mongo(?:db)?|replica[_ -]?set)\b", re.I),
    "mysql": re.compile(r"\b(?:mysql|mariadb)\b", re.I),
    "object-storage": re.compile(r"(?:azure[_ -]?blob|azblob|object[_ -]?storage|\bs3\b|filesystem[_ -]?provider)", re.I),
    "postgresql": re.compile(r"\b(?:postgres(?:ql)?|pgpass|sslmode)\b", re.I),
    "redis": re.compile(r"\bredis\b", re.I),
    "search": re.compile(r"\b(?:azure[_ -]?search|search[_ -]?(?:endpoint|api[_ -]?key|index))\b", re.I),
    "sqlserver": re.compile(r"\b(?:mssql|sql[_ -]?server|tedious|trust[_ -]?server[_ -]?certificate)\b", re.I),
}
CONNECTION_LINE = re.compile(
    r"(?:account|auth|bootstrap|certificate|connection|container|database|"
    r"endpoint|encrypt|host|key|mechanism|password|port|protocol|queue|sasl|"
    r"secret|source[_ -]?address|ssl|target[_ -]?address|tls|token|url|user(?:name)?)",
    re.I,
)
PROVIDER_LINE_PATTERNS = {
    "azure": re.compile(r"(?:azure|event[_ -]?hubs?|service[_ -]?bus)|\.windows\.net\b", re.I),
    "kubernetes": re.compile(r"\b(?:kubernetes|k8s|serviceaccount)\b", re.I),
    "portable": re.compile(r"\b(?:portable|self[_ -]?hosted)\b", re.I),
}
SECRET_ENV_PATTERN = re.compile(
    r"(?:PASSWORD|PASSWD|PWD|SECRET|APIKEY|API_KEY|TOKEN|ACCOUNTKEY|"
    r"ACCOUNT_KEY|CONNECTIONSTRING|CONNECTION_STRING|MASTER_KEY|"
    r"COOKIESECRET|SESSIONSECRET|CLIENT_SECRET|PRIVATE_KEY|JAAS_CONFIG)$",
    re.I,
)
BOOTSTRAP_ENV_PATTERN = re.compile(
    r"(?:^|_)(?:ADMIN|AUTH|BOOTSTRAP|IMPORT|INIT|LOAD(?:DATA)?|SEED)(?:_|$)",
    re.I,
)
OPERATIONAL_SECRET_ROLES = (
    ("masterKey", re.compile(r"(?:^|_)MASTER_KEY$", re.I)),
    ("cookieSecret", re.compile(r"(?:^|_)COOKIE_?SECRET$", re.I)),
    ("sessionSecret", re.compile(r"(?:^|_)SESSION_?SECRET$", re.I)),
    ("jwtSecret", re.compile(r"(?:^|_)JWT_SECRET$", re.I)),
    ("signingKey", re.compile(r"(?:^|_)SIGNING_KEY$", re.I)),
    ("encryptionKey", re.compile(r"(?:^|_)ENCRYPTION_KEY$", re.I)),
)
DYNAMIC_ENV_PREFIX_PATTERNS = [
    re.compile(
        r"\.(?:startsWith|starts_with)\(\s*['\"]"
        r"([A-Z][A-Z0-9_]*_{1,})['\"]\s*\)"
    ),
    re.compile(
        r"\b(?:HasPrefix|has_prefix)\([^,\n]+,\s*['\"]"
        r"([A-Z][A-Z0-9_]*_{1,})['\"]\s*\)"
    ),
]
DYNAMIC_ENV_DELIMITER_PATTERN = re.compile(
    r"\.(?:split|Split)\(\s*['\"]([^'\"]{1,8})['\"]\s*\)"
)
CONNECTION_NAMESPACE_PATTERN = re.compile(
    r"\b(?:connections?|data[_ -]?sources?|clusters?)\b",
    re.I,
)


class Evidence:
    def __init__(self):
        self.items = []

    def add(self, kind, path, line, excerpt, scope="production"):
        evidence_id = f"E{len(self.items) + 1:05d}"
        self.items.append(
            {
                "id": evidence_id,
                "kind": kind,
                "path": path,
                "line": line,
                "excerpt": excerpt.strip()[:500],
                "scope": scope,
            }
        )
        return evidence_id

    def add_intent(self, kind, value):
        evidence_id = f"I{sum(item['id'].startswith('I') for item in self.items) + 1:04d}"
        self.items.append(
            {
                "id": evidence_id,
                "kind": kind,
                "path": None,
                "line": None,
                "excerpt": value,
                "scope": "user-intent",
            }
        )
        return evidence_id


def main():
    parser = argparse.ArgumentParser(
        description="Collect source-backed facts for Radius application modeling."
    )
    parser.add_argument("--repo-root", default=None)
    parser.add_argument("--source-root", default=os.getcwd())
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--repository-url",
        default=os.environ.get("APP_MODELING_REPOSITORY_URL"),
    )
    parser.add_argument(
        "--profile",
        default=os.environ.get("APP_MODELING_PROFILE"),
    )
    parser.add_argument(
        "--exposure",
        choices=("none", "internal", "external"),
        default=os.environ.get("APP_MODELING_EXPOSURE", "none"),
    )
    parser.add_argument(
        "--provider",
        choices=("azure", "kubernetes", "portable"),
        default=os.environ.get("APP_MODELING_PROVIDER"),
    )
    parser.add_argument("--role", action="append", default=[])
    args = parser.parse_args()

    source_root = Path(args.source_root).resolve()
    repo_root = (
        Path(args.repo_root).resolve()
        if args.repo_root
        else Path(git(["rev-parse", "--show-toplevel"], source_root)).resolve()
    )
    output = Path(args.output).resolve()
    require_within(source_root, repo_root, "source root")
    if is_within(output, repo_root):
        raise SystemExit("Source facts are temporary and the output must be outside the repository.")

    evidence = Evidence()
    files = list(walk_files(source_root))
    dockerfiles = collect_dockerfiles(files, source_root, evidence)
    if not dockerfiles:
        raise SystemExit(
            "This repository does not contain a Dockerfile. The Radius app modeling "
            "skill currently supports only repositories that already include a Dockerfile."
        )

    repository_url = args.repository_url or git_optional(
        ["config", "--get", "remote.origin.url"], repo_root
    )
    repository_url = sanitize_repository_url(repository_url)
    profile_evidence = (
        evidence.add_intent("deployment-profile", args.profile) if args.profile else None
    )
    exposure_evidence = evidence.add_intent("exposure", args.exposure)
    provider_evidence = (
        evidence.add_intent("deployment-provider", args.provider)
        if args.provider
        else None
    )
    role_evidence = [
        {"role": role, "evidence": evidence.add_intent("workload-role", role)}
        for role in args.role
    ]

    compose = collect_compose(files, source_root, evidence)
    dependencies = collect_dependencies(files, source_root, evidence)
    compose_env = {}
    for manifest in compose:
        for service in manifest["services"]:
            for name in service["environment"]:
                compose_env.setdefault(name, []).append(
                    {
                        "evidence": service["evidence"],
                        "scope": manifest["scope"],
                    }
                )
    environment, environment_total = collect_environment(
        files,
        source_root,
        evidence,
        args.profile,
        compose_env,
    )
    environment_namespaces = collect_environment_namespaces(
        files,
        source_root,
        evidence,
        args.profile,
    )
    config_files = collect_config_files(files, source_root, evidence)
    profile_matches = collect_profile_matches(
        files,
        source_root,
        evidence,
        args.profile,
        args.provider,
    )
    profile_blocks = collect_profile_blocks(
        files,
        source_root,
        evidence,
        args.profile,
        args.provider,
    )
    hints = derive_client_hints(dependencies)
    workloads = derive_workload_candidates(dockerfiles, compose, role_evidence)

    facts = {
        "schemaVersion": SCHEMA_VERSION,
        "generatorVersion": GENERATOR_VERSION,
        "repository": {
            "root": str(repo_root),
            "sourceRoot": str(source_root),
            "sourceSubdirectory": relative(source_root, repo_root),
            "commit": git(["rev-parse", "HEAD"], repo_root),
            "repositoryUrl": repository_url,
        },
        "intent": {
            "profile": args.profile,
            "profileEvidence": profile_evidence,
            "provider": args.provider,
            "providerEvidence": provider_evidence,
            "exposure": args.exposure,
            "exposureEvidence": exposure_evidence,
            "roles": role_evidence,
            "unresolved": [
                value
                for value, resolved in (
                    ("deployment-profile", args.profile),
                    ("deployment-provider", args.provider),
                )
                if not resolved
            ],
        },
        "scan": {
            "filesConsidered": len(files),
            "maxFiles": MAX_FILES,
            "maxFileBytes": MAX_FILE_BYTES,
            "truncated": len(files) >= MAX_FILES,
            "environmentVariablesFound": environment_total,
            "environmentVariablesRetained": len(environment),
            "environmentVariablesTruncated": environment_total > len(environment),
            "readWarnings": [
                {
                    "path": relative(path, source_root),
                    "error": error,
                }
                for path, error in sorted(
                    READ_WARNINGS.items(),
                    key=lambda item: str(item[0]),
                )
            ],
        },
        "dockerfiles": dockerfiles,
        "compose": compose,
        "dependencies": dependencies,
        "clientHints": hints,
        "environmentVariables": environment,
        "environmentNamespaces": environment_namespaces,
        "configFiles": config_files,
        "profileMatches": profile_matches,
        "profileBlocks": profile_blocks,
        "workloadCandidates": workloads,
        "reviewRequiredEvidence": sorted(
            {
                evidence_id
                for item in environment
                if item["reviewRequired"]
                for evidence_id in item["evidence"][:1]
            }
            | {
                item["evidence"]
                for item in environment_namespaces
                if item["reviewRequired"]
            }
            | {
                item["evidence"]
                for item in dockerfiles
                if item["scope"] == "production"
                and item["score"]
                == max(
                    (
                        candidate["score"]
                        for candidate in dockerfiles
                        if candidate["scope"] == "production"
                    ),
                    default=item["score"],
                )
            }
            | {
                service["evidence"]
                for manifest in compose
                if manifest["scope"] == "production"
                and len(Path(manifest["path"]).parts) == 1
                for service in manifest["services"]
            }
        ),
    }
    used_evidence = collect_evidence_ids(facts)
    facts["evidence"] = [
        item for item in evidence.items if item["id"] in used_evidence
    ]
    canonical = json.dumps(facts, sort_keys=True, separators=(",", ":")).encode()
    facts["factsDigest"] = f"sha256:{hashlib.sha256(canonical).hexdigest()}"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(facts, indent=2, sort_keys=True) + "\n")
    print(
        json.dumps(
            {
                "output": str(output),
                "dockerfiles": len(dockerfiles),
                "composeFiles": len(compose),
                "environmentVariables": len(environment),
                "clientHints": sorted({item["service"] for item in hints}),
                "readWarnings": len(READ_WARNINGS),
                "unresolved": facts["intent"]["unresolved"],
            },
            sort_keys=True,
        )
    )


def walk_files(root):
    count = 0
    for current, dirs, names in os.walk(root):
        dirs[:] = sorted(directory for directory in dirs if directory not in SKIP_DIRS)
        for name in sorted(names):
            path = Path(current) / name
            try:
                if path.is_symlink() or path.stat().st_size > MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            yield path
            count += 1
            if count >= MAX_FILES:
                return


def collect_dockerfiles(files, root, evidence):
    result = []
    for path in files:
        name = path.name.lower()
        if (
            not (
                name == "dockerfile"
                or name.startswith("dockerfile.")
                or name.endswith(".dockerfile")
            )
            or name.endswith(".dockerignore")
        ):
            continue
        text = read_text(path)
        instructions = docker_instructions(text)
        scope = classify_scope(path, root)
        evidence_id = evidence.add(
            "dockerfile",
            relative(path, root),
            1,
            instructions[0]["raw"] if instructions else path.name,
            scope,
        )
        result.append(
            {
                "path": relative(path, root),
                "scope": scope,
                "evidence": evidence_id,
                "from": [
                    item["value"].split()[0]
                    for item in instructions
                    if item["name"] == "FROM" and item["value"]
                ],
                "workdir": last_instruction(instructions, "WORKDIR"),
                "entrypoint": parse_command(last_instruction(instructions, "ENTRYPOINT")),
                "cmd": parse_command(last_instruction(instructions, "CMD")),
                "exposedPorts": parse_expose(instructions),
                "score": dockerfile_score(path, root, scope),
            }
        )
    return sorted(result, key=lambda item: (-item["score"], item["path"]))


def docker_instructions(text):
    logical = []
    buffer = ""
    start_line = 1
    for line_number, line in enumerate(text.splitlines(), 1):
        stripped = line.rstrip()
        if not buffer:
            start_line = line_number
        buffer += stripped[:-1] if stripped.endswith("\\") else stripped
        if stripped.endswith("\\"):
            buffer += " "
            continue
        match = re.match(r"^\s*([A-Za-z]+)\s+(.*)$", buffer)
        if match and not buffer.lstrip().startswith("#"):
            logical.append(
                {
                    "name": match.group(1).upper(),
                    "value": match.group(2).strip(),
                    "line": start_line,
                    "raw": buffer.strip(),
                }
            )
        buffer = ""
    return logical


def last_instruction(instructions, name):
    values = [item["value"] for item in instructions if item["name"] == name]
    return values[-1] if values else None


def parse_command(value):
    if value is None:
        return None
    if value.startswith("["):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return {"form": "exec", "value": parsed}
        except json.JSONDecodeError:
            pass
    return {"form": "shell", "value": value}


def parse_expose(instructions):
    ports = []
    for instruction in instructions:
        if instruction["name"] != "EXPOSE":
            continue
        for token in instruction["value"].split():
            match = re.match(r"^(\d+)(?:/(tcp|udp))?$", token)
            if match:
                ports.append(
                    {
                        "port": int(match.group(1)),
                        "protocol": match.group(2) or "tcp",
                        "line": instruction["line"],
                    }
                )
    return ports


def dockerfile_score(path, root, scope):
    depth = len(path.relative_to(root).parts)
    score = 100 - depth * 5
    if path.name == "Dockerfile":
        score += 15
    if scope != "production":
        score -= 100
    return score


def collect_compose(files, root, evidence):
    manifests = []
    for path in files:
        if path.name not in COMPOSE_NAMES and not re.match(r"^compose[.-].+\.ya?ml$", path.name):
            continue
        text = read_text(path)
        lines = text.splitlines()
        services_line = next(
            (
                index
                for index, line in enumerate(lines)
                if re.match(r"^\s*services\s*:\s*(?:#.*)?$", line)
            ),
            None,
        )
        if services_line is None:
            continue
        base_indent = indentation(lines[services_line])
        services = []
        index = services_line + 1
        while index < len(lines):
            line = lines[index]
            if line.strip() and indentation(line) <= base_indent:
                break
            service_match = re.match(rf"^\s{{{base_indent + 2}}}([A-Za-z0-9_.-]+)\s*:\s*$", line)
            if not service_match:
                index += 1
                continue
            name = service_match.group(1)
            start = index
            index += 1
            while index < len(lines):
                candidate = lines[index]
                if candidate.strip() and indentation(candidate) <= base_indent + 2:
                    break
                index += 1
            block = lines[start:index]
            evidence_id = evidence.add(
                "compose-service",
                relative(path, root),
                start + 1,
                line,
                classify_scope(path, root),
            )
            services.append(parse_compose_service(name, block, evidence_id))
        manifests.append(
            {
                "path": relative(path, root),
                "scope": classify_scope(path, root),
                "services": services,
            }
        )
    return manifests


def parse_compose_service(name, lines, evidence_id):
    joined = "\n".join(lines)
    image = capture_yaml_scalar(joined, "image")
    dockerfile = capture_yaml_scalar(joined, "dockerfile")
    command = capture_yaml_scalar(joined, "command")
    build = capture_yaml_scalar(joined, "context")
    env_names = capture_compose_environment(lines)
    ports = [
        int(match.group(1))
        for match in re.finditer(r"^\s*-\s*(?:['\"])?(?:\d+:)?(\d+)(?:/(?:tcp|udp))?", joined, re.MULTILINE)
    ]
    return {
        "name": name,
        "evidence": evidence_id,
        "image": image,
        "buildContext": build,
        "dockerfile": dockerfile,
        "command": command,
        "ports": sorted(set(ports)),
        "environment": env_names,
    }


def capture_compose_environment(lines):
    names = set()
    for index, line in enumerate(lines):
        if not re.match(r"^\s+environment\s*:\s*(?:#.*)?$", line):
            continue
        base_indent = indentation(line)
        for candidate in lines[index + 1 :]:
            if candidate.strip() and indentation(candidate) <= base_indent:
                break
            match = re.match(
                r"^\s+(?:-\s*)?([A-Za-z][A-Za-z0-9_.-]*)"
                r"(?:\s*:|\s*=|\s*(?:#.*)?$)",
                candidate,
            )
            if match:
                names.add(match.group(1))
    return sorted(names)


def capture_yaml_scalar(text, key):
    match = re.search(rf"^\s+{re.escape(key)}\s*:\s*['\"]?([^'\"#\n]+)", text, re.MULTILINE)
    return match.group(1).strip() if match else None


def collect_dependencies(files, root, evidence):
    result = []
    for path in files:
        relative_path = relative(path, root)
        scope = classify_scope(path, root)
        packages = []
        ecosystem = None
        if path.name == "package.json":
            try:
                data = json.loads(read_text(path))
                for section in ("dependencies", "optionalDependencies", "peerDependencies"):
                    packages.extend((data.get(section) or {}).keys())
                ecosystem = "npm"
            except json.JSONDecodeError:
                continue
        elif path.name == "pyproject.toml":
            try:
                data = tomllib.loads(read_text(path))
                packages.extend(data.get("project", {}).get("dependencies", []))
                ecosystem = "python"
            except (tomllib.TOMLDecodeError, AttributeError):
                continue
        elif path.name.startswith("requirements") and path.suffix == ".txt":
            packages.extend(
                line.split(";", 1)[0].strip()
                for line in read_text(path).splitlines()
                if line.strip() and not line.lstrip().startswith(("#", "-"))
            )
            ecosystem = "python"
        elif path.name == "go.mod":
            packages.extend(
                match.group(1)
                for match in re.finditer(
                    r"^\s*([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+)\s+v\d",
                    read_text(path),
                    re.MULTILINE,
                )
            )
            ecosystem = "go"
        elif path.name == "pom.xml":
            packages.extend(
                f"{group}:{artifact}"
                for group, artifact in re.findall(
                    r"<groupId>([^<]+)</groupId>\s*<artifactId>([^<]+)</artifactId>",
                    read_text(path),
                )
            )
            ecosystem = "maven"
        if not ecosystem or not packages:
            continue
        if scope != "production" or len(Path(relative_path).parts) > 5:
            continue
        normalized_packages = sorted(set(packages))
        evidence_id = evidence.add(
            "dependency-manifest",
            relative_path,
            1,
            path.name,
            scope,
        )
        result.append(
            {
                "path": relative_path,
                "scope": scope,
                "ecosystem": ecosystem,
                "packageCount": len(normalized_packages),
                "packages": normalized_packages[:300],
                "evidence": evidence_id,
            }
        )
        if len(result) >= 40:
            break
    return result


def derive_client_hints(dependencies):
    hints = []
    seen = set()
    for manifest in dependencies:
        if manifest["scope"] != "production":
            continue
        for package in manifest["packages"]:
            for pattern, service in CLIENT_HINTS:
                if pattern.search(package) and (service, package, manifest["path"]) not in seen:
                    seen.add((service, package, manifest["path"]))
                    hints.append(
                        {
                            "service": service,
                            "package": package,
                            "manifest": manifest["path"],
                            "evidence": manifest["evidence"],
                            "confidence": "candidate",
                        }
                    )
    return sorted(hints, key=lambda item: (item["service"], item["package"]))


def collect_environment(files, root, evidence, profile, explicit_env):
    found = {}
    focus_tokens = profile_tokens(profile)
    selected_services = profile_services(profile)
    for path in files:
        if path.suffix.lower() not in SOURCE_EXTENSIONS | {".yaml", ".yml"}:
            continue
        text = read_text(path)
        scope = classify_scope(path, root)
        relative_path = relative(path, root)
        lines = text.splitlines()
        for parser_name, pattern, required_hint in ENV_PATTERNS:
            for match in pattern.finditer(text):
                name = match.group(1)
                line_number = text.count("\n", 0, match.start()) + 1
                line = lines[line_number - 1] if line_number <= len(lines) else ""
                required = infer_required(line, parser_name, required_hint)
                add_environment_fact(
                    found,
                    name,
                    required,
                    evidence,
                    relative_path,
                    line_number,
                    line,
                    scope,
                    parser_name,
                    focus_tokens,
                )
        for match in NODE_DESTRUCTURE.finditer(text):
            line_number = text.count("\n", 0, match.start()) + 1
            line = lines[line_number - 1] if line_number <= len(lines) else ""
            for part in match.group(1).split(","):
                name = part.split(":", 1)[0].split("=", 1)[0].strip()
                if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name):
                    continue
                add_environment_fact(
                    found,
                    name,
                    None,
                    evidence,
                    relative_path,
                    line_number,
                    line,
                    scope,
                    "node-destructure",
                    focus_tokens,
                )
    for name, declarations in explicit_env.items():
        item = found.setdefault(
            name,
            {
                "name": name,
                "evidence": [],
                "occurrences": 0,
                "requiredValues": set(),
                "scopes": set(),
                "parsers": set(),
            },
        )
        item["occurrences"] += len(declarations)
        item["requiredValues"].add(None)
        item["parsers"].add("compose")
        for declaration in declarations:
            item["scopes"].add(declaration["scope"])
            if (
                declaration["evidence"] not in item["evidence"]
                and len(item["evidence"]) < MAX_EVIDENCE_PER_ENV
            ):
                item["evidence"].append(declaration["evidence"])
    result = []
    for name, item in sorted(found.items()):
        required_values = item.pop("requiredValues")
        item["required"] = True if True in required_values else False if required_values == {False} else None
        item["profileFocused"] = any(token in name.upper() for token in focus_tokens)
        item["profilePriority"] = any(
            PROFILE_PRIORITY_PATTERNS[service].search(name.upper())
            for service in selected_services
        )
        item["declaredByCompose"] = name in explicit_env
        item["secretLike"] = bool(SECRET_ENV_PATTERN.search(name))
        item["bootstrapLike"] = bool(BOOTSTRAP_ENV_PATTERN.search(name))
        item["operationalSecretRole"] = next(
            (
                role
                for role, pattern in OPERATIONAL_SECRET_ROLES
                if pattern.search(name)
            ),
            None,
        )
        item["reviewRequired"] = "production" in item["scopes"] and (
            item["profilePriority"]
            or item["declaredByCompose"]
            or item["bootstrapLike"]
            or item["operationalSecretRole"] is not None
            or (item["secretLike"] and item["required"] is True)
            or (not selected_services and item["required"] is True)
        )
        item["scopes"] = sorted(item["scopes"])
        item["parsers"] = sorted(item["parsers"])
        result.append(item)
    result.sort(
        key=lambda item: (
            not item["reviewRequired"],
            not item["profilePriority"],
            not item["profileFocused"],
            not item["declaredByCompose"],
            item["required"] is not True,
            -item["occurrences"],
            item["name"],
        )
    )
    retained = [item for item in result if item["reviewRequired"]][:60]
    retained_names = {item["name"] for item in retained}
    for item in result:
        if len(retained) >= 80:
            break
        if item["name"] in retained_names or "production" not in item["scopes"]:
            continue
        retained.append(item)
        retained_names.add(item["name"])
    retained.sort(key=lambda item: item["name"])
    return retained, len(result)


def collect_environment_namespaces(files, root, evidence, profile):
    if not profile:
        return []
    namespaces = []
    seen = set()
    for path in files:
        if path.suffix.lower() not in SOURCE_EXTENSIONS:
            continue
        text = read_text(path)
        lines = text.splitlines()
        relative_path = relative(path, root)
        scope = classify_scope(path, root)
        for pattern in DYNAMIC_ENV_PREFIX_PATTERNS:
            for match in pattern.finditer(text):
                prefix = match.group(1)
                line_number = text.count("\n", 0, match.start()) + 1
                start = max(0, line_number - 8)
                end = min(len(lines), line_number + 8)
                excerpt = "\n".join(lines[start:end]).strip()
                delimiter_match = DYNAMIC_ENV_DELIMITER_PATTERN.search(excerpt)
                delimiter = (
                    delimiter_match.group(1)
                    if delimiter_match
                    else "__"
                    if prefix.endswith("__")
                    else "_"
                    if prefix.endswith("_")
                    else None
                )
                key = (relative_path, prefix, delimiter)
                if key in seen:
                    continue
                seen.add(key)
                namespaces.append(
                    {
                        "prefix": prefix,
                        "delimiter": delimiter,
                        "purpose": (
                            "connection"
                            if CONNECTION_NAMESPACE_PATTERN.search(
                                f"{prefix.replace('_', ' ')} {excerpt}"
                            )
                            else "configuration"
                        ),
                        "path": relative_path,
                        "line": line_number,
                        "scope": scope,
                        "reviewRequired": scope == "production",
                        "evidence": evidence.add(
                            "environment-namespace",
                            relative_path,
                            line_number,
                            excerpt,
                            scope,
                        ),
                    }
                )
    namespaces.sort(
        key=lambda item: (
            not item["reviewRequired"],
            item["path"],
            item["line"],
            item["prefix"],
        )
    )
    return namespaces[:20]


def add_environment_fact(
    found,
    name,
    required,
    evidence,
    path,
    line,
    excerpt,
    scope,
    parser_name,
    focus_tokens,
):
    item = found.setdefault(
        name,
        {
            "name": name,
            "evidence": [],
            "occurrences": 0,
            "requiredValues": set(),
            "scopes": set(),
            "parsers": set(),
        },
    )
    item["occurrences"] += 1
    item["requiredValues"].add(required)
    item["scopes"].add(scope)
    item["parsers"].add(parser_name)
    if len(item["evidence"]) < MAX_EVIDENCE_PER_ENV:
        item["evidence"].append(
            evidence.add("environment-read", path, line, excerpt, scope)
        )


def infer_required(line, parser_name, hint):
    if hint is True:
        return True
    if re.search(r"\|\||\?\?|getenvDefault|\.get\([^,]+,", line):
        return False
    if re.search(r"\bif\s*\(|\?\s*|&&", line):
        return None
    if parser_name in {"python-environ"}:
        return True
    return None


def profile_tokens(profile):
    if not profile:
        return set()
    tokens = set()
    for service in profile_services(profile):
        tokens.update(PROFILE_ENV_TOKENS[service])
    return tokens


def profile_services(profile):
    if not profile:
        return set()
    normalized = profile.lower()
    return {
        service
        for service, aliases in PROFILE_ALIASES.items()
        if any(alias in normalized for alias in aliases)
    }


def collect_config_files(files, root, evidence):
    result = []
    for path in files:
        lower = path.name.lower()
        if (
            lower not in CONFIG_NAMES
            and path.suffix.lower() not in CONFIG_SUFFIXES
            and "config" not in lower
        ):
            continue
        scope = classify_scope(path, root)
        if scope != "production":
            continue
        relative_path = relative(path, root)
        result.append(
            {
                "path": relative_path,
                "evidence": evidence.add(
                    "configuration-file",
                    relative_path,
                    1,
                    path.name,
                    scope,
                ),
            }
        )
        if len(result) >= 60:
            break
    return result


def collect_profile_matches(files, root, evidence, profile, provider):
    services = profile_services(profile)
    if not services:
        return []
    matches = []
    for path in files:
        lower_name = path.name.lower()
        if (
            path.suffix.lower()
            not in SOURCE_EXTENSIONS | CONFIG_SUFFIXES | DOCUMENTATION_SUFFIXES
            and lower_name not in CONFIG_NAMES
        ):
            continue
        if (
            lower_name.endswith((".lock", ".min.js", ".map"))
            or "lock" in lower_name
            or lower_name in {"package.json", "tsconfig.json"}
        ):
            continue
        scope = classify_scope(path, root)
        relative_path = relative(path, root)
        path_text = relative_path.lower()
        path_services = [
            service
            for service in services
            if PROFILE_LINE_PATTERNS[service].search(path_text)
        ]
        context_services = []
        context_remaining = 0
        provider_context_remaining = 0
        for line_number, line in enumerate(read_text(path).splitlines(), 1):
            excerpt = line.strip()
            if not excerpt or len(excerpt) > 1000:
                context_remaining = max(0, context_remaining - 1)
                provider_context_remaining = max(0, provider_context_remaining - 1)
                continue
            direct_services = [
                service
                for service in services
                if PROFILE_LINE_PATTERNS[service].search(excerpt)
            ]
            matching_services = direct_services
            if not matching_services and path_services and CONNECTION_LINE.search(excerpt):
                matching_services = path_services
            if (
                not matching_services
                and context_remaining > 0
                and CONNECTION_LINE.search(excerpt)
            ):
                matching_services = context_services
            if not matching_services:
                context_remaining = max(0, context_remaining - 1)
                provider_context_remaining = max(0, provider_context_remaining - 1)
                continue
            if direct_services:
                context_services = direct_services
                context_remaining = 24
            else:
                context_remaining = max(0, context_remaining - 1)
            provider_direct = bool(
                provider and PROVIDER_LINE_PATTERNS[provider].search(excerpt)
            )
            if provider_direct:
                provider_context_remaining = 24
            else:
                provider_context_remaining = max(0, provider_context_remaining - 1)
            score = 2
            if CONNECTION_LINE.search(excerpt):
                score += 5
            if set(matching_services) & set(path_services):
                score += 3
            if path.suffix.lower() in SOURCE_EXTENSIONS:
                score += 2
            if path.suffix.lower() in CONFIG_SUFFIXES:
                score += 3
            if re.search(r"(?:config|connection|client|driver|model)", path_text):
                score += 2
            if "openapi" in path_text:
                score += 2
            if provider_direct or provider_context_remaining > 0:
                score += 3
            if re.search(r"(?:docs?|documentation|example)", path_text):
                score -= 3
            if scope != "production":
                score -= 2
            if re.search(r"(?:frontend|client|locales|static)", path_text):
                score -= 4
            if re.search(r"(?:contract|swagger)", path_text):
                score -= 5
            if "/src/main/" in f"/{path_text}":
                score += 2
            if re.match(r"^(?:import|package)\b", excerpt):
                score -= 6
            if re.match(r"^(?://|#|\*)", excerpt):
                score -= 3
            matches.append(
                {
                    "services": sorted(matching_services),
                    "path": relative_path,
                    "line": line_number,
                    "excerpt": excerpt[:500],
                    "score": score,
                    "scope": scope,
                    "evidence": None,
                }
            )
    matches.sort(key=lambda item: (-item["score"], item["path"], item["line"]))
    selected = []
    path_counts = {}
    buckets = (
        (
            lambda item: item["scope"] == "production",
            48,
        ),
        (
            lambda item: item["scope"] != "production"
            and Path(item["path"]).suffix.lower() in CONFIG_SUFFIXES,
            8,
        ),
        (
            lambda item: item["scope"] != "production"
            and Path(item["path"]).suffix.lower() in DOCUMENTATION_SUFFIXES,
            8,
        ),
    )
    for includes, limit in buckets:
        bucket_count = 0
        for item in matches:
            if not includes(item) or path_counts.get(item["path"], 0) >= 8:
                continue
            add_profile_match(item, selected, path_counts, evidence)
            bucket_count += 1
            if bucket_count >= limit:
                break
    for item in matches:
        if len(selected) >= 64:
            break
        if item in selected or path_counts.get(item["path"], 0) >= 8:
            continue
        add_profile_match(item, selected, path_counts, evidence)
    return selected


def add_profile_match(item, selected, path_counts, evidence):
    item["evidence"] = evidence.add(
        "profile-match",
        item["path"],
        item["line"],
        item["excerpt"],
        item["scope"],
    )
    selected.append(item)
    path_counts[item["path"]] = path_counts.get(item["path"], 0) + 1


def collect_profile_blocks(files, root, evidence, profile, provider):
    services = profile_services(profile)
    if not services:
        return []
    candidates = []
    for path in files:
        lower_name = path.name.lower()
        if (
            path.suffix.lower()
            not in SOURCE_EXTENSIONS | CONFIG_SUFFIXES | DOCUMENTATION_SUFFIXES
            and lower_name not in CONFIG_NAMES
        ):
            continue
        if (
            lower_name.endswith((".lock", ".min.js", ".map"))
            or "lock" in lower_name
        ):
            continue
        scope = classify_scope(path, root)
        lines = read_text(path).splitlines()
        relative_path = relative(path, root)
        path_text = relative_path.lower()
        last_selected = -100
        for index, line in enumerate(lines):
            direct = [
                service
                for service in services
                if PROFILE_LINE_PATTERNS[service].search(line)
            ]
            provider_direct = bool(
                provider and PROVIDER_LINE_PATTERNS[provider].search(line)
            )
            if not direct or (index - last_selected < 16 and not provider_direct):
                continue
            start = max(0, index - 24)
            end = min(len(lines), index + 28)
            excerpt = "\n".join(lines[start:end]).strip()[:4000]
            if len(excerpt.splitlines()) < 2:
                continue
            score = 5
            connection_terms = {
                match.group(0).lower()
                for match in CONNECTION_LINE.finditer(excerpt)
            }
            score += min(len(connection_terms), 8)
            if provider and PROVIDER_LINE_PATTERNS[provider].search(excerpt):
                score += 5
            if re.search(r"(?:config|connection|client|driver|model|openapi)", path_text):
                score += 3
            if path.suffix.lower() in CONFIG_SUFFIXES:
                score += 2
            if re.search(r"(?:docs?|documentation|example|frontend|static)", path_text):
                score -= 3
            candidates.append(
                {
                    "services": sorted(direct),
                    "path": relative_path,
                    "startLine": start + 1,
                    "matchLine": index + 1,
                    "endLine": end,
                    "excerpt": excerpt,
                    "score": score,
                    "scope": scope,
                    "providerSpecific": bool(
                        provider and PROVIDER_LINE_PATTERNS[provider].search(excerpt)
                    ),
                    "evidence": None,
                }
            )
            last_selected = index
    candidates.sort(
        key=lambda item: (-item["score"], item["path"], item["startLine"])
    )
    selected = []
    path_counts = {}
    buckets = (
        (
            lambda item: item["scope"] == "production",
            10,
        ),
        (
            lambda item: item["scope"] != "production"
            and Path(item["path"]).suffix.lower() in CONFIG_SUFFIXES,
            3,
        ),
        (
            lambda item: item["scope"] != "production"
            and Path(item["path"]).suffix.lower() in DOCUMENTATION_SUFFIXES,
            3,
        ),
    )
    for includes, limit in buckets:
        bucket_count = 0
        for provider_specific in (True, False):
            for item in candidates:
                if not includes(item):
                    continue
                if item["providerSpecific"] is not provider_specific:
                    continue
                if path_counts.get(item["path"], 0) >= 2:
                    continue
                add_profile_block(item, selected, path_counts, evidence)
                bucket_count += 1
                if bucket_count >= limit:
                    break
            if bucket_count >= limit:
                break
    for item in candidates:
        if len(selected) >= 16:
            break
        if item in selected or path_counts.get(item["path"], 0) >= 2:
            continue
        add_profile_block(item, selected, path_counts, evidence)
    return selected


def add_profile_block(item, selected, path_counts, evidence):
    item["evidence"] = evidence.add(
        "profile-block",
        item["path"],
        item["startLine"],
        item["excerpt"],
        item["scope"],
    )
    selected.append(item)
    path_counts[item["path"]] = path_counts.get(item["path"], 0) + 1


def derive_workload_candidates(dockerfiles, compose, role_intent):
    workloads = []
    root_compose = [
        manifest
        for manifest in compose
        if manifest["scope"] == "production"
        and len(Path(manifest["path"]).parts) == 1
    ]
    for manifest in root_compose:
        if manifest["scope"] != "production":
            continue
        for service in manifest["services"]:
            workloads.append(
                {
                    "id": service["name"],
                    "source": "compose",
                    "evidence": [service["evidence"]],
                    "image": service["image"],
                    "dockerfile": service["dockerfile"],
                    "command": service["command"],
                    "ports": service["ports"],
                    "environment": service["environment"],
                }
            )
    if not workloads and dockerfiles:
        dockerfile = dockerfiles[0]
        workloads.append(
            {
                "id": "app",
                "source": "dockerfile",
                "evidence": [dockerfile["evidence"]],
                "image": None,
                "dockerfile": dockerfile["path"],
                "command": dockerfile["entrypoint"] or dockerfile["cmd"],
                "ports": [item["port"] for item in dockerfile["exposedPorts"]],
                "environment": [],
            }
        )
    for role in role_intent:
        if not any(item["id"] == role["role"] for item in workloads):
            workloads.append(
                {
                    "id": role["role"],
                    "source": "user-intent",
                    "evidence": [role["evidence"]],
                    "image": None,
                    "dockerfile": dockerfiles[0]["path"] if dockerfiles else None,
                    "command": None,
                    "ports": [],
                    "environment": [],
                }
            )
    return workloads


def classify_scope(path, root):
    parts = {part.lower() for part in path.relative_to(root).parts[:-1]}
    part_tokens = {
        token
        for part in parts
        for token in re.split(r"[._-]+", part)
        if token
    }
    lower_name = path.name.lower()
    test_file = bool(
        re.search(r"(?:^|[._-])(?:test|spec)(?:[._-]|$)", lower_name)
        or re.search(r"_test\.[a-z0-9]+$", lower_name)
    )
    return (
        "non-production"
        if parts & NON_PRODUCTION_SEGMENTS
        or part_tokens & NON_PRODUCTION_SEGMENTS
        or any(
            len(token) > 4 and token.endswith(("test", "tests", "testdata"))
            for token in part_tokens
        )
        or test_file
        else "production"
    )


def indentation(line):
    return len(line) - len(line.lstrip(" "))


def read_text(path):
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as error:
        READ_WARNINGS[path] = type(error).__name__
        return ""


def relative(path, root):
    value = path.relative_to(root)
    return "." if str(value) == "." else value.as_posix()


def git(arguments, cwd):
    result = subprocess.run(
        ["git", "-C", str(cwd), *arguments],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise SystemExit(result.stderr.strip() or "git command failed")
    return result.stdout.strip()


def git_optional(arguments, cwd):
    result = subprocess.run(
        ["git", "-C", str(cwd), *arguments],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def sanitize_repository_url(value):
    if not value:
        return None
    if value.startswith("git@"):
        return value
    try:
        parsed = urlsplit(value)
    except ValueError:
        return value
    if not parsed.scheme or not parsed.netloc:
        return value
    host = parsed.hostname or parsed.netloc.rsplit("@", 1)[-1]
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return urlunsplit((parsed.scheme, host, parsed.path, parsed.query, parsed.fragment))


def collect_evidence_ids(value):
    result = set()

    def visit(item):
        if isinstance(item, dict):
            for nested in item.values():
                visit(nested)
        elif isinstance(item, list):
            for nested in item:
                visit(nested)
        elif isinstance(item, str) and re.fullmatch(r"[EI]\d+", item):
            result.add(item)

    visit(value)
    return result


def require_within(path, root, label):
    if not is_within(path, root):
        raise SystemExit(f"{label} must be inside the repository root")


def is_within(path, root):
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


if __name__ == "__main__":
    main()
