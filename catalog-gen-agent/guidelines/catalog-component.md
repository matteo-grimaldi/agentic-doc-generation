# Backstage Component Entity Guidelines

Rules for generating the `Component` entity in `catalog-info.yaml`. Apply
these using only what you actually observed in the repository — never
invent a value you could not confirm from the file tree, a manifest, or
a build/config file you read or outlined.

## Required `metadata` fields

- `metadata.name` — derive from the repo name, lowercase, hyphenated
  (no spaces/underscores). Must be a valid Kubernetes-style name
  (`[a-z0-9]([-a-z0-9]*[a-z0-9])?`).
- `metadata.title` — a human-readable title (the repo name with normal
  capitalization is fine if no better name is evident from the README).
- `metadata.description` — one or two sentences, derived from the
  README's opening paragraph or the repo's own description if available.
  Never leave this as a placeholder like "TODO" or "description here".
- `metadata.annotations` — see the annotation table below; only include
  an annotation if you have concrete evidence for its value.
- `metadata.tags` — short, lowercase, hyphenated tags (see "Tag
  derivation" below). 3-8 tags is typical; do not pad with generic tags
  like `software` or `code`.

## Annotation conventions

| Annotation | When to set it | Value source |
|---|---|---|
| `backstage.io/source-location` | Always | `url:<repo URL>` (optionally `/tree/<default_branch>`) |
| `backstage.io/kubernetes-id` | A K8s/OpenShift manifest (Deployment/StatefulSet) was found | The `metadata.name` of the Deployment, or this component's own `metadata.name` if they match |
| `argocd/app-name` | An ArgoCD `Application` manifest referencing this repo was found, OR the repo is clearly GitOps-deployed (has a `k8s/`/`gitops/` directory structured for it) | The ArgoCD `Application`'s `metadata.name` |
| `backstage.io/techdocs-ref` | A `docs/` directory with an `mkdocs.yml` (or similar TechDocs config) was found | `dir:.` (TechDocs convention for "docs live in this repo") |

Do not invent an annotation's value when no manifest evidence supports
it — omit the annotation entirely rather than guessing.

## `spec.type` classification

Classify based on what the repo actually builds/runs, in this priority
order:

1. **`service`** — exposes a long-running process reachable over the
   network (HTTP/gRPC server, listens on a port, has a Dockerfile/K8s
   Deployment). This is the default for anything with an entry point
   that starts a server.
2. **`website`** — a frontend-only application (static site, SPA built
   with React/Vue/Angular with no backend server code in the same repo).
3. **`library`** — a package meant to be imported by other code (has a
   `setup.py`/`pyproject.toml` with no server entry point, a published
   npm package with a `main`/`exports` field and no server code, or
   similarly consumed as a dependency, not run standalone).
4. **`tool`** — a CLI utility with no server component.

If genuinely ambiguous after looking at the entry points and manifests,
default to `service` and note the uncertainty is acceptable — do not
block on it.

## `spec.lifecycle` determination

- `production` — the default, unless you find explicit evidence
  otherwise (e.g. a README badge/section saying "experimental",
  "alpha", "proof of concept", or a version like `0.0.x`/`0.1.x` with
  no releases).
- `experimental` — use when the repo's own README or version clearly
  signals pre-production status.
- Never use `deprecated` unless the README explicitly says so.

## Tag derivation

Derive tags only from what you actually found:

- Primary language(s) (e.g. `python`, `java`, `go`, `typescript`) —
  from build manifests (`pyproject.toml`/`requirements.txt`,
  `pom.xml`/`build.gradle`, `go.mod`, `package.json`) or file
  extensions in the repo structure.
- Framework(s) in use (e.g. `fastapi`, `spring-boot`, `react`,
  `express`) — from imports/dependencies you actually saw in a
  manifest or outline.
- Deployment platform signals (e.g. `kubernetes`, `openshift`,
  `docker`) — only if a Dockerfile or K8s/OpenShift manifest was found.
- Domain/purpose tags (e.g. `api`, `cli`, `frontend`) consistent with
  the `spec.type` you chose.

## `spec.system` and `spec.subcomponentOf`

- Only set `spec.system` if the repo's own documentation or an existing
  `catalog-info.yaml`/organizational convention names a system this
  component belongs to. Do not invent a system name.
- Only set `spec.subcomponentOf` if there is clear evidence this repo
  is a sub-part of another named component (e.g. a monorepo README that
  says so). Otherwise omit it.

## `spec.owner`

- If an existing `catalog-info.yaml`, `CODEOWNERS`, or `OWNERS` file
  names a team/group, use that value (prefixed `group:` per Backstage
  convention if it clearly is a team, otherwise `user:` for an
  individual).
- If no ownership evidence exists at all, set `spec.owner` to
  `"unknown"` rather than guessing a name — a human can fix this during
  PR review, which is the whole point of opening a PR instead of
  committing directly.

## Augmenting an existing `catalog-info.yaml`

If the repo already has a `catalog-info.yaml`:

- Preserve every field that already has a non-placeholder value —
  never overwrite a human-authored value with a guess.
- Fill in only the fields that are missing, empty, or an obvious
  placeholder (e.g. `description: TODO`, `owner: ""`, `tags: []`).
- Keep the entity's existing `metadata.name` unchanged — renaming it
  would break any existing references to it elsewhere in a Backstage
  catalog.
- Add any missing `API` entities (see `api-detection.md`) and
  `providesApis`/`consumesApis` relationships, but do not remove
  entities that are already present.
