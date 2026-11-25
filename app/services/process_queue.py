"""Imports that have raw rows, and where each stands against main."""

from __future__ import annotations

from datetime import date, timedelta

from pymysql.cursors import DictCursor

import app.config as cfg
from app.db import get_conn
from app.services.apply import apply_confirmed
from app.services.diff import kinds_phrase
from app.services.config_watch import check_all, dismiss, states
from app.services.plan import filter_rows, label_for, load_plan, load_preview
from app.services.processing import process_import
from app.services.when import friendly

_QUEUE_SQL = """
    SELECT
      b.import_id,
      b.created_at,
      b.archived_at,
      f.file_name,
      (
        SELECT COUNT(*)
        FROM raw_row r
        WHERE r.import_id = b.import_id
      ) AS raw_count,
      pr.run_id,
      pr.config_version,
      pr.finished_at
    FROM import_batch b
    LEFT JOIN import_file f ON f.import_id = b.import_id
    LEFT JOIN processing_run pr ON pr.run_id = (
      SELECT MAX(run.run_id)
      FROM processing_run run
      WHERE run.import_id = b.import_id
    )
    WHERE b.status = 'succeed' {where}
    ORDER BY b.import_id DESC
"""


def _fetch(import_id: int | None = None) -> list[dict]:
    from app.services.schema import ensure_model

    ensure_model()
    where = "AND b.archived_at IS NULL" if import_id is None else "AND b.import_id = %s"
    params = () if import_id is None else (import_id,)
    with get_conn() as conn:
        with conn.cursor(DictCursor) as cur:
            cur.execute(_QUEUE_SQL.format(where=where), params)
            rows = cur.fetchall()
    seen: set[int] = set()
    kept = []
    for row in rows:
        found = int(row["import_id"])
        if found in seen or int(row["raw_count"] or 0) == 0:
            continue
        seen.add(found)
        kept.append(row)
    return kept


def queue() -> list[dict]:
    check_all()
    return _by_date([_item(row) for row in _fetch()])


def import_detail(import_id: int, state: str | None = None, query: str = "", page: int = 1) -> dict | None:
    rows = _fetch(import_id)
    if not rows:
        return None
    detail = _item(rows[0])
    detail["rows"] = filter_rows(detail["plan"], state, query, page) if detail["run_id"] else None
    detail["filter"] = state or ""
    detail["query"] = query
    return detail


def reprocess_import(import_id: int):
    if not _fetch(import_id):
        raise ValueError(f"Import {import_id} has no raw rows.")
    return process_import(import_id)


def affected_imports() -> list[int]:
    check_all()
    return [import_id for import_id, state in states().items() if state["state"] == "affected"]


def reprocess_affected() -> int:
    ids = affected_imports()
    for import_id in ids:
        process_import(import_id)
    return len(ids)


def keep_results(import_id: int) -> None:
    dismiss(import_id)


def confirm_write(import_id: int, action: str, run_id: int, digest: str):
    if not _fetch(import_id):
        raise ValueError(f"Import {import_id} has no raw rows.")
    return apply_confirmed(import_id, action, run_id, digest)


def dry_brief(import_id: int) -> dict:
    return load_preview(import_id)


def _item(row: dict) -> dict:
    plan = load_plan(int(row["import_id"]))
    state, label = label_for(plan)
    when = row["finished_at"] or row["created_at"] if row["run_id"] else row["created_at"]
    return {
        "raw_count": int(row["raw_count"] or 0),
        "import_id": int(row["import_id"]),
        "file_name": row["file_name"] or "",
        "run_id": plan["run_id"],
        "run_no": plan["run_no"],
        "state": state,
        "label": label,
        "result": _result(plan, state),
        "when": friendly(when),
        "ran_on": when if row["run_id"] else None,
        "config_version": plan["config_version"] or "",
        "config_state": plan["config"]["state"],
        "counts": plan["counts"],
        "plan": plan,
    }


def _result(plan: dict, state: str) -> str:
    """One short line for the Process list."""
    counts = plan["state_counts"]
    if state == "matches":
        in_main = counts["saved"] + counts["in_main"] + counts["newer"]
        text = f"{in_main:,} row{'s' if in_main != 1 else ''} in main"
        if counts["rejected"]:
            text += f" · {counts['rejected']:,} fail config"
        return text
    if state == "not_run":
        return ""
    if plan["config"]["state"] in {"affected", "unchecked"} and plan["status"] == "completed":
        phrase = kinds_phrase(plan["config"]["counts"])
        return f"With {cfg.CONFIG_VERSION}: {phrase}" if phrase else f"Not tested with {cfg.CONFIG_VERSION} yet"
    if state == "review":
        return plan["verdict"]["title"]
    return plan["verdict"]["detail"]


def _by_date(items: list[dict]) -> list[dict]:
    today = date.today()
    waiting = [item for item in items if item["state"] == "not_run"]
    ran = [item for item in items if item["state"] != "not_run"]
    buckets: dict[date, list[dict]] = {}
    for item in ran:
        ran_on = item["ran_on"]
        day = ran_on.date() if hasattr(ran_on, "date") else today
        buckets.setdefault(day, []).append(item)
    groups = []
    if waiting:
        groups.append({"title": "Not processed", "items": waiting})
    for day in sorted(buckets, reverse=True):
        buckets[day].sort(key=lambda item: item["ran_on"], reverse=True)
        groups.append({"title": _day_title(day, today), "items": buckets[day]})
    return groups


def _day_title(day: date, today: date) -> str:
    if day == today:
        return "Today"
    if day == today - timedelta(days=1):
        return "Yesterday"
    return f"{day.strftime('%b')} {day.day}, {day.year}"
