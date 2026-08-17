from repo_instructions import load_repo_instructions


def test_agents_md_takes_precedence_over_legacy_claude_md(tmp_path):
    (tmp_path / "AGENTS.md").write_text("modern")
    (tmp_path / "CLAUDE.md").write_text("legacy")

    result = load_repo_instructions(str(tmp_path))

    assert "modern" in result
    assert "legacy" not in result


def test_agents_directory_is_loaded_in_stable_order(tmp_path):
    directory = tmp_path / ".agents"
    directory.mkdir()
    (directory / "b.md").write_text("second")
    (directory / "a.md").write_text("first")

    result = load_repo_instructions(str(tmp_path))

    assert result.index("first") < result.index("second")


def test_legacy_files_are_fallback_when_modern_guidance_is_absent(tmp_path):
    rules = tmp_path / ".claude" / "rules"
    rules.mkdir(parents=True)
    (tmp_path / "CLAUDE.md").write_text("legacy root")
    (rules / "style.md").write_text("legacy rule")

    result = load_repo_instructions(str(tmp_path))

    assert "legacy root" in result
    assert "legacy rule" in result


def test_legacy_ignore_gates_are_applied_independently(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("legacy root")
    rules = tmp_path / ".claude" / "rules"
    rules.mkdir(parents=True)
    (rules / "python.md").write_text("legacy rule")

    result = load_repo_instructions(str(tmp_path), ignored=frozenset({"claude_md"}))

    assert "legacy root" not in result
    assert "legacy rule" in result
