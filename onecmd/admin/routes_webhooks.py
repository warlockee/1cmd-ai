"""P2.2c — Deploy webhook endpoint for git push triggers.

Lightweight webhook endpoint on the existing FastAPI admin server that
triggers terminal commands when a git push event is received.

Calling spec:
  Inputs:  GitHub/Gitea webhook POST payload
  Outputs: JSON response with status
  Side effects: sends commands to configured terminal

Configuration:
  .onecmd/deploy_hooks.json:
  [
    {
      "repo": "warlockee/myapp",
      "branch": "main",
      "terminal_id": "deploy-term",
      "command": "cd /app && git pull && docker-compose up -d --build",
      "secret": "optional-webhook-secret"
    }
  ]

Security:
  - Webhook secret verification (GitHub HMAC-SHA256)
  - Only configured repos/branches are accepted
  - Requires admin panel to be running (--admin-port)

Routes:
  POST /api/webhooks/deploy       -> receive webhook and trigger deploy
  GET  /api/webhooks/hooks        -> list configured hooks (auth required)
  POST /api/webhooks/hooks        -> add a new hook (auth required)
  DELETE /api/webhooks/hooks/{idx} -> remove a hook (auth required)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from onecmd.admin.auth import require_auth

log = logging.getLogger(__name__)

webhooks_router = APIRouter(tags=["webhooks"])

HOOKS_FILE = ".onecmd/deploy_hooks.json"


# ---------------------------------------------------------------------------
# Hook config persistence
# ---------------------------------------------------------------------------


class DeployHook(BaseModel):
    repo: str  # e.g. "warlockee/myapp"
    branch: str = "main"
    terminal_id: str = ""  # If empty, uses first available terminal
    command: str = "git pull && docker-compose up -d --build"
    secret: str = ""  # Optional webhook secret for HMAC verification


def _load_hooks() -> list[dict[str, Any]]:
    try:
        return json.loads(Path(HOOKS_FILE).read_text())
    except (OSError, json.JSONDecodeError):
        return []


def _save_hooks(hooks: list[dict[str, Any]]) -> None:
    import os
    os.makedirs(".onecmd", exist_ok=True)
    Path(HOOKS_FILE).write_text(json.dumps(hooks, indent=2))


def _verify_github_signature(payload: bytes, signature: str,
                             secret: str) -> bool:
    """Verify GitHub webhook HMAC-SHA256 signature."""
    if not signature or not secret:
        return not secret  # No secret configured = no verification needed
    if not signature.startswith("sha256="):
        return False
    expected = hmac.new(
        secret.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", signature)


# ---------------------------------------------------------------------------
# Webhook endpoint (no auth — uses secret verification instead)
# ---------------------------------------------------------------------------


@webhooks_router.post("/api/webhooks/deploy")
async def deploy_webhook(request: Request):
    """Receive a git push webhook and trigger the configured deploy command.

    Supports GitHub and Gitea webhook formats.
    """
    body = await request.body()

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    # Extract repo and branch from payload
    # GitHub format: payload.repository.full_name, payload.ref
    # Gitea format: same structure
    repo_name = ""
    branch = ""

    repository = payload.get("repository", {})
    if isinstance(repository, dict):
        repo_name = repository.get("full_name", "")

    ref = payload.get("ref", "")
    if ref.startswith("refs/heads/"):
        branch = ref[len("refs/heads/"):]

    if not repo_name or not branch:
        raise HTTPException(
            status_code=400,
            detail="Could not extract repo/branch from payload")

    # Find matching hook
    hooks = _load_hooks()
    matched_hook: dict[str, Any] | None = None
    for hook in hooks:
        if (hook.get("repo", "").lower() == repo_name.lower()
                and hook.get("branch", "main") == branch):
            matched_hook = hook
            break

    if matched_hook is None:
        log.info("No hook configured for %s@%s", repo_name, branch)
        return {"status": "ignored", "reason": "no matching hook"}

    # Verify webhook secret if configured
    secret = matched_hook.get("secret", "")
    if secret:
        signature = request.headers.get("X-Hub-Signature-256", "")
        if not _verify_github_signature(body, signature, secret):
            raise HTTPException(status_code=403, detail="Invalid signature")

    # Execute deploy command
    backend = request.app.state.backend
    command = matched_hook.get("command", "")
    terminal_id = matched_hook.get("terminal_id", "")

    if not command:
        return {"status": "error", "reason": "no command configured"}

    # Find target terminal
    terminals = backend.list()
    if not terminals:
        return {"status": "error", "reason": "no terminals available"}

    target_tid = None
    if terminal_id:
        for t in terminals:
            if t.id == terminal_id:
                target_tid = t.id
                break
        if target_tid is None:
            # Try matching by alias
            try:
                aliases_path = Path(".onecmd/aliases.json")
                aliases = json.loads(aliases_path.read_text())
                for tid, alias in aliases.items():
                    if alias == terminal_id:
                        target_tid = tid
                        break
            except (OSError, json.JSONDecodeError):
                pass

    if target_tid is None:
        target_tid = terminals[0].id

    # Send the command
    try:
        backend.send_keys(target_tid, command + "\n", literal=True)
        log.info("Deploy triggered: %s@%s -> terminal %s: %s",
                 repo_name, branch, target_tid, command)
        return {
            "status": "triggered",
            "repo": repo_name,
            "branch": branch,
            "terminal": target_tid,
            "command": command,
        }
    except Exception as exc:
        log.error("Deploy command failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


# ---------------------------------------------------------------------------
# Hook management endpoints (auth required)
# ---------------------------------------------------------------------------


@webhooks_router.get("/api/webhooks/hooks")
async def list_hooks(
    request: Request,
    _auth: bool = Depends(require_auth),
):
    """List all configured deploy hooks."""
    hooks = _load_hooks()
    # Redact secrets in response
    safe_hooks = []
    for h in hooks:
        safe = dict(h)
        if safe.get("secret"):
            safe["secret"] = "***"
        safe_hooks.append(safe)
    return safe_hooks


@webhooks_router.post("/api/webhooks/hooks")
async def add_hook(
    hook: DeployHook,
    request: Request,
    _auth: bool = Depends(require_auth),
):
    """Add a new deploy hook."""
    hooks = _load_hooks()
    hooks.append(hook.model_dump())
    _save_hooks(hooks)
    log.info("Added deploy hook for %s@%s", hook.repo, hook.branch)
    return {"status": "added", "total_hooks": len(hooks)}


@webhooks_router.delete("/api/webhooks/hooks/{idx}")
async def remove_hook(
    idx: int,
    request: Request,
    _auth: bool = Depends(require_auth),
):
    """Remove a deploy hook by index."""
    hooks = _load_hooks()
    if idx < 0 or idx >= len(hooks):
        raise HTTPException(status_code=404, detail="Hook not found")
    removed = hooks.pop(idx)
    _save_hooks(hooks)
    log.info("Removed deploy hook for %s", removed.get("repo"))
    return {"status": "removed", "removed": removed.get("repo")}
