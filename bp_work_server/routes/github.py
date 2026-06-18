from __future__ import annotations

from fastapi import APIRouter, Request

from bp_work_server.models import GitHubOverviewResponse

router = APIRouter(prefix="/github")


@router.get("/overview", response_model=GitHubOverviewResponse)
async def github_overview(request: Request) -> dict:
    return await request.app.state.github.overview()
