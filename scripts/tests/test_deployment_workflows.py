"""Offline orchestration checks. Requires Python 3 + PyYAML; never contacts oc.

Run: python3 -m unittest discover -s scripts/tests -v
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]
AGENTS = ("doc-gen-agent", "openapi-gen-agent", "catalog-gen-agent", "compliance-scan-agent")
COMPONENTS = ("github-mcp-server", *AGENTS, "governance-hub", "compliance-mcp-server")

FAKE_OC = r'''#!/usr/bin/env python3
import json, os, re, sys
from pathlib import Path
import yaml
a = sys.argv[1:]
state_path = Path(os.environ["FAKE_OC_STATE"])
s = json.loads(state_path.read_text())
record = {"args": a}
with open(os.environ["FAKE_OC_LOG"], "a") as f:
    f.write(json.dumps(record) + "\n")
def save(): state_path.write_text(json.dumps(s))
def emit(obj): print(json.dumps(obj))
def store(obj):
    if obj:
        s[obj["kind"] + "/" + obj["metadata"]["name"]] = obj
def opt(name, default=""):
    return a[a.index(name)+1] if name in a else default
if a[0] == "whoami":
    print("test-token" if "-t" in a else "test-admin")
elif a[0] == "get":
    kind, name = a[1], a[2] if len(a)>2 else ""
    if name == os.getenv("FAKE_OC_MISSING", ""):
        sys.exit(1)
    if kind == "configmap":
        if os.getenv("FAKE_OC_READ_ERROR") == "1": sys.exit(1)
        obj = s.get("ConfigMap/"+name)
        if not obj:
            sys.exit(0 if "--ignore-not-found" in a else 1)
        fmt = opt("-o")
        if fmt == "json": emit(obj)
        elif fmt == "name": print("configmap/"+name)
        elif "jsonpath" in fmt:
            key = fmt.split(".data.")[-1].split("}")[0]
            print(obj.get("data", {}).get(key, ""), end="")
    elif kind == "imagestream":
        sys.exit(0 if "ImageStream/"+name in s else 1)
    elif kind == "imagestreamtag":
        sys.exit(0 if "ImageStreamTag/"+name in s else 1)
    elif kind == "deployment" and "containers[" in opt("-o"):
        index = int(re.search(r"containers\[(\d+)\]", opt("-o")).group(1))
        containers = s.get("Deployment/"+name, {}).get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
        if index < len(containers): print(containers[index]["name"], end="")
    elif kind == "build": print("Complete", end="")
    # Other prerequisite resources exist; optional lists/routes are empty.
elif a[0] == "new-build":
    name = next(x.split("=",1)[1] for x in a if x.startswith("--name="))
    s["ImageStream/"+name] = {}; save()
elif a[0] == "start-build":
    name = a[1]
    s["ImageStreamTag/"+name+":latest"] = {}; save()
    print("build/"+name+"-1")
elif a[0] == "create":
    if "-f" in a:
        emit(yaml.safe_load(Path(opt("-f")).read_text()))
    elif a[1] in ("configmap", "secret"):
        secret = a[1] == "secret"
        name = a[3] if secret else a[2]
        data = {}
        for arg in a:
            if arg.startswith("--from-file="):
                spec = arg.split("=",1)[1]
                if "=" in spec:
                    key, path = spec.split("=",1)
                    data[key] = Path(path).read_text()
                else:
                    data.update({p.name:p.read_text() for p in Path(spec).iterdir() if p.is_file()})
        emit({"apiVersion":"v1", "kind":"Secret" if secret else "ConfigMap",
              "metadata":{"name":name}, "data":data})
elif a[0] == "apply":
    path = opt("-f")
    if path == "-":
        objects = list(yaml.safe_load_all(sys.stdin.read()))
    else:
        p = Path(path)
        files = sorted(p.glob("*.yaml")) if p.is_dir() else [p]
        objects = [obj for f in files for obj in yaml.safe_load_all(f.read_text())]
    for obj in objects: store(obj)
    save()
elif a[0] == "patch" and a[1:3] == ["configmap", "cluster-config"]:
    patch = json.loads(opt("-p"))
    s["ConfigMap/cluster-config"]["data"].update(patch["data"]); save()
elif a[0] == "wait" and "job/mcp-virtualserver-config-sync" in a:
    if os.getenv("FAKE_OC_JOB_FAIL") == "1": sys.exit(1)
'''


class DeploymentWorkflows(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for directory in ("scripts", "gitops", "mcp-gateway"):
            shutil.copytree(ROOT / directory, self.root / directory,
                            ignore=shutil.ignore_patterns("tests", "__pycache__"))
        for component in COMPONENTS:
            target = self.root / component
            target.mkdir()
            shutil.copy2(ROOT / component / "build-and-deploy.sh", target)
            shutil.copytree(ROOT / component / "k8s", target / "k8s")
            for name in ("ogx", "guidelines"):
                if (ROOT / component / name).exists():
                    shutil.copytree(ROOT / component / name, target / name)
            for name in ("knowledge.md", "openapi-knowledge.md"):
                if (ROOT / component / name).exists():
                    shutil.copy2(ROOT / component / name, target)
        shutil.copy2(ROOT / "build-and-deploy-all.sh", self.root)
        for component in ("ci-cd-pipeline", "keycloak", "developer-hub"):
            target = self.root / component
            target.mkdir()
            stub = target / "build-and-deploy.sh"
            stub.write_text(f'#!/usr/bin/env bash\nset -eu\noc marker {component}\n')
            stub.chmod(0o755)
        # Keep platform installation outside this orchestration test: model its
        # existing public contract (patch URL after the gateway is programmed).
        (self.root / "install-mcp-gateway.sh").write_text(
            '#!/usr/bin/env bash\nset -eu\noc marker installer\n'
            'oc patch configmap cluster-config -n doc-gen-demo --type=merge '
            '-p \'{"data":{"mcp_gateway_url":"http://programmed-gateway:8080/mcp"}}\'\n'
        )
        fake = self.root / "bin" / "oc"
        fake.parent.mkdir()
        fake.write_text(FAKE_OC)
        fake.chmod(0o755)
        self.state_path = self.root / "state.json"
        self.state_path.write_text("{}")
        self.log_path = self.root / "calls.jsonl"
        self.env = dict(os.environ)
        for key in ("NAMESPACE", "PREPARE_ONLY", "GITHUB_TOKEN", "EXTERNAL_MODEL_API_KEY",
                    "OGX_MLFLOW_SIDECAR_MODE", "GITOPS_PREFLIGHT_WARN_ONLY", "MCP_SYSTEM_NS"):
            self.env.pop(key, None)
        self.env.update(PATH=str(fake.parent)+os.pathsep+self.env["PATH"],
                        FAKE_OC_STATE=str(self.state_path), FAKE_OC_LOG=str(self.log_path),
                        MLFLOW_TRACKING_TOKEN="test-token", PRUNE_BUILDS_BEFORE_BUILD="false")

    def run_script(self, name, *args, ok=True, **env):
        result = subprocess.run(["bash", str(self.root / name), *args],
                                env={**self.env, **env}, text=True, capture_output=True, timeout=30)
        if ok:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def state(self):
        return json.loads(self.state_path.read_text())

    def calls(self):
        return [json.loads(x)["args"] for x in self.log_path.read_text().splitlines()]

    def test_configuration_preserves_urls_and_uses_canonical_stack(self):
        old = {"kind":"ConfigMap", "metadata":{"name":"cluster-config"},
               "data":{"model_base_url":"https://live-model/v1", "mcp_gateway_url":"http://live-mcp/mcp",
                       "model_name":"old-model"}}
        self.state_path.write_text(json.dumps({"ConfigMap/cluster-config":old}))
        self.run_script("scripts/configure-agents.sh", "catalog-gen-agent")
        state = self.state()
        self.assertEqual(state["ConfigMap/cluster-config"]["data"]["model_base_url"], "https://live-model/v1")
        self.assertEqual(state["ConfigMap/cluster-config"]["data"]["mcp_gateway_url"], "http://live-mcp/mcp")
        desired = yaml.safe_load((ROOT / "doc-gen-agent/k8s/cluster-config.yaml").read_text())
        self.assertEqual(state["ConfigMap/cluster-config"]["data"]["model_name"], desired["data"]["model_name"])
        self.assertEqual(state["ConfigMap/ogx-stack-config"]["data"]["stack_run_config.yaml"],
                         (ROOT / "doc-gen-agent/ogx/stack_run_config.yaml").read_text())
        self.assertIn("ConfigMap/catalog-gen-knowledge", state)
        self.assertNotIn("ConfigMap/agent-knowledge", state)

    def test_configuration_read_error_does_not_overwrite_config(self):
        self.run_script("scripts/configure-agents.sh", ok=False, FAKE_OC_READ_ERROR="1")
        self.assertNotIn("ConfigMap/cluster-config", self.state())

    def test_installer_url_patch_seeds_config_before_preparation(self):
        script = self.root / "patch-url.sh"
        script.write_text('set -euo pipefail\nsource "'+str(self.root / 'scripts/lib-demo-agents.sh')+'"\n'
                          'patch_cluster_config_data doc-gen-demo \'{"data":{"model_base_url":"https://discovered/v1"}}\'\n')
        self.run_script("patch-url.sh")
        self.run_script("scripts/configure-agents.sh")
        self.assertEqual(self.state()["ConfigMap/cluster-config"]["data"]["model_base_url"], "https://discovered/v1")

    def test_preparation_builds_all_images_without_deploying_workloads(self):
        self.run_script("gitops/prepare.sh")
        state = self.state()
        for component in COMPONENTS:
            self.assertIn("ImageStreamTag/"+component+":latest", state)
        for name in ("cluster-config", "ogx-stack-config", "agent-knowledge", "openapi-agent-knowledge",
                     "catalog-gen-knowledge", "compliance-scan-knowledge"):
            self.assertIn("ConfigMap/"+name, state)
        self.assertFalse(any(k.split("/")[0] in ("Deployment", "MCPServer", "Route", "Service") for k in state))
        self.assertFalse(any(c[:2] == ["rollout", "restart"] for c in self.calls()))
        self.run_script("gitops/preflight.sh")

    def test_preflight_rejects_missing_knowledge_or_image(self):
        self.run_script("gitops/prepare.sh")
        for missing in ("catalog-gen-knowledge", "compliance-mcp-server:latest"):
            result = self.run_script("gitops/preflight.sh", ok=False, FAKE_OC_MISSING=missing)
            self.assertIn("gitops/prepare.sh", result.stderr)
        self.run_script("gitops/preflight.sh", FAKE_OC_MISSING="catalog-gen-knowledge",
                        GITOPS_PREFLIGHT_WARN_ONLY="1")

    def test_gateway_waits_for_compatibility_job_before_url_setup(self):
        self.run_script("mcp-gateway/build-and-deploy.sh")
        calls = self.calls()
        job = next(i for i,c in enumerate(calls) if c[0] == "wait" and "job/mcp-virtualserver-config-sync" in c)
        install = calls.index(["marker", "installer"])
        self.assertLess(job, install)
        self.assertTrue(any(c[:3] == ["delete", "job", "mcp-virtualserver-config-sync"] for c in calls))
        self.assertEqual(self.state()["ConfigMap/cluster-config"]["data"]["mcp_gateway_url"],
                         "http://programmed-gateway:8080/mcp")

    def test_gateway_failure_stops_before_url_setup(self):
        self.run_script("mcp-gateway/build-and-deploy.sh", ok=False, FAKE_OC_JOB_FAIL="1")
        self.assertNotIn(["marker", "installer"], self.calls())

    def test_default_aggregate_deploys_gateway_before_agents_and_includes_portal(self):
        self.run_script("build-and-deploy-all.sh")
        calls = self.calls()
        install = calls.index(["marker", "installer"])
        first_agent_rollout = next(i for i,c in enumerate(calls) if c[:3] == ["rollout", "status", "deployment/doc-gen-agent"])
        self.assertLess(install, first_agent_rollout)
        self.assertLess(calls.index(["wait", "--for=create", "deployment/github-mcp-server", "-n", "doc-gen-demo", "--timeout=180s"]),
                        next(i for i,c in enumerate(calls) if c[:3] == ["rollout", "restart", "deployment/github-mcp-server"]))
        for component in (*AGENTS, "governance-hub"):
            self.assertTrue(any(c[:3] == ["rollout", "restart", "deployment/"+component] for c in calls))
        self.assertEqual(self.state()["ConfigMap/cluster-config"]["data"]["mcp_gateway_url"],
                         "http://programmed-gateway:8080/mcp")
        markers = [c[1] for c in calls if c[0] == "marker"]
        self.assertEqual(markers, ["installer", "ci-cd-pipeline", "keycloak", "developer-hub"])

    def test_agent_refuses_empty_gateway_url_before_build(self):
        self.run_script("catalog-gen-agent/build-and-deploy.sh", ok=False)
        self.assertFalse(any(c[0] == "start-build" for c in self.calls()))


if __name__ == "__main__":
    unittest.main()
