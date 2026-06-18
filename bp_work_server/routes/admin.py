from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response, Query, status

from bp_work_server.dependencies import get_store, require_admin_worker
from bp_work_server.models import (
    ImportResponse,
    ReconcileEventsRequest,
    ReconcileEventsResponse,
    SyncRequest,
    SyncResponse,
    WorkerCreateRequest,
    WorkerListResponse,
    WorkerResponse,
)
from bp_work_server.services.dashboard import invalidate_dashboard_cache
from bp_work_server.store import WorkStore

router = APIRouter(prefix="/admin")
log = logging.getLogger(__name__)


@router.post("/workers", response_model=WorkerResponse, status_code=status.HTTP_201_CREATED)
def create_worker(
    req: WorkerCreateRequest,
    request: Request,
    _admin: str = Depends(require_admin_worker),
    store: WorkStore = Depends(get_store),
) -> WorkerResponse:
    result = store.create_worker(
        req.username, is_admin=req.is_admin, github_username=req.github_username
    )
    invalidate_dashboard_cache(request)
    log.info("admin created worker username=%s admin=%s", req.username, req.is_admin)
    return WorkerResponse(**result)


@router.get("/workers", response_model=WorkerListResponse)
def list_workers(
    _admin: str = Depends(require_admin_worker),
    store: WorkStore = Depends(get_store),
) -> WorkerListResponse:
    return WorkerListResponse(workers=store.list_workers())


@router.delete("/workers/{token}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_worker(
    token: str,
    request: Request,
    _admin: str = Depends(require_admin_worker),
    store: WorkStore = Depends(get_store),
) -> Response:
    if not store.revoke_worker(token):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown worker token")
    invalidate_dashboard_cache(request)
    log.info("admin revoked worker token_suffix=%s", token[-6:])
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/import", response_model=ImportResponse)
def import_workflow(
    request: Request,
    workflow_root: str = Query(..., description="Path to BP-Decomp_Workflow"),
    reset: bool = Query(False),
    _admin: str = Depends(require_admin_worker),
    store: WorkStore = Depends(get_store),
) -> dict[str, int]:
    try:
        result = store.import_workflow(workflow_root, reset=reset)
        invalidate_dashboard_cache(request)
        log.info("admin import workflow_root=%s reset=%s", workflow_root, reset)
        return result
    except FileNotFoundError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


@router.post("/sync", response_model=SyncResponse)
def sync_workflow(
    req: SyncRequest,
    request: Request,
    _admin: str = Depends(require_admin_worker),
    store: WorkStore = Depends(get_store),
) -> dict:
    try:
        import bp_work_server.api as api

        result = api.sync_workflow_repo(store, branch=req.branch, reset=req.reset)
        invalidate_dashboard_cache(request)
        log.info("admin sync branch=%s reset=%s", req.branch, req.reset)
        return result
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


@router.post("/reconcile-events", response_model=ReconcileEventsResponse)
async def reconcile_events(
    req: ReconcileEventsRequest,
    request: Request,
    _admin: str = Depends(require_admin_worker),
    store: WorkStore = Depends(get_store),
) -> ReconcileEventsResponse:
    import bp_work_server.api as api

    result = await asyncio.to_thread(
        api.reconcile_review_events_from_decomp,
        store,
        request.app.state.decomp,
        actors=set(req.actors or []),
        apply=req.apply,
    )
    invalidate_dashboard_cache(request)
    log.info("admin reconcile_events actors=%s apply=%s inserted=%s", req.actors, req.apply, result.inserted)
    return ReconcileEventsResponse(
        scanned_tus=result.scanned_tus,
        scanned_commits=result.scanned_commits,
        inserted=result.inserted,
        skipped_existing_real=result.skipped_existing_real,
        skipped_existing_reconstructed=result.skipped_existing_reconstructed,
        skipped_actor_filter=result.skipped_actor_filter,
        skipped_unresolved_actor=result.skipped_unresolved_actor,
        applied=req.apply,
    )
