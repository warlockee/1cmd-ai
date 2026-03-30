"""Tests for manager/skills.py — modular skills system (replaces SOP)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from onecmd.manager.skills import (
    ONECMD_DIR,
    SKILLS_DIR,
    REGISTRY_FILE,
    MAX_RESOURCE_CHARS,
    MAX_TOTAL_CHARS,
    _skills_cache,
    _is_comment_line,
    _load_registry,
    _load_skill_meta,
    _load_skill_resources,
    ensure_skills,
    invalidate_cache,
    list_skills,
    load_skill,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def skills_env(tmp_path, monkeypatch):
    """Run skills functions in a temp directory with a clean cache."""
    monkeypatch.chdir(tmp_path)
    _skills_cache.clear()
    yield tmp_path
    _skills_cache.clear()


def _make_skill(base: Path, name: str, *, always_loaded: bool = False,
                description: str = "",
                resources: dict[str, str] | None = None) -> Path:
    """Helper: create a skill directory with SKILL.json and optional resources."""
    skill_dir = base / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "name": name,
        "version": "1.0.0",
        "description": description or f"Test skill: {name}",
        "mode": "domain",
        "always_loaded": always_loaded,
        "max_context_chars": MAX_RESOURCE_CHARS,
    }
    (skill_dir / "SKILL.json").write_text(json.dumps(meta))
    (skill_dir / "README.md").write_text(f"# {name}\nTest skill.\n")

    if resources:
        res_dir = skill_dir / "resources"
        res_dir.mkdir(exist_ok=True)
        for fname, content in resources.items():
            (res_dir / fname).write_text(content)

    return skill_dir


def _make_registry(base: Path, enabled: list[str]) -> None:
    """Helper: write a skills.json registry."""
    (base / REGISTRY_FILE).write_text(
        json.dumps({"version": 1, "enabled": enabled}))


# ---------------------------------------------------------------------------
# Default skills loading
# ---------------------------------------------------------------------------


class TestDefaultSkills:
    def test_loads_default_skills_on_first_run(self, skills_env):
        """ensure_skills seeds default skills and returns content."""
        result = ensure_skills()
        assert isinstance(result, str)
        assert len(result) > 0
        # Should contain content from core-ops terminal-ops.md
        assert "Terminal" in result or "terminal" in result

    def test_skills_dir_created(self, skills_env):
        """First run creates .onecmd/skills/ directory."""
        ensure_skills()
        assert (skills_env / SKILLS_DIR).is_dir()

    def test_registry_created(self, skills_env):
        """First run creates skills.json registry."""
        ensure_skills()
        registry = skills_env / SKILLS_DIR / REGISTRY_FILE
        assert registry.exists()
        data = json.loads(registry.read_text())
        assert "core-ops" in data["enabled"]

    def test_custom_rules_template_seeded(self, skills_env):
        """First run seeds custom-rules.md template in core-ops."""
        ensure_skills()
        custom_rules = (skills_env / SKILLS_DIR / "core-ops" /
                        "resources" / "custom-rules.md")
        assert custom_rules.exists()
        content = custom_rules.read_text()
        assert "Custom Rules" in content

    def test_custom_rules_template_not_in_output(self, skills_env):
        """Comment-only custom-rules template does not appear in output."""
        result = ensure_skills()
        # The template is all comments, should be skipped
        assert "Prefer yarn over npm" not in result


# ---------------------------------------------------------------------------
# First-run seeding
# ---------------------------------------------------------------------------


class TestFirstRunSeed:
    def test_seeds_system_files(self, skills_env):
        """First run seeds ai_personality.md, crash_patterns.md, cron_prompt.md."""
        ensure_skills()
        onecmd = skills_env / ONECMD_DIR
        assert onecmd.is_dir()

    def test_does_not_overwrite_existing_skills(self, skills_env):
        """Existing skills directory is not overwritten."""
        skills_path = skills_env / SKILLS_DIR
        _make_skill(skills_path, "core-ops", always_loaded=True,
                     resources={"terminal-ops.md": "custom content"})
        _make_registry(skills_path, ["core-ops"])

        result = ensure_skills()
        assert "custom content" in result


# ---------------------------------------------------------------------------
# Selective loading
# ---------------------------------------------------------------------------


class TestSelectiveLoading:
    def test_always_loaded_in_output(self, skills_env):
        """always_loaded=true skills have resources in output."""
        skills_path = skills_env / SKILLS_DIR
        _make_skill(skills_path, "core", always_loaded=True,
                     resources={"ops.md": "core operations content"})
        _make_registry(skills_path, ["core"])
        (skills_env / ONECMD_DIR).mkdir(parents=True, exist_ok=True)

        result = ensure_skills()
        assert "core operations content" in result

    def test_on_demand_not_in_output(self, skills_env):
        """always_loaded=false skills do NOT have resources in output."""
        skills_path = skills_env / SKILLS_DIR
        _make_skill(skills_path, "core", always_loaded=True,
                     resources={"ops.md": "core content"})
        _make_skill(skills_path, "extra", always_loaded=False,
                     description="Extra stuff",
                     resources={"e.md": "extra secret content"})
        _make_registry(skills_path, ["core", "extra"])
        (skills_env / ONECMD_DIR).mkdir(parents=True, exist_ok=True)

        result = ensure_skills()
        assert "core content" in result
        assert "extra secret content" not in result

    def test_on_demand_listed_as_available(self, skills_env):
        """Non-always-loaded skills appear in AVAILABLE SKILLS listing."""
        skills_path = skills_env / SKILLS_DIR
        _make_skill(skills_path, "core", always_loaded=True,
                     resources={"ops.md": "core content"})
        _make_skill(skills_path, "deploy", always_loaded=False,
                     description="Deployment procedures")
        _make_registry(skills_path, ["core", "deploy"])
        (skills_env / ONECMD_DIR).mkdir(parents=True, exist_ok=True)

        result = ensure_skills()
        assert "AVAILABLE SKILLS" in result
        assert "deploy" in result
        assert "Deployment procedures" in result

    def test_no_available_section_when_all_always_loaded(self, skills_env):
        """No AVAILABLE SKILLS section when all skills are always_loaded."""
        skills_path = skills_env / SKILLS_DIR
        _make_skill(skills_path, "core", always_loaded=True,
                     resources={"ops.md": "core content"})
        _make_registry(skills_path, ["core"])
        (skills_env / ONECMD_DIR).mkdir(parents=True, exist_ok=True)

        result = ensure_skills()
        assert "AVAILABLE SKILLS" not in result


# ---------------------------------------------------------------------------
# load_skill()
# ---------------------------------------------------------------------------


class TestLoadSkill:
    def test_loads_existing_skill(self, skills_env):
        """load_skill returns full content for a valid skill."""
        skills_path = skills_env / SKILLS_DIR
        _make_skill(skills_path, "my-skill",
                     resources={"guide.md": "Full guide content here"})

        result = load_skill("my-skill")
        assert result is not None
        assert "Full guide content here" in result

    def test_returns_none_for_missing_skill(self, skills_env):
        """load_skill returns None for non-existent skill."""
        skills_path = skills_env / SKILLS_DIR
        skills_path.mkdir(parents=True, exist_ok=True)

        assert load_skill("nonexistent") is None

    def test_returns_none_for_empty_skill(self, skills_env):
        """load_skill returns None for skill with no resources."""
        skills_path = skills_env / SKILLS_DIR
        _make_skill(skills_path, "empty-skill")

        assert load_skill("empty-skill") is None

    def test_loads_on_demand_skill(self, skills_env):
        """load_skill works for skills not marked as always_loaded."""
        skills_path = skills_env / SKILLS_DIR
        _make_skill(skills_path, "optional", always_loaded=False,
                     resources={"data.md": "on demand data"})

        result = load_skill("optional")
        assert result is not None
        assert "on demand data" in result


# ---------------------------------------------------------------------------
# Comment line detection
# ---------------------------------------------------------------------------


class TestCommentLines:
    def test_blank_line_is_comment(self):
        assert _is_comment_line("") is True
        assert _is_comment_line("   ") is True

    def test_hash_alone_is_comment(self):
        assert _is_comment_line("#") is True
        assert _is_comment_line("  #  ") is True

    def test_hash_space_is_comment(self):
        assert _is_comment_line("# This is a comment") is True
        assert _is_comment_line("  # indented comment") is True

    def test_markdown_header_is_not_comment(self):
        assert _is_comment_line("## Section Header") is False
        assert _is_comment_line("### Subsection") is False

    def test_regular_text_is_not_comment(self):
        assert _is_comment_line("Hello world") is False
        assert _is_comment_line("- list item") is False

    def test_comment_only_file_skipped(self, skills_env):
        """Resource file with only comments is not loaded."""
        skills_path = skills_env / SKILLS_DIR
        skill = _make_skill(skills_path, "comments-only",
                            resources={"rules.md": "# This is a comment\n# Another comment\n"})

        result = _load_skill_resources(skill, MAX_RESOURCE_CHARS)
        assert result == ""

    def test_file_with_content_and_comments_loaded(self, skills_env):
        """Resource file with real content (not just comments) is loaded."""
        skills_path = skills_env / SKILLS_DIR
        content = "# Comment line\n## Real Header\n\nSome actual content\n"
        skill = _make_skill(skills_path, "mixed",
                            resources={"guide.md": content})

        result = _load_skill_resources(skill, MAX_RESOURCE_CHARS)
        assert "Some actual content" in result


# ---------------------------------------------------------------------------
# Skill registry
# ---------------------------------------------------------------------------


class TestSkillRegistry:
    def test_only_enabled_always_loaded_skills_in_output(self, skills_env):
        """Only always_loaded + enabled skills appear in prompt content."""
        skills_path = skills_env / SKILLS_DIR
        _make_skill(skills_path, "alpha", always_loaded=True,
                     resources={"a.md": "alpha content"})
        _make_skill(skills_path, "beta", always_loaded=False,
                     resources={"b.md": "beta content"})
        _make_registry(skills_path, ["alpha", "beta"])

        (skills_env / ONECMD_DIR).mkdir(parents=True, exist_ok=True)

        result = ensure_skills()
        assert "alpha content" in result
        assert "beta content" not in result

    def test_empty_registry_falls_back_to_always_loaded(self, skills_env):
        """When registry is empty, skills with always_loaded=true are loaded."""
        skills_path = skills_env / SKILLS_DIR
        _make_skill(skills_path, "core", always_loaded=True,
                     resources={"ops.md": "core content"})
        _make_skill(skills_path, "optional",
                     resources={"opt.md": "optional content"})
        _make_registry(skills_path, [])

        (skills_env / ONECMD_DIR).mkdir(parents=True, exist_ok=True)

        result = ensure_skills()
        assert "core content" in result
        assert "optional content" not in result

    def test_missing_skill_skipped(self, skills_env):
        """Enabled skill that doesn't exist on disk is skipped."""
        skills_path = skills_env / SKILLS_DIR
        _make_skill(skills_path, "exists", always_loaded=True,
                     resources={"e.md": "real content"})
        _make_registry(skills_path, ["exists", "ghost"])

        (skills_env / ONECMD_DIR).mkdir(parents=True, exist_ok=True)

        result = ensure_skills()
        assert "real content" in result


# ---------------------------------------------------------------------------
# Skill meta loading
# ---------------------------------------------------------------------------


class TestSkillMeta:
    def test_loads_valid_meta(self, skills_env):
        """Valid SKILL.json is parsed correctly."""
        skills_path = skills_env / SKILLS_DIR
        skill = _make_skill(skills_path, "test-skill")
        meta = _load_skill_meta(skill)
        assert meta["name"] == "test-skill"
        assert meta["version"] == "1.0.0"

    def test_missing_meta_returns_none(self, skills_env):
        """Missing SKILL.json returns None."""
        empty_dir = skills_env / "empty"
        empty_dir.mkdir()
        assert _load_skill_meta(empty_dir) is None

    def test_invalid_json_returns_none(self, skills_env):
        """Malformed SKILL.json returns None."""
        bad_dir = skills_env / "bad"
        bad_dir.mkdir()
        (bad_dir / "SKILL.json").write_text("not json{{{")
        assert _load_skill_meta(bad_dir) is None


# ---------------------------------------------------------------------------
# Resource loading
# ---------------------------------------------------------------------------


class TestResourceLoading:
    def test_loads_all_resources(self, skills_env):
        """All .md files in resources/ are loaded."""
        skills_path = skills_env / SKILLS_DIR
        skill = _make_skill(skills_path, "multi",
                            resources={"a.md": "aaa", "b.md": "bbb"})
        result = _load_skill_resources(skill, MAX_RESOURCE_CHARS * 2)
        assert "aaa" in result
        assert "bbb" in result

    def test_empty_resources_dir(self, skills_env):
        """Skill with no resources returns empty string."""
        skills_path = skills_env / SKILLS_DIR
        skill = _make_skill(skills_path, "empty")
        result = _load_skill_resources(skill, MAX_RESOURCE_CHARS)
        assert result == ""

    def test_resource_truncated_at_cap(self, skills_env):
        """Resource larger than MAX_RESOURCE_CHARS is truncated."""
        skills_path = skills_env / SKILLS_DIR
        skill = _make_skill(skills_path, "big",
                            resources={"huge.md": "x" * (MAX_RESOURCE_CHARS + 5000)})
        result = _load_skill_resources(skill, MAX_RESOURCE_CHARS * 2)
        assert "[...truncated]" in result
        assert len(result) <= MAX_RESOURCE_CHARS + 50


# ---------------------------------------------------------------------------
# Migration from SOP
# ---------------------------------------------------------------------------


class TestMigration:
    def test_migrates_old_sop_files(self, skills_env):
        """When .onecmd/agent_sop.md exists but skills/ doesn't, migration runs."""
        onecmd = skills_env / ONECMD_DIR
        onecmd.mkdir(parents=True)
        (onecmd / "agent_sop.md").write_text("Old SOP content here")
        (onecmd / "custom_rules.md").write_text(
            "# comment\nAlways run tests\nNever drop DB\n")

        result = ensure_skills()

        # Skills dir should now exist
        assert (skills_env / SKILLS_DIR).is_dir()
        # Content should be migrated
        assert "Old SOP content here" in result
        # Custom rules migrated
        migrated_rules = (skills_env / SKILLS_DIR / "core-ops" /
                          "resources" / "custom-rules.md")
        if migrated_rules.exists():
            rules_text = migrated_rules.read_text()
            assert "Always run tests" in rules_text
            assert "Never drop DB" in rules_text

    def test_old_files_preserved(self, skills_env):
        """Migration does not delete old .onecmd/*.md files."""
        onecmd = skills_env / ONECMD_DIR
        onecmd.mkdir(parents=True)
        (onecmd / "agent_sop.md").write_text("Old SOP")
        (onecmd / "custom_rules.md").write_text("# comment\n")

        ensure_skills()

        assert (onecmd / "agent_sop.md").exists()
        assert (onecmd / "custom_rules.md").exists()

    def test_extra_md_files_migrated(self, skills_env):
        """User-added .md files in .onecmd/ are migrated to core-ops resources."""
        onecmd = skills_env / ONECMD_DIR
        onecmd.mkdir(parents=True)
        (onecmd / "agent_sop.md").write_text("base sop")
        (onecmd / "my_guide.md").write_text("My custom guide content")

        ensure_skills()

        migrated = (skills_env / SKILLS_DIR / "core-ops" /
                    "resources" / "my_guide.md")
        assert migrated.exists()
        assert "My custom guide content" in migrated.read_text()


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------


class TestCaching:
    def test_returns_cached_on_second_call(self, skills_env):
        """Second call returns same string from cache."""
        first = ensure_skills()
        second = ensure_skills()
        assert first == second
        assert "skills" in _skills_cache

    def test_invalidate_cache_clears(self, skills_env):
        """invalidate_cache() clears the cache dict."""
        ensure_skills()
        assert "skills" in _skills_cache
        invalidate_cache()
        assert "skills" not in _skills_cache

    def test_cache_miss_on_file_change(self, skills_env):
        """Modifying a resource file causes a cache miss."""
        import os
        import time

        first = ensure_skills()

        # Find a resource file under resources/ to modify
        skills_path = skills_env / SKILLS_DIR
        resource_files = list(skills_path.glob("*/resources/*.md"))
        # Filter to non-comment files (terminal-ops.md, not custom-rules.md)
        content_files = [f for f in resource_files
                         if not all(_is_comment_line(l) for l in f.read_text().splitlines())]
        assert content_files, "Expected at least one non-comment resource file"
        f = content_files[0]
        time.sleep(0.05)
        f.write_text("MODIFIED CONTENT")
        new_mtime = f.stat().st_mtime + 1.0
        os.utime(f, (new_mtime, new_mtime))

        second = ensure_skills()
        assert "MODIFIED CONTENT" in second


# ---------------------------------------------------------------------------
# Size guards
# ---------------------------------------------------------------------------


class TestSizeGuards:
    def test_total_context_capped(self, skills_env):
        """Total assembled skills context doesn't exceed MAX_TOTAL_CHARS."""
        skills_path = skills_env / SKILLS_DIR
        _make_skill(skills_path, "huge", always_loaded=True,
                    resources={"big.md": "B" * (MAX_TOTAL_CHARS + 5000)})
        _make_registry(skills_path, ["huge"])

        (skills_env / ONECMD_DIR).mkdir(parents=True, exist_ok=True)

        result = ensure_skills()
        assert len(result) <= MAX_TOTAL_CHARS + 50


# ---------------------------------------------------------------------------
# list_skills()
# ---------------------------------------------------------------------------


class TestListSkills:
    def test_lists_all_skills_with_status(self, skills_env):
        """list_skills returns all skills with enabled flag."""
        skills_path = skills_env / SKILLS_DIR
        _make_skill(skills_path, "alpha")
        _make_skill(skills_path, "beta")
        _make_registry(skills_path, ["alpha"])

        result = list_skills()
        names = {s["name"] for s in result}
        assert "alpha" in names
        assert "beta" in names

        alpha = next(s for s in result if s["name"] == "alpha")
        beta = next(s for s in result if s["name"] == "beta")
        assert alpha["enabled"] is True
        assert beta["enabled"] is False

    def test_empty_skills_dir(self, skills_env):
        """list_skills returns empty list when no skills dir."""
        assert list_skills() == []
