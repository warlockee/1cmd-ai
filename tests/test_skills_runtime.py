from __future__ import annotations

import json

from onecmd.manager.skills_registry import load_skills_metadata
from onecmd.manager.skills_runtime import tool_run_skill


class _FakeLLMClient:
    default_max_tokens = 1024


def test_skill_schema_defaults_and_structured_sections(tmp_path):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    skill_dir = skills_dir / "review-code"
    skill_dir.mkdir()
    (skill_dir / "SKILL.json").write_text(json.dumps({
        "name": "review-code",
        "description": "Review a code change.",
        "resources": [
            {"name": "checklist", "type": "inline", "content": "Check tests first."},
        ],
        "scripts": [
            {"name": "pytest", "path": "scripts/run_pytest.sh", "description": "Run tests."},
        ],
    }))

    skills, warnings = load_skills_metadata(skills_dir)

    assert warnings == []
    skill = skills[0]["skill"]
    assert skill["mode"] == "domain"
    assert skill["max_rounds"] == 3
    assert skill["max_steps"] is None
    assert skill["failure_policy"] == "stop_and_report"
    assert skill["resources"][0]["name"] == "checklist"
    assert skill["scripts"][0]["name"] == "pytest"


def test_legacy_step_skill_still_runs(tmp_path):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    (skills_dir / "deploy.json").write_text(json.dumps({
        "name": "deploy",
        "steps": [
            {"tool": "send_message_to_user", "args": {"text": "$message"}},
        ],
    }))

    calls: list[tuple[str, dict]] = []

    def dispatch(tool_name: str, tool_args: dict, _ctx: dict) -> str:
        calls.append((tool_name, tool_args))
        return "ok"

    result = tool_run_skill({
        "skills_enabled": True,
        "skills_dir": str(skills_dir),
        "skills_max_steps": 20,
        "dispatch_fn": dispatch,
    }, {
        "skill_name": "deploy",
        "inputs": {"message": "Ship it"},
    })

    assert "Skill completed." in result
    assert calls == [("send_message_to_user", {"text": "Ship it"})]


def test_domain_skill_stops_after_max_rounds(tmp_path):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    skill_dir = skills_dir / "triage"
    skill_dir.mkdir()
    (skill_dir / "SKILL.json").write_text(json.dumps({
        "name": "triage",
        "mode": "domain",
        "max_rounds": 2,
        "max_steps": 5,
        "failure_policy": "stop_and_report",
    }))

    round_calls = {"count": 0}

    def chat_fn(_system: str, _tools: list[dict], _messages: list[dict], _max_tokens: int):
        round_calls["count"] += 1
        return (
            [{"type": "tool_use", "id": f"r{round_calls['count']}", "name": "read_sop", "input": {}}],
            [],
            [(f"r{round_calls['count']}", "read_sop", {})],
            "tool_use",
        )

    def dispatch(tool_name: str, tool_args: dict, _ctx: dict) -> str:
        assert tool_name == "read_sop"
        assert tool_args == {}
        return "SOP loaded"

    result = tool_run_skill({
        "skills_enabled": True,
        "skills_dir": str(skills_dir),
        "skills_max_steps": 20,
        "dispatch_fn": dispatch,
        "chat_fn": chat_fn,
        "format_results_fn": lambda results: {"role": "user", "content": results},
        "llm_client": _FakeLLMClient(),
    }, {
        "skill_name": "triage",
        "inputs": {"request": "Investigate failing deploy"},
    })

    assert round_calls["count"] == 2
    assert "did not complete within max_rounds (2)" in result


def test_stop_and_report_stops_on_first_tool_failure(tmp_path):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    skill_dir = skills_dir / "deploy"
    skill_dir.mkdir()
    (skill_dir / "SKILL.json").write_text(json.dumps({
        "name": "deploy",
        "mode": "capability",
        "max_rounds": 3,
        "failure_policy": "stop_and_report",
    }))

    round_calls = {"count": 0}

    def chat_fn(_system: str, _tools: list[dict], _messages: list[dict], _max_tokens: int):
        round_calls["count"] += 1
        return (
            [{"type": "tool_use", "id": "t1", "name": "restart_service", "input": {"service_name": "nginx"}}],
            [],
            [("t1", "restart_service", {"service_name": "nginx"})],
            "tool_use",
        )

    def dispatch(_tool_name: str, _tool_args: dict, _ctx: dict) -> str:
        return "Error: confirmation required"

    result = tool_run_skill({
        "skills_enabled": True,
        "skills_dir": str(skills_dir),
        "skills_max_steps": 20,
        "dispatch_fn": dispatch,
        "chat_fn": chat_fn,
        "format_results_fn": lambda results: {"role": "user", "content": results},
        "llm_client": _FakeLLMClient(),
    }, {
        "skill_name": "deploy",
        "inputs": {"service": "nginx"},
    })

    assert round_calls["count"] == 1
    assert "Skill stopped: deploy hit a tool failure during restart_service" in result
