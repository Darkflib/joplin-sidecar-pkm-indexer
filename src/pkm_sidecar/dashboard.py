"""Minimal local dashboard (PRD §14).

A static HTML shell served at ``GET /`` plus assets under ``/static``. All data
loading happens client-side via authenticated ``fetch`` calls; the server never
embeds a token in the HTML. The token reaches the browser only via a URL fragment
(``/#token=…``, handed off by ``pkm-sidecar open`` or the launcher) or a manual
paste banner — both stored in ``sessionStorage`` only.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates
from starlette.responses import Response

from pkm_sidecar import __version__

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
router = APIRouter()

CSP = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'"


@router.get("/")
async def index(request: Request) -> Response:
    response = templates.TemplateResponse(
        request,
        "index.html",
        {
            "service_name": "pkm-sidecar",
            "version": __version__,
            "has_server_seeded_token": False,
        },
    )
    response.headers["Content-Security-Policy"] = CSP
    return response
