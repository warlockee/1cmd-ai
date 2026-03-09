"""
onecmd mgr — Agent SOP (Standard Operating Procedure) loading.

Ships a default SOP at mgr/default_sop.md. On first run, copies it to
.onecmd/agent_sop.md where users can customize it. If the user's copy
exists, it takes priority over the default.
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

SOP_DIR: str = ".onecmd"
SOP_FILE: str = "agent_sop.md"
CUSTOM_RULES_FILE: str = "custom_rules.md"
DEFAULT_SOP: Path = Path(__file__).parent / "default_sop.md"


def _read_default() -> str:
    """Read the shipped default SOP."""
    try:
        return DEFAULT_SOP.read_text()
    except OSError:
        logger.warning("Default SOP not found at %s", DEFAULT_SOP)
        return ""


def ensure_sop() -> str:
    """Ensure the SOP file exists. Returns default SOP + custom rules."""
    sop_path: Path = Path(SOP_DIR) / SOP_FILE
    custom_path: Path = Path(SOP_DIR) / CUSTOM_RULES_FILE

    if not sop_path.exists():
        try:
            sop_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(DEFAULT_SOP, sop_path)
            logger.info("Copied default SOP to %s", sop_path)
        except OSError as e:
            logger.warning("Could not write SOP file: %s", e)

    # Read base SOP
    try:
        content = sop_path.read_text()
    except OSError:
        content = _read_default()

    # Append custom rules if present
    if custom_path.exists():
        try:
            custom = custom_path.read_text().strip()
            if custom:
                content += "\n\n---\n\n## Custom Rules\n\n" + custom
                logger.info("Loaded custom rules from %s", custom_path)
        except OSError:
            pass

    return content
