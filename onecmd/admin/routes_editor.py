"""Editor API routes for the admin panel — .onecmd/ files and memories.

Calling spec:
  Inputs: .onecmd/ directory (auto-discovered), memory DB
  Outputs: file contents, memory records
  Side effects: file writes, SOP reload, memory CRUD

Routes:
  GET    /api/files              -> list all .onecmd/*.md files
  GET    /api/files/{key}        -> read file content
  PUT    /api/files/{key}        -> write file + trigger SOP reload
  POST   /api/files              -> create a new .md file
  DELETE /api/files/{key}        -> delete a user-created .md file
  POST   /api/files/reload       -> force SOP reload
  GET    /api/memories           -> list all memories
  POST   /api/memories           -> create memory
  PUT    /api/memories/{id}      -> update memory content
  DELETE /api/memories/{id}      -> delete memory
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

_SOP_DIR = Path(sop.SOP_DIR)

# System-managed files that cannot be deleted (but can be edited)
_PROTECTED_FILES: set[str] = {
    "agent_sop.md",
    "custom_rules.md",
    "ai_personality.md",
    "crash_patterns.md",
    "cron_prompt.md",
}

# Friendly labels for known files
_LABELS: dict[str, str] = {
    "agent_sop.md": "Agent SOP",
    "custom_rules.md": "Custom Rules",
    "ai_personality.md": "AI Personality",
    "crash_patterns.md": "Crash Patterns",
    "cron_prompt.md": "Cron Prompt",
}

# Bundled defaults for reset (filename → source path)
_BUNDLED_DEFAULTS: dict[str, Path] = {
    "agent_sop.md": Path(__file__).parent.parent / "manager" / "default_sop.md",
    "ai_personality.md": Path(__file__).parent.parent / "manager" / "default_agent_prompt.md",
    "crash_patterns.md": Path(__file__).parent.parent / "manager" / "default_crash_patterns.md",
    "cron_prompt.md": Path(__file__).parent.parent / "cron" / "default_compiler_prompt.md",
}


def _file_key(name: str) -> str:
    """Convert filename to API key (strip .md)."""
    return name.removesuffix(".md")


def _file_label(name: str) -> str:
    """Human-readable label for a file."""
    if name in _LABELS:
        return _LABELS[name]
    # Auto-generate from filename
    return name.removesuffix(".md").replace("_", " ").replace("-", " ").title()


def _discover_files() -> list[dict]:
    """Auto-discover all .md files in .onecmd/ directory."""
    # Ensure defaults exist first
    sop.ensure_sop()

    files = []
    if _SOP_DIR.is_dir():
        for f in sorted(_SOP_DIR.glob("*.md")):
            files.append({
                "key": _file_key(f.name),
                "label": _file_label(f.name),
                "filename": f.name,
                "path": str(f),
                "protected": f.name in _PROTECTED_FILES,
                "has_default": f.name in _BUNDLED_DEFAULTS,
            })
    return files


def _resolve_path(key: str) -> Path:
    """Resolve an API key to a file path. Raises 404 if not found."""
    path = _SOP_DIR / f"{key}.md"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"File not found: {key}.md")
    return path


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
    """Return all .md files in .onecmd/ (auto-discovered)."""
    return _discover_files()


@editor_router.get("/api/files/{key}")
async def read_file(key: str, _auth: bool = Depends(require_auth)):
    """Read the content of a file."""
    # Ensure defaults exist
    sop.ensure_sop()

    path = _SOP_DIR / f"{key}.md"
    try:
        content = path.read_text()
    except OSError:
        raise HTTPException(status_code=404, detail=f"File not found: {key}.md")

    return {
        "key": key,
        "label": _file_label(path.name),
        "content": content,
        "protected": path.name in _PROTECTED_FILES,
        "has_default": path.name in _BUNDLED_DEFAULTS,
    }


@editor_router.put("/api/files/{key}")
async def write_file(
    key: str,
    body: FileWriteRequest,
    _auth: bool = Depends(require_auth),
):
    """Write content to a file and reload the SOP."""
    path = _SOP_DIR / f"{key}.md"

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body.content)
    except OSError as exc:
        log.error("Failed to write %s: %s", path, exc)
        raise HTTPException(status_code=500, detail=f"Write failed: {exc}")

    # Rebuild the SOP so changes take effect immediately
    sop.ensure_sop()
    log.info("File '%s' saved and SOP reloaded", key)

    return {"ok": True, "reloaded": True}


@editor_router.post("/api/files")
async def create_file(
    body: FileCreateRequest,
    _auth: bool = Depends(require_auth),
):
    """Create a new .md file in .onecmd/."""
    # Sanitize name
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Name cannot be empty")
    if not name.endswith(".md"):
        name += ".md"
    # Basic path safety
    if "/" in name or "\\" in name or ".." in name:
        raise HTTPException(status_code=400, detail="Invalid filename")

    path = _SOP_DIR / name
    if path.exists():
        raise HTTPException(status_code=409, detail=f"File already exists: {name}")

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        content = body.content or f"# {_file_label(name)}\n\n"
        path.write_text(content)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Create failed: {exc}")

    log.info("Created new file: %s", path)
    return {"ok": True, "key": _file_key(name), "label": _file_label(name)}


@editor_router.delete("/api/files/{key}")
async def delete_file(
    key: str,
    _auth: bool = Depends(require_auth),
):
    """Delete a user-created .md file. Protected files cannot be deleted."""
    path = _SOP_DIR / f"{key}.md"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"File not found: {key}.md")
    if path.name in _PROTECTED_FILES:
        raise HTTPException(status_code=403, detail="Cannot delete a system file")

    try:
        path.unlink()
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Delete failed: {exc}")

    log.info("Deleted file: %s", path)
    return {"ok": True, "deleted": key}


@editor_router.post("/api/files/{key}/reset")
async def reset_file(
    key: str,
    _auth: bool = Depends(require_auth),
):
    """Reset a file to its bundled default. Only works for files with defaults."""
    filename = f"{key}.md"
    src = _BUNDLED_DEFAULTS.get(filename)
    if src is None:
        raise HTTPException(
            status_code=400, detail=f"No bundled default for {filename}")
    if not src.exists():
        raise HTTPException(
            status_code=500, detail="Bundled default file missing from package")

    dest = _SOP_DIR / filename
    try:
        import shutil
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Reset failed: {exc}")

    sop.ensure_sop()
    log.info("Reset '%s' to bundled default", key)
    return {"ok": True, "reset": key}


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
