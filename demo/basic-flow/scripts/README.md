# Prepare the existing Gradio UI

Log in with oc and run a numbered script from this directory. Each script configures the existing doc-gen-agent Deployment in doc-gen-demo, waits for rollout, and prints the Gradio URL and the text to paste. Submit the text yourself. Scripts can be run in any order.

| Script | What to observe in Gradio |
|---|---|
| `./01-basic.sh` | OGX calls external Qwen directly and uses the GitHub MCP service directly. |
| `./02-tooling-blocked.sh` | Direct inference is configured. MCP Gateway rejects the agent token because its audience is wrong. |
| `./03-tooling-allowed.sh` | Direct inference is configured and MCP uses the original gateway and token. |
| `./04-ai-blocked.sh` | AI Gateway rejects the demo credential on the agent's inference request. |
| `./05-ai-allowed.sh` | The agent uses its original AI Gateway and MCP Gateway credentials. |
| `./06-guardrails-blocked.sh` | Paste the printed prompt including the instruction override. NeMo rejects it before generation. |
| `./07-guardrails-allowed.sh` | Paste the plain URL. The agent checks input, generates using the two gateways, and checks output. |
| `./99-restore.sh` | Restore the configuration saved immediately before the first scenario. |

Run restore from the same workstation and local account, with oc pointing to the same cluster. The first scenario saves the two containers' environment lists, the MCP token volume, and both Argo CD skip-reconcile annotations under `$XDG_STATE_HOME/doc-gen-basic-flow` (default `~/.local/state/doc-gen-basic-flow`). Keep this private snapshot until restore succeeds. Switching scenarios reuses it. Avoid concurrent edits to those fields during the session. A failed rollout or interrupted setup retains the snapshot for recovery.

The scripts temporarily pause reconciliation for the root and doc-gen-agent Argo CD Applications. Direct-model scenarios copy the upstream provider key to a temporary Secret in doc-gen-demo so OGX can reference it. Restore removes that Secret after rollout and reinstates both original annotation values. No additional Python packages are required.

The deployed UI enforces NeMo policy on user input and generated output. The diagram illustrates finer-grained per-model-message checks. OGX controls tool discovery and inference order, and the UI can surface gateway failures as agent errors. Allowed scenarios assume a functioning baseline with sufficient model/tool budget. The two gateway denial scenarios demonstrate authentication rejection scoped to the agent.
