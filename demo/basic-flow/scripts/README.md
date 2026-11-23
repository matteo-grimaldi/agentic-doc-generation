# Prepare the existing Gradio UI

Log in with oc and run a numbered script from this directory. Each script configures the existing doc-gen-agent Deployment in doc-gen-demo, waits for rollout, and prints the Gradio URL and the text to paste. Submit the text yourself. Scripts can be run in any order.

Each launcher selects a scenario in [prepare.py](prepare.py). Its `configure()` function builds the patch from the saved Deployment configuration. Use the files below to explain the change, then show the live fields to confirm what the script applied.

| Script | What to observe in Gradio | Files to show and what to point out |
|---|---|---|
| [01-basic.sh](01-basic.sh) | OGX calls external Qwen directly and uses GitHub MCP directly. | Show the `basic` branch in [prepare.py](prepare.py) beside the [Deployment](../../../doc-gen-agent/k8s/deployment.yaml). Point out the direct `INFERENCE_BASE_URL`, temporary upstream Secret reference, GitHub MCP service URL, and pod-name placeholder in the token volume. The [ExternalProvider](../../../ai-gateway/k8s/external-provider.yaml) identifies the upstream host and credential reference. |
| [02-tooling-blocked.sh](02-tooling-blocked.sh) | Direct inference is configured. MCP Gateway rejects the agent token. | Compare the `tooling-blocked` branch in [prepare.py](prepare.py), which projects a token for `basic-flow-denied`, with the [MCP AuthPolicy](../../../mcp-gateway/k8s/auth-policy.yaml), which accepts the `mcp-gateway` audience. Show the mismatch in the live token volume. |
| [03-tooling-allowed.sh](03-tooling-allowed.sh) | Direct inference is configured and the agent can use GitHub MCP through the gateway. | Show the token projection and `MCP_GATEWAY_URL` in the [Deployment](../../../doc-gen-agent/k8s/deployment.yaml), then the matching audience in the [MCP AuthPolicy](../../../mcp-gateway/k8s/auth-policy.yaml). Show the direct-model override for tooling scenarios in [prepare.py](prepare.py). |
| [04-ai-blocked.sh](04-ai-blocked.sh) | AI Gateway rejects the demo credential on the agent's inference request. | Compare the `ai-blocked` branch in [prepare.py](prepare.py) with the `ogx-server` environment in the [Deployment](../../../doc-gen-agent/k8s/deployment.yaml). The gateway URL still comes from cluster-config, but the live `INFERENCE_API_KEY` entry uses a deliberately invalid literal. |
| [05-ai-allowed.sh](05-ai-allowed.sh) | The agent uses its original AI Gateway and MCP Gateway credentials. | Show the `INFERENCE_API_KEY` Secret reference and `INFERENCE_BASE_URL` ConfigMap reference in the [Deployment](../../../doc-gen-agent/k8s/deployment.yaml). Follow the model route through the [ExternalModel](../../../ai-gateway/k8s/external-model.yaml) to the [ExternalProvider](../../../ai-gateway/k8s/external-provider.yaml). Compare the live credential source with scenario 04. |
| [06-guardrails-blocked.sh](06-guardrails-blocked.sh) | Paste the printed prompt including the instruction override. NeMo rejects it before generation. | Show the guardrails URL and the blocked scenario's prompt in [prepare.py](prepare.py). Follow the input check in the [Gradio chat handler](../../../doc-gen-agent/app.py) to the `instruction_override` pattern in the [NeMo service](../../../nemo-guardrails/app.py). |
| [07-guardrails-allowed.sh](07-guardrails-allowed.sh) | Paste the plain URL. The agent checks input, generates using both gateways, and checks output. | Show the same guardrails URL and the plain prompt in [prepare.py](prepare.py). In the [Gradio chat handler](../../../doc-gen-agent/app.py), point out the input and output calls to `check_content`. The [guardrails client](../../../doc-gen-agent/guardrails.py) sends each phase to NeMo. The prompt distinguishes this run from scenario 06. |
| [99-restore.sh](99-restore.sh) | The existing UI returns to its pre-demo configuration. | Show `snapshot()` and the restore branch of `run()` in [prepare.py](prepare.py). Point out the two saved container environments, MCP token volume, temporary Secret removal, and restoration of both Argo CD annotations. Compare the live view below with the one taken before the first scenario. |

## Commands for the live demo

From this directory, paste the setup block once into Bash or Zsh. Keep that terminal open for the baseline, the scenarios you want to show, and restore. The inspection commands use `oc` and `jq`. Scenario 07 also uses `rg`.

```sh
set -o pipefail

heading() {
  printf '\n=== %s ===\n' "$1"
}
agent_spec() {
  oc -n doc-gen-demo get deployment/doc-gen-agent -o json | jq '.spec.template.spec'
}
routes() {
  heading 'Where the agent sends requests'
  agent_spec | jq -r '.containers[] | .name as $container |
    (.env | sort_by(.name)[]) |
    select(.name == "INFERENCE_BASE_URL" or .name == "INFERENCE_API_KEY" or
           .name == "MCP_GATEWAY_URL" or .name == "NEMO_GUARDRAILS_URL") |
    (if .valueFrom.secretKeyRef then "Secret/" + .valueFrom.secretKeyRef.name
     elif .valueFrom.configMapKeyRef then
       "ConfigMap/" + .valueFrom.configMapKeyRef.name + " / " + .valueFrom.configMapKeyRef.key
     elif .name == "INFERENCE_API_KEY" then "literal (hidden)"
     elif .name == "NEMO_GUARDRAILS_URL" and (.value // "") == "" then "disabled"
     else .value end) as $source |
    "  \($container) / \(.name)\n    \($source)"'
}
token_volume() {
  agent_spec | jq -S '.volumes[] | select(.name == "mcp-gateway-token")'
}
audiences() {
  heading 'Audience requested by the agent token'
  token_volume | jq -r '.projected.sources[] | .serviceAccountToken? // empty | .audience'
  heading 'Audiences accepted by the MCP Gateway'
  oc -n mcp-gateway get authpolicy/mcp-gateway-tokenreview -o json |
    jq -r '.spec.rules.authentication["k8s-token"].kubernetesTokenReview.audiences[]'
}
demo_state() {
  routes || return 1
  heading 'MCP token volume'
  token_volume || return 1
  heading 'Argo CD applications and their skip-reconcile settings'
  oc -n openshift-gitops get applications doc-gen-demo-root doc-gen-agent -o json |
    jq -r '.items | sort_by(.metadata.name)[] |
      "  \(.metadata.name)    \(.metadata.annotations["argocd.argoproj.io/skip-reconcile"] // "(unset)")"'
}
```

Before running the first launcher, display the baseline and save a copy for the restore comparison.

```sh
heading 'Pre-demo baseline'
demo_before=$(mktemp)
demo_state | tee "$demo_before"
```

**01 · Direct connections.** Compare the host in `INFERENCE_BASE_URL` with the upstream provider below. The MCP URL points to the GitHub MCP service. The volume supplies the pod name as a placeholder.

```sh
./01-basic.sh
routes
heading 'Source of the MCP token file'
token_volume | jq -r '.downwardAPI.items[] | "  \(.path) comes from pod field \(.fieldRef.fieldPath)"'
heading 'Upstream model provider host'
oc -n llm get externalprovider/workshop-maas -o json | jq -r '.spec.endpoint'
```

**02 · MCP Gateway denies the token.** Point out the mismatch between the requested audience (`basic-flow-denied`) and the accepted audience (`mcp-gateway`), then submit the printed URL in Gradio.

```sh
./02-tooling-blocked.sh
routes
audiences
```

**03 · MCP Gateway accepts the token.** The audiences now match. Compare the MCP Gateway address with the direct inference URL.

```sh
./03-tooling-allowed.sh
audiences
heading 'MCP Gateway address from cluster-config'
oc -n doc-gen-demo get configmap/cluster-config -o json | jq -r '.data.mcp_gateway_url'
routes
```

**04 · AI Gateway denies the credential.** Show the gateway address and the `literal (hidden)` credential source, then submit the URL in Gradio to demonstrate the rejection.

```sh
./04-ai-blocked.sh
heading 'AI Gateway address from cluster-config'
oc -n doc-gen-demo get configmap/cluster-config -o json | jq -r '.data.model_base_url'
routes
```

**05 · AI Gateway accepts the credential.** Point out the restored Secret reference under `INFERENCE_API_KEY`, then trace the model request to its provider.

```sh
./05-ai-allowed.sh
routes
heading 'How the AI Gateway routes the model request'
oc -n llm get externalmodel/qwen38-27b -o json |
  jq -r '.spec | "  Gateway model    \(.modelName)",
    (.externalProviderRefs[] | "  Provider         \(.ref.name)\n  Target model     \(.targetModel)")'
```

**06 · NeMo denies the input.** Show the NeMo address under `NEMO_GUARDRAILS_URL`.

```sh
./06-guardrails-blocked.sh
routes
```

Submit the printed prompt, including the instruction override, in Gradio. Once the denial appears, show the corresponding log entry.

```sh
heading 'Recent input denials from NeMo Guardrails'
oc -n doc-gen-demo logs deployment/doc-gen-agent -c doc-gen-agent --since=5m |
  awk '/NeMo Guardrails denied input/ {print; found=1}
       END {if (!found) print "No input denials in the last five minutes."}' | tail -n 3
```

**07 · NeMo allows the request.** Show the same NeMo address, then submit the plain repository URL. Show the completed document in Gradio.

```sh
./07-guardrails-allowed.sh
routes
```

Return to the terminal to show where the chat handler checks the incoming message and the generated document.

```sh
heading 'Input and output guardrail checks in the Gradio handler'
rg -n 'await check_content\((message|document),' ../../../doc-gen-agent/app.py
```

**99 · Restore and compare.** Compare the displayed routes, token volume, and GitOps annotations with the baseline. A difference prints the affected lines. Check rollout readiness separately.

```sh
./99-restore.sh
heading 'Restored configuration compared with the pre-demo baseline'
if demo_state | diff -u "$demo_before" -; then
  printf 'Routes, token volume, and Argo CD settings match the baseline.\n'
fi
heading 'Restored doc-gen-agent rollout'
oc -n doc-gen-demo rollout status deployment/doc-gen-agent --timeout=2m
```

Run restore from the same workstation and local account, with oc pointing to the same cluster. The first scenario saves the two containers' environment lists, the MCP token volume, and both Argo CD skip-reconcile annotations under `$XDG_STATE_HOME/doc-gen-basic-flow` (default `~/.local/state/doc-gen-basic-flow`). Keep this private snapshot until restore succeeds. Switching scenarios reuses it. Avoid concurrent edits to those fields during the session. A failed rollout or interrupted setup retains the snapshot for recovery.

The scripts temporarily pause reconciliation for the root and doc-gen-agent Argo CD Applications. Direct-model scenarios copy the upstream provider key to a temporary Secret in doc-gen-demo so OGX can reference it. Restore removes that Secret after rollout and reinstates both original annotation values. No additional Python packages are required.

The deployed UI enforces NeMo policy on user input and generated output. The diagram illustrates finer-grained per-model-message checks. OGX controls tool discovery and inference order, and the UI can surface gateway failures as agent errors. Allowed scenarios assume a functioning baseline with sufficient model/tool budget. The two gateway denial scenarios demonstrate authentication rejection scoped to the agent.
