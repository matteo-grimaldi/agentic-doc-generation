#!/usr/bin/env bash
# Shared helpers for the read-only gateway wiring tour.
# Never applies manifests, never prints Secrets / tokens / API keys.
#
# Screen scripts live in demo/gateways/; presenter notes in demo/gateways/notes/.
# Both source this file (notes use ../lib.sh). DEMO_DIR is always demo/gateways/.

set -euo pipefail

DEMO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${DEMO_DIR}/../.." && pwd)"

AI_GATEWAY_DIR="${REPO_ROOT}/ai-gateway/k8s"
MCP_GATEWAY_DIR="${REPO_ROOT}/mcp-gateway/k8s"
DOC_GEN_K8S="${REPO_ROOT}/doc-gen-agent/k8s"
DOC_GEN_AGENT_PY="${REPO_ROOT}/doc-gen-agent/agent.py"

DEMO_NS="${DEMO_NS:-doc-gen-demo}"
MCP_NS="${MCP_NS:-mcp-gateway}"
MAAS_NS="${MAAS_NS:-models-as-a-service}"
LLM_NS="${LLM_NS:-llm}"
MAAS_GATEWAY_NS="${MAAS_GATEWAY_NS:-openshift-ingress}"

# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

demo_banner() {
  echo
  echo "================================================================"
  echo " $*"
  echo "================================================================"
}

demo_step() {
  echo
  echo "--- $* ---"
}

# Presenter-facing line (notes/ scripts only — keep off the shared screen).
demo_readout() {
  echo
  echo ">>> $*"
}

# ---------------------------------------------------------------------------
# Syntax highlighting (YAML / Python excerpts)
# Prefer bat → pygmentize → rich; plain cat when none available or no TTY.
# Force color with DEMO_COLOR=1; disable with DEMO_COLOR=0.
# ---------------------------------------------------------------------------

_color_enabled() {
  case "${DEMO_COLOR:-}" in
    0|false|no|off) return 1 ;;
    1|true|yes|on|always) return 0 ;;
  esac
  [[ -t 1 ]]
}

_guess_lexer() {
  local path="$1"
  case "${path##*.}" in
    py) echo python ;;
    yml|yaml) echo yaml ;;
    sh) echo bash ;;
    *) echo yaml ;;
  esac
}

# Highlight a file. Args: path [lexer]
highlight_file() {
  local path="$1"
  local lexer="${2:-$(_guess_lexer "${path}")}"
  if ! _color_enabled; then
    cat "${path}"
    return 0
  fi
  if command -v bat >/dev/null 2>&1; then
    bat --paging=never --style=plain --color=always --language="${lexer}" "${path}"
    return 0
  fi
  if command -v batcat >/dev/null 2>&1; then
    batcat --paging=never --style=plain --color=always --language="${lexer}" "${path}"
    return 0
  fi
  if command -v pygmentize >/dev/null 2>&1; then
    pygmentize -l "${lexer}" -f terminal256 "${path}"
    return 0
  fi
  if command -v rich >/dev/null 2>&1; then
    rich "${path}" --syntax --lexer "${lexer}" --theme monokai
    return 0
  fi
  cat "${path}"
}

# Highlight stdin. Args: [lexer] (default yaml)
highlight_stream() {
  local lexer="${1:-yaml}"
  if ! _color_enabled; then
    cat
    return 0
  fi
  if command -v bat >/dev/null 2>&1; then
    bat --paging=never --style=plain --color=always --language="${lexer}" -
    return 0
  fi
  if command -v batcat >/dev/null 2>&1; then
    batcat --paging=never --style=plain --color=always --language="${lexer}" -
    return 0
  fi
  if command -v pygmentize >/dev/null 2>&1; then
    pygmentize -l "${lexer}" -f terminal256
    return 0
  fi
  if command -v rich >/dev/null 2>&1; then
    rich - --syntax --lexer "${lexer}" --theme monokai
    return 0
  fi
  cat
}

show_yaml() {
  local path="$1"
  demo_step "YAML: ${path#"${REPO_ROOT}/"}"
  highlight_file "${path}" yaml
  echo
}

# Print lines matching a sed address range from a file (1-based inclusive).
show_file_range() {
  local path="$1"
  local start="$2"
  local end="$3"
  local label="${4:-${path#"${REPO_ROOT}/"}:${start}-${end}}"
  local lexer
  lexer="$(_guess_lexer "${path}")"
  demo_step "${label}"
  sed -n "${start},${end}p" "${path}" | highlight_stream "${lexer}"
  echo
}

require_oc() {
  command -v oc >/dev/null 2>&1 || {
    echo "error: oc not found in PATH" >&2
    exit 1
  }
  oc whoami >/dev/null 2>&1 || {
    echo "error: oc is not logged in" >&2
    exit 1
  }
}

# Live object dump. Never use for Secrets.
show_live() {
  local kind="$1"
  local name="$2"
  local ns="${3:-}"
  local yaml=""
  demo_step "Live: ${kind}/${name}${ns:+ (ns/${ns})}"
  # Avoid "${arr[@]}" under set -u when arr is empty (cluster-scoped kinds).
  if [[ -n "${ns}" ]]; then
    yaml="$(oc get "${kind}" "${name}" -n "${ns}" -o yaml 2>/dev/null || true)"
  else
    yaml="$(oc get "${kind}" "${name}" -o yaml 2>/dev/null || true)"
  fi
  if [[ -z "${yaml}" ]]; then
    echo "(not found or not reachable — committed YAML above is the source of truth)"
  else
    printf '%s\n' "${yaml}" | highlight_stream yaml
  fi
  echo
}

# Print selected ConfigMap data keys only (never dump whole Secret).
show_live_configmap_keys() {
  local name="$1"
  local ns="$2"
  shift 2
  local key
  demo_step "Live ConfigMap/${name} keys (ns/${ns})"
  if ! oc get configmap "${name}" -n "${ns}" >/dev/null 2>&1; then
    echo "(ConfigMap/${name} not found in ${ns})"
    echo
    return 0
  fi
  for key in "$@"; do
    local val
    val="$(oc get configmap "${name}" -n "${ns}" \
      -o "jsonpath={.data.${key}}" 2>/dev/null || true)"
    if [[ -z "${val}" ]]; then
      echo "  ${key}: (empty or absent)"
    else
      echo "  ${key}: ${val}"
    fi
  done
  echo
}

show_gateway_programmed() {
  local name="$1"
  local ns="$2"
  demo_step "Live Gateway/${name} Programmed (ns/${ns})"
  local status
  status="$(oc get gateway "${name}" -n "${ns}" \
    -o jsonpath='{.status.conditions[?(@.type=="Programmed")].status}' 2>/dev/null || true)"
  if [[ -z "${status}" ]]; then
    echo "(Gateway/${name} not found or status unavailable)"
  else
    echo "  Programmed: ${status}"
  fi
  echo
}
