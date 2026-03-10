"""Editor API routes for the admin panel — SOP files and memories.

Calling spec:
  Inputs: file paths (allowlisted), memory DB
  Outputs: file contents, memory records
  Side effects: file writes, SOP reload, memory CRUD

Routes:
  GET  /api/files              -> list editable files
  GET  /api/files/{key}        -> read file content
  PUT  /api/files/{key}        -> write file + trigger SOP reload
  POST /api/files/reload       -> force SOP reload
  GET  /api/memories           -> list all memories
  POST /api/memories           -> create memory
  PUT  /api/memories/{id}      -> update memory content
  DELETE /api/memories/{id}    -> delete memory
"""


import logging
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from onecmd.admin.auth import require_auth
from onecmd.manager import memory, sop

if TYPE_CHECKING:
    from fastapi import Request

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# File allowlist — safe keys mapped to real paths
# ---------------------------------------------------------------------------

EDITABLE_FILES: dict[str, dict] = {
    "sop": {
        "path": ".onecmd/agent_sop.md",
        "label": "Agent SOP",
        "readonly": False,
    },
    "custom_rules": {
        "path": ".onecmd/custom_rules.md",
        "label": "Custom Rules",
        "readonly": False,
    },
    "default_sop": {
        "path": str(
            Path(__file__).parent.parent / "manager" / "default_sop.md"
        ),
        "label": "Default SOP (bundled)",
        "readonly": True,
    },
}

# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class FileWriteRequest(BaseModel):
    content: str


class MemoryCreateRequest(BaseModel):
    content: str
    category: str = "general"
    chat_id: int = 0


class MemoryUpdateRequest(BaseModel):
    content: str
    category: Optional[str] = None


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

editor_router = APIRouter(tags=["editor"])


# ---------------------------------------------------------------------------
# File endpoints
# ---------------------------------------------------------------------------


@editor_router.get("/api/files")
async def list_files(_auth: bool = Depends(require_auth)):
    """Return the list of editable files (key, label, readonly)."""
    return [
        {"key": key, "label": info["label"], "readonly": info["readonly"]}
        for key, info in EDITABLE_FILES.items()
    ]


@editor_router.get("/api/files/{key}")
async def read_file(key: str, _auth: bool = Depends(require_auth)):
    """Read the content of an editable file."""
    if key not in EDITABLE_FILES:
        raise HTTPException(status_code=404, detail="Unknown file key")

    info = EDITABLE_FILES[key]
    file_path = Path(info["path"])

    # Ensure the SOP directory and files exist before trying to read
    sop.ensure_sop()

    try:
        content = file_path.read_text()
    except OSError:
        content = ""

    return {
        "key": key,
        "label": info["label"],
        "content": content,
        "readonly": info["readonly"],
    }


@editor_router.put("/api/files/{key}")
async def write_file(
    key: str,
    body: FileWriteRequest,
    _auth: bool = Depends(require_auth),
):
    """Write content to an editable file and reload the SOP."""
    if key not in EDITABLE_FILES:
        raise HTTPException(status_code=404, detail="Unknown file key")

    info = EDITABLE_FILES[key]
    if info["readonly"]:
        raise HTTPException(status_code=403, detail="File is read-only")

    file_path = Path(info["path"])

    try:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(body.content)
    except OSError as exc:
        log.error("Failed to write %s: %s", file_path, exc)
        raise HTTPException(status_code=500, detail=f"Write failed: {exc}")

    # Rebuild the SOP so changes take effect immediately
    sop.ensure_sop()
    log.info("File '%s' saved and SOP reloaded", key)

    return {"ok": True, "reloaded": True}


@editor_router.post("/api/files/reload")
async def reload_sop(_auth: bool = Depends(require_auth)):
    """Force a SOP reload."""
    sop.ensure_sop()
    log.info("SOP force-reloaded via admin")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Memory endpoints
# ---------------------------------------------------------------------------


@editor_router.get("/api/memories")
async def list_memories(_auth: bool = Depends(require_auth)):
    """Return all memories across all chats."""
    rows = memory.list_all()
    return [
        {
            "id": row[0],
            "chat_id": row[1],
            "content": row[2],
            "category": row[3],
            "created_at": row[4],
        }
        for row in rows
    ]


@editor_router.post("/api/memories")
async def create_memory(
    body: MemoryCreateRequest,
    _auth: bool = Depends(require_auth),
):
    """Create a new memory."""
    row_id = memory.save(body.chat_id, body.content, body.category)
    if row_id is None:
        raise HTTPException(status_code=500, detail="Failed to save memory")
    return {"ok": True, "id": row_id}


@editor_router.put("/api/memories/{memory_id}")
async def update_memory(
    memory_id: int,
    body: MemoryUpdateRequest,
    _auth: bool = Depends(require_auth),
):
    """Update a memory's content and optionally its category."""
    updated = memory.update(memory_id, body.content, body.category)
    if not updated:
        raise HTTPException(status_code=404, detail="Memory not found")
    return {"ok": True}


@editor_router.delete("/api/memories/{memory_id}")
async def delete_memory(
    memory_id: int,
    _auth: bool = Depends(require_auth),
):
    """Delete a memory by ID (admin — no chat_id scope)."""
    removed = memory.delete_by_id(memory_id)
    if not removed:
        raise HTTPException(status_code=404, detail="Memory not found")
    return {"ok": True}
