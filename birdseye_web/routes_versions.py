"""Footer fragment with the versions of birdseye-web, birdseye and NetBird."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from birdseye_web.context import TEMPLATES, ctx, current_session, user_api
from birdseye_web.nbapi import NetBirdAPI
from birdseye_web.sessions import Session
from birdseye_web.versions import container_version, netbird_version, web_version

router = APIRouter()


@router.get("/versions")
async def versions(
    request: Request,
    s: Session = Depends(current_session),
    api: NetBirdAPI = Depends(user_api),
) -> Response:
    web, rev = web_version()
    return TEMPLATES.TemplateResponse(
        request,
        "_versions.html",
        {
            "web": web,
            "rev": rev,
            "container": container_version(ctx(request).settings.jobs_dir),
            "netbird": await netbird_version(api),
        },
    )
