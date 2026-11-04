# Governance demo — Authorino and Limitador

Screen-share runbook. Each script prints the YAML it applies, applies it,
then curls the model or MCP gateway and prints **only** the HTTP status
and a short body snippet. Tokens and API keys are never echoed.

Open baseline still requires authentication. "Without control" means the
demo identity is allowed and the budgets are too high to trip.

## Prerequisites

- Cluster already installed (`./install-openshift-ai.sh`, GitOps synced).
- `oc` logged in as a user that can patch MaaS and MCP gateway objects.
- Secret `external-model-credentials` in `doc-gen-demo` (MaaS `sk-oai-` key).
- Gateway `maas-default-gateway` Programmed; MCP Gateway Programmed.

## Talk order

Run from the repo root (or from this directory):

```bash
cd demo/governance
./00-open-baseline.sh
./01-model-authorization-deny.sh
./02-model-authorization-allow.sh
./03-model-budget-tighten.sh
./04-model-budget-open.sh
./05-mcp-authorization-deny.sh
./06-mcp-authorization-open.sh
./07-mcp-budget-tighten.sh   # leave Gradio agents idle
./08-mcp-budget-open.sh
./99-restore-and-resume-gitops.sh
```

| Script | What changes | Read out |
|---|---|---|
| `00` | Pause Argo auto-sync on `mcp-gateway`; apply open MaaS auth + 10M/1h subscription; TokenReview AuthPolicy; MCP `RateLimitPolicy` 10000/1h | model **200**, MCP untrusted **200**, MCP trusted **200** |
| `01` | `MaaSAuthPolicy` → group `demo-no-access` | model **403** |
| `02` | Restore `ai-gateway/k8s/maas-auth-policy.yaml` | model **200** |
| `03` | `MaaSSubscription` → 1 token / 1h | model **429** (first attempt may still be 200) |
| `04` | Restore `maas-subscription.yaml` (10M/1h) | model **200** |
| `05` | AuthPolicy allowlist = four agent SAs only | untrusted MCP **403**, trusted **200** |
| `06` | Restore TokenReview-only AuthPolicy | both MCP **200** |
| `07` | MCP `RateLimitPolicy` → 2 / 1m | third MCP call **429** |
| `08` | Restore MCP budget 10000/1h | MCP **200** |
| `99` | Re-apply all permissive policies; resume Argo `selfHeal` | always run at the end |

## Why GitOps is paused

[`gitops/apps/mcp-gateway.yaml`](../../gitops/apps/mcp-gateway.yaml) has
`selfHeal: true`. Live MCP AuthPolicy / RateLimitPolicy edits would be
reverted within seconds. `00` removes `/spec/syncPolicy/automated`;
`99` puts prune + selfHeal back.

MaaS objects (`MaaSAuthPolicy`, `MaaSSubscription`) are applied by
`install-openshift-ai.sh`, not by Argo — no pause needed for those.

## Overlays vs GitOps baseline

| Path | Role |
|---|---|
| [`ai-gateway/k8s/maas-subscription.yaml`](../../ai-gateway/k8s/maas-subscription.yaml) | Committed open model budget (10M/1h) |
| [`ai-gateway/k8s/maas-auth-policy.yaml`](../../ai-gateway/k8s/maas-auth-policy.yaml) | Committed model auth (group `doc-gen-agents`) |
| [`mcp-gateway/k8s/auth-policy.yaml`](../../mcp-gateway/k8s/auth-policy.yaml) | Committed MCP TokenReview-only auth |
| [`mcp-gateway/k8s/rate-limit-policy.yaml`](../../mcp-gateway/k8s/rate-limit-policy.yaml) | Committed open MCP budget (10000/1h) |
| [`manifests/`](manifests/) | Demo-only overlays (deny / tight) — never the GitOps source of truth |

## Notes for the presenter

- Curl the gateways, do not run a full Gradio agent for the deny acts —
  failures stay clearly attributable to Authorino (403) or Limitador (429).
- During `07`, keep agent UIs idle so they do not burn the 2 req/min budget.
- If `03` or `07` does not show 429 immediately, say so and re-run that
  script once (quota counters can lag a request behind).
- Always finish with `99` so Argo selfHeal returns and agents keep working.
