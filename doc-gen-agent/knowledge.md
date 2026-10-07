# Architecture Documentation Style Guide

This knowledge base is injected into the agent's system prompt (mounted from a
ConfigMap) to keep generated documents consistent, concise, and genuinely
useful — rather than a verbose restatement of the file tree.

## Writing principles

- **Be concrete.** Name the actual frameworks, files, and classes you found
  instead of generic statements like "uses modern best practices."
- **Be honest about gaps.** If you could not determine something (e.g. no
  CI config was found), say so rather than guessing.
- **Prefer the project's own words.** Quote or closely paraphrase README
  descriptions of purpose rather than inventing new framing.
- **Keep it proportional.** A small CLI tool does not need the same depth
  of "Security Considerations" as a multi-service platform — write what the
  evidence supports.

## Component Architecture section

- Group by directory/module, not by programming language.
- For each component, state: what it does, and what it depends on
  (other components, external services, libraries).
- If the repo is a single small application (not a multi-component system),
  it is fine to have just one or two components.

## Data Flow section

- Trace a concrete example (e.g. "an HTTP request to POST /orders flows
  through X, then Y, then is persisted in Z") rather than an abstract
  description.
- If the project is a library (not a running service), describe the main
  call sequence a consumer would trigger instead.

## Diagrams

- Diagrams must reflect only what was actually found in the code — do not
  invent components.
- Component diagram: one box per real component/service/module.
- Data flow diagram: use a sequenceDiagram only if there is a clear
  request/response flow between distinct components; otherwise a flowchart
  with directional edges is fine.
- Dependency diagram: show the project's node (one box) pointing to its
  key external dependencies (databases, APIs, message brokers, major
  libraries/frameworks) — not an exhaustive package list.
