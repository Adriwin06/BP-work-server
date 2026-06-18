from __future__ import annotations

from importlib.resources import files

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()


def static_dir():
    return files("bp_work_server").joinpath("static")


@router.get("/", response_class=HTMLResponse)
def dashboard() -> str:
    return static_dir().joinpath("index.html").read_text(encoding="utf-8")


@router.get("/health")
def health() -> dict[str, str]:
    from bp_work_server import __version__

    return {"ok": "true", "version": __version__}
