"""
onecmd.manager.router — Manager mode toggle and message routing.

Calling spec:
  Inputs:  message text, chat_id, Agent instance
  Outputs: response text or None
  Side effects: toggles manager mode, routes messages to agent

State:
  mgr_mode: bool — toggled by .mgr / .exit / .N commands
  agent: Agent | None — lazy-initialized on first .mgr

Usage:
  router = ManagerRouter(backend, config, notify_fn)
  router.activate()          # .mgr command
  router.deactivate()        # .exit or .N command
  response = router.handle(chat_id, text)  # returns str or None
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from onecmd.config import Config
    from onecmd.terminal.backend import ValidatedBackend

logger = logging.getLogger(__name__)


class ManagerRouter:
    """Routes messages to the LLM manager agent when active."""

    def __init__(
        self,
        backend: ValidatedBackend,
        config: Config,
        notify_fn,
    ):
        self._backend = backend
        self._config = config
        self._notify_fn = notify_fn
        self._active = False
        self._agent = None  # Lazy init
        self.debug = False

    @property
    def active(self) -> bool:
        return self._active

    def activate(self) -> str:
        """Enter manager mode. Returns status message."""
        if self._agent is None:
            self._init_agent()
        if self._agent is None:
            return (
                "Manager unavailable — no LLM API key.\n\n"
                "Set one of these environment variables:\n"
                "<code>GOOGLE_API_KEY</code> — Gemini (recommended)\n"
                "<code>ANTHROPIC_API_KEY</code> — Claude\n\n"
                "Then restart onecmd."
            )
        self._active = True
        logger.info("Manager mode activated")
        return "Manager mode ON. Send messages to the AI agent. Use .exit to leave."

    def deactivate(self) -> str:
        """Exit manager mode. Returns status message."""
        self._active = False
        logger.info("Manager mode deactivated")
        return "Manager mode OFF."

    def handle(self, chat_id: int, text: str) -> str | None:
        """Route a message to the agent. Returns response or None."""
        if not self._active or self._agent is None:
            return None
        if not text or not text.strip():
            return None
        try:
            self._agent.debug = self.debug
            return self._agent.handle_message(chat_id, text)
        except Exception:
            logger.exception("Agent error")
            return "Error processing your message. Try again."

    def health(self) -> dict:
        """Return manager health info."""
        info = {"active": self._active, "agent_initialized": self._agent is not None}
        if self._agent is not None and hasattr(self._agent, "health"):
            info.update(self._agent.health())
        return info

    def _init_agent(self):
        """Lazy-initialize the agent."""
        if not self._config.has_llm_key:
            logger.warning("No LLM API key — manager disabled")
            return
        try:
            from onecmd.manager.agent import Agent

            self._agent = Agent(
                backend=self._backend,
                config=self._config,
                notify_fn=self._notify_fn,
            )
            logger.info("Manager agent initialized")
        except Exception:
            logger.exception("Failed to initialize manager agent")
            self._agent = None
