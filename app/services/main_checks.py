"""SQL checks on main. Each check is one query built from the loaded config.

The query shown on the page is the query that runs. Each failure names the
import, the file, and the file row its main value came from, and marks the
columns that fail.
"""

from __future__ import annotations

from pymysql.cursors import DictCursor

from app.db import get_conn
from app.services.rules import business_values, check_catalog, current_rules, overlap_check_name

SHOWN = 25
_NUMBER = "'^[-+]?([0-9]+([.][0-9]*)?|[.][0-9]+)$'"


def run_checks() -> list[dict]:
    rules = current_rules()
    numeric = {rules.interval_from, rules.interval_to, *rules.analytes}
    queries = check_queries(rules)
    checks = []
    with get_conn() as conn:
        with conn.cursor(DictCursor) as cur:
            for name, rule, columns in check_catalog(rules):
                sql = queries[name]
                cur.execute(sql)
                found = cur.fetchall()
                if name == overlap_check_name(rules):
                    _name_overlaps(found)
                shown = found[:SHOWN]
                details = _details(cur, [row["row_key"] for row in shown])
                checks.append(
                    {
                        "name": name,
                        "rule": rule,
                        "sql": sql.strip(),
                        "passed": not found,
                        "failure_count": len(found),
                        "detail": "Pass" if not found else f"{len(found):,} row{'s' if len(found) != 1 else ''} fail",
                        "columns": [{"name": column, "num": column in numeric} for column in columns],
                        "failures": [
                            _failure(row, details.get(row["row_key"]), columns, numeric, rules)
                            for row in shown
                        ],
                    }
                )
    return checks


def check_queries(rules) -> dict[str, str]:
    """Check name → SELECT returning the failing main rows (row_key, why)."""
    key, hole, kind = _col(rules.key), _col(rules.hole), _col(rules.sample_type)
    start, end = _col(rules.interval_from), _col(rules.interval_to)
    qaqc = ", ".join(f"'{name.upper()}'" for name in rules.qaqc_types)
    queries = {
        f"{rules.key} is present": f"""
SELECT m.row_key, '{rules.key} is blank' AS why
FROM main_assay m
WHERE {key} = ''
ORDER BY m.row_key""",
        f"{rules.key} matches the row key": f"""
SELECT m.row_key, '{rules.key} does not match the row key' AS why
FROM main_assay m
WHERE {key} <> m.row_key
ORDER BY m.row_key""",
        f"{rules.hole} is present on interval samples": f"""
SELECT m.row_key, '{rules.hole} is blank' AS why
FROM main_assay m
WHERE UPPER({kind}) NOT IN ({qaqc})
  AND {hole} = ''
ORDER BY m.row_key""",
        f"{rules.interval_from} is less than {rules.interval_to}": f"""
SELECT m.row_key,
  CASE
    WHEN NOT {start} REGEXP {_NUMBER} THEN '{rules.interval_from} is not a number'
    WHEN NOT {end} REGEXP {_NUMBER} THEN '{rules.interval_to} is not a number'
    ELSE '{rules.interval_from} must be less than {rules.interval_to}'
  END AS why
FROM main_assay m
WHERE NOT (UPPER({kind}) IN ({qaqc}) AND {start} = '' AND {end} = '')
  AND (
    NOT {start} REGEXP {_NUMBER}
    OR NOT {end} REGEXP {_NUMBER}
    OR CAST({start} AS DECIMAL(18, 6)) >= CAST({end} AS DECIMAL(18, 6))
  )
ORDER BY m.row_key""",
    }
    from app.services.rules import analyte_check_name

    for name in rules.analytes:
        value = _col(name)
        minimum = ""
        why = f"'{name} is not a number'"
        if rules.min_analyte is not None:
            minimum = f"\n  OR CAST({value} AS DECIMAL(18, 6)) < {rules.min_analyte}"
            why = (
                f"CASE WHEN NOT {value} REGEXP {_NUMBER} THEN '{name} is not a number' "
                f"ELSE '{name} must be {rules.min_analyte} or greater' END"
            )
        queries[analyte_check_name(name, rules.min_analyte)] = f"""
SELECT m.row_key, {why} AS why
FROM main_assay m
WHERE NOT {value} REGEXP {_NUMBER}{minimum}
ORDER BY m.row_key"""
    skipped = ", ".join(f"'{name.upper()}'" for name in (*rules.qaqc_types, *rules.duplicate_types))
    queries[overlap_check_name(rules)] = f"""
WITH intervals AS (
  SELECT m.row_key,
    {hole} AS hole,
    CAST({start} AS DECIMAL(18, 6)) AS depth_from,
    CAST({end} AS DECIMAL(18, 6)) AS depth_to
  FROM main_assay m
  WHERE {hole} <> ''
    AND UPPER({kind}) NOT IN ({skipped})
    AND {start} REGEXP {_NUMBER}
    AND {end} REGEXP {_NUMBER}
    AND CAST({start} AS DECIMAL(18, 6)) < CAST({end} AS DECIMAL(18, 6))
),
reach AS (
  SELECT i.*,
    MAX(depth_to) OVER (down_hole ROWS UNBOUNDED PRECEDING) AS reach_here,
    LEAD(depth_from) OVER down_hole AS next_from
  FROM intervals i
  WINDOW down_hole AS (PARTITION BY hole ORDER BY depth_from, depth_to, row_key)
),
ordered AS (
  SELECT r.*,
    LAG(reach_here) OVER (PARTITION BY hole ORDER BY depth_from, depth_to, row_key) AS reach_before
  FROM reach r
)
SELECT row_key, hole, depth_from, depth_to, 'Overlaps another sample on the same hole' AS why
FROM ordered
WHERE reach_before > depth_from OR next_from < depth_to
ORDER BY row_key"""
    return queries


def _name_overlaps(found: list[dict]) -> None:
    """Say which samples each failing interval overlaps. Every partner is itself a failure."""
    by_hole: dict[str, list[dict]] = {}
    for row in found:
        by_hole.setdefault(row["hole"], []).append(row)
    for rows in by_hole.values():
        rows.sort(key=lambda row: (row["depth_from"], row["depth_to"]))
        for row in rows:
            partners = [
                other for other in rows
                if other is not row and other["depth_from"] < row["depth_to"] and row["depth_from"] < other["depth_to"]
            ]
            described = ", ".join(
                f"{other['row_key']} ({_metres(max(row['depth_from'], other['depth_from']))}–"
                f"{_metres(min(row['depth_to'], other['depth_to']))} m)"
                for other in partners[:3]
            )
            more = f" and {len(partners) - 3} more" if len(partners) > 3 else ""
            row["why"] = f"Shares metres with {described}{more}" if partners else row["why"]


def _metres(value) -> str:
    return f"{value.normalize():f}" if hasattr(value, "normalize") else str(value)


def _col(name: str) -> str:
    return f"COALESCE(JSON_UNQUOTE(JSON_EXTRACT(m.payload_json, '$.\"{name}\"')), '')"


def _details(cur, keys: list[str]) -> dict[str, dict]:
    """Main value, import, file, and file row for each shown key."""
    if not keys:
        return {}
    marks = ", ".join(["%s"] * len(keys))
    cur.execute(
        f"""
        SELECT m.row_key, m.import_id, m.payload_json, f.file_name,
          (
            SELECT MIN(r.source_row_no) FROM raw_row r
            WHERE r.import_id = m.import_id AND r.row_key = m.row_key
          ) AS source_row_no
        FROM main_assay m
        LEFT JOIN import_file f ON f.import_id = m.import_id
        WHERE m.row_key IN ({marks})
        """,
        keys,
    )
    return {row["row_key"]: row for row in cur.fetchall()}


def _failure(row: dict, detail: dict | None, columns, numeric, rules) -> dict:
    import json

    payload = detail["payload_json"] if detail else {}
    if isinstance(payload, str):
        payload = json.loads(payload)
    values, _ = business_values(payload or {}, rules)
    why = row["why"] or ""
    bad = {name for name in columns if name in why}
    if "hole" in row:
        bad = {rules.interval_from, rules.interval_to}
    return {
        "row_key": row["row_key"],
        "import_id": int(detail["import_id"]) if detail else None,
        "file_name": (detail or {}).get("file_name") or "",
        "source_row_no": (detail or {}).get("source_row_no"),
        "reason": why,
        "values": [
            {"name": name, "value": values.get(name, ""), "bad": name in bad, "num": name in numeric}
            for name in columns
        ],
    }
