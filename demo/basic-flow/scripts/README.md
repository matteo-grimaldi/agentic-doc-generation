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

In the presentation terminal, define these read-only shortcuts once. They require `oc` and `jq`. The environment view prints only the routing settings and the source of the inference credential.

```sh
agent_spec() {
  oc -n doc-gen-demo get deployment/doc-gen-agent -o json | jq '.spec.template.spec'
}
routes() {
  agent_spec | jq -r '.containers[] | .name as $container | .env[] |
    select(.name == "INFERENCE_BASE_URL" or .name == "INFERENCE_API_KEY" or
           .name == "MCP_GATEWAY_URL" or .name == "NEMO_GUARDRAILS_URL") |
    [$container, .name, (if .valueFrom.secretKeyRef then "Secret/" + .valueFrom.secretKeyRef.name
      elif .valueFrom.configMapKeyRef then "ConfigMap/" + .valueFrom.configMapKeyRef.name + ":" + .valueFrom.configMapKeyRef.key
      elif .name == "INFERENCE_API_KEY" then "literal (hidden)"
      else .value end)] | @tsv'
}
token_volume() {
  agent_spec | jq '.volumes[] | select(.name == "mcp-gateway-token")'
}
gateway_audience() {
  oc -n mcp-gateway get authpolicy/mcp-gateway-tokenreview -o json |
    jq -r '.spec.rules.authentication["k8s-token"].kubernetesTokenReview.audiences[]'
}
gitops_state() {
  oc -n openshift-gitops get applications doc-gen-demo-root doc-gen-agent -o json |
    jq -r '.items[] | [.metadata.name, (.metadata.annotations["argocd.argoproj.io/skip-reconcile"] // "(unset)")] | @tsv' | sort
}
```

Before the first scenario, capture the displayed fields for the restore comparison. Keep the same terminal open.

```sh
demo_before=$(mktemp)
{ routes; token_volume; gitops_state; } > "$demo_before"
```

**01-basic.sh** — Run after setup. Compare the direct inference URL with the upstream provider host. The MCP URL points to the GitHub MCP service, and the volume projects the pod name.

```sh
routes
token_volume
oc -n llm get externalprovider/workshop-maas -o json | jq -r '.spec.endpoint'
```

**02-tooling-blocked.sh** — Show the projected audience `basic-flow-denied` alongside the gateway's accepted audience `mcp-gateway`.

```sh
routes
token_volume | jq -r '.projected.sources[].serviceAccountToken.audience'
gateway_audience
```

**03-tooling-allowed.sh** — The two audiences now match. Resolve the gateway URL from the ConfigMap and show that inference still uses the direct URL.

```sh
token_volume | jq -r '.projected.sources[].serviceAccountToken.audience'
gateway_audience
oc -n doc-gen-demo get configmap/cluster-config -o json | jq -r '.data.mcp_gateway_url'
routes
```

**04-ai-blocked.sh** — Show the AI Gateway URL and the credential source `literal (hidden)`. Submit the URL in Gradio to demonstrate the rejection.

```sh
oc -n doc-gen-demo get configmap/cluster-config -o json | jq -r '.data.model_base_url'
routes
```

**05-ai-allowed.sh** — Show that the inference credential comes from the original Secret reference. Show the model and provider the gateway routes to.

```sh
routes
oc -n llm get externalmodel/qwen38-27b -o json |
  jq '.spec | {modelName, providers: [.externalProviderRefs[] | {provider: .ref.name, targetModel}]}'
```

**06-guardrails-blocked.sh** — Show the NeMo URL with the first command. After submitting the script's override prompt in Gradio, run the second command to show the input-phase denial.

```sh
routes
oc -n doc-gen-demo logs deployment/doc-gen-agent -c doc-gen-agent --since=5m |
  grep -F 'NeMo Guardrails denied input' | tail -3
```

**07-guardrails-allowed.sh** — Show the enabled NeMo URL, then submit the plain repository URL. After generation, inspect the chat handler from this directory to show where the input and output checks occur. The completed document in Gradio demonstrates the user-visible outcome.

```sh
routes
rg -n 'await check_content\((message|document),' ../../../doc-gen-agent/app.py
```

**99-restore.sh** — After restore, compare the displayed routes, token volume, and GitOps annotations against the captured values. An empty diff means those fields match. The rollout command confirms the restored Deployment is ready.

```sh
{ routes; token_volume; gitops_state; } | diff -u "$demo_before" -
oc -n doc-gen-demo rollout status deployment/doc-gen-agent --timeout=2m
```

Run restore from the same workstation and local account, with oc pointing to the same cluster. The first scenario saves the two containers' environment lists, the MCP token volume, and both Argo CD skip-reconcile annotations under `$XDG_STATE_HOME/doc-gen-basic-flow` (default `~/.local/state/doc-gen-basic-flow`). Keep this private snapshot until restore succeeds. Switching scenarios reuses it. Avoid concurrent edits to those fields during the session. A failed rollout or interrupted setup retains the snapshot for recovery.

The scripts temporarily pause reconciliation for the root and doc-gen-agent Argo CD Applications. Direct-model scenarios copy the upstream provider key to a temporary Secret in doc-gen-demo so OGX can reference it. Restore removes that Secret after rollout and reinstates both original annotation values. No additional Python packages are required.

The deployed UI enforces NeMo policy on user input and generated output. The diagram illustrates finer-grained per-model-message checks. OGX controls tool discovery and inference order, and the UI can surface gateway failures as agent errors. Allowed scenarios assume a functioning baseline with sufficient model/tool budget. The two gateway denial scenarios demonstrate authentication rejection scoped to the agent.
