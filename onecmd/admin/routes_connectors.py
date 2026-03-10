"""Connector status routes for the admin panel.

Calling spec:
  Inputs: Config, Store (from app.state)
  Outputs: connector status info
  Side effects: none (read-only)

Routes:
  GET /api/connectors          -> list all connectors
  GET /api/connectors/{name}   -> connector detail
"""


import logging
import time
from fastapi import APIRouter, Depends, HTTPException, Request

from onecmd.admin.auth import require_auth
from onecmd.bot.handler import _START_TIME

log = logging.getLogger(__name__)

connectors_router = APIRouter(tags=["connectors"])


# ---------------------------------------------------------------------------
# REST endpoints
# ---------------------------------------------------------------------------


@connectors_router.get("/api/connectors")
async def list_connectors(
    request: Request,
    _auth: bool = Depends(require_auth),
):
    """List all configured connectors with summary status."""
    return [
        {
            "name": "telegram",
            "type": "telegram",
            "status": "running",
            "config_summary": "Long-polling mode",
        }
    ]


@connectors_router.get("/api/connectors/{name}")
async def connector_detail(
    name: str,
    request: Request,
    _auth: bool = Depends(require_auth),
):
    """Return detailed status for a specific connector."""
    if name != "telegram":
        raise HTTPException(status_code=404, detail="Connector not found")

    config = request.app.state.config
    uptime_seconds = time.time() - _START_TIME

    return {
        "name": "telegram",
        "type": "telegram",
        "status": "running",
        "uptime_seconds": uptime_seconds,
        "auth": {
            "otp_enabled": config.enable_otp,
            "weak_security": config.weak_security,
        },
        "config": {
            "mode": "long-polling",
            "visible_lines": config.visible_lines,
            "split_messages": config.split_messages,
        },
    }
