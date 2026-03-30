"""Editor API routes for the admin panel — skills files and memories.

Calling spec:
  Inputs: skills module API, memory DB
  Outputs: file contents, memory records
  Side effects: file writes, skills reload, memory CRUD

Routes:
  GET    /api/files              -> list all editable files (skills + system)
  GET    /api/files/{key}        -> read file content
  PUT    /api/files/{key}        -> write file + trigger skills reload
  POST   /api/files              -> create a new .md file
  DELETE /api/files/{key}        -> delete a user-created .md file
  POST   /api/files/{key}/reset  -> reset to bundled default
  POST   /api/files/reload       -> force skills reload
  GET    /api/memories           -> list all memories
  POST   /api/memories           -> create memory
  PUT    /api/memories/{id}      -> update memory content
  DELETE /api/memories/{id}      -> delete memory
"""


import logging
import shutil
from typing import TYPE_CHECKING, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from onecmd.admin.auth import require_auth
from onecmd.manager import memory, skills

if TYPE_CHECKING:
    from fastapi import Request

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class FileWriteRequest(BaseModel):
    content: str


class FileCreateRequest(BaseModel):
    name: str
    content: str = ""


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
    """Return all editable files (system + skill resources)."""
    return skills.discover_files()


@editor_router.get("/api/files/{key:path}")
async def read_file(key: str, _auth: bool = Depends(require_auth)):
    """Read the content of a file."""
    skills.ensure_skills()

    path = skills.resolve_file(key)
    try:
        content = path.read_text()
    except OSError:
        raise HTTPException(status_code=404, detail=f"File not found: {key}")

    return {
        "key": key,
        "label": path.stem.replace("_", " ").replace("-", " ").title(),
        "content": content,
        "protected": skills.is_system_file(key),
        "has_default": skills.get_bundled_default(key) is not None,
    }


@editor_router.put("/api/files/{key:path}")
async def write_file(
    key: str,
    body: FileWriteRequest,
    _auth: bool = Depends(require_auth),
):
    """Write content to a file and reload skills."""
    path = skills.resolve_file(key)

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body.content)
    except OSError as exc:
        log.error("Failed to write %s: %s", path, exc)
        raise HTTPException(status_code=500, detail=f"Write failed: {exc}")

    skills.invalidate_cache()
    log.info("File '%s' saved and skills reloaded", key)

    return {"ok": True, "reloaded": True}


@editor_router.post("/api/files")
async def create_file(
    body: FileCreateRequest,
    _auth: bool = Depends(require_auth),
):
    """Create a new .md file."""
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Name cannot be empty")
    if not name.endswith(".md"):
        name += ".md"
    if ".." in name or "/" in name or "\\" in name:
        raise HTTPException(status_code=400, detail="Invalid filename")

    key = name.removesuffix(".md")
    path = skills.resolve_file(key)
    if path.exists():
        raise HTTPException(status_code=409, detail=f"File already exists: {name}")

    label = name.removesuffix(".md").replace("_", " ").replace("-", " ").title()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        content = body.content or f"# {label}\n\n"
        path.write_text(content)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Create failed: {exc}")

    log.info("Created new file: %s", path)
    return {"ok": True, "key": key, "label": label}


@editor_router.delete("/api/files/{key:path}")
async def delete_file(
    key: str,
    _auth: bool = Depends(require_auth),
):
    """Delete a user-created file. System files cannot be deleted."""
    if skills.is_system_file(key):
        raise HTTPException(status_code=403, detail="Cannot delete a system file")

    path = skills.resolve_file(key)
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"File not found: {key}")

    try:
        path.unlink()
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Delete failed: {exc}")

    skills.invalidate_cache()
    log.info("Deleted file: %s", path)
    return {"ok": True, "deleted": key}


@editor_router.post("/api/files/{key:path}/reset")
async def reset_file(
    key: str,
    _auth: bool = Depends(require_auth),
):
    """Reset a file to its bundled default."""
    src = skills.get_bundled_default(key)
    if src is None:
        raise HTTPException(status_code=400, detail=f"No bundled default for {key}")

    dest = skills.resolve_file(key)
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Reset failed: {exc}")

    skills.invalidate_cache()
    log.info("Reset '%s' to bundled default", key)
    return {"ok": True, "reset": key}


@editor_router.post("/api/files/reload")
async def reload_skills(_auth: bool = Depends(require_auth)):
    """Force a skills reload."""
    skills.invalidate_cache()
    skills.ensure_skills()
    log.info("Skills force-reloaded via admin")
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
