# OpenAPI Specification Style Guide

This knowledge base is injected into the OpenAPI agent's system prompt (mounted
from a ConfigMap, separate from `knowledge.md`) to keep generated specs
accurate and genuinely usable — rather than a speculative skeleton. This agent
runs independently of, and with a smaller budget than, the
architecture-documentation agent; keep that in mind when adding guidance here
— don't add steps that assume the larger budget is available.

## Confirming the project is an API (before spending any read budget)

- Trust the code over naming conventions. A directory named `api/` does not
  guarantee an HTTP surface — confirm with `get_file_outline` that it actually
  declares routes/handlers before treating it as evidence.
- If, after a quick survey, the strongest signal you found is a single
  ambiguous file, prefer bailing out with `NOT_AN_API` over guessing. A wrong
  `NOT_AN_API` costs nothing (no PR is opened); a wrong, invented spec is
  actively misleading to whoever reviews the resulting PR.
- Libraries, CLIs, data pipelines, and frontend-only apps are NOT APIs for
  this purpose, even if they happen to use an HTTP client internally.

## Exploration principles (smaller budget than the doc agent)

- Go straight for route/controller/handler files and the app/router bootstrap
  file (wherever routes are registered) — that is almost always where the
  real endpoint list lives, not the README.
- Use `get_file_outline` to confirm a file's declared functions/decorators
  before spending a `read_file` call on it.
- Spend read calls on files that define request/response shapes (DTOs,
  Pydantic/dataclass models, serializers, protobuf messages) only if the
  outline didn't already make the field types obvious.
- Do not re-read a file you already scanned with `get_file_outline` unless it
  showed something that needs full content to specify accurately.

## Writing principles

- One path per actually-found route; one operation (method) per actually-found
  handler. Do not pad the spec with plausible-sounding but unverified
  endpoints.
- Reflect the real base path (e.g. a router mounted under `/api/v1` or
  `/v2`), not a guessed default.
- Prefer concrete `operationId`s derived from the function/handler name you
  found, not generic placeholders like `op1`.
- Populate `summary`/`description` from docstrings, comments, or the
  handler's own naming — don't leave them as generic filler text if better
  information was available in the code.

## Schemas

- Derive `components.schemas` from real request/response models found in the
  source. Field types should match what the code declares (e.g. a Pydantic
  `int` field becomes `type: integer`, not `type: string`).
- If a model's exact fields couldn't be determined (e.g. a dynamically typed
  object, or a model defined in a file you didn't read), it is fine to use a
  minimal `type: object` schema rather than fabricating field names.
- Reuse a schema via `$ref` whenever the same model is referenced from
  multiple operations, rather than inlining it repeatedly.

## Confidence and honesty

- Add `x-confidence: low` next to any operation whose request/response shape
  you are not fully sure of, rather than omitting the endpoint or inventing
  precise detail you don't have evidence for.
- It is better to ship a smaller, accurate spec than a complete-looking one
  padded with guesses — the PR reviewer should be able to trust every path
  and schema in the document as something the agent actually found in the
  code.
