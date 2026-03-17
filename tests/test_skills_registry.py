from __future__ import annotations

import json
from pathlib import Path

from onecmd.manager.skills_registry import load_skills_metadata


def test_registry_controls_enabled_disabled_skills(tmp_path):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    deploy_dir = skills_dir / "deploy-check"
    deploy_dir.mkdir()
    (deploy_dir / "SKILL.json").write_text(json.dumps({
        "name": "deploy-check",
        "steps": [],
    }))
    hidden_dir = skills_dir / "hidden-skill"
    hidden_dir.mkdir()
    (hidden_dir / "SKILL.json").write_text(json.dumps({
        "name": "hidden-skill",
        "steps": [],
    }))
    (skills_dir / "skills.json").write_text(json.dumps({
        "version": 1,
        "skills": [
            {"name": "deploy-check"},
            {"name": "hidden-skill", "enabled": False},
        ],
    }))

    skills, warnings = load_skills_metadata(skills_dir)

    assert warnings == []
    assert [skill["name"] for skill in skills] == ["deploy-check", "hidden-skill"]
    assert skills[0]["enabled"] is True
    assert skills[1]["enabled"] is False


def test_registry_custom_command_name_is_loaded(tmp_path):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    deploy_dir = skills_dir / "deploy-check"
    deploy_dir.mkdir()
    (deploy_dir / "SKILL.json").write_text(json.dumps({
        "name": "deploy-check",
        "steps": [],
    }))
    (skills_dir / "skills.json").write_text(json.dumps({
        "version": 1,
        "skills": [
            {"name": "deploy-check", "command": "skill_deploy"},
        ],
    }))

    skills, warnings = load_skills_metadata(skills_dir)

    assert warnings == []
    assert skills[0]["command"] == "skill_deploy"


def test_registry_missing_falls_back_to_discovery(tmp_path):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    (skills_dir / "deploy.json").write_text(json.dumps({
        "name": "deploy",
        "description": "Legacy deploy",
        "steps": [],
    }))
    cleanup_dir = skills_dir / "cleanup"
    cleanup_dir.mkdir()
    (cleanup_dir / "SKILL.json").write_text(json.dumps({
        "name": "cleanup",
        "steps": [],
    }))

    skills, warnings = load_skills_metadata(skills_dir)

    assert warnings == []
    assert [skill["name"] for skill in skills] == ["deploy", "cleanup"]


def test_readme_fallback_description_path_works(tmp_path):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    deploy_dir = skills_dir / "deploy-check"
    deploy_dir.mkdir()
    (deploy_dir / "SKILL.json").write_text(json.dumps({
        "name": "deploy-check",
        "steps": [],
    }))
    (deploy_dir / "README.md").write_text("# Deploy Check\n\nValidate production before release.\n")

    skills, warnings = load_skills_metadata(skills_dir)

    assert warnings == []
    assert skills[0]["description"] == "Validate production before release."


def test_invalid_registry_entries_warn_without_crashing(tmp_path):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    deploy_dir = skills_dir / "deploy-check"
    deploy_dir.mkdir()
    (deploy_dir / "SKILL.json").write_text(json.dumps({
        "name": "deploy-check",
        "steps": [],
    }))
    (skills_dir / "skills.json").write_text(json.dumps({
        "version": 1,
        "skills": [
            {"name": "deploy-check"},
            {"name": "missing-skill"},
            {"name": "deploy-check", "enabled": "yes"},
        ],
    }))

    skills, warnings = load_skills_metadata(skills_dir)

    assert [skill["name"] for skill in skills] == ["deploy-check"]
    assert warnings == ["ignored 2 invalid registry entries"]


def test_repo_bootstrap_registry_only_enables_new_skill():
    repo_root = Path(__file__).resolve().parent.parent
    skills_dir = repo_root / ".onecmd" / "skills"

    skills, warnings = load_skills_metadata(skills_dir)

    assert warnings == []
    assert [skill["name"] for skill in skills] == ["new-skill"]
    assert skills[0]["enabled"] is True
    assert skills[0]["slash"] is True


def test_repo_bootstrap_skill_and_design_docs_exist():
    repo_root = Path(__file__).resolve().parent.parent

    assert (repo_root / ".onecmd" / "skills" / "new-skill" / "SKILL.json").is_file()
    assert (repo_root / ".onecmd" / "skills" / "new-skill" / "README.md").is_file()
    assert (repo_root / ".onecmd" / "skills" / "skills.json").is_file()
    assert (repo_root / "docs" / "SKILLS_DESIGN.md").is_file()
