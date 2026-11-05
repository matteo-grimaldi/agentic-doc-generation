# Gateway config demo — wiring tour

Read-only screen-share runbook. Split into two folders for a dual-terminal
demo:

| Folder | Purpose |
|---|---|
| [`demo/gateways/`](.) (this dir) | **Screen** — prints committed YAML and live `oc get` status only |
| [`notes/`](notes/) | **Presenter** — talking points to read while the matching screen script runs |

**This folder does not mutate the cluster.** No applies, no Argo pause, no
HTTP probes. For Authorino deny/allow and Limitador 429 acts, use
[`demo/governance/`](../governance/).

Tokens and API keys are never echoed. Secret values are never dumped.

YAML/Python excerpts are syntax-highlighted when stdout is a TTY and one of
`bat`, `pygmentize`, or `rich` is on `PATH` (checked in that order). Plain
`cat` otherwise. Force or disable with `DEMO_COLOR=1` / `DEMO_COLOR=0`.

## Prerequisites

- Cluster already installed (`./install-openshift-ai.sh`, GitOps synced).
- `oc` logged in (screen scripts use it for live status reads).
- Gateways Programmed (`maas-default-gateway`, `mcp-gateway`).

## Talk order

Two terminals, same act numbers. Run the screen script on the shared
display; run the matching notes script on your laptop.

**Screen** (shared):

```bash
cd demo/gateways
./00-overview.sh
./01-agent-ai-gateway.sh
./02-ai-gateway-access.sh
./03-ai-gateway-budget.sh
./04-agent-mcp-gateway.sh
./05-mcp-gateway-access.sh
./06-mcp-gateway-budget.sh
```

**Notes** (presenter only):

```bash
cd demo/gateways/notes
./00-overview.sh
./01-agent-ai-gateway.sh
./02-ai-gateway-access.sh
./03-ai-gateway-budget.sh
./04-agent-mcp-gateway.sh
./05-mcp-gateway-access.sh
./06-mcp-gateway-budget.sh
```

| Script | Screen shows | Notes say |
|---|---|---|
| `00` | Path diagram + gateway Programmed + `cluster-config` URLs | Agents never call upstream/MCP Services directly |
| `01` | `cluster-config` + Deployment `INFERENCE_*` | OGX is the only inference client; MaaS `sk-oai-...` key |
| `02` | Group, `MaaSAuthPolicy`, `ExternalModel` | Auth is group-scoped on bare model id `qwen38-27b` |
| `03` | `MaaSSubscription` | Open budget 10M/1h; governance tightens for 429 |
| `04` | `MCP_GATEWAY_*`, projected SA token, `agent.py` tool dict | TokenReview bearer + `X-Mcp-Virtualserver` |
| `05` | MCP `AuthPolicy` + `MCPVirtualServer` | Auth = TokenReview; tools = virtual-server allowlist |
| `06` | MCP `RateLimitPolicy` | Open budget 10000/1h; governance tightens for 429 |

## After this tour

Continue with [`demo/governance/`](../governance/) to apply deny/tighten
overlays and curl the gateways for 403 / 429.
