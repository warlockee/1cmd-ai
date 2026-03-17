from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable

from onecmd.manager.skills_registry import load_skills_metadata
from onecmd.manager.tools import TOOL_SCHEMAS


_VAR_RE = re.compile(r"^\$([A-Za-z_][A-Za-z0-9_]*)$")


def _skills_dir(ctx: dict[str, Any]) -> Path:
    p = str(ctx.get("skills_dir") or ".onecmd/skills")
    return Path(p)


def _load_all_skills(ctx: dict[str, Any]) -> list[dict[str, Any]]:
    skills, _warnings = load_skills_metadata(_skills_dir(ctx))
    return [item["skill"] for item in skills if item.get("enabled", True)]


def _resolve_skill_path(skill: dict[str, Any], raw_path: str) -> Path:
    skill_file = Path(str(skill.get("_file") or ""))
    if skill_file.name == "SKILL.json":
        return skill_file.parent / raw_path
    return skill_file.parent / raw_path


def _render_resources(skill: dict[str, Any]) -> list[str]:
    rendered: list[str] = []
    for resource in skill.get("resources", []):
        name = str(resource.get("name") or "resource").strip() or "resource"
        resource_type = str(resource.get("type") or "file").strip() or "file"
        description = str(resource.get("description") or "").strip()
        content = resource.get("content")
        path = resource.get("path")
        body = ""

        if isinstance(content, str) and content.strip():
            body = content.strip()
        elif isinstance(path, str) and path.strip():
            try:
                body = _resolve_skill_path(skill, path.strip()).read_text().strip()
            except OSError as exc:
                body = f"[resource load failed: {exc}]"

        parts = [f"{name} ({resource_type})"]
        if description:
            parts.append(description)
        if body:
            parts.append(body)
        rendered.append(": ".join(parts[:2]) + (f"\n{parts[2]}" if len(parts) > 2 else ""))
    return rendered


def _render_scripts(skill: dict[str, Any]) -> list[str]:
    rendered: list[str] = []
    for script in skill.get("scripts", []):
        name = str(script.get("name") or "script").strip() or "script"
        description = str(script.get("description") or "").strip()
        path = str(script.get("path") or "").strip()
        tool = str(script.get("tool") or "").strip()
        summary = [name]
        if description:
            summary.append(description)
        details: list[str] = []
        if path:
            details.append(f"path={path}")
        if tool:
            details.append(f"tool={tool}")
        if details:
            summary.append(f"({', '.join(details)})")
        rendered.append(" ".join(summary).strip())
    return rendered


def _resolve_vars(obj: Any, inputs: dict[str, Any]) -> Any:
    if isinstance(obj, str):
        m = _VAR_RE.match(obj.strip())
        if m:
            return inputs.get(m.group(1))
        return obj
    if isinstance(obj, list):
        return [_resolve_vars(x, inputs) for x in obj]
    if isinstance(obj, dict):
        return {k: _resolve_vars(v, inputs) for k, v in obj.items()}
    return obj


def tool_list_skills(ctx: dict[str, Any], args: dict[str, Any]) -> str:
    if not ctx.get("skills_enabled", False):
        return "Skills mode disabled."
    skills = _load_all_skills(ctx)
    if not skills:
        return f"No skills found in {_skills_dir(ctx)}"
    lines = [f"Skills ({len(skills)}):"]
    for s in skills:
        desc = str(s.get("description") or "")
        lines.append(f"- {s['name']}: {desc}")
    return "\n".join(lines)


def _skill_limit_reached(target: dict[str, Any], step_count: int, max_steps: int) -> str:
    message = (
        f"Skill stopped: reached max_steps ({step_count} > {max_steps}) for "
        f"{target['name']}. Report the partial progress and next safe step to the user."
    )
    if target.get("failure_policy") == "fallback":
        return (
            f"Skill fallback: reached max_steps ({step_count} > {max_steps}) for "
            f"{target['name']}."
        )
    return message


def _skill_rounds_exhausted(target: dict[str, Any], max_rounds: int) -> str:
    if target.get("failure_policy") == "fallback":
        return f"Skill fallback: reached max_rounds ({max_rounds}) for {target['name']}."
    return (
        f"Skill stopped: {target['name']} did not complete within max_rounds "
        f"({max_rounds}). Report what was completed, what is blocked, and the next safe step."
    )


def _is_failed_result(result: str) -> bool:
    lowered = result.strip().lower()
    return lowered.startswith("[error") or lowered.startswith("error:") or lowered.startswith("unknown tool:")


def _policy_max_steps(target: dict[str, Any], ctx: dict[str, Any]) -> int:
    ctx_max_steps = int(ctx.get("skills_max_steps", 20))
    skill_max_steps = target.get("max_steps")
    if isinstance(skill_max_steps, int):
        return min(ctx_max_steps, skill_max_steps)
    return ctx_max_steps


def _execute_step_sequence(
    target: dict[str, Any],
    inputs: dict[str, Any],
    dry_run: bool,
    max_steps: int,
    dispatch_fn: Callable[[str, dict[str, Any], dict[str, Any]], str],
    ctx: dict[str, Any],
) -> str:
    steps = target.get("steps", [])
    if len(steps) > max_steps:
        return f"Skill has too many steps ({len(steps)} > {max_steps})"

    outputs: list[str] = [f"Running skill: {target['name']}"]
    for i, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            return f"Invalid step #{i}: must be an object"
        tool = step.get("tool")
        raw_args = step.get("args", {})
        if not isinstance(tool, str) or not tool:
            return f"Invalid step #{i}: missing tool"
        if not isinstance(raw_args, dict):
            return f"Invalid step #{i}: args must be object"

        resolved_args = _resolve_vars(raw_args, inputs)

        if dry_run:
            outputs.append(f"{i}. {tool}({json.dumps(resolved_args, ensure_ascii=False)})")
            continue

        result = dispatch_fn(tool, resolved_args, ctx)
        short = result if len(result) <= 300 else result[:300] + "..."
        outputs.append(f"{i}. {tool} -> {short}")

    outputs.append("Dry run only. No tools executed." if dry_run else "Skill completed.")
    return "\n".join(outputs)


def _build_skill_system_prompt(target: dict[str, Any], max_rounds: int, max_steps: int) -> str:
    mode = target.get("mode", "domain")
    resources = _render_resources(target)
    scripts = _render_scripts(target)
    resource_text = "\n".join(f"- {item}" for item in resources) if resources else "- none"
    script_text = "\n".join(f"- {item}" for item in scripts) if scripts else "- none"
    mode_guidance = (
        "Solve the concrete task from the provided inputs within the policy limits."
        if mode == "domain"
        else "Choose the appropriate tools for the user's intent within the skill scope and policy limits."
    )
    return "\n".join([
        f"You are executing skill '{target['name']}' in {mode} mode.",
        f"Description: {target.get('description') or 'No description provided.'}",
        mode_guidance,
        "Respect existing confirmation flags and dangerous-command guardrails.",
        f"failure_policy={target.get('failure_policy', 'stop_and_report')}",
        f"max_rounds={max_rounds}",
        f"max_steps={max_steps}",
        "Resources:",
        resource_text,
        "Scripts:",
        script_text,
        "When the task is complete, reply to the user directly without any tool calls.",
    ])


def _run_llm_skill(
    target: dict[str, Any],
    inputs: dict[str, Any],
    dry_run: bool,
    max_steps: int,
    dispatch_fn: Callable[[str, dict[str, Any], dict[str, Any]], str],
    ctx: dict[str, Any],
) -> str:
    max_rounds = int(target.get("max_rounds") or 3)
    if dry_run:
        return (
            f"Dry run for skill: {target['name']}\n"
            f"mode={target.get('mode', 'domain')}\n"
            f"max_rounds={max_rounds}\n"
            f"max_steps={max_steps}\n"
            "LLM-guided execution would use manager tools within these limits."
        )

    chat_fn = ctx.get("chat_fn")
    format_results_fn = ctx.get("format_results_fn")
    llm_client = ctx.get("llm_client")
    if not callable(chat_fn) or not callable(format_results_fn) or llm_client is None:
        return f"Skill runtime unavailable for {target['name']}: missing LLM context."

    system_prompt = _build_skill_system_prompt(target, max_rounds, max_steps)
    messages: list[dict[str, Any]] = [{
        "role": "user",
        "content": json.dumps({
            "skill_name": target["name"],
            "description": target.get("description", ""),
            "inputs": inputs,
        }, ensure_ascii=False, indent=2),
    }]

    step_count = 0
    for _round in range(1, max_rounds + 1):
        serialized, text_parts, tool_uses, _stop = chat_fn(
            system_prompt,
            TOOL_SCHEMAS,
            messages,
            llm_client.default_max_tokens,
        )
        messages.append({"role": "assistant", "content": serialized})

        if not tool_uses:
            return "\n".join(text_parts).strip() or "(no response)"

        if step_count + len(tool_uses) > max_steps:
            return _skill_limit_reached(target, step_count + len(tool_uses), max_steps)

        results: list[tuple[str, str, str]] = []
        for tool_use_id, tool_name, tool_args in tool_uses:
            result = dispatch_fn(tool_name, tool_args, ctx)
            results.append((tool_use_id, tool_name, result))
            step_count += 1
            if _is_failed_result(result) and target.get("failure_policy") == "stop_and_report":
                return (
                    f"Skill stopped: {target['name']} hit a tool failure during "
                    f"{tool_name}: {result}"
                )
        messages.append(format_results_fn(results))

    return _skill_rounds_exhausted(target, max_rounds)


def tool_run_skill(ctx: dict[str, Any], args: dict[str, Any]) -> str:
    if not ctx.get("skills_enabled", False):
        return "Skills mode disabled."

    skill_name = str(args.get("skill_name") or "").strip()
    inputs = args.get("inputs") or {}
    dry_run = bool(args.get("dry_run", False))
    dispatch_fn: Callable[[str, dict[str, Any], dict[str, Any]], str] = ctx.get("skill_step_dispatch_fn") or ctx["dispatch_fn"]

    target = None
    for s in _load_all_skills(ctx):
        if s.get("name") == skill_name:
            target = s
            break
    if not target:
        return f"Skill not found: {skill_name}"

    max_steps = _policy_max_steps(target, ctx)
    steps = target.get("steps", [])
    if steps:
        return _execute_step_sequence(target, inputs, dry_run, max_steps, dispatch_fn, ctx)
    return _run_llm_skill(target, inputs, dry_run, max_steps, dispatch_fn, ctx)


def skill_tool_schemas() -> list[dict[str, Any]]:
    return [
        {
            "name": "list_skills",
            "description": "List available skills from .onecmd/skills (*.json).",
            "input_schema": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
        {
            "name": "run_skill",
            "description": "Run a named skill workflow from .onecmd/skills/*.json.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "skill_name": {"type": "string", "description": "Skill name"},
                    "inputs": {"type": "object", "description": "Input variables for $var placeholders"},
                    "dry_run": {"type": "boolean", "description": "If true, preview steps only"},
                },
                "required": ["skill_name"],
            },
        },
    ]


def skill_tool_registry() -> dict[str, Callable[[dict[str, Any], dict[str, Any]], str]]:
    return {
        "list_skills": tool_list_skills,
        "run_skill": tool_run_skill,
    }
