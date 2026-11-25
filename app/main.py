import json
from urllib.parse import parse_qs, urlencode

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.services.demos import cases, run_demo
from app.services.import_groups import file_groups
from app.services.main_checks import run_checks
from app.services.config_watch import affected_count, check_all
from app.services.plan import run_detail
from app.services.process_queue import (
    confirm_write,
    dry_brief,
    import_detail,
    keep_results,
    queue,
    reprocess_affected,
    reprocess_import,
)
from app.services.dismiss import dismiss_changes, restore_changes
from app.services.raw_import import SameFile, delete_import, replace_import, roll_back_import, void_import
from app.services.main_view import main_table
from app.services.schema import clear_import_data
from app.services.names import name_lookup
from app.services.schema import ensure_model
import app.config as cfg
from app.config import activate, choices, describe, summary
from app.brand import PRODUCT_NAME, PRODUCT_TAGLINE
from app.settings import ROOT, github_repo_url
from app.services.dashboard import home_stats
from app.services.integrity_doc import doc_blocks

templates = Jinja2Templates(directory=str(ROOT / "templates"))
templates.env.globals["active_config"] = lambda: cfg.CONFIG_VERSION
templates.env.globals["import_names"] = name_lookup
templates.env.globals["product_name"] = PRODUCT_NAME
templates.env.globals["product_tagline"] = PRODUCT_TAGLINE
templates.env.globals["github_repo_url"] = github_repo_url


def _config_banner() -> int:
    try:
        return affected_count()
    except Exception:  # noqa: BLE001
        return 0


templates.env.globals["config_affected"] = _config_banner
templates.env.filters["tojson_attr"] = lambda value: json.dumps(value, ensure_ascii=False)

app = FastAPI(title=PRODUCT_NAME, docs_url="/api-docs", redoc_url=None)
app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")


@app.on_event("startup")
def _startup() -> None:
    try:
        ensure_model()
    except Exception:
        return


@app.get("/", response_class=HTMLResponse)
def home_page(request: Request) -> HTMLResponse:
    try:
        stats = home_stats()
        db_error = None
    except Exception as exc:  # noqa: BLE001
        return templates.TemplateResponse(
            request,
            "home.html",
            _db_down("home", exc) | {"stats": {"files": 0, "main_rows": 0, "rejections": 0, "pending_review": 0, "config_flagged": 0, "config_affected": 0, "config_version": cfg.CONFIG_VERSION}},
        )
    return templates.TemplateResponse(
        request,
        "home.html",
        {
            "nav": "home",
            "stats": stats,
            "message": _message(request.query_params),
            "error": request.query_params.get("error"),
            "db_error": None,
        },
    )


@app.get("/docs", response_class=HTMLResponse)
def integrity_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "docs.html",
        {
            "nav": "docs",
            "doc_blocks": doc_blocks(),
            "message": None,
            "error": None,
            "db_error": None,
        },
    )


@app.get("/imports")
@app.get("/walkthrough")
def retired() -> RedirectResponse:
    return RedirectResponse("/import", status_code=302)


@app.get("/import", response_class=HTMLResponse)
def import_page(request: Request) -> HTMLResponse:
    try:
        context = {
            "nav": "import",
            "groups": file_groups(),
            "message": _message(request.query_params),
            "error": request.query_params.get("error"),
            "db_error": None,
        }
    except Exception as exc:  # noqa: BLE001
        context = _db_down("import", exc)
    return templates.TemplateResponse(request, "import.html", context)


@app.get("/demo", response_class=HTMLResponse)
def demo_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "demo.html",
        {
            "nav": "demo",
            "demos": cases(),
            "message": _message(request.query_params),
            "error": request.query_params.get("error"),
            "db_error": None,
        },
    )


@app.post("/demo")
async def load_demo(request: Request) -> RedirectResponse:
    form = await _read_form(request)
    try:
        result = run_demo(form.get("demo", ""))
    except SameFile as exc:
        return _redirect("/demo", {"note": str(exc)})
    except ValueError as exc:
        return _redirect("/demo", {"error": str(exc)})
    if isinstance(result, dict) and result.get("replaced"):
        return _redirect(
            "/demo",
            {"swapped": result["replaced"], "into": result["result"].import_id, "loaded": result["result"].file_name},
        )
    if isinstance(result, dict) and result.get("already"):
        return _redirect(
            "/demo",
            {"already": result["import_id"], "loaded": result["file_name"]},
        )
    if isinstance(result, dict) and result.get("failed"):
        return _redirect(
            "/demo",
            {
                "stopped": result["file_name"],
                "done": result["done"],
                "total": result["total"],
            },
        )
    if result.already_loaded:
        return _redirect(
            "/demo",
            {"already": result.import_id, "loaded": result.file_name},
        )
    params = {"loaded": result.file_name, "rows": result.row_count, "batches": result.batches}
    if result.blank_rows:
        params["blanks"] = result.blank_rows
    return _redirect("/demo", params)


@app.post("/demo/clear")
def clear_demo() -> RedirectResponse:
    try:
        clear_import_data()
    except Exception as exc:  # noqa: BLE001
        return _redirect("/demo", {"error": str(exc)})
    return _redirect("/demo", {"cleared": "1"})


@app.post("/import/{import_id}/delete")
def remove_import(import_id: int) -> RedirectResponse:
    try:
        file_name = delete_import(import_id)
    except ValueError as exc:
        return _redirect("/import", {"error": str(exc)})
    return _redirect("/import", {"deleted": file_name or import_id})


@app.post("/import/{import_id}/replace")
async def replace_upload(import_id: int, request: Request) -> RedirectResponse:
    form = await request.form()
    upload = form.get("file")
    if upload is None or not getattr(upload, "filename", ""):
        return _redirect("/import", {"error": "Choose a CSV file to replace it with."})
    try:
        result = replace_import(import_id, upload.filename, await upload.read())
    except SameFile as exc:
        return _redirect("/import", {"note": str(exc)})
    except ValueError as exc:
        return _redirect("/import", {"error": str(exc)})
    return _redirect(f"/process/import/{result.import_id}", {"swapped": import_id})


@app.post("/import/{import_id}/void")
def void(import_id: int) -> RedirectResponse:
    try:
        result = void_import(import_id)
    except ValueError as exc:
        return _redirect("/import", {"error": str(exc)})
    return _redirect("/import", {"voided": result["file_name"] or import_id, "removed": result["removed"]})


@app.post("/import/{import_id}/roll-back")
def roll_back(import_id: int) -> RedirectResponse:
    try:
        result = roll_back_import(import_id)
    except ValueError as exc:
        return _redirect("/import", {"error": str(exc)})
    return _redirect(
        f"/process/import/{result['earlier_id']}",
        {"rolled_back": import_id, "current": result["earlier_id"], "removed": result["removed"], "restored": result["restored"]},
    )


@app.post("/process/import/{import_id}/dismiss")
async def dismiss(import_id: int, request: Request) -> RedirectResponse:
    form = await _read_form(request)
    target = f"/process/import/{import_id}"
    key = form.get("key") or None
    try:
        count = dismiss_changes(import_id, form.get("kind") or "", [key] if key else None)
    except ValueError as exc:
        return _redirect(target, {"error": str(exc)})
    return _redirect(target, {"dismissed": count})


@app.post("/process/import/{import_id}/restore")
async def restore(import_id: int, request: Request) -> RedirectResponse:
    form = await _read_form(request)
    count = restore_changes(import_id, form.get("key") or None)
    return _redirect(f"/process/import/{import_id}", {"restored_changes": count})


@app.get("/process", response_class=HTMLResponse)
def process_page(request: Request) -> HTMLResponse:
    return _process_screen(request, error=request.query_params.get("error"))


@app.get("/process/import/{import_id}/dry", response_class=HTMLResponse)
def dry_fragment(request: Request, import_id: int, view: str = "row") -> HTMLResponse:
    try:
        dry = dry_brief(import_id)
        error = None
    except ValueError as exc:
        dry = None
        error = str(exc)
    return templates.TemplateResponse(
        request,
        "dry_panel.html" if view == "panel" else "dry_row.html",
        {"plan": dry, "writes": False, "error": error},
    )


@app.post("/process/import/{import_id}/save")
async def save_process(import_id: int, request: Request) -> RedirectResponse:
    return await _confirmed(request, import_id, "adds", "saved")


@app.post("/process/import/{import_id}/reprocess")
async def reprocess_process(import_id: int, request: Request) -> RedirectResponse:
    form = await _read_form(request)
    back = form.get("back") if (form.get("back") or "").startswith("/") else f"/process/import/{import_id}"
    try:
        result = reprocess_import(import_id)
    except ValueError as exc:
        return _redirect(back, {"error": str(exc)})
    if not result.stored:
        return _redirect(back, {"same_run": result.run_no})
    return _redirect(back, {"new_run": result.run_no, "changes": result.changes})


@app.post("/process/import/{import_id}/keep-config")
async def keep_config(import_id: int, request: Request) -> RedirectResponse:
    form = await _read_form(request)
    back = form.get("back") if (form.get("back") or "").startswith("/") else "/process"
    try:
        keep_results(import_id)
    except ValueError as exc:
        return _redirect(back, {"error": str(exc)})
    return _redirect(back, {"kept": import_id})


@app.get("/process/import/{import_id}/runs/{run_no}", response_class=HTMLResponse)
def past_run_page(request: Request, import_id: int, run_no: int) -> HTMLResponse:
    params = request.query_params
    try:
        run = run_detail(
            import_id,
            run_no,
            params.get("outcome") or None,
            (params.get("q") or "").strip(),
            _optional_int(params.get("page")) or 1,
        )
        item = import_detail(import_id)
        error = None
    except ValueError as exc:
        run, item, error = None, None, str(exc)
    return templates.TemplateResponse(
        request,
        "run_past.html",
        {"nav": "process", "run": run, "item": item, "message": None, "error": error, "db_error": None},
    )


@app.post("/process/import/{import_id}/replace")
async def replace_process(import_id: int, request: Request) -> RedirectResponse:
    return await _confirmed(request, import_id, "replace", "replaced")


@app.post("/process/import/{import_id}/set-aside")
async def set_aside_process(import_id: int, request: Request) -> RedirectResponse:
    return await _confirmed(request, import_id, "set_aside", "set_aside")


@app.post("/process/import/{import_id}/absent")
async def absent_process(import_id: int, request: Request) -> RedirectResponse:
    return await _confirmed(request, import_id, "absent", "absent")


@app.get("/process/import/{import_id}", response_class=HTMLResponse)
def process_import_page(request: Request, import_id: int) -> HTMLResponse:
    params = request.query_params
    try:
        item = import_detail(
            import_id,
            params.get("show") or None,
            (params.get("q") or "").strip(),
            _optional_int(params.get("page")) or 1,
        )
        db_error = None
    except Exception as exc:  # noqa: BLE001
        return templates.TemplateResponse(
            request,
            "run.html",
            _db_down("process", exc) | {"item": None},
        )
    return templates.TemplateResponse(
        request,
        "run.html",
        {
            "nav": "process",
            "item": item,
            "message": _message(request.query_params),
            "error": request.query_params.get("error"),
            "db_error": db_error,
        },
    )


@app.get("/main", response_class=HTMLResponse)
def main_page(request: Request) -> HTMLResponse:
    page = _optional_int(request.query_params.get("page")) or 1
    view = "checks" if request.query_params.get("view") == "checks" else "rows"
    try:
        context = {
            "nav": "main",
            "view": view,
            "table": main_table(
                request.query_params.get("sort") or "sample",
                request.query_params.get("dir") or "asc",
                page,
            ),
            "checks": run_checks() if view == "checks" else [],
            "db_error": None,
        }
    except Exception as exc:  # noqa: BLE001
        context = _db_down("main", exc)
        context["table"] = None
        context["checks"] = []
    return templates.TemplateResponse(request, "main_assays.html", context)


@app.get("/config", response_class=HTMLResponse)
def config_page(request: Request) -> HTMLResponse:
    return _config_screen(request)


@app.post("/config/ask", response_class=HTMLResponse)
async def config_ask(request: Request) -> HTMLResponse:
    form = await _read_form(request)
    try:
        chosen = describe(form.get("module", ""))
    except ValueError as exc:
        return _config_screen(request, error=str(exc))
    return _config_screen(request, stage="confirm", chosen=chosen)


@app.post("/config/apply", response_class=HTMLResponse)
async def config_apply(request: Request) -> HTMLResponse:
    form = await _read_form(request)
    try:
        chosen = activate(form.get("module", ""))
    except ValueError as exc:
        return _config_screen(request, error=str(exc))
    tested = check_all()
    return _redirect(
        "/config",
        {"config_loaded": chosen["version"], "tested": tested["checked"], "affected": tested["affected"]},
    )


@app.post("/config/reprocess")
def config_reprocess() -> RedirectResponse:
    try:
        count = reprocess_affected()
    except ValueError as exc:
        return _redirect("/process", {"error": str(exc)})
    return _redirect("/process", {"reprocessed": count})


def _process_screen(request: Request, error: str | None, db_error: str | None = None):
    groups = []
    if db_error is None:
        try:
            groups = queue()
        except Exception as exc:  # noqa: BLE001
            db_error = str(exc)
    return templates.TemplateResponse(
        request,
        "process.html",
        {
            "nav": "process",
            "groups": groups,
            "message": None if error else _message(request.query_params),
            "error": error,
            "db_error": db_error,
        },
    )


def _config_screen(
    request: Request,
    stage: str | None = None,
    chosen: dict | None = None,
    error: str | None = None,
):
    return templates.TemplateResponse(
        request,
        "config.html",
        {
            "nav": "config",
            "config": summary(),
            "choices": [] if stage else choices(),
            "stage": stage,
            "chosen": chosen,
            "message": _message(request.query_params),
            "error": error or request.query_params.get("error"),
            "db_error": None,
        },
    )


def _db_down(nav: str, exc: Exception) -> dict:
    return {
        "nav": nav,
        "message": None,
        "error": None,
        "db_error": str(exc),
        "groups": [],
        "demos": [],
    }


def _message(params) -> str | None:
    def rows(count) -> str:
        shown = f"{int(count):,}" if str(count).isdigit() else count
        return f"{shown} row" if str(count) == "1" else f"{shown} rows"

    def name(value) -> str:
        if not str(value or "").isdigit():
            return str(value or "")
        return name_lookup()[int(value)]["label"]

    def version(value) -> str:
        found = name_lookup()[int(value)] if str(value or "").isdigit() else None
        return f"version {found['version']}" if found else str(value or "")

    if params.get("note"):
        return params.get("note")
    if params.get("cleared"):
        return "Database cleared. Every import, run, and main row is gone. The loaded config is unchanged."
    if params.get("rolled_back"):
        text = f"Rolled back {name(params.get('rolled_back'))}. {version(params.get('current')).capitalize()} ({name_lookup()[int(params.get('current') or 0)]['file']}) is current again."
        removed, restored = params.get("removed") or "0", params.get("restored") or "0"
        if removed != "0":
            text += f" Took {rows(removed)} it saved out of main."
        if restored != "0":
            text += f" Put back {rows(restored)} it had changed."
        return text
    if "dismissed" in params:
        count = params.get("dismissed")
        return f"Dismissed {count} proposed change{'s' if count != '1' else ''}. Main was not changed."
    if "restored_changes" in params:
        count = params.get("restored_changes")
        return f"Restored {count} dismissed change{'s' if count != '1' else ''}. Save {'it' if count == '1' else 'them'} below if {'it is' if count == '1' else 'they are'} right."
    if params.get("config_loaded"):
        text = f"Loaded {params.get('config_loaded')}."
        tested = params.get("tested")
        if tested and tested != "0":
            affected = params.get("affected") or "0"
            imports = "import" if tested == "1" else "imports"
            if affected == "0":
                text += f" Tested {tested} {imports}: no results change."
            else:
                text += f" Tested {tested} {imports}: {affected} would get different results. See Process."
        else:
            text += " No import results change."
        return text
    if params.get("swapped"):
        loaded = params.get("loaded")
        into = params.get("into")
        if into:
            return f"{name(params.get('swapped'))} was replaced by {version(into)} ({loaded}). Process it on Process to see what changed."
        return f"This file replaces {name(params.get('swapped'))}. Process it to see what changed."
    if params.get("already"):
        return f"These exact bytes are already stored as {name(params.get('already'))}. Nothing new was stored."
    if params.get("loaded"):
        loaded = f"Loaded {params.get('loaded')}: {rows(params.get('rows'))}."
        blanks = params.get("blanks")
        if blanks and str(blanks) != "0":
            loaded += f" Skipped {blanks} blank line{'s' if str(blanks) != '1' else ''}."
        return loaded
    if params.get("stopped"):
        return (
            f"Loading {params.get('stopped')} stopped during batch {params.get('done')} "
            f"of {params.get('total')}. Nothing was stored."
        )
    if params.get("deleted"):
        return f"Deleted {params.get('deleted')}."
    if params.get("voided"):
        removed = params.get("removed") or "0"
        if removed == "0":
            return f"Voided {params.get('voided')}. None of its rows were in main."
        return f"Voided {params.get('voided')}. Its {rows(removed)} in main moved to the archive."
    if params.get("same_run"):
        return f"Processed again. Every row got the same result as run {params.get('same_run')}, so no new run was stored."
    if params.get("new_run"):
        changes = params.get("changes")
        if params.get("new_run") == "1":
            return "Processed. Results stored as run 1."
        return f"Processed again. Results stored as run {params.get('new_run')}" + (f": {changes}." if changes else ".")
    if params.get("kept"):
        return f"{name(params.get('kept'))} keeps its current results. It won't be flagged for {cfg.CONFIG_VERSION} again."
    if "saved" in params:
        return f"Added {rows(params.get('saved'))} to main."
    if "reprocessed" in params:
        count = params.get("reprocessed")
        return f"Processed {count} import{'s' if count != '1' else ''} again with the loaded config."
    if "replaced" in params:
        return f"Updated {rows(params.get('replaced'))} in main. The old values are in the archive."
    if "set_aside" in params:
        return f"Removed {rows(params.get('set_aside'))} that fail the config. They are in the archive."
    if "absent" in params:
        return f"Removed {rows(params.get('absent'))} that this file no longer has. They are in the archive."
    return None


async def _confirmed(request: Request, import_id: int, action: str, key: str) -> RedirectResponse:
    form = await _read_form(request)
    target = f"/process/import/{import_id}"
    try:
        result = confirm_write(
            import_id,
            action,
            _required_int(form.get("run_id")),
            form.get("digest") or "",
        )
    except ValueError as exc:
        return _redirect(target, {"error": str(exc)})
    return _redirect(target, {key: result.count})


def _required_int(value: str | None) -> int:
    if value is None or not str(value).isdigit():
        raise ValueError("The review is missing its run. Main was not changed.")
    return int(value)


def _redirect(path: str, params: dict) -> RedirectResponse:
    query = urlencode({key: value for key, value in params.items() if value is not None})
    target = path if not query else f"{path}?{query}"
    return RedirectResponse(target, status_code=303)


async def _read_form(request: Request) -> dict[str, str]:
    raw = (await request.body()).decode("utf-8")
    parsed = parse_qs(raw, keep_blank_values=True)
    return {key: values[0] for key, values in parsed.items() if values}


def _optional_int(value: str | None) -> int | None:
    if value is None or not str(value).isdigit():
        return None
    return int(value)
