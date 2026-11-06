import pytest
import yaml

URL = "https://github.com/test/demo"


def test_classification_signals(agent_modules):
    agent = agent_modules[0]
    paths = ["CATALOG-INFO.YML", "k8s/deployment.yaml", "k8s/docker-compose.yml", "gitops/application.yaml", "Dockerfile.dev", "pom.xml", "docs/openapi.yaml", "asyncapi.json", "api/demo.proto", "api/demo.graphql", "src/controllers/Demo.java", "CODEOWNERS", ".github/workflows/build.yml", "mkdocs.yml", "README.md"]
    result = agent._classify_catalog_files(paths)
    assert result["existing_catalog_info"] == paths[0]
    assert result["k8s_manifests"] == ["k8s/deployment.yaml"]
    assert result["argocd_manifests"] == ["gitops/application.yaml"]
    assert result["dockerfile"] == "Dockerfile.dev" and result["build_manifests"] == ["pom.xml"]
    assert result["openapi_spec"] == "docs/openapi.yaml" and result["asyncapi_spec"] == "asyncapi.json"
    assert result["proto_files"] == ["api/demo.proto"] and result["graphql_schemas"] == ["api/demo.graphql"]
    assert all(result[key] for key in ("has_readme", "has_codeowners", "has_ci_config", "has_techdocs_config"))


def test_empty_classification_and_limits(agent_modules):
    agent = agent_modules[0]
    result = agent._classify_catalog_files([])
    assert not any(result.values())
    result = agent._classify_catalog_files([f"k8s/{i}.yaml" for i in range(20)] + [f"api/{i}.proto" for i in range(20)] + [f"routes/{i}.py" for i in range(20)])
    assert len(result["k8s_manifests"]) == agent.MAX_CLASSIFIED_K8S_FILES
    assert len(result["proto_files"]) == agent.MAX_CLASSIFIED_PROTO_FILES
    assert len(result["api_signal_paths"]) == agent.MAX_CLASSIFIED_API_FILES


@pytest.mark.parametrize("structure", [None, {"error": "offline"}])
def test_user_message_discovery(agent_modules, structure):
    text = agent_modules[0]._build_user_message(URL, structure)
    assert URL in text and "call get_repo_structure" in text


def test_augment_message_and_no_api_evidence(agent_modules):
    text = agent_modules[0]._build_user_message(URL, {"ref": "release", "files": [{"path": "catalog-info.yaml"}, {"path": "README.md"}, None]})
    for fragment in ("release", "catalog-info.yaml", "augment rather than", "No API surface signals", "do not call get_repo_info"):
        assert fragment in text


@pytest.mark.parametrize("raw,expected", [("", ""), ("narration only", ""), ("```yaml\nkind: Component\n```", ""), ("apiVersion: other/v1\nkind: Component", ""), ("```yaml\napiVersion: backstage.io/v1alpha1\nkind: Component\n```", "apiVersion: backstage.io/v1alpha1\nkind: Component\n")])
def test_extraction(agent_modules, raw, expected):
    assert agent_modules[0].extract_catalog_info(raw) == expected


def test_multi_document_relationships(agent_modules, profile):
    component = profile.output + "  providesApis: [demo-api]\n"
    api = "apiVersion: backstage.io/v1alpha1\nkind: API\nmetadata:\n  name: demo-api\nspec:\n  type: openapi\n  definition:\n    $text: ./openapi.yaml\n"
    result = agent_modules[0].extract_catalog_info("```yaml\n" + component + "---\n" + api + "```")
    docs = list(yaml.safe_load_all(result))
    assert len(docs) == 2
    assert docs[0]["spec"]["providesApis"] == [docs[1]["metadata"]["name"]]
    assert docs[1]["spec"]["definition"] == {"$text": "./openapi.yaml"}
