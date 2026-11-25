"""Paged, sorted read of main. Writes go through app.services.apply.

Each sortable column has an indexed copy on main_assay (see schema.SORT_COLUMNS),
so a page reads 25 rows from an index instead of sorting every row.
"""

from __future__ import annotations

import json

from pymysql.cursors import DictCursor

from app.db import get_conn
from app.services.when import friendly

PAGE_SIZE = 25

_SORT = {
    "sample": "m.row_key",
    "hole": "m.sort_hole",
    "samp_from": "m.sort_from",
    "samp_to": "m.sort_to",
    "sample_type": "m.sort_type",
    "au": "m.sort_au",
    "cu": "m.sort_cu",
    "ag": "m.sort_ag",
    "arsenic": "m.sort_as",
    "import": "m.import_id",
    "loaded": "m.loaded_at",
}

COLUMNS = (
    ("sample", "Sample", "key"),
    ("hole", "Hole", "text"),
    ("samp_from", "From m", "num"),
    ("samp_to", "To m", "num"),
    ("sample_type", "Type", "text"),
    ("au", "Au ppm", "num"),
    ("cu", "Cu %", "num"),
    ("ag", "Ag ppm", "num"),
    ("arsenic", "As ppm", "num"),
    ("import", "File", "text"),
    ("loaded", "Loaded", "text"),
)


def main_table(sort: str, direction: str, page: int) -> dict:
    from app.services.schema import ensure_model

    ensure_model()
    sort_key = sort if sort in _SORT else "sample"
    way = "DESC" if str(direction).lower() == "desc" else "ASC"
    page = 1 if page < 1 else page
    order = f"{_SORT[sort_key]} {way}" if sort_key == "sample" else f"{_SORT[sort_key]} {way}, m.row_key {way}"
    with get_conn() as conn:
        with conn.cursor(DictCursor) as cur:
            cur.execute("SELECT COUNT(*) AS n FROM main_assay")
            total = int(cur.fetchone()["n"])
            pages = max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1)
            page = min(page, pages)
            offset = (page - 1) * PAGE_SIZE
            cur.execute(
                f"""
                SELECT m.row_key, m.import_id, m.payload_json, m.loaded_at
                FROM main_assay m
                ORDER BY {order}
                LIMIT %s OFFSET %s
                """,
                (PAGE_SIZE, offset),
            )
            fetched = cur.fetchall()
    rows = [_table_row(row) for row in fetched]
    return {
        "rows": rows,
        "columns": COLUMNS,
        "page": page,
        "pages": pages,
        "total": total,
        "start": 0 if total == 0 else offset + 1,
        "end": offset + len(rows),
        "sort": sort_key,
        "direction": way.lower(),
        "prev": page - 1 if page > 1 else None,
        "next": page + 1 if page < pages else None,
    }


def _table_row(row: dict) -> dict:
    payload = _json_dict(row["payload_json"])
    return {
        "sample": row["row_key"] or "",
        "hole": payload.get("HoleID") or "",
        "samp_from": payload.get("SampFrom") or "",
        "samp_to": payload.get("SampTo") or "",
        "sample_type": payload.get("SampleType") or "",
        "au": payload.get("Au_ppm") or "",
        "cu": payload.get("Cu_pct") or "",
        "ag": payload.get("Ag_ppm") or "",
        "arsenic": payload.get("As_ppm") or "",
        "import_id": int(row["import_id"]),
        "loaded": friendly(row["loaded_at"]),
    }


def _json_dict(value: object) -> dict:
    if isinstance(value, str):
        loaded = json.loads(value)
        return loaded if isinstance(loaded, dict) else {}
    if isinstance(value, dict):
        return value
    return {}
