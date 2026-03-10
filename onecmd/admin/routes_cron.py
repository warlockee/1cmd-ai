"""Admin REST API for cron job management.

Calling spec:
  Inputs: cron store, cron engine, LLM compiler
  Outputs: job CRUD responses
  Side effects: job creation/modification, engine registration

Routes:
  GET    /api/cron              -> list all jobs
  POST   /api/cron              -> create job {description}
  GET    /api/cron/{id}         -> get job detail
  PUT    /api/cron/{id}         -> update job fields
  DELETE /api/cron/{id}         -> delete job
  POST   /api/cron/{id}/compile -> LLM compile description -> plan
  POST   /api/cron/{id}/activate -> set active, register with engine
  POST   /api/cron/{id}/pause   -> set paused, unregister
"""


import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from onecmd.admin.auth import require_auth
from onecmd.cron.store import CronStore

logger = logging.getLogger(__name__)

cron_router = APIRouter(prefix="/api/cron", tags=["cron"])


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class CreateJobRequest(BaseModel):
    description: str


class UpdateJobRequest(BaseModel):
    description: str | None = None
    schedule: str | None = None
    action_type: str | None = None
    action_config: str | None = None
    llm_plan: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_store(request: Request) -> CronStore:
    """Retrieve the CronStore from app state."""
    store = getattr(request.app.state, "cron_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="Cron store not available")
    return store


def _get_engine(request: Request) -> Any | None:
    """Retrieve the CronEngine from app state (may be None)."""
    return getattr(request.app.state, "cron_engine", None)


def _job_or_404(store: CronStore, job_id: int) -> dict:
    """Fetch a job or raise 404."""
    job = store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    return job


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@cron_router.get("")
async def list_jobs(
    request: Request,
    _auth: bool = Depends(require_auth),
) -> list[dict]:
    """List all cron jobs."""
    store = _get_store(request)
    return store.list_all()


@cron_router.post("")
async def create_job(
    body: CreateJobRequest,
    request: Request,
    _auth: bool = Depends(require_auth),
) -> dict:
    """Create a new draft cron job."""
    store = _get_store(request)
    if not body.description.strip():
        raise HTTPException(status_code=400, detail="Description cannot be empty")
    job_id = store.create(body.description.strip())
    job = store.get(job_id)
    return job  # type: ignore[return-value]


@cron_router.get("/{job_id}")
async def get_job(
    job_id: int,
    request: Request,
    _auth: bool = Depends(require_auth),
) -> dict:
    """Get a single cron job by id."""
    store = _get_store(request)
    return _job_or_404(store, job_id)


@cron_router.put("/{job_id}")
async def update_job(
    job_id: int,
    body: UpdateJobRequest,
    request: Request,
    _auth: bool = Depends(require_auth),
) -> dict:
    """Update fields on a cron job."""
    store = _get_store(request)
    _job_or_404(store, job_id)

    fields: dict[str, Any] = {}
    if body.description is not None:
        fields["description"] = body.description
    if body.schedule is not None:
        fields["schedule"] = body.schedule
    if body.action_type is not None:
        fields["action_type"] = body.action_type
    if body.action_config is not None:
        fields["action_config"] = body.action_config
    if body.llm_plan is not None:
        fields["llm_plan"] = body.llm_plan

    if fields:
        store.update(job_id, **fields)

    return _job_or_404(store, job_id)


@cron_router.delete("/{job_id}")
async def delete_job(
    job_id: int,
    request: Request,
    _auth: bool = Depends(require_auth),
) -> dict:
    """Delete a cron job."""
    store = _get_store(request)
    _job_or_404(store, job_id)

    # Unregister from engine if active
    engine = _get_engine(request)
    if engine is not None:
        engine.remove_job(job_id)

    store.delete(job_id)
    return {"deleted": True, "id": job_id}


@cron_router.post("/{job_id}/compile")
async def compile_job(
    job_id: int,
    request: Request,
    _auth: bool = Depends(require_auth),
) -> dict:
    """Use LLM to compile the job description into a structured plan."""
    store = _get_store(request)
    job = _job_or_404(store, job_id)

    from onecmd.cron.compiler import compile_job as do_compile

    config = getattr(request.app.state, "config", None)
    result = do_compile(job["description"], config)

    # Update the job with compiled data
    action_config_str = json.dumps(result.get("action_config", {}))
    store.update(
        job_id,
        schedule=result.get("schedule", ""),
        action_type=result.get("action_type", "send_command"),
        action_config=action_config_str,
        llm_plan=result.get("plan", ""),
        status="compiled",
        error=None,
    )

    return _job_or_404(store, job_id)


@cron_router.post("/{job_id}/activate")
async def activate_job(
    job_id: int,
    request: Request,
    _auth: bool = Depends(require_auth),
) -> dict:
    """Set a job to active and register with the engine."""
    store = _get_store(request)
    job = _job_or_404(store, job_id)

    if not job.get("schedule"):
        raise HTTPException(
            status_code=400,
            detail="Cannot activate a job without a schedule. Compile first.",
        )

    store.update(job_id, status="active", error=None)

    engine = _get_engine(request)
    if engine is not None:
        engine.add_job(job_id)

    return _job_or_404(store, job_id)


@cron_router.post("/{job_id}/pause")
async def pause_job(
    job_id: int,
    request: Request,
    _auth: bool = Depends(require_auth),
) -> dict:
    """Pause an active job and unregister from the engine."""
    store = _get_store(request)
    _job_or_404(store, job_id)

    store.update(job_id, status="paused")

    engine = _get_engine(request)
    if engine is not None:
        engine.remove_job(job_id)

    return _job_or_404(store, job_id)
