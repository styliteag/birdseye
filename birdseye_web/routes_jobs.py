"""Jobs page: status of the birdseye container's jobs, manual runs for admins."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi import Path as PathParam
from fastapi.responses import Response

from birdseye_web.context import (
    TEMPLATES,
    csrf_protect,
    ctx,
    current_session,
    log_change,
)
from birdseye_web.jobs import JobError, load_jobs, request_run
from birdseye_web.sessions import Session

router = APIRouter(prefix="/jobs")
JOB_KEY = r"^[a-z0-9-]{1,40}$"


def _render(request: Request, s: Session, flash: str = "", error: str = "", status: int = 200):
    base = ctx(request).settings.jobs_dir
    view = load_jobs(Path(base)) if base else None
    partial = request.headers.get("HX-Target") == "jobs-body"
    return TEMPLATES.TemplateResponse(
        request,
        "_jobs_body.html" if partial else "jobs.html",
        {"session": s, "view": view, "configured": bool(base), "flash": flash, "error": error},
        status_code=status,
    )


@router.get("")
async def jobs_page(request: Request, s: Session = Depends(current_session)) -> Response:
    return _render(request, s)


@router.post("/{key}/run")
async def run_job(
    request: Request,
    key: Annotated[str, PathParam(pattern=JOB_KEY)],
    s: Session = Depends(csrf_protect),
) -> Response:
    if not s.is_admin:
        return _render(
            request, s, error="Only NetBird owners and admins can start jobs.", status=403
        )
    base = ctx(request).settings.jobs_dir
    if not base:
        return _render(request, s, error="Jobs are not configured (WEB_JOBS_DIR).", status=400)
    form = await request.form()
    mode = str(form.get("mode", "run"))
    try:
        rid = request_run(Path(base), key, mode, by=s.user_name)
    except JobError as exc:
        return _render(request, s, error=str(exc), status=400)
    log_change(s, f"request job {key}", None, {"mode": mode, "request": rid})
    return _render(request, s, flash=f"{key} ({mode}) queued – it starts within a few seconds.")
