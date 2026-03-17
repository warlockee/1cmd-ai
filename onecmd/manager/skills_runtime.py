from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable


_VAR_RE = re.compile(r"^\$([A-Za-z_][A-Za-z0-9_]*)$")


def _skills_dir(ctx: dict[str, Any]) -> Path:
    p = str(ctx.get("skills_dir") or ".onecmd/skills")
    return Path(p)


def _load_skill(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text())
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    name = data.get("name")
    steps = data.get("steps")
    if not isinstance(name, str) or not name.strip() or not isinstance(steps, list):
        return None
    return data


def _load_all_skills(ctx: dict[str, Any]) -> list[dict[str, Any]]:
    root = _skills_dir(ctx)
    if not root.exists():
        return []
    skills: list[dict[str, Any]] = []
    for f in sorted(root.glob("*.json")):
        s = _load_skill(f)
        if s:
            s["_file"] = str(f)
            skills.append(s)
    return skills


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


def tool_run_skill(ctx: dict[str, Any], args: dict[str, Any]) -> str:
    if not ctx.get("skills_enabled", False):
        return "Skills mode disabled."

    skill_name = str(args.get("skill_name") or "").strip()
    inputs = args.get("inputs") or {}
    dry_run = bool(args.get("dry_run", False))
    max_steps = int(ctx.get("skills_max_steps", 20))
    dispatch_fn: Callable[[str, dict[str, Any], dict[str, Any]], str] = ctx.get("skill_step_dispatch_fn") or ctx["dispatch_fn"]

    target = None
    for s in _load_all_skills(ctx):
        if s.get("name") == skill_name:
            target = s
            break
    if not target:
        return f"Skill not found: {skill_name}"

    steps = target.get("steps", [])
    if len(steps) > max_steps:
        return f"Skill has too many steps ({len(steps)} > {max_steps})"

    outputs: list[str] = [f"Running skill: {skill_name}"]
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

    if dry_run:
        outputs.append("Dry run only. No tools executed.")
    else:
        outputs.append("Skill completed.")
    return "\n".join(outputs)


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
