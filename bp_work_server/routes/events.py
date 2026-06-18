from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse

from bp_work_server.dependencies import get_store
from bp_work_server.models import EventsResponse, FileHistoryResponse
from bp_work_server.services.attribution import attribute_commit
from bp_work_server.store import WorkStore

router = APIRouter(prefix="/events")


@router.get("", response_model=EventsResponse)
def events(
    after: int = Query(0, ge=0),
    limit: int = Query(200, ge=1, le=1000),
    store: WorkStore = Depends(get_store),
) -> EventsResponse:
    return EventsResponse(events=store.events(after=after, limit=limit))


@router.get("/file-history", response_model=FileHistoryResponse)
async def events_file_history(request: Request, store: WorkStore = Depends(get_store)) -> dict:
    targets = await asyncio.to_thread(store.backfilled_event_targets)
    login_map = await request.app.state.github.author_login_map()
    aliases, _profiles = await asyncio.to_thread(store.actor_maps)
    decomp = request.app.state.decomp

    def resolve() -> dict[str, list]:
        history: dict[str, list] = {}
        for tu_id, dest_path in targets.items():
            commits = decomp.history(dest_path)
            if commits:
                history[tu_id] = [attribute_commit(c, login_map, aliases) for c in commits]
        return history

    return {"history": await asyncio.to_thread(resolve)}


@router.get("/stream")
async def event_stream(
    after: int = Query(0, ge=0),
    store: WorkStore = Depends(get_store),
) -> StreamingResponse:
    async def stream():
        last_id = after
        yield "event: connected\ndata: {}\n\n"
        while True:
            events = await asyncio.to_thread(store.events, after=last_id, limit=100)
            for event in events:
                last_id = max(last_id, event["id"])
                payload = json.dumps(event, default=str)
                yield f"id: {event['id']}\nevent: work-event\ndata: {payload}\n\n"
            yield ": keepalive\n\n"
            await asyncio.sleep(2)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
