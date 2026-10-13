# Architecture Documentation Style Guide

This knowledge base is injected into the agent's system prompt (mounted from a
ConfigMap) to keep generated documents consistent, concise, and genuinely
useful — rather than a verbose restatement of the file tree.

## Exploration principles (budget-conscious)

- **Scan before you read.** Use `get_file_outline` on candidate files first —
  it returns imports/signatures almost for free. Only fall back to `read_file`
  (full content) for the handful of files whose outline shows something that
  actually needs the implementation to explain (a route table, a data model,
  a non-obvious config value).
- **Don't let README content substitute for looking at the code.** It's fine
  to read a project's README for framing, but a short outdated/aspirational
  README does not override what the file tree and source files actually show.
  If the README and the code disagree, trust the code and note the gap rather
  than silently repeating the README's claims.
- **One full doc read is usually enough.** Once you have the project's
  purpose from `get_repo_info`'s description or a quick README skim, don't
  spend more of the read budget on CONTRIBUTING/CHANGELOG/docs/ prose — that
  budget is better spent on one more entry point, manifest, or deploy config.

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
- This is prose only — do not add a `sequenceDiagram` or any other diagram
  here. The required Mermaid diagrams are the three C4 `flowchart`s below.

## C4 Diagrams

- Diagrams must reflect only what was actually found in the code — do not
  invent components, actors, or external systems.
- Use plain Mermaid `flowchart` syntax with `<<Stereotype>>` labels for all
  three levels (Context, Container, Component) rather than Mermaid's native
  `C4Context`/`C4Container` diagram types, which render unreliably across
  viewers (Gradio, GitHub, etc.).
- **Context diagram**: exactly one box for the system under analysis —
  every other box is an external person or system. Do not decompose the
  system internally at this level.
- **Container diagram**: one box per separately deployable/runnable unit
  actually found in the repo (web app, API server, database, queue,
  worker, CLI) — not a code module. If the repo is a single service with
  no sidecar/database/queue, it is fine to have just one container box,
  with its external dependencies from the Context diagram shown again for
  reference.
- **Component diagram**: zoom into the most architecturally interesting
  container — one box per major module, package, or handler group, with
  edges showing internal dependencies. If the codebase is small enough
  that the Container diagram already shows everything, it is acceptable
  for this diagram to repeat it at slightly more detail.
- **Honesty rule**: if you could not determine the external actors (for
  Context) or the deployment topology (for Container) from what you read,
  say so in the surrounding prose and keep that diagram as simple as the
  evidence supports — do not pad it with guessed boxes.
