"""Slack connector — uses slack-bolt with Socket Mode.

Socket Mode means no public URL is needed (no ngrok, no port forwarding).
Messages arrive via WebSocket, similar to Telegram long-polling.

Calling spec:
  Inputs:  SLACK_BOT_TOKEN, SLACK_APP_TOKEN
  Outputs: SlackConnector instance
  Side effects: Slack API calls, WebSocket connection

Required Slack app setup:
  1. Create Slack app at api.slack.com
  2. Enable Socket Mode (generates SLACK_APP_TOKEN starting with xapp-)
  3. Add Bot Token Scopes: chat:write, im:read, im:write, im:history
  4. Subscribe to Events: message.im (for DMs)
  5. Install to workspace (generates SLACK_BOT_TOKEN starting with xoxb-)

Env vars:
  SLACK_BOT_TOKEN  - Bot User OAuth Token (xoxb-...)
  SLACK_APP_TOKEN  - App-Level Token with connections:write scope (xapp-...)

Usage:
  connector = SlackConnector(bot_token="xoxb-...", app_token="xapp-...")
  await connector.start(message_handler)
"""

from __future__ import annotations

import asyncio
import logging
import queue
import threading
import time
from typing import Any

from onecmd.connectors.base import CallbackHandler, Connector, MessageHandler

logger = logging.getLogger(__name__)

# Slack rate limit: ~1 msg/sec/channel.  We enforce with a simple queue.
_RATE_LIMIT_INTERVAL = 1.0
MAX_TEXT_LENGTH = 4000  # Slack limit for message text


def _truncate(text: str) -> str:
    if len(text) <= MAX_TEXT_LENGTH:
        return text
    return text[: MAX_TEXT_LENGTH - 3] + "..."


def _html_to_slack(text: str) -> str:
    """Convert basic HTML formatting (used by existing handler) to Slack mrkdwn.

    The bot handler uses Telegram HTML (<b>, <code>, <pre>) so we translate
    the most common tags to Slack-compatible formatting.
    """
    import re
    # Bold: <b>text</b> -> *text*
    text = re.sub(r"<b>(.*?)</b>", r"*\1*", text, flags=re.DOTALL)
    # Inline code: <code>text</code> -> `text`
    text = re.sub(r"<code>(.*?)</code>", r"`\1`", text, flags=re.DOTALL)
    # Pre blocks: <pre>text</pre> -> ```text```
    text = re.sub(r"<pre>(.*?)</pre>", r"```\1```", text, flags=re.DOTALL)
    # HTML entities
    text = text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
    return text


class SlackConnector(Connector):
    """Connector implementation for Slack using slack-bolt with Socket Mode."""

    def __init__(self, bot_token: str, app_token: str) -> None:
        self._bot_token = bot_token
        self._app_token = app_token
        self._app = None  # slack_bolt.App
        self._client = None  # slack_sdk.web.async_client.AsyncWebClient
        self._handler = None  # SocketModeHandler
        self._loop: asyncio.AbstractEventLoop | None = None
        self._send_lock = threading.Lock()
        self._last_send_time: dict[str, float] = {}

    @property
    def platform_name(self) -> str:
        return "slack"

    async def start(self, message_handler: MessageHandler,
                    callback_handler: CallbackHandler | None = None) -> None:
        """Start the Slack bot with Socket Mode.

        This method blocks until the bot is stopped.
        """
        try:
            from slack_bolt import App
            from slack_bolt.adapter.socket_mode import SocketModeHandler
            from slack_sdk import WebClient
        except ImportError:
            raise RuntimeError(
                "Slack connector requires slack-bolt. "
                "Install with: pip install 'onecmd[slack]'"
            )

        logger.info("Building Slack connector (Socket Mode)...")

        self._loop = asyncio.get_event_loop()
        self._app = App(token=self._bot_token)
        self._client = WebClient(token=self._bot_token)

        # Handle incoming messages (DMs and mentions)
        @self._app.event("message")
        def handle_message(event: dict, say: Any) -> None:
            # Ignore bot's own messages, message_changed, etc.
            subtype = event.get("subtype")
            if subtype is not None:
                return

            text = event.get("text", "")
            user_id = event.get("user", "")
            channel = event.get("channel", "")

            if not user_id or not channel:
                return

            # Strip bot mention prefix if present (e.g. "<@U123> command")
            import re
            text = re.sub(r"^<@[A-Z0-9]+>\s*", "", text)

            # Schedule the async handler on the event loop
            future = asyncio.run_coroutine_threadsafe(
                message_handler(self, channel, user_id, text, event),
                self._loop,
            )
            # Wait for it to complete (blocking in the bolt thread)
            try:
                future.result(timeout=120)
            except Exception:
                logger.exception("Error in Slack message handler")

        # Handle /start equivalent — app_home_opened
        @self._app.event("app_home_opened")
        def handle_home(event: dict, say: Any) -> None:
            user_id = event.get("user", "")
            channel = event.get("channel", "")
            if user_id and channel:
                future = asyncio.run_coroutine_threadsafe(
                    message_handler(self, channel, user_id, "/start", event),
                    self._loop,
                )
                try:
                    future.result(timeout=30)
                except Exception:
                    logger.exception("Error in Slack home handler")

        # Start Socket Mode in a background thread
        self._handler = SocketModeHandler(self._app, self._app_token)
        logger.info("Starting Slack Socket Mode...")

        # SocketModeHandler.start() blocks, run in thread
        stop_event = asyncio.Event()

        def _run_socket_mode() -> None:
            try:
                self._handler.start()
            except Exception:
                logger.exception("Slack Socket Mode error")
            finally:
                self._loop.call_soon_threadsafe(stop_event.set)

        thread = threading.Thread(
            target=_run_socket_mode, daemon=True, name="slack-socket-mode")
        thread.start()

        logger.info("Slack connector running.")
        # Block until stop is called
        await stop_event.wait()
        logger.info("Slack connector stopped.")

    async def stop(self) -> None:
        if self._handler:
            try:
                self._handler.close()
            except Exception:
                logger.exception("Error closing Slack handler")

    def _rate_limit(self, channel: str) -> None:
        """Enforce Slack's ~1 msg/sec rate limit per channel."""
        with self._send_lock:
            now = time.time()
            last = self._last_send_time.get(channel, 0)
            wait = _RATE_LIMIT_INTERVAL - (now - last)
            if wait > 0:
                time.sleep(wait)
            self._last_send_time[channel] = time.time()

    async def send_message(self, chat_id: str, text: str, **kwargs: Any) -> str:
        """Send a message to a Slack channel/DM.  Returns message timestamp as ID."""
        if self._client is None:
            raise RuntimeError("Connector not started")

        text = _html_to_slack(_truncate(text))

        # Rate limit
        await asyncio.get_event_loop().run_in_executor(
            None, self._rate_limit, chat_id)

        try:
            response = self._client.chat_postMessage(
                channel=chat_id,
                text=text,
                mrkdwn=True,
            )
            return response.get("ts", "")
        except Exception as exc:
            logger.error("Slack send_message failed channel=%s: %s",
                         chat_id, exc)
            return ""

    async def edit_message(self, chat_id: str, message_id: str,
                           text: str, **kwargs: Any) -> None:
        if self._client is None:
            return
        text = _html_to_slack(_truncate(text))
        try:
            self._client.chat_update(
                channel=chat_id, ts=message_id, text=text)
        except Exception as exc:
            logger.error("Slack edit_message failed: %s", exc)

    async def delete_message(self, chat_id: str, message_id: str) -> None:
        if self._client is None:
            return
        try:
            self._client.chat_delete(channel=chat_id, ts=message_id)
        except Exception as exc:
            logger.error("Slack delete_message failed: %s", exc)

    async def send_image(self, chat_id: str, image: bytes,
                         caption: str = "") -> str:
        if self._client is None:
            raise RuntimeError("Connector not started")
        try:
            import io
            response = self._client.files_upload_v2(
                channel=chat_id,
                file=io.BytesIO(image),
                filename="image.png",
                initial_comment=caption,
            )
            return response.get("ts", "")
        except Exception as exc:
            logger.error("Slack send_image failed: %s", exc)
            return ""

    async def send_chat_action(self, chat_id: str,
                               action: str = "typing") -> None:
        """Slack doesn't have a typing indicator API for bots.  No-op."""
        pass
