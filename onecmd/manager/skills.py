"""
onecmd.manager.skills — Modular skills system (replaces SOP).

Each skill is a directory under .onecmd/skills/ with:
  SKILL.json   — metadata and policy
  README.md    — human-readable description
  resources/   — read-only context files (.md)

Registry:
  .onecmd/skills/skills.json — list of enabled skills

Loading behavior:
  always_loaded=true  → resources injected into every system prompt
  always_loaded=false → listed by name+description; content loaded on demand
                        via load_skill()

Calling spec:
  ensure_skills() → str   (assembled context for system prompt)
  load_skill(name) → str  (full resource content for one skill)
  invalidate_cache()       (clears mtime-based cache)
  list_skills()            (returns all skills with status)
"""
from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ONECMD_DIR: str = ".onecmd"
SKILLS_DIR: str = ".onecmd/skills"
REGISTRY_FILE: str = "skills.json"

# Limits
MAX_RESOURCE_CHARS: int = 8_000     # per resource file (~2K tokens)
MAX_SKILL_CHARS: int = 16_000       # per skill total (~4K tokens)
MAX_TOTAL_CHARS: int = 40_000       # hard cap on assembled output (~10K tokens)

# Bundled defaults
_DEFAULT_SKILLS_DIR: Path = Path(__file__).parent / "default_skills"

# Seed files that live in .onecmd/ (outside skills/) — not touched by skills
_SEED_FILES: list[tuple[Path, str]] = [
    (Path(__file__).parent / "default_agent_prompt.md", "ai_personality.md"),
    (Path(__file__).parent / "default_crash_patterns.md", "crash_patterns.md"),
    (Path(__file__).parent.parent / "cron" / "default_compiler_prompt.md", "cron_prompt.md"),
]

# Old SOP system files — used to detect migration
_OLD_SOP_FILE: str = "agent_sop.md"
_OLD_CUSTOM_RULES_FILE: str = "custom_rules.md"
_OLD_SYSTEM_FILES: set[str] = {
    _OLD_SOP_FILE,
    _OLD_CUSTOM_RULES_FILE,
    "ai_personality.md",
    "crash_patterns.md",
    "cron_prompt.md",
}

# Cache
_skills_cache: dict[str, tuple[float, str]] = {}

# ---------------------------------------------------------------------------
# Fingerprinting
# ---------------------------------------------------------------------------


def _dir_fingerprint(skills_dir: Path) -> float:
    """Fingerprint based on mtimes of all relevant files under skills/.

    Changes when any SKILL.json, README.md, or resources/*.md is
    modified, added, or deleted.
    """
    total = 0.0
    count = 0
    try:
        for root, _dirs, files in os.walk(skills_dir):
            for name in files:
                if name.endswith((".json", ".md")):
                    count += 1
                    try:
                        total += os.path.getmtime(os.path.join(root, name))
                    except OSError:
                        pass
    except OSError:
        return 0.0
    return total + float(count)


# ---------------------------------------------------------------------------
# Cache management
# ---------------------------------------------------------------------------


def invalidate_cache() -> None:
    """Clear the skills cache. Called after admin writes/resets."""
    _skills_cache.clear()


# ---------------------------------------------------------------------------
# Skill loading
# ---------------------------------------------------------------------------


def _load_skill_meta(skill_dir: Path) -> dict | None:
    """Read and parse SKILL.json from a skill directory."""
    meta_path = skill_dir / "SKILL.json"
    if not meta_path.exists():
        return None
    try:
        return json.loads(meta_path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Bad SKILL.json in %s: %s", skill_dir.name, e)
        return None


def _is_comment_line(line: str) -> bool:
    """Check if a line is a comment (# followed by space or end-of-line).

    Markdown headers (## Header, #word) are NOT comments.
    """
    stripped = line.strip()
    if not stripped:
        return True  # blank lines are skippable
    if stripped == "#":
        return True
    if stripped.startswith("# "):
        # Single # + space = comment. But "## " is a markdown header.
        return True
    return False


def _load_skill_resources(skill_dir: Path, max_chars: int) -> str:
    """Read all .md files under skill_dir/resources/, respecting caps.

    Files that contain only comments (lines starting with '# ') are skipped.
    """
    resources_dir = skill_dir / "resources"
    if not resources_dir.is_dir():
        return ""

    parts: list[str] = []
    total = 0
    try:
        md_files = sorted(resources_dir.glob("*.md"))
    except OSError:
        return ""

    for f in md_files:
        try:
            text = f.read_text().strip()
        except OSError:
            continue
        if not text:
            continue

        # Skip comment-only files
        has_content = any(not _is_comment_line(l) for l in text.splitlines())
        if not has_content:
            continue

        # Per-resource cap
        if len(text) > MAX_RESOURCE_CHARS:
            text = text[:MAX_RESOURCE_CHARS] + "\n[...truncated]"
            logger.warning("Resource %s/%s truncated at %d chars",
                           skill_dir.name, f.name, MAX_RESOURCE_CHARS)

        # Per-skill budget
        if total + len(text) > max_chars:
            logger.warning("Skill %s resource budget exhausted, skipping %s",
                           skill_dir.name, f.name)
            break

        parts.append(text)
        total += len(text)

    return "\n\n---\n\n".join(parts)


def _load_registry(skills_path: Path) -> list[str]:
    """Read skills.json registry and return list of enabled skill names."""
    registry_path = skills_path / REGISTRY_FILE
    if not registry_path.exists():
        return []
    try:
        data = json.loads(registry_path.read_text())
        return data.get("enabled", [])
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Bad skills.json: %s", e)
        return []


# ---------------------------------------------------------------------------
# Public: load a single skill on demand
# ---------------------------------------------------------------------------


def load_skill(name: str) -> str | None:
    """Load the full resource content for a single skill by name.

    Returns None if the skill doesn't exist or has no content.
    Used by the read_skill tool for on-demand skill loading.
    """
    skill_dir = Path(SKILLS_DIR) / name
    if not skill_dir.is_dir():
        return None
    meta = _load_skill_meta(skill_dir)
    if meta is None:
        return None
    max_chars = meta.get("max_context_chars", MAX_RESOURCE_CHARS)
    content = _load_skill_resources(skill_dir, max_chars)
    return content if content else None


# ---------------------------------------------------------------------------
# Public: list all skills
# ---------------------------------------------------------------------------


def list_skills() -> list[dict]:
    """Return all skills with their metadata and enabled status."""
    skills_path = Path(SKILLS_DIR)
    if not skills_path.is_dir():
        return []

    enabled = set(_load_registry(skills_path))
    result: list[dict] = []

    for entry in sorted(skills_path.iterdir()):
        if not entry.is_dir():
            continue
        meta = _load_skill_meta(entry)
        if meta is None:
            continue
        meta["enabled"] = entry.name in enabled
        result.append(meta)

    return result


# ---------------------------------------------------------------------------
# Migration from SOP
# ---------------------------------------------------------------------------


def _migrate_sop_to_skills() -> None:
    """Migrate old .onecmd/ SOP files to .onecmd/skills/ structure."""
    onecmd_dir = Path(ONECMD_DIR)
    skills_path = Path(SKILLS_DIR)

    logger.info("Migrating SOP to skills system...")

    # Copy default skills structure first
    _seed_default_skills()

    core_resources = skills_path / "core-ops" / "resources"
    core_resources.mkdir(parents=True, exist_ok=True)

    # Migrate agent_sop.md → core-ops/resources/terminal-ops.md
    old_sop = onecmd_dir / _OLD_SOP_FILE
    if old_sop.exists():
        try:
            content = old_sop.read_text().strip()
            if content:
                dest = core_resources / "terminal-ops.md"
                dest.write_text(content)
                logger.info("Migrated %s → %s", old_sop, dest)
        except OSError as e:
            logger.warning("Could not migrate SOP: %s", e)

    # Migrate custom_rules.md → core-ops/resources/custom-rules.md
    old_rules = onecmd_dir / _OLD_CUSTOM_RULES_FILE
    if old_rules.exists():
        try:
            lines = old_rules.read_text().splitlines()
            rules = [l for l in lines if l.strip() and not l.strip().startswith("#")]
            if rules:
                dest = core_resources / "custom-rules.md"
                dest.write_text("## Custom Rules\n\n" + "\n".join(rules) + "\n")
                logger.info("Migrated %d custom rules → %s", len(rules), dest)
        except OSError as e:
            logger.warning("Could not migrate custom rules: %s", e)

    # Migrate any user-added .md files → core-ops/resources/
    try:
        for f in sorted(onecmd_dir.glob("*.md")):
            if f.name in _OLD_SYSTEM_FILES:
                continue
            try:
                text = f.read_text().strip()
                if not text:
                    continue
                dest = core_resources / f.name
                if not dest.exists():
                    dest.write_text(text)
                    logger.info("Migrated extra file %s → %s", f.name, dest)
            except OSError:
                continue
    except OSError:
        pass

    logger.info("SOP migration complete. Old files preserved in %s/", ONECMD_DIR)


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


def _seed_default_skills() -> None:
    """Copy bundled default skills to .onecmd/skills/ on first run."""
    skills_path = Path(SKILLS_DIR)

    if not _DEFAULT_SKILLS_DIR.is_dir():
        logger.warning("Bundled default skills not found at %s", _DEFAULT_SKILLS_DIR)
        return

    try:
        shutil.copytree(
            _DEFAULT_SKILLS_DIR,
            skills_path,
            dirs_exist_ok=True,
        )
        logger.info("Seeded default skills to %s", skills_path)
    except OSError as e:
        logger.warning("Could not seed default skills: %s", e)


def _seed_onecmd_files() -> None:
    """Seed non-skill config files into .onecmd/ on first run."""
    onecmd_dir = Path(ONECMD_DIR)
    onecmd_dir.mkdir(parents=True, exist_ok=True)

    for src, dest_name in _SEED_FILES:
        dest = onecmd_dir / dest_name
        if not dest.exists() and src.exists():
            try:
                shutil.copy2(src, dest)
                logger.info("Seeded %s", dest)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def ensure_skills() -> str:
    """Ensure skills directory exists. Returns assembled skill context.

    Only always_loaded skills have their resources injected.
    Other enabled skills are listed by name+description for on-demand loading.
    Uses mtime-based caching — only re-reads when files change.
    """
    skills_path = Path(SKILLS_DIR)
    onecmd_dir = Path(ONECMD_DIR)

    # Check cache
    if skills_path.is_dir():
        fp = _dir_fingerprint(skills_path)
        cached = _skills_cache.get("skills")
        if cached and cached[0] == fp:
            return cached[1]

    # First run: seed or migrate
    if not skills_path.is_dir():
        onecmd_dir.mkdir(parents=True, exist_ok=True)

        # Check for old SOP files → migrate
        old_sop = onecmd_dir / _OLD_SOP_FILE
        if old_sop.exists():
            _migrate_sop_to_skills()
        else:
            _seed_default_skills()

    # Seed non-skill config files (ai_personality, crash_patterns, cron_prompt)
    _seed_onecmd_files()

    # Load enabled skills
    enabled = _load_registry(skills_path)
    if not enabled:
        # Fallback: if registry is empty/missing, enable all skills with always_loaded
        for entry in sorted(skills_path.iterdir()):
            if entry.is_dir():
                meta = _load_skill_meta(entry)
                if meta and meta.get("always_loaded"):
                    enabled.append(entry.name)

    # Separate always-loaded vs on-demand skills
    always_parts: list[str] = []
    available: list[tuple[str, str]] = []  # (name, description)
    total_chars = 0

    for skill_name in enabled:
        skill_dir = skills_path / skill_name
        if not skill_dir.is_dir():
            logger.warning("Enabled skill %r not found, skipping", skill_name)
            continue

        meta = _load_skill_meta(skill_dir)
        if meta is None:
            continue

        if not meta.get("always_loaded"):
            # On-demand: just list it
            desc = meta.get("description", "")
            available.append((skill_name, desc))
            continue

        # Always-loaded: inject resources
        max_chars = meta.get("max_context_chars", MAX_RESOURCE_CHARS)
        resources_text = _load_skill_resources(skill_dir, max_chars)
        if not resources_text:
            continue

        # Per-skill cap
        if len(resources_text) > MAX_SKILL_CHARS:
            resources_text = resources_text[:MAX_SKILL_CHARS] + "\n[...truncated]"

        # Total budget
        if total_chars + len(resources_text) > MAX_TOTAL_CHARS:
            logger.warning("Skills context budget exhausted at %d chars", total_chars)
            break

        always_parts.append(resources_text)
        total_chars += len(resources_text)

    content = "\n\n---\n\n".join(always_parts)

    # Append available (non-always-loaded) skills listing
    if available:
        listing = "\n\nAVAILABLE SKILLS (use read_skill tool to load):"
        for name, desc in available:
            listing += f"\n- {name}: {desc}"
        content += listing

    # Hard cap
    if len(content) > MAX_TOTAL_CHARS:
        content = content[:MAX_TOTAL_CHARS] + "\n[...skills context truncated]"
        logger.warning("Assembled skills context truncated at %d chars", MAX_TOTAL_CHARS)

    # Cache
    _skills_cache["skills"] = (_dir_fingerprint(skills_path), content)

    return content


# ---------------------------------------------------------------------------
# File management API (used by admin panel)
# ---------------------------------------------------------------------------

# System files in .onecmd/ (outside skills/) — editable, not deletable
_SYSTEM_FILES: dict[str, str] = {
    "ai_personality.md": "AI Personality",
    "crash_patterns.md": "Crash Patterns",
    "cron_prompt.md": "Cron Prompt",
}

# Bundled defaults for system files
_SYSTEM_DEFAULTS: dict[str, Path] = {
    "ai_personality": Path(__file__).parent / "default_agent_prompt.md",
    "crash_patterns": Path(__file__).parent / "default_crash_patterns.md",
    "cron_prompt": Path(__file__).parent.parent / "cron" / "default_compiler_prompt.md",
}


def _file_label(name: str) -> str:
    """Human-readable label from a filename."""
    return name.removesuffix(".md").replace("_", " ").replace("-", " ").title()


def discover_files() -> list[dict]:
    """Return all editable files: system files + skill resources.

    Each entry has: key, label, filename, path, protected, has_default, group.
    """
    ensure_skills()

    files: list[dict] = []
    onecmd_dir = Path(ONECMD_DIR)
    skills_path = Path(SKILLS_DIR)

    # System files in .onecmd/
    for filename, label in sorted(_SYSTEM_FILES.items()):
        path = onecmd_dir / filename
        if path.exists():
            key = filename.removesuffix(".md")
            files.append({
                "key": key,
                "label": label,
                "filename": filename,
                "path": str(path),
                "protected": True,
                "has_default": key in _SYSTEM_DEFAULTS,
                "group": "System",
            })

    # Skill resource files
    if skills_path.is_dir():
        for skill_dir in sorted(skills_path.iterdir()):
            if not skill_dir.is_dir():
                continue
            resources_dir = skill_dir / "resources"
            if not resources_dir.is_dir():
                continue
            skill_name = skill_dir.name
            for f in sorted(resources_dir.glob("*.md")):
                key = f"{skill_name}--{f.stem}"
                files.append({
                    "key": key,
                    "label": f"{_file_label(skill_name)}: {_file_label(f.name)}",
                    "filename": f.name,
                    "path": str(f),
                    "protected": False,
                    "has_default": _has_bundled_default(key),
                    "group": f"Skill: {_file_label(skill_name)}",
                })

    return files


def resolve_file(key: str) -> Path:
    """Resolve an API key to a file path.

    Keys:
      - "ai_personality"          → .onecmd/ai_personality.md
      - "core-ops--terminal-ops"  → .onecmd/skills/core-ops/resources/terminal-ops.md
    """
    if "--" in key:
        skill_name, resource_name = key.split("--", 1)
        return Path(SKILLS_DIR) / skill_name / "resources" / f"{resource_name}.md"
    return Path(ONECMD_DIR) / f"{key}.md"


def is_system_file(key: str) -> bool:
    """Check if a key refers to a protected system file."""
    return f"{key}.md" in _SYSTEM_FILES


def _has_bundled_default(key: str) -> bool:
    """Check if a key has a bundled default for reset."""
    if key in _SYSTEM_DEFAULTS:
        return True
    if "--" in key:
        skill_name, resource_name = key.split("--", 1)
        return (_DEFAULT_SKILLS_DIR / skill_name / "resources" / f"{resource_name}.md").exists()
    return False


def get_auto_restart_services() -> list[str]:
    """Return list of service names pre-approved for auto-restart.

    Scans skill resources for lines like: "auto-restart: nginx, redis, myapp"
    """
    import re
    content = ensure_skills()
    if not content:
        return []
    match = re.search(r"auto[- ]restart\s*:\s*(.+)", content, re.IGNORECASE)
    if not match:
        return []
    return [s.strip().lower() for s in match.group(1).split(",")]


def get_bundled_default(key: str) -> Path | None:
    """Return the path to the bundled default for a key, or None."""
    if key in _SYSTEM_DEFAULTS:
        src = _SYSTEM_DEFAULTS[key]
        return src if src.exists() else None
    if "--" in key:
        skill_name, resource_name = key.split("--", 1)
        src = _DEFAULT_SKILLS_DIR / skill_name / "resources" / f"{resource_name}.md"
        return src if src.exists() else None
    return None
