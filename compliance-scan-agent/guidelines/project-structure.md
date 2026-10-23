# Project Structure Compliance Guidelines

These checks are always applicable (every repo has a root), and are
determined purely from the file list — the user message already tells
you whether each signal was found, so no tool calls are needed for this
category at all. Score these checks directly from that information.

## Checks

1. **README present** (severity: MEDIUM) — A `README.md` (or `README`/
   `README.rst`/`README.txt`) must exist at the repo root. Missing is a
   FAIL.

2. **Dockerfile present (if containerized)** (severity: LOW) — If the
   repo contains Kubernetes/OpenShift manifests (see
   `k8s-manifests.md`) but no Dockerfile was found anywhere in the repo,
   that is a FAIL (the project deploys a container image it doesn't
   build in-repo, which is at least worth flagging). If no K8s manifests
   were found either, mark this check SKIP ("not a containerized
   project").

3. **`.gitignore` present** (severity: LOW) — A `.gitignore` file must
   exist at the repo root. Missing is a FAIL.

4. **License file present** (severity: MEDIUM) — A `LICENSE`/
   `LICENSE.md`/`LICENSE.txt` file must exist at the repo root. Missing
   is a FAIL.

5. **CI/CD configuration present** (severity: LOW) — The repo should
   have some form of CI/CD configuration: `.github/workflows/`,
   `.tekton/`, a `Jenkinsfile`, or `.gitlab-ci.yml`. Missing any of
   these is a FAIL.

## Note on evidence

Because every check in this file is answerable directly from the
pre-computed booleans in the user message (`README present`,
`.gitignore present`, `LICENSE present`, `CI/CD config present`,
`Dockerfile present`), do not spend any tool calls verifying them —
report PASS/FAIL straight from those values.
