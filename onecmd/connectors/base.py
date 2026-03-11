"""Abstract connector interface for messaging platforms.

All messaging platform connectors (Telegram, Slack, etc.) implement this
interface so the bot handler logic is decoupled from any specific platform.

Calling spec:
  Inputs:  platform-specific config (tokens, etc.)
  Outputs: Connector instance
  Side effects: platform API calls (send, edit, delete messages)

Interface:
  start()                                    -> None (blocking)
  stop()                                     -> None
  send_message(chat_id, text, **kw)          -> str (message ID)
  edit_message(chat_id, message_id, text)    -> None
  delete_message(chat_id, message_id)        -> None
  send_image(chat_id, image, caption)        -> str (message ID)
  platform_name                              -> str (e.g. "telegram", "slack")
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Callable, Coroutine


# Type for the message handler callback that connectors invoke on incoming messages.
# Signature: async handler(connector, chat_id, user_id, text, raw_event) -> None
MessageHandler = Callable[
    ["Connector", str, str, str, Any],
    Coroutine[Any, Any, None],
]

# Type for callback query handler.
# Signature: async handler(connector, chat_id, user_id, callback_data, callback_id, raw_event) -> None
CallbackHandler = Callable[
    ["Connector", str, str, str, str, Any],
    Coroutine[Any, Any, None],
]


class Connector(ABC):
    """Base class for messaging platform connectors.

    Each connector translates platform-specific events into a uniform
    interface that the bot handler can consume.  Chat IDs and message IDs
    are strings to accommodate both numeric (Telegram) and alphanumeric
    (Slack) identifiers.
    """

    @property
    @abstractmethod
    def platform_name(self) -> str:
        """Return the platform identifier (e.g. 'telegram', 'slack')."""

    @abstractmethod
    async def start(self, message_handler: MessageHandler,
                    callback_handler: CallbackHandler | None = None) -> None:
        """Start listening for messages.  May block until stopped."""

    @abstractmethod
    async def stop(self) -> None:
        """Gracefully stop the connector."""

    @abstractmethod
    async def send_message(self, chat_id: str, text: str, **kwargs: Any) -> str:
        """Send a text message.  Returns the platform message ID as a string."""

    @abstractmethod
    async def edit_message(self, chat_id: str, message_id: str,
                           text: str, **kwargs: Any) -> None:
        """Edit an existing message."""

    @abstractmethod
    async def delete_message(self, chat_id: str, message_id: str) -> None:
        """Delete a message."""

    @abstractmethod
    async def send_image(self, chat_id: str, image: bytes,
                         caption: str = "") -> str:
        """Send an image.  Returns the platform message ID as a string."""

    async def answer_callback(self, callback_id: str) -> None:
        """Acknowledge a callback query.  No-op by default (not all platforms use it)."""

    async def send_chat_action(self, chat_id: str,
                               action: str = "typing") -> None:
        """Send a typing indicator or similar action.  No-op by default."""
