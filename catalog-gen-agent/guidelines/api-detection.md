# Backstage API Entity Detection Guidelines

Rules for deciding whether to generate an `API` entity, and how to fill
in its `spec`. Only produce an `API` entity when you found concrete
evidence of an API surface — never invent paths, operations, or a
schema you did not actually see.

## When to create an API entity

Create one `API` entity per distinct API surface the repo exposes:

- **REST/HTTP** — controllers/routes/handlers found (e.g.
  `@RestController`/`@RequestMapping` annotations, Express/FastAPI
  route decorators, `urls.py`-style route tables), OR an existing
  OpenAPI/Swagger spec file (`openapi.yaml`, `swagger.json`, etc.).
- **gRPC** — one or more `.proto` files found.
- **GraphQL** — a `.graphql`/`.gql` schema file found, or a GraphQL
  server library (e.g. `graphql`, `apollo-server`, `graphql-java`) is a
  dependency.
- **AsyncAPI** — an existing `asyncapi.yaml`/`asyncapi.json` file, or
  clear message-queue producer/consumer code (Kafka/RabbitMQ/etc.)
  paired with a documented topic/event contract.

If none of the above evidence exists, do not create an API entity —
a CLI tool, library, or frontend-only app typically has none, and
that is a correct, complete result, not a gap to fill.

## `spec.type` mapping

| Evidence found | `spec.type` |
|---|---|
| REST controllers/routes, or an OpenAPI/Swagger file | `openapi` |
| `.proto` files | `grpc` |
| GraphQL schema or server library | `graphql` |
| AsyncAPI file or message-queue contract | `asyncapi` |

## `spec.definition` approach, in priority order

1. **Existing spec file in the repo** — reference it with a `$text`
   pointer relative to `catalog-info.yaml`'s location, e.g.:
   ```yaml
   spec:
     type: openapi
     definition:
       $text: ./openapi.yaml
   ```
   Never inline a spec that already exists as a file — referencing it
   keeps the catalog entry in sync with the file as it's edited later.

2. **No spec file, but you derived enough from code to write a minimal
   one** (e.g. you read controller signatures/annotations and are
   confident about a small number of paths) — inline a minimal
   `openapi: "3.0.0"` document under `definition:` with only the
   paths/operations you have real evidence for. Mark any uncertain
   operation with `x-confidence: low`. Do not invent request/response
   schemas you did not see; use a bare `type: object` placeholder
   instead of a guessed shape.

3. **Evidence of an API exists but you could not derive even a
   minimal spec** (e.g. you found route signals but ran out of budget
   to inspect them) — still create the `API` entity with `spec.type`
   set correctly and a one-line `spec.definition` stub noting the gap,
   e.g.:
   ```yaml
   definition: |
     openapi: "3.0.0"
     info:
       title: <component> API
       version: "0.0.0"
       description: "Specification not yet derived — see component source."
     paths: {}
   ```
   This still gives Backstage a valid entity and signals to a human
   reviewer that the spec needs manual completion.

## Naming

- `metadata.name` for an API entity: `<component-name>-api` (or
  `<component-name>-events-api` for AsyncAPI, `<component-name>-grpc`
  for gRPC) to keep it unambiguous when a component exposes more than
  one API type.

## Relationships

- The `Component` entity that implements the API must list it under
  `spec.providesApis`.
- If the repo's own code shows it calling another known API (e.g. an
  SDK/client for a specific external service you can name), you may
  add that name under `spec.consumesApis` on the `Component` — but
  only when the target API entity name is something you have reason to
  believe already exists in the target Backstage catalog (e.g. it's
  named in existing config/docs). Do not invent `consumesApis` entries
  for generic third-party services with no evidence of a specific
  catalog entity to link to.
