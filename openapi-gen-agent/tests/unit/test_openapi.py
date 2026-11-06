from unittest.mock import AsyncMock, Mock

import pytest
import yaml

URL = "https://github.com/test/demo"


@pytest.mark.parametrize("paths,needed", [
    ([], False), (["README.md"], False), (["pom.xml"], False),
    (["src/controllers/Demo.java"], False),
    (["pom.xml", "src/controllers/Demo.java"], True),
    (["requirements.txt", "routes/demo.py"], True),
    (["POM.XML", "SRC/CONTROLLERS/Demo.java"], True),
    (["pom.xml", "controllers/Demo.java", "docs/OPENAPI.YAML"], False),
    (["pom.xml", "controllers/Demo.java", "swagger.json"], False),
])
def test_detection(agent_modules, paths, needed):
    assert agent_modules[0].detect_needs_openapi({"files": [{"path": path} for path in paths]}) is needed


@pytest.mark.parametrize("structure", [{}, {"files": None}, {"files": [None, "bad", {}, {"size": 1}]}])
def test_detection_empty_entries(agent_modules, structure):
    assert agent_modules[0].detect_needs_openapi(structure) is False


def test_classifier_limits_and_non_java(agent_modules):
    agent = agent_modules[0]
    files = ["pom.xml"] + [f"src/controllers/C{i}.java" for i in range(12)] + [f"src/dto/M{i}.java" for i in range(12)]
    result = agent._classify_api_files(files)
    assert result["is_java"] and result["build_file"] == "pom.xml"
    assert result["controllers"] == files[1:1 + agent.MAX_CLASSIFIED_CONTROLLERS]
    assert len(result["models"]) == agent.MAX_CLASSIFIED_MODELS
    assert agent._classify_api_files(["routes/demo.py"])["is_java"] is False


@pytest.mark.parametrize("structure", [None, {"error": "offline"}])
def test_user_message_discovery_fallback(agent_modules, structure):
    text = agent_modules[0]._build_user_message(URL, structure)
    assert URL in text and "call get_repo_structure" in text


def test_message_exact_paths_and_ref(agent_modules):
    text = agent_modules[0]._build_user_message(URL, {"ref": "release", "files": [{"path": "pom.xml"}, {"path": "src/controllers/Demo.java"}, {"path": "src/dto/Demo.java"}, None]})
    assert "release" in text and "src/controllers/Demo.java" in text and "src/dto/Demo.java" in text
    assert "do not call get_repo_info" in text


@pytest.mark.parametrize("raw,expected", [("", ""), (" \n", ""), ("NOT_AN_API: CLI", ""), ("not_an_api: library", ""), ('```yaml\n# comment\nopenapi: "3.0.3"\npaths: {}\n```', 'openapi: "3.0.3"\npaths: {}\n'), ('narration\nopenapi: "3.0.3"\npaths: {}', 'openapi: "3.0.3"\npaths: {}\n')])
def test_spec_extraction(agent_modules, raw, expected):
    assert agent_modules[0].extract_openapi_spec(raw) == expected


def test_extraction_is_not_a_validator(agent_modules):
    # Current behavior retains unrelated nonempty fenced text; do not call it valid OpenAPI.
    assert agent_modules[0].extract_openapi_spec('```yaml\nhello: world\n```') == "hello: world\n"


def test_fixture_yaml_contract(agent_modules, profile):
    result = yaml.safe_load(agent_modules[0].extract_openapi_spec(profile.stream))
    assert result["openapi"].startswith("3.0.")
    assert result["info"]["title"] == "Demo" and result["paths"] == {}


@pytest.mark.parametrize("content,expected", [(None, ""), ("", ""), (" \n ", ""), ("style", "style")])
def test_knowledge_loader(agent_modules, tmp_path, content, expected):
    path = tmp_path / "knowledge.md"
    if content is not None:
        path.write_text(content)
    assert agent_modules[0]._load_knowledge(str(path)) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["chat", "background"])
@pytest.mark.parametrize("kind", ["preflight-skip", "model-skip"])
async def test_skip_never_proposes(agent_modules, monkeypatch, path, kind):
    app = agent_modules[1]
    structure = {"files": [] if kind == "preflight-skip" else [{"path": "pom.xml"}, {"path": "controllers/Demo.java"}]}
    monkeypatch.setattr(app, "_fetch_repo_structure", AsyncMock(return_value=structure))
    calls = []
    async def run(*args, **kwargs):
        calls.append(args)
        yield "NOT_AN_API: CLI only"
    monkeypatch.setattr(app, "run_agent", run)
    proposal, report = AsyncMock(), AsyncMock()
    monkeypatch.setattr(app, "_propose_openapi_spec", proposal)
    monkeypatch.setattr(app, "_report_to_governance", report)
    if path == "chat":
        result = [part async for part in app.chat(URL, [])]
        assert result[-1][1:] == ("", "")
    else:
        await app._run_agent_in_background(URL, "job")
    proposal.assert_not_awaited()
    assert len(calls) == int(kind == "model-skip")
    # Chat reports model bail-out as skipped; the worker's empty extraction is failed.
    expected = "failed" if kind == "model-skip" and path == "background" else "skipped"
    assert report.await_args.args[2] == expected
