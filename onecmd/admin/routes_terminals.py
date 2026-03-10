"""Terminal API routes for the admin panel.

Calling spec:
  Inputs: ValidatedBackend (from app.state)
  Outputs: terminal list, capture text, send result
  Side effects: terminal operations via backend

Routes:
  GET  /api/terminals           -> list terminals
  POST /api/terminals/new       -> create new terminal
  GET  /api/terminals/{id}      -> capture terminal
  POST /api/terminals/{id}/keys -> send keystrokes
  PUT  /api/terminals/{id}/alias -> rename terminal
  WS   /ws/terminal/{id}        -> stream terminal output
"""


import asyncio
import json
import logging
import os
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from onecmd.admin.auth import _sessions, require_auth

log = logging.getLogger(__name__)

ALIASES_PATH = ".onecmd/aliases.json"

terminals_router = APIRouter(tags=["terminals"])


# ---------------------------------------------------------------------------
# Alias helpers (same pattern as handler.py)
# ---------------------------------------------------------------------------

def _load_aliases() -> dict[str, str]:
    try:
        return json.loads(Path(ALIASES_PATH).read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _save_alias(term_id: str, name: str) -> None:
    os.makedirs(".onecmd", exist_ok=True)
    aliases = _load_aliases()
    aliases[term_id] = name
    Path(ALIASES_PATH).write_text(json.dumps(aliases, indent=2))


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class SendKeysRequest(BaseModel):
    text: str
    suppress_newline: bool = False


class AliasRequest(BaseModel):
    name: str


# ---------------------------------------------------------------------------
# REST endpoints
# ---------------------------------------------------------------------------

@terminals_router.get("/api/terminals")
async def list_terminals(request: Request, _auth: bool = Depends(require_auth)):
    """List all terminals with aliases and index numbers."""
    backend = request.app.state.backend
    terminals = backend.list()
    aliases = _load_aliases()
    result = []
    for i, t in enumerate(terminals, 1):
        result.append({
            "id": t.id,
            "name": t.name,
            "title": t.title,
            "alias": aliases.get(t.id, ""),
            "index": i,
        })
    return result


@terminals_router.post("/api/terminals/new")
async def create_terminal(request: Request, _auth: bool = Depends(require_auth)):
    """Open a new terminal window/pane."""
    backend = request.app.state.backend
    result = backend.create()
    if result is None:
        raise HTTPException(status_code=500, detail="Failed to create terminal")
    return {"ok": True, "terminal_id": result}


@terminals_router.get("/api/terminals/{term_id:path}")
async def capture_terminal(term_id: str, request: Request, _auth: bool = Depends(require_auth)):
    """Capture the content of a specific terminal."""
    backend = request.app.state.backend
    try:
        content = backend.capture(term_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Unknown terminal ID")
    connected = False
    try:
        connected = backend.connected(term_id)
    except ValueError:
        pass
    return {
        "id": term_id,
        "content": content or "",
        "connected": connected,
    }


@terminals_router.post("/api/terminals/{term_id:path}/keys")
async def send_keys(term_id: str, body: SendKeysRequest, request: Request, _auth: bool = Depends(require_auth)):
    """Send keystrokes to a terminal."""
    backend = request.app.state.backend
    text = body.text
    if not body.suppress_newline:
        text += "\n"
    try:
        backend.send_keys(term_id, text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=429, detail=str(exc))
    return {"ok": True}


@terminals_router.put("/api/terminals/{term_id:path}/alias")
async def set_alias(term_id: str, body: AliasRequest, request: Request, _auth: bool = Depends(require_auth)):
    """Set or update a terminal alias."""
    _save_alias(term_id, body.name)
    return {"ok": True}


# ---------------------------------------------------------------------------
# WebSocket endpoint
# ---------------------------------------------------------------------------

def _validate_ws_token(token: str | None) -> bool:
    """Check a WebSocket token against active sessions."""
    if token is None:
        return False
    return token in _sessions


@terminals_router.websocket("/ws/terminal/{term_id:path}")
async def ws_terminal(websocket: WebSocket, term_id: str, token: str | None = Query(default=None)):
    """Stream terminal output and accept keystrokes over WebSocket.

    Auth: validated via token query param or session cookie.
    """
    # Check auth from query param or cookie
    cookie_token = websocket.cookies.get("onecmd_session")
    if not _validate_ws_token(token) and not _validate_ws_token(cookie_token):
        await websocket.close(code=4001, reason="Not authenticated")
        return

    await websocket.accept()

    backend = websocket.app.state.backend

    async def _capture_loop():
        """Periodically capture terminal output and send to client."""
        try:
            while True:
                loop = asyncio.get_event_loop()
                try:
                    content = await loop.run_in_executor(None, backend.capture, term_id)
                except ValueError:
                    await websocket.send_json({"content": "", "error": "Terminal not found"})
                    break
                await websocket.send_json({"content": content or ""})
                await asyncio.sleep(1)
        except (WebSocketDisconnect, Exception):
            pass

    async def _receive_loop():
        """Accept incoming messages as keystrokes."""
        try:
            while True:
                data = await websocket.receive_json()
                text = data.get("text", "")
                if text:
                    loop = asyncio.get_event_loop()
                    try:
                        await loop.run_in_executor(
                            None, backend.send_keys, term_id, text + "\n"
                        )
                    except (ValueError, RuntimeError) as exc:
                        await websocket.send_json({"error": str(exc)})
        except (WebSocketDisconnect, Exception):
            pass

    # Run both loops concurrently; cancel both when either finishes
    capture_task = asyncio.create_task(_capture_loop())
    receive_task = asyncio.create_task(_receive_loop())
    try:
        done, pending = await asyncio.wait(
            [capture_task, receive_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
    except Exception:
        capture_task.cancel()
        receive_task.cancel()
    finally:
        try:
            await websocket.close()
        except Exception:
            pass
