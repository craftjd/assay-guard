from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.db import ping

ROOT = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(ROOT / "templates"))

app = FastAPI(title="Assay import demo")
app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")


@app.get("/", response_class=HTMLResponse)
def home(request: Request) -> HTMLResponse:
    db_name = None
    db_error = None
    try:
        db_name = ping()
    except Exception as exc:  # noqa: BLE001 — show setup errors on the home page
        db_error = str(exc)

    return templates.TemplateResponse(
        request,
        "home.html",
        {
            "db_name": db_name,
            "db_error": db_error,
        },
    )
