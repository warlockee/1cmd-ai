"""
onecmd.manager.sop — Agent SOP (Standard Operating Procedure) loading.

Calling spec:
  Inputs:  None
  Outputs: system prompt suffix string
  Side effects: copies default SOP on first run, seeds customizable files

Files (bundled defaults → user-editable copies):
  onecmd/manager/default_sop.md            → .onecmd/agent_sop.md
  onecmd/manager/default_agent_prompt.md   → .onecmd/ai_personality.md
  onecmd/manager/default_crash_patterns.md → .onecmd/crash_patterns.md
  onecmd/cron/default_compiler_prompt.md   → .onecmd/cron_prompt.md
  (generated)                              → .onecmd/custom_rules.md

Auto-pickup:
  Any additional .md files the user drops into .onecmd/ are automatically
  appended to the agent context (non-comment lines only).
"""
from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

# mtime-based cache for ensure_sop()
_sop_cache: dict[str, tuple[float, str]] = {}  # "sop" -> (fingerprint, content)

# Limits — prevent unbounded token growth from user files
MAX_FILE_CHARS: int = 8_000        # per auto-pickup file (~2K tokens)
MAX_EXTRAS_CHARS: int = 32_000     # total auto-pickup budget (~8K tokens)
MAX_SOP_CHARS: int = 40_000        # hard cap on assembled SOP (~10K tokens)

SOP_DIR: str = ".onecmd"
SOP_FILE: str = "agent_sop.md"
CUSTOM_RULES_FILE: str = "custom_rules.md"
DEFAULT_SOP: Path = Path(__file__).parent / "default_sop.md"

# Bundled defaults to seed into .onecmd/ on first run
_SEED_FILES: list[tuple[Path, str]] = [
    (Path(__file__).parent / "default_agent_prompt.md", "ai_personality.md"),
    (Path(__file__).parent / "default_crash_patterns.md", "crash_patterns.md"),
    (Path(__file__).parent.parent / "cron" / "default_compiler_prompt.md", "cron_prompt.md"),
]

# Files managed by the system — not included in auto-pickup
_SYSTEM_FILES: set[str] = {
    SOP_FILE,
    CUSTOM_RULES_FILE,
    "ai_personality.md",
    "crash_patterns.md",
    "cron_prompt.md",
}

_CUSTOM_RULES_TEMPLATE: str = """\
# Custom Rules
#
# Add your own rules here. These are appended to the default SOP
# and guide how the AI manager behaves.
#
# Lines starting with # are comments and ignored.
#
# Examples (uncomment to use):
#
# - Always run tests before deploying
# - Never restart the database without asking me first
# - Prefer yarn over npm
# - When a build fails, check the logs before retrying
# - Summarize what you did after completing a task
"""


def _read_default() -> str:
    """Read the shipped default SOP."""
    try:
        return DEFAULT_SOP.read_text()
    except OSError:
        logger.warning("Default SOP not found at %s", DEFAULT_SOP)
        return ""


def _read_extra_files(sop_dir: Path) -> list[str]:
    """Read all user-created .md files in .onecmd/ (auto-pickup).

    Skips system-managed files and files whose content is all comments.
    Each file is capped at MAX_FILE_CHARS; total budget is MAX_EXTRAS_CHARS.
    Returns list of formatted context sections.
    """
    extras: list[str] = []
    total_chars = 0
    try:
        md_files = sorted(sop_dir.glob("*.md"))
    except OSError:
        return extras

    for f in md_files:
        if f.name in _SYSTEM_FILES:
            continue
        try:
            text = f.read_text()
        except OSError:
            continue
        # Extract non-comment, non-blank lines
        lines = [l for l in text.splitlines() if l.strip() and not l.strip().startswith("#")]
        if not lines:
            continue

        title = f.stem.replace("_", " ").replace("-", " ").title()
        section = f"## {title}\n\n" + "\n".join(lines)

        # Per-file cap
        if len(section) > MAX_FILE_CHARS:
            section = section[:MAX_FILE_CHARS] + "\n[...truncated]"
            logger.warning("Auto-pickup file %s truncated at %d chars", f.name, MAX_FILE_CHARS)

        # Total budget cap
        if total_chars + len(section) > MAX_EXTRAS_CHARS:
            logger.warning("Auto-pickup budget exhausted (%d chars), skipping remaining files", total_chars)
            break

        extras.append(section)
        total_chars += len(section)

    return extras


def invalidate_cache() -> None:
    """Clear the SOP cache. Called after admin writes/resets."""
    _sop_cache.clear()


def _dir_fingerprint(sop_dir: Path) -> float:
    """Return a fingerprint based on mtimes of all .md files in the directory.

    Changes when any file is modified, added, or deleted.
    """
    try:
        files = sorted(sop_dir.glob("*.md"))
    except OSError:
        return 0.0
    # Combine: file count + sum of mtimes (detects add/delete + edits)
    total = float(len(files))
    for f in files:
        try:
            total += f.stat().st_mtime
        except OSError:
            pass
    return total


def ensure_sop() -> str:
    """Ensure the SOP file exists. Returns default SOP + custom rules + extras.

    Uses mtime-based caching — only re-reads when files change.
    """
    sop_path = Path(SOP_DIR) / SOP_FILE
    custom_path = Path(SOP_DIR) / CUSTOM_RULES_FILE
    sop_dir = sop_path.parent

    # Check cache before doing any I/O (skip on first run when files don't exist yet)
    if sop_path.exists():
        fp = _dir_fingerprint(sop_dir)
        cached = _sop_cache.get("sop")
        if cached and cached[0] == fp:
            return cached[1]

    if not sop_path.exists():
        try:
            sop_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(DEFAULT_SOP, sop_path)
            logger.info("Copied default SOP to %s", sop_path)
        except OSError as e:
            logger.warning("Could not write SOP file: %s", e)

    # Seed customizable files into .onecmd/ on first run
    for src, dest_name in _SEED_FILES:
        dest = sop_dir / dest_name
        if not dest.exists() and src.exists():
            try:
                sop_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest)
                logger.info("Seeded %s", dest)
            except OSError:
                pass

    # Read base SOP (fall back to bundled default if empty)
    try:
        content = sop_path.read_text()
        if not content.strip():
            content = _read_default()
    except OSError:
        content = _read_default()

    # Create custom rules template if missing
    if not custom_path.exists():
        try:
            custom_path.write_text(_CUSTOM_RULES_TEMPLATE)
            logger.info("Created custom rules template at %s", custom_path)
        except OSError:
            pass

    # Append custom rules if present
    if custom_path.exists():
        try:
            lines = custom_path.read_text().splitlines()
            rules = [l for l in lines if l.strip() and not l.strip().startswith("#")]
            if rules:
                content += "\n\n---\n\n## Custom Rules\n\n" + "\n".join(rules)
                logger.info("Loaded %d custom rules from %s", len(rules), custom_path)
        except OSError:
            pass

    # Auto-pickup: append any extra .md files the user added
    extras = _read_extra_files(sop_dir)
    if extras:
        for section in extras:
            content += "\n\n---\n\n" + section
        logger.info("Auto-loaded %d extra .md file(s) from %s", len(extras), sop_dir)

    # Hard cap — prevent context window overflow
    if len(content) > MAX_SOP_CHARS:
        content = content[:MAX_SOP_CHARS] + "\n[...SOP truncated]"
        logger.warning("Assembled SOP truncated at %d chars", MAX_SOP_CHARS)

    # Cache the result
    _sop_cache["sop"] = (_dir_fingerprint(sop_dir), content)

    return content
