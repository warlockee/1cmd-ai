"""
onecmd mgr — Thin LLM abstraction for Anthropic and Gemini providers.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Normalised response types
# ---------------------------------------------------------------------------
# tool_use: (id, name, args)  — id is str for Anthropic, generated for Gemini
# text: plain string

ToolUse = tuple[str, str, dict[str, Any]]  # (id, name, args)


def _is_anthropic(client: Any) -> bool:
    return type(client).__module__.startswith("anthropic")


# ---------------------------------------------------------------------------
# Chat: single LLM round-trip
# ---------------------------------------------------------------------------

def chat(
    client: Any,
    model: str,
    system: str,
    tools: list[dict[str, Any]],
    messages: list[dict[str, Any]],
    max_tokens: int = 2048,
) -> tuple[list[dict[str, Any]], list[str], list[ToolUse], str | None]:
    """Call the LLM and return (serialized_content, text_parts, tool_uses, stop_reason).

    serialized_content: list of dicts suitable for appending to conversation history.
    text_parts: list of text strings from the response.
    tool_uses: list of (id, name, args) tuples.
    stop_reason: "end_turn" | "tool_use" | None
    """
    if _is_anthropic(client):
        return _chat_anthropic(client, model, system, tools, messages, max_tokens)
    else:
        return _chat_gemini(client, model, system, tools, messages, max_tokens)


# ---------------------------------------------------------------------------
# Anthropic implementation
# ---------------------------------------------------------------------------

def _chat_anthropic(
    client: Any,
    model: str,
    system: str,
    tools: list[dict[str, Any]],
    messages: list[dict[str, Any]],
    max_tokens: int,
) -> tuple[list[dict[str, Any]], list[str], list[ToolUse], str | None]:
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        tools=tools,
        messages=messages,
    )

    # Serialize content blocks
    serialized: list[dict[str, Any]] = []
    text_parts: list[str] = []
    tool_uses: list[ToolUse] = []

    for block in response.content:
        if block.type == "text":
            serialized.append({"type": "text", "text": block.text})
            text_parts.append(block.text)
        elif block.type == "tool_use":
            serialized.append({
                "type": "tool_use",
                "id": block.id,
                "name": block.name,
                "input": block.input,
            })
            tool_uses.append((block.id, block.name, block.input))

    stop = "tool_use" if tool_uses else "end_turn"
    return serialized, text_parts, tool_uses, stop


def format_tool_results_anthropic(
    results: list[tuple[str, str]],
) -> dict[str, Any]:
    """Format tool results as an Anthropic user message.

    results: list of (tool_use_id, result_text) tuples.
    Returns a message dict.
    """
    return {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": tid, "content": text}
            for tid, text in results
        ],
    }


# ---------------------------------------------------------------------------
# Gemini implementation
# ---------------------------------------------------------------------------

def _chat_gemini(
    client: Any,
    model: str,
    system: str,
    tools: list[dict[str, Any]],
    messages: list[dict[str, Any]],
    max_tokens: int,
) -> tuple[list[dict[str, Any]], list[str], list[ToolUse], str | None]:
    from google.genai import types

    # Convert tools to Gemini format
    gemini_tools = _to_gemini_tools(tools)
    config = types.GenerateContentConfig(
        system_instruction=system,
        tools=gemini_tools,
        max_output_tokens=max_tokens,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )

    # Convert messages to Gemini contents format
    contents = _to_gemini_contents(messages)

    response = client.models.generate_content(
        model=model,
        contents=contents,
        config=config,
    )

    # Parse response
    serialized: list[dict[str, Any]] = []
    text_parts: list[str] = []
    tool_uses: list[ToolUse] = []

    if response.candidates and response.candidates[0].content:
        for part in response.candidates[0].content.parts:
            # Capture thought signature if present (required for Gemini 3.x)
            # Convert bytes to hex string for JSON-safe storage; reconverted in _to_gemini_contents.
            thought_sig = getattr(part, 'thought_signature', None) or getattr(part, 'thoughtSignature', None)
            if thought_sig is not None and isinstance(thought_sig, (bytes, bytearray)):
                thought_sig = thought_sig.hex() or None  # empty bytes → None
            is_thought = getattr(part, 'thought', None)

            if part.text is not None:
                entry: dict[str, Any] = {"type": "text", "text": part.text}
                if thought_sig:
                    entry["thought_signature"] = thought_sig
                if is_thought:
                    entry["thought"] = True
                serialized.append(entry)
                if not is_thought:
                    text_parts.append(part.text)
            elif part.function_call is not None:
                fc = part.function_call
                # Gemini doesn't have tool_use IDs; generate one
                tool_id = f"gemini_{fc.name}_{id(fc)}"
                args = dict(fc.args) if fc.args else {}
                entry = {
                    "type": "tool_use",
                    "id": tool_id,
                    "name": fc.name,
                    "input": args,
                }
                if thought_sig:
                    entry["thought_signature"] = thought_sig
                serialized.append(entry)
                tool_uses.append((tool_id, fc.name, args))

    stop = "tool_use" if tool_uses else "end_turn"
    return serialized, text_parts, tool_uses, stop


def _to_gemini_tools(tools: list[dict[str, Any]]) -> list[Any]:
    """Convert Anthropic-style tool defs to Gemini function_declarations."""
    from google.genai import types

    declarations = []
    for tool in tools:
        # Anthropic uses "input_schema", Gemini uses "parameters"
        schema = tool.get("input_schema", {})
        declarations.append({
            "name": tool["name"],
            "description": tool.get("description", ""),
            "parameters": schema,
        })

    return [types.Tool(function_declarations=declarations)]


def _to_gemini_contents(messages: list[dict[str, Any]]) -> list[Any]:
    """Convert Anthropic-style messages to Gemini contents format."""
    from google.genai import types

    contents = []
    for msg in messages:
        role = msg["role"]
        # Gemini uses "user" and "model" (not "assistant")
        gemini_role = "model" if role == "assistant" else "user"
        raw_content = msg.get("content", "")

        if isinstance(raw_content, str):
            contents.append(types.Content(
                role=gemini_role,
                parts=[types.Part.from_text(text=raw_content)],
            ))
        elif isinstance(raw_content, list):
            parts = []
            for block in raw_content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type", "")
                if btype == "text":
                    part = types.Part(text=block["text"])
                    if block.get("thought"):
                        part.thought = True
                    if block.get("thought_signature"):
                        sig = block["thought_signature"]
                        # Convert hex string back to bytes for Gemini API
                        if isinstance(sig, str):
                            sig = bytes.fromhex(sig)
                        part.thought_signature = sig
                    parts.append(part)
                elif btype == "tool_use":
                    # Assistant requested a tool call — represent as function_call
                    part = types.Part.from_function_call(
                        name=block["name"],
                        args=block.get("input", {}),
                    )
                    if block.get("thought_signature"):
                        sig = block["thought_signature"]
                        if isinstance(sig, str):
                            sig = bytes.fromhex(sig)
                        part.thought_signature = sig
                    parts.append(part)
                elif btype == "tool_result":
                    # User sending tool results back
                    result_text = block.get("content", "")
                    # Use stored tool_name if available, else look it up
                    tool_name = block.get("tool_name") or \
                        _find_tool_name(contents, block.get("tool_use_id", ""))
                    parts.append(types.Part.from_function_response(
                        name=tool_name,
                        response={"result": result_text},
                    ))
            if parts:
                contents.append(types.Content(role=gemini_role, parts=parts))

    return contents


def _find_tool_name(contents: list[Any], tool_use_id: str) -> str:
    """Find the tool name for a given tool_use_id by searching previous contents."""
    # Walk backwards through contents to find the matching function_call
    for content in reversed(contents):
        if content.role == "model":
            for part in content.parts:
                if part.function_call is not None:
                    # Check if this is the right one by matching generated ID pattern
                    if tool_use_id.startswith(f"gemini_{part.function_call.name}_"):
                        return part.function_call.name
    return "unknown"


def format_tool_results_gemini(
    results: list[tuple[str, str, str]],
) -> dict[str, Any]:
    """Format tool results as a Gemini-compatible message.

    results: list of (tool_use_id, tool_name, result_text) tuples.
    Returns a message dict in our internal format (converted to Gemini on next call).
    """
    return {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": tid, "tool_name": name, "content": text}
            for tid, name, text in results
        ],
    }


def format_tool_results(
    client: Any,
    results: list[tuple[str, str, str]],
) -> dict[str, Any]:
    """Format tool results for the current provider.

    results: list of (tool_use_id, tool_name, result_text) tuples.
    """
    if _is_anthropic(client):
        return format_tool_results_anthropic([(tid, text) for tid, _, text in results])
    else:
        return format_tool_results_gemini(results)


# ---------------------------------------------------------------------------
# Cross-provider conversation conversion
# ---------------------------------------------------------------------------

def convert_conversation(conv: list[dict[str, Any]], target: str) -> list[dict[str, Any]]:
    """Convert conversation history to be compatible with the target provider.

    Modifies messages in-place and returns the same list.
    target: "anthropic" or "gemini"
    """
    # Build a tool_use_id -> tool_name lookup from assistant messages
    id_to_name: dict[str, str] = {}
    for msg in conv:
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                id_to_name[block.get("id", "")] = block.get("name", "unknown")

    to_remove: list[int] = []
    for idx, msg in enumerate(conv):
        content = msg.get("content")
        if not isinstance(content, list):
            continue

        if target == "anthropic":
            cleaned: list[dict[str, Any]] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type", "")
                if btype == "text":
                    # Drop Gemini internal thought blocks (not useful for Anthropic)
                    if block.get("thought"):
                        continue
                    new_block = {"type": "text", "text": block["text"]}
                    cleaned.append(new_block)
                elif btype == "tool_use":
                    # Keep only Anthropic-compatible fields
                    cleaned.append({
                        "type": "tool_use",
                        "id": block["id"],
                        "name": block["name"],
                        "input": block.get("input", {}),
                    })
                elif btype == "tool_result":
                    # Strip tool_name (Gemini-specific), keep tool_use_id + content
                    cleaned.append({
                        "type": "tool_result",
                        "tool_use_id": block.get("tool_use_id", ""),
                        "content": block.get("content", ""),
                    })
                else:
                    cleaned.append(block)
            if cleaned:
                msg["content"] = cleaned
            else:
                to_remove.append(idx)

        elif target == "gemini":
            cleaned = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type", "")
                if btype == "tool_result" and not block.get("tool_name"):
                    # Add tool_name from lookup (needed for Gemini function_response)
                    tid = block.get("tool_use_id", "")
                    new_block = dict(block)
                    new_block["tool_name"] = id_to_name.get(tid, "unknown")
                    cleaned.append(new_block)
                else:
                    cleaned.append(block)
            msg["content"] = cleaned

    # Remove messages that became empty (e.g. all-thought assistant messages)
    for idx in reversed(to_remove):
        conv.pop(idx)

    return conv
