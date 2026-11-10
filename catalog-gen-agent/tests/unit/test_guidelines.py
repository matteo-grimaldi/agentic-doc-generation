import pytest


@pytest.mark.parametrize("contents", [(None, None), ("", " "), ("first", "second"), ("first", None)])
def test_guideline_loading(agent_modules, tmp_path, contents):
    agent = agent_modules[0]
    names = agent.GUIDELINE_FILES
    for name, content in zip(names, contents):
        if content is not None:
            (tmp_path / name).write_text(content)
    result = agent._load_guidelines(str(tmp_path), names)
    expected = [f"### {name}\n\n{content}" for name, content in zip(names, contents) if content and content.strip()]
    assert result == "\n\n".join(expected)


def test_guideline_explicit_order(agent_modules, tmp_path):
    for name in ("a.md", "b.md"):
        (tmp_path / name).write_text(name)
    result = agent_modules[0]._load_guidelines(str(tmp_path), ("b.md", "a.md"))
    assert result.index("### b.md") < result.index("### a.md")
