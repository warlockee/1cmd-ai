from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


_HEADING_RE = re.compile(r"^\s*#\s+(.*\S)\s*$")
_VALID_MODES = {"domain", "capability"}
_VALID_FAILURE_POLICIES = {"stop_and_report", "fallback"}


def _is_structured_section(items: Any) -> bool:
    return isinstance(items, list) and all(isinstance(item, dict) for item in items)


def _normalize_skill_data(data: dict[str, Any], path: Path) -> dict[str, Any] | None:
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        return None

    steps = data.get("steps", [])
    if not isinstance(steps, list):
        return None

    mode = data.get("mode", "domain")
    if not isinstance(mode, str):
        return None
    mode = mode.strip().lower()
    if mode not in _VALID_MODES:
        return None

    failure_policy = data.get("failure_policy", "stop_and_report")
    if not isinstance(failure_policy, str):
        return None
    failure_policy = failure_policy.strip().lower()
    if failure_policy not in _VALID_FAILURE_POLICIES:
        return None

    max_rounds = data.get("max_rounds")
    if max_rounds is None and mode == "domain":
        max_rounds = 3
    if max_rounds is not None and (not isinstance(max_rounds, int) or max_rounds < 1):
        return None

    max_steps = data.get("max_steps")
    if max_steps is not None and (not isinstance(max_steps, int) or max_steps < 1):
        return None

    resources = data.get("resources", [])
    scripts = data.get("scripts", [])
    if not _is_structured_section(resources) or not _is_structured_section(scripts):
        return None

    return {
        **data,
        "name": name.strip(),
        "mode": mode,
        "steps": steps,
        "max_rounds": max_rounds,
        "max_steps": max_steps,
        "failure_policy": failure_policy,
        "resources": resources,
        "scripts": scripts,
        "_file": str(path),
    }


def _parse_readme_metadata(path: Path) -> tuple[str, str]:
    try:
        text = path.read_text()
    except OSError:
        return "", ""

    title = ""
    paragraph_lines: list[str] = []
    in_paragraph = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not title:
            match = _HEADING_RE.match(raw_line)
            if match:
                title = match.group(1).strip()
                continue
        if not line:
            if in_paragraph:
                break
            continue
        if line.startswith("#"):
            if in_paragraph:
                break
            continue
        paragraph_lines.append(line)
        in_paragraph = True
    return title, " ".join(paragraph_lines).strip()


def _read_skill_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return _normalize_skill_data(data, path)


def _build_skill_metadata(skill_path: Path) -> dict[str, Any] | None:
    skill_data = _read_skill_json(skill_path)
    if not skill_data:
        return None

    skill_dir = skill_path.parent if skill_path.name == "SKILL.json" else None
    readme_path = skill_dir / "README.md" if skill_dir else None
    readme_title, readme_paragraph = ("", "")
    if readme_path and readme_path.exists():
        readme_title, readme_paragraph = _parse_readme_metadata(readme_path)

    description = skill_data.get("description")
    if not isinstance(description, str) or not description.strip():
        description = readme_paragraph or readme_title or ""

    return {
        "name": skill_data["name"].strip(),
        "description": description.strip(),
        "enabled": True,
        "slash": True,
        "command": None,
        "path": str(skill_path),
        "skill": skill_data,
    }


def _discover_skill_paths(skills_dir: Path) -> list[Path]:
    paths = [path for path in sorted(skills_dir.glob("*.json")) if path.name != "skills.json"]
    for child in sorted(skills_dir.iterdir()):
        if not child.is_dir():
            continue
        skill_path = child / "SKILL.json"
        if skill_path.is_file():
            paths.append(skill_path)
    return paths


def _resolve_registered_skill_path(skills_dir: Path, name: str) -> Path | None:
    folder_path = skills_dir / name / "SKILL.json"
    if folder_path.is_file():
        return folder_path
    file_path = skills_dir / f"{name}.json"
    if file_path.is_file():
        return file_path
    return None


def load_skills_metadata(skills_dir: str | Path) -> tuple[list[dict[str, Any]], list[str]]:
    root = Path(skills_dir)
    warnings: list[str] = []
    if not root.exists():
        warnings.append(f"skills dir missing: {root}")
        return [], warnings
    if not root.is_dir():
        warnings.append(f"skills dir is not a directory: {root}")
        return [], warnings

    registry_path = root / "skills.json"
    if not registry_path.exists():
        skills: list[dict[str, Any]] = []
        invalid_count = 0
        for skill_path in _discover_skill_paths(root):
            metadata = _build_skill_metadata(skill_path)
            if metadata:
                skills.append(metadata)
            else:
                invalid_count += 1
        if invalid_count:
            warnings.append(f"ignored {invalid_count} invalid skill file(s)")
        return skills, warnings

    try:
        registry = json.loads(registry_path.read_text())
    except (OSError, json.JSONDecodeError):
        warnings.append(f"invalid registry file: {registry_path}")
        return [], warnings

    if not isinstance(registry, dict):
        warnings.append(f"invalid registry file: {registry_path}")
        return [], warnings

    skills_list = registry.get("skills")
    if not isinstance(skills_list, list):
        warnings.append("invalid registry: skills must be a list")
        return [], warnings

    skills: list[dict[str, Any]] = []
    invalid_count = 0
    for entry in skills_list:
        if not isinstance(entry, dict):
            invalid_count += 1
            continue
        raw_name = entry.get("name")
        if not isinstance(raw_name, str) or not raw_name.strip():
            invalid_count += 1
            continue
        skill_path = _resolve_registered_skill_path(root, raw_name.strip())
        if not skill_path:
            invalid_count += 1
            continue
        metadata = _build_skill_metadata(skill_path)
        if not metadata:
            invalid_count += 1
            continue

        enabled = entry.get("enabled", True)
        slash = entry.get("slash", True)
        command = entry.get("command")
        description = entry.get("description")

        if not isinstance(enabled, bool) or not isinstance(slash, bool):
            invalid_count += 1
            continue
        if command is not None and (not isinstance(command, str) or not command.strip()):
            invalid_count += 1
            continue
        if description is not None and (not isinstance(description, str) or not description.strip()):
            invalid_count += 1
            continue

        metadata["enabled"] = enabled
        metadata["slash"] = slash
        metadata["command"] = command.strip() if isinstance(command, str) else None
        if isinstance(description, str):
            metadata["description"] = description.strip()
        skills.append(metadata)

    if invalid_count:
        warnings.append(f"ignored {invalid_count} invalid registry entries")
    return skills, warnings
