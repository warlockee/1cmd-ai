"""LLM-powered natural language to cron action compiler.

Calling spec:
  Inputs: natural language description, config (for LLM access)
  Outputs: {schedule, action_type, action_config, plan}
  Side effects: LLM API call
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from onecmd.config import Config

logger = logging.getLogger(__name__)

_DEFAULT_PROMPT_FILE = Path(__file__).parent / "default_compiler_prompt.md"
_USER_PROMPT_FILE = Path(".onecmd/cron_prompt.md")
_FALLBACK_PROMPT = (
    "You are a cron job compiler. Given a task description, return a JSON object with: "
    "schedule (5-field cron), action_type, action_config, plan."
)


def _load_system_prompt() -> str:
    """Load compiler prompt: user override > bundled default.

    Empty files are skipped — always falls back to a working prompt.
    """
    for path in (_USER_PROMPT_FILE, _DEFAULT_PROMPT_FILE):
        if path.exists():
            try:
                text = path.read_text().rstrip()
                if text:
                    return text
            except OSError:
                continue
    return _FALLBACK_PROMPT


def compile_job(description: str, config: Config | None = None) -> dict[str, Any]:
    """Use the LLM to parse a natural language description into structured cron config.

    Returns a dict with keys: schedule, action_type, action_config, plan.
    Falls back to sensible defaults if LLM is unavailable.
    """
    # Try to use LLM provider
    try:
        return _compile_with_llm(description, config)
    except Exception as exc:
        logger.warning("LLM compilation failed (%s), using defaults", exc)
        return _default_result(description)


def _compile_with_llm(description: str, config: Config | None) -> dict[str, Any]:
    """Attempt LLM-based compilation."""
    from onecmd.manager.llm import ProviderManager

    provider_mgr = ProviderManager()
    provider = provider_mgr.active

    # Determine model
    model: str | None = None
    if config and config.mgr_model:
        model = config.mgr_model
    else:
        # Use reasonable defaults per provider
        if provider.name == "anthropic":
            model = "claude-sonnet-4-20250514"
        elif provider.name == "google":
            model = "gemini-2.5-flash"
        else:
            model = "claude-sonnet-4-20250514"

    messages = [
        {"role": "user", "content": f"Compile this cron job description:\n\n{description}"},
    ]

    _serialized, text_parts, _tool_uses, _stop = provider.chat(
        model=model,
        system=_load_system_prompt(),
        tools=[],
        messages=messages,
        max_tokens=1024,
    )

    # Parse JSON from response
    response_text = "\n".join(text_parts)
    result = _extract_json(response_text)

    if result is None:
        logger.warning("Could not parse JSON from LLM response: %s", response_text[:200])
        return _default_result(description)

    # Validate and normalize
    return _normalize_result(result, description)


def _extract_json(text: str) -> dict[str, Any] | None:
    """Extract a JSON object from the LLM response text."""
    # Try direct parse first
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Try to find JSON block in the response
    match = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    return None


def _normalize_result(result: dict[str, Any], description: str) -> dict[str, Any]:
    """Ensure the result has all required keys with valid values."""
    schedule = result.get("schedule", "")
    action_type = result.get("action_type", "send_command")
    action_config = result.get("action_config", {})
    plan = result.get("plan", description)

    if action_type not in ("send_command", "notify", "smart_task"):
        action_type = "send_command"

    if isinstance(action_config, str):
        try:
            action_config = json.loads(action_config)
        except json.JSONDecodeError:
            action_config = {}

    return {
        "schedule": schedule,
        "action_type": action_type,
        "action_config": action_config,
        "plan": plan,
    }


def _default_result(description: str) -> dict[str, Any]:
    """Return a sensible default when LLM is not available."""
    return {
        "schedule": "0 * * * *",
        "action_type": "notify",
        "action_config": {"message": description},
        "plan": f"(LLM unavailable) Hourly notification: {description}",
    }
