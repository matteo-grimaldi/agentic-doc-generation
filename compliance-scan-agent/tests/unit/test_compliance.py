import json
from unittest.mock import AsyncMock

import pytest

URL = "https://github.com/test/demo"


def test_file_classification(agent_modules):
    result = agent_modules[0]._classify_compliance_files(["k8s/deploy.YAML", "k8s/docker-compose.yml", "Dockerfile.dev", "pom.xml", "README.md", ".gitignore", "LICENSE", ".github/workflows/ci.yml"])
    assert result["k8s_manifests"] == ["k8s/deploy.YAML"]
    assert result["dockerfiles"] == ["Dockerfile.dev"] and result["pom_xml"] == "pom.xml"
    assert all(result[key] for key in ("has_readme", "has_gitignore", "has_license", "has_ci_config"))


def test_empty_classification_and_caps(agent_modules):
    agent = agent_modules[0]
    assert not any(agent._classify_compliance_files([]).values())
    result = agent._classify_compliance_files([f"k8s/{i}.yaml" for i in range(20)] + [f"build/Dockerfile.{i}" for i in range(20)])
    assert len(result["k8s_manifests"]) == agent.MAX_CLASSIFIED_K8S_FILES
    assert len(result["dockerfiles"]) == agent.MAX_CLASSIFIED_DOCKERFILES


@pytest.mark.parametrize("structure", [None, {"error": "offline"}])
def test_user_message_discovery(agent_modules, structure):
    text = agent_modules[0]._build_user_message(URL, structure)
    assert URL in text and "call get_repo_structure" in text


def test_non_maven_non_k8s_message(agent_modules):
    text = agent_modules[0]._build_user_message(URL, {"ref": "release", "files": [{"path": "README.md"}, None]})
    for fragment in ("release", "No K8s/OpenShift", "No pom.xml", "README present: True", "do not call get_repo_info"):
        assert fragment in text
    prompt = agent_modules[0]._build_system_prompt()
    assert "project-structure checks are always applicable" in prompt
    assert "PASS/FAIL/SKIP" in prompt and "HIGH/MEDIUM/LOW" in prompt


@pytest.mark.parametrize("raw,expected", [("", ""), ("narration", ""), ("```markdown\n\n```", ""), ("```md\n# Compliance Report\n```", "# Compliance Report\n"), ("# Compliance Report", "# Compliance Report\n"), ("preamble\n```markdown\n# Compliance Report\n```", "# Compliance Report\n")])
def test_report_extraction(agent_modules, raw, expected):
    assert agent_modules[0].extract_compliance_report(raw) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["chat", "background"])
async def test_report_only_http_contract(agent_modules, profile, monkeypatch, http_mock, path):
    import httpx
    app = agent_modules[1]
    monkeypatch.setattr(app, "GOVERNANCE_HUB_URL", "https://hub.invalid")
    async def run(*args, **kwargs):
        yield profile.stream
    monkeypatch.setattr(app, "run_agent", run)
    calls = []
    def handler(request):
        calls.append((request.method, request.url.path))
        if request.method == "GET" and request.url.path == "/repo-structure":
            return httpx.Response(200, json={"files": []})
        assert request.method == "POST" and request.url.path == "/api/runs", "Compliance attempted an unexpected write"
        payload = json.loads(request.content)
        assert payload["report"] == profile.output and payload["agent_type"] == "compliance-scan"
        return httpx.Response(200, json={})
    http_mock(handler)
    if path == "chat":
        _ = [part async for part in app.chat(URL, [])]
    else:
        await app._run_agent_in_background(URL, "job")
    assert calls == [("GET", "/repo-structure"), ("POST", "/api/runs")]
    assert not hasattr(app, "propose_pr_clicked")
