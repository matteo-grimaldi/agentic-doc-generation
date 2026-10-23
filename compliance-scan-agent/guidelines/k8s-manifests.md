# Kubernetes / OpenShift Manifest Compliance Guidelines

Applies to YAML manifests found under common deploy directories (`k8s/`,
`kubernetes/`, `openshift/`, `manifests/`, `deploy/`, `chart(s)/`,
`helm/`) and to any Dockerfile in the repo. Checks are only applicable if
at least one such manifest/Dockerfile was found — see each check's scope.

Check these against the manifest/Dockerfile files you were given. A check
applies per-manifest; if several manifests were found, you may report a
single row summarizing the category's overall result (PASS only if every
checked manifest satisfies it) rather than one row per file, to keep the
report short.

## Checks

1. **Resource requests/limits** (severity: HIGH) — Every container in
   every Deployment/StatefulSet/DaemonSet/Pod manifest must declare
   `resources.requests` and `resources.limits` (at least `cpu` and
   `memory`). A container missing either is a FAIL.

2. **Non-root security context** (severity: HIGH) — Pod or container
   spec should not set `privileged: true`, and should not omit a
   non-root user entirely when the repo's own Dockerfile already sets a
   non-root `USER`. Prefer `runAsNonRoot: true` or an explicit
   non-root `runAsUser` somewhere in the pod/container `securityContext`.

3. **Liveness/readiness probes** (severity: MEDIUM) — Every container
   that serves traffic (exposes a `containerPort`) should declare both a
   `livenessProbe` and a `readinessProbe`. Missing either is a FAIL for
   that container.

4. **No `:latest` image tags** (severity: MEDIUM) — No container image
   reference should use the `:latest` tag (or omit a tag entirely, which
   defaults to `latest`) in a Deployment/StatefulSet/DaemonSet manifest.
   Flag any `image:` value ending in `:latest` or with no `:tag` at all.

5. **Explicit namespace** (severity: LOW) — Namespaced resources
   (Deployment, Service, ConfigMap, Secret, Route, etc.) should set
   `metadata.namespace` explicitly rather than relying on
   `kubectl`/`oc apply`'s current-context default.

6. **No hardcoded secrets in env values** (severity: HIGH) — Container
   `env` entries should not set a literal value (via `value:`) for a key
   that looks like a credential (names containing `password`, `secret`,
   `token`, `api_key`, `apikey`, case-insensitive). Such values should
   come from `valueFrom.secretKeyRef` instead. A literal `value:` on a
   credential-shaped key is a FAIL.

## Dockerfile-specific check (if a Dockerfile was found)

7. **Non-root `USER`** (severity: MEDIUM) — The Dockerfile should set a
   non-root `USER` (a numeric UID, or a named user other than `root`)
   before the final `ENTRYPOINT`/`CMD`. A Dockerfile with no `USER`
   instruction at all is a FAIL.
