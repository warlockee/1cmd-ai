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
        self._ceo_active = False
        self._ceo_agent = None  # Lazy init
        self.debug = False

    @property
    def active(self) -> bool:
        return self._active

    @property
    def ceo_active(self) -> bool:
        return self._ceo_active

    def activate(self) -> str:
        """Enter manager mode. Returns status message."""
        if self._agent is None:
            self._init_agent()
        if self._agent is None:
            return (
                "Manager unavailable — no LLM provider credentials.\n\n"
                "Configure one of:\n"
                "<code>GOOGLE_API_KEY</code> — Gemini (recommended)\n"
                "<code>ANTHROPIC_API_KEY</code> — Claude API key\n"
                "Login with <code>claude</code> CLI — Claude OAuth (auto-detected)\n"
                "<code>~/.onecmd/auth.json</code> with <code>openai-codex</code> OAuth creds\n\n"
                "Then restart onecmd."
            )
        self._active = True
        self._ceo_active = False
        logger.info("Manager mode activated")
        return "Manager mode ON. Send messages to the AI agent. Use .exit to leave."

    def activate_ceo(self) -> str:
        """Enter CEO mode. Returns status message."""
        if self._ceo_agent is None:
            self._init_ceo_agent()
        if self._ceo_agent is None:
            return (
                "CEO unavailable — no LLM provider credentials.\n\n"
                "Configure one of:\n"
                "<code>GOOGLE_API_KEY</code> — Gemini (recommended)\n"
                "<code>ANTHROPIC_API_KEY</code> — Claude\n"
                "<code>~/.onecmd/auth.json</code> with <code>openai-codex</code> OAuth creds\n\n"
                "Then restart onecmd."
            )
        self._ceo_active = True
        self._active = False
        logger.info("CEO mode activated")
        return "CEO mode ON."

    def deactivate(self) -> str:
        """Exit manager mode. Returns status message."""
        self._active = False
        logger.info("Manager mode deactivated")
        return "Manager mode OFF."

    def deactivate_ceo(self) -> str:
        """Exit CEO mode. Returns status message."""
        self._ceo_active = False
        logger.info("CEO mode deactivated")
        return "CEO mode OFF."

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

    def handle_ceo(self, chat_id: int, text: str) -> str | None:
        """Route a message to the CEO agent. Returns response or None."""
        if not self._ceo_active or self._ceo_agent is None:
            return None
        if not text or not text.strip():
            return None
        try:
            self._ceo_agent.debug = self.debug
            return self._ceo_agent.handle_message(chat_id, text)
        except Exception:
            logger.exception("CEO agent error")
            return "Error processing your message. Try again."

    def set_model(self, provider_key: str | None, model: str | None) -> str:
        """Switch provider/model on both agents. Returns status."""
        results = []
        for label, agent in [("mgr", self._agent), ("ceo", self._ceo_agent)]:
            if agent is not None:
                try:
                    results.append(agent.set_model(provider_key, model))
                except (KeyError, RuntimeError) as e:
                    return str(e)
        return results[0] if results else "No agent initialized. Use .mgr first."

    def get_model_info(self) -> str:
        """Return current provider/model from whichever agent is active."""
        for agent in (self._ceo_agent, self._agent):
            if agent is not None:
                return agent.get_model_info()
        return "No agent initialized."

    def health(self) -> dict:
        """Return manager health info."""
        info = {
            "active": self._active,
            "agent_initialized": self._agent is not None,
            "ceo_active": self._ceo_active,
            "ceo_initialized": self._ceo_agent is not None,
        }
        if self._agent is not None and hasattr(self._agent, "health"):
            info.update(self._agent.health())
        return info

    def _init_agent(self):
        """Lazy-initialize the manager agent."""
        if not self._config.has_llm_key:
            logger.warning("No LLM API key — manager disabled")
            return
        try:
            from onecmd.manager.agent import Agent
            from onecmd.manager.tools import TOOL_SCHEMAS, dispatch

            mode = getattr(self._config, "agent_mode", "legacy")
            if mode == "skills":
                from onecmd.manager.skills_runtime import (
                    skill_tool_registry,
                    skill_tool_schemas,
                )

                registry = skill_tool_registry()
                schemas = skill_tool_schemas()

                def dispatch_skills(tool_name: str, tool_args: dict, ctx: dict) -> str:
                    fn = registry.get(tool_name)
                    if fn is None:
                        return f"Unknown tool: {tool_name}"
                    try:
                        local_ctx = dict(ctx)
                        local_ctx["skill_step_dispatch_fn"] = dispatch
                        return fn(local_ctx, tool_args)
                    except Exception as e:
                        logger.exception("Skills tool error")
                        return f"[Error in {tool_name}: {e}]"

                self._agent = Agent(
                    backend=self._backend,
                    config=self._config,
                    notify_fn=self._notify_fn,
                    tool_schemas=schemas,
                    dispatch_fn=dispatch_skills,
                )
                logger.info("Manager agent initialized in skills mode")
            else:
                self._agent = Agent(
                    backend=self._backend,
                    config=self._config,
                    notify_fn=self._notify_fn,
                    tool_schemas=TOOL_SCHEMAS,
                    dispatch_fn=dispatch,
                )
                logger.info("Manager agent initialized in legacy mode")
        except Exception:
            logger.exception("Failed to initialize manager agent")
            self._agent = None

    def _init_ceo_agent(self):
        """Lazy-initialize the CEO agent."""
        if not self._config.has_llm_key:
            logger.warning("No LLM API key — CEO disabled")
            return
        try:
            from pathlib import Path
            from onecmd.manager.agent import Agent

            prompt_path = Path(__file__).parent / "ceo_prompt.md"
            ceo_prompt = prompt_path.read_text().rstrip()

            self._ceo_agent = Agent(
                backend=self._backend,
                config=self._config,
                notify_fn=self._notify_fn,
                system_prompt_override=ceo_prompt,
            )
            logger.info("CEO agent initialized")
        except Exception:
            logger.exception("Failed to initialize CEO agent")
            self._ceo_agent = None
