# Agentic verification loop

Use this bounded loop to improve source fidelity without replacing the
app-modeling workflow or building a second analysis system.

## Select the source profile

Launch these two fast/default-reasoning read-only Explore agents in parallel
and keep their agent IDs:

1. **Runtime investigator** — inspect production entrypoints, configuration
   reads, client construction, listeners, secrets, persistent paths, and
   executable roles. Return only source-backed
   requirements and omissions with file-and-line citations. For every backing
   service, return ledger-ready records for each source-native setting: setting
   name, delivery kind, exact key or structured location, required value or
   parsed default, and evidence.
2. **Packaging investigator** — inspect only production Dockerfiles, build
   contexts, manifests, and checked-in deployment configuration. Return the
   buildable workload set, immutable source ref, selected default deployment
   profile, rejected optional/test profiles, and packaging blockers.

Don't inspect source in parallel with these agents.

When both return:

- reject empty, uncited, contradictory, or blocked critical results;
- select one profile supported by both reports;
- before authoring, stop with `needs_more_info` when the request and pinned
  source leave multiple materially different runnable profiles. Skill examples
  and nearby supported versions aren't evidence for choosing one;
- don't treat the absence of an existing `app.bicep` as a blocker;
- don't model development-only proxy, admin, hot-reload, or frontend services
  when the production Dockerfile packages one runnable workload.

Then launch one read-only Explore **contract investigator** for only the backing
types in the selected profile. It must run `contract_query.py bundle <type>`
once per backing type and must not query Core or Compute types. It returns the
Recipe outputs, managed-secret keys, and every required client setting mapped
to the runtime investigator's source-native delivery. Online lookup is allowed
only when the bundled provenance doesn't match the requested target.

After all three investigators return, don't reopen their cited source files or reread
large skill references merely to confirm accepted findings. Build the ledger
directly from their records. Read again only when a required field is missing,
the investigators contradict one another, or a citation doesn't support its
claim.

Before authoring, write a temporary `requirements.json` outside the repository:

```json
{
  "dependencies": [
    {
      "resourceSymbol": "databaseSymbol",
      "settings": [
        {
          "name": "tls",
          "evidence": "path/to/source:line",
          "delivery": {
            "kind": "env",
            "key": "APP_TLS_SETTING",
            "value": "true"
          }
        }
      ]
    }
  ]
}
```

Include every item in the selected protocol profile's
`runtimeRequiredClientSettings`. Use `delivery.kind: "sourceDefault"` only when
the source citation proves the parsed runtime default supplies that exact
setting. This file is a handoff artifact, not application output.

Call the Task tool exactly once with `agent_type: general-purpose`,
`mode: sync`, and description `Author Radius candidate`. Its prompt must contain
the complete investigator findings and the absolute repository, skill,
requirements, candidate, and config paths. The author writes only the two
`.radius` files and runs the validator. It must not rescan source, read skill or
validator source, fetch contracts, or perform Git/deployment actions.

## Review in parallel

After writing the candidate, run:

```text
python3 scripts/validate_candidate.py \
  --app-bicep <absolute-candidate-path> \
  --bicepconfig <absolute-bicepconfig-path> \
  --requirements <absolute-requirements-path>
```

If validation fails, send its JSON errors verbatim to the same author agent for
the single correction pass. The main agent mustn't inspect validator source or
edit the candidate. Once the report is valid, send one follow-up turn to the
runtime and contract investigators in parallel:

- The runtime investigator checks the candidate for omitted or extra workloads,
  dependencies, native settings, process/listener behavior, secrets, and
  persistence.
- The contract investigator checks every emitted type, property, Recipe output,
  managed-secret key, service endpoint, and graph relationship. Reject a
  missing connection for any workload-to-backing-resource dependency, and
  reject native wiring that relies on the connection projection. Also reject
  unless the candidate has cited source-native delivery for every applicable
  `requiredClientSettings` and `runtimeRequiredClientSettings` item in the
  selected protocol profile.

Give each agent the candidate path and only the evidence needed for its scope.
Give the absolute candidate path, and require each reviewer to read that file
itself rather than accepting a summary. Give both reviewers the absolute
requirements path too. Require a short verdict: `accepted`, `rejected`, or
`needs_more_info`, followed by at most six concise cited findings. Don't repeat
schemas or the full requirement ledger. Agents remain read-only and don't
rewrite the Bicep.

## Correct once

Combine both reviews and apply one complete correction pass. If a resource
representation changes, re-derive its endpoint, port, protocol, TLS,
authentication, secrets, persistence, native settings, and connections
together.

Recompile and run the full validation checklist once. Deliver only when both
reviews are accepted and final validation passes, and perform Git operations
only when the request explicitly asks for them. There is no second repair loop.
The main agent can't reinterpret, waive, or overrule a `rejected` or
`needs_more_info` verdict. Correct the candidate or evidence once, then ask the
same reviewer for a new explicit verdict. If that verdict isn't `accepted`,
delete any candidate files created during the run and report the blocker.
A source-fidelity rejection can be resolved only with new cited source
evidence, not with a general naming, security, schema-optionality, or
client-compatibility rule.
