"""After a config change, test each import's latest run against the loaded config.

A run whose rows would all get the same result keeps working as is. A run where
some rows would differ is flagged until it is processed again or the change is
dismissed for that import.
"""

from __future__ import annotations

from pymysql.cursors import DictCursor

import app.config as cfg
from app.db import get_conn
from app.services.diff import count_kinds


def check_all() -> dict:
    """Test every run that has not been tested against the loaded config yet."""
    from app.services.processing import compare_with_latest, preview_import, record_check

    checked = affected = 0
    for import_id, run_id in _unchecked():
        with get_conn() as conn:
            evaluated = preview_import(conn, import_id)
            compared = compare_with_latest(conn, import_id, evaluated)
            conn.rollback()
            changes = compared["changes"] if compared["differs"] else []
            count = len(changes) if changes else (1 if compared["differs"] else 0)
            record_check(conn, run_id, count, count_kinds(changes))
            conn.commit()
        checked += 1
        affected += 1 if count else 0
    return {"checked": checked, "affected": affected}


def states() -> dict[int, dict]:
    """Per import: how its latest run stands against the loaded config."""
    with get_conn() as conn:
        with conn.cursor(DictCursor) as cur:
            cur.execute(
                """
                SELECT pr.import_id, pr.run_id, pr.config_version,
                       cc.affected, cc.summary_json, cc.dismissed_at
                FROM processing_run pr
                INNER JOIN import_batch b ON b.import_id = pr.import_id AND b.archived_at IS NULL
                LEFT JOIN config_check cc ON cc.run_id = pr.run_id AND cc.config_version = %s
                WHERE pr.run_id = (
                  SELECT MAX(run.run_id) FROM processing_run run WHERE run.import_id = pr.import_id
                )
                """,
                (cfg.CONFIG_VERSION,),
            )
            rows = cur.fetchall()
    return {int(row["import_id"]): _state(row) for row in rows}


def state_for(cur, run_id: int, run_config: str) -> dict:
    cur.execute(
        """
        SELECT %s AS config_version, affected, summary_json, dismissed_at
        FROM config_check
        WHERE run_id = %s AND config_version = %s
        """,
        (run_config, run_id, cfg.CONFIG_VERSION),
    )
    row = cur.fetchone() or {"config_version": run_config, "affected": None, "summary_json": None, "dismissed_at": None}
    return _state(row)


def affected_count() -> int:
    return sum(1 for state in states().values() if state["state"] == "affected")


def dismiss(import_id: int) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE config_check cc
                INNER JOIN processing_run pr ON pr.run_id = cc.run_id
                SET cc.dismissed_at = CURRENT_TIMESTAMP
                WHERE pr.import_id = %s
                  AND cc.config_version = %s
                  AND pr.run_id = (SELECT MAX(run_id) FROM (SELECT run_id FROM processing_run WHERE import_id = %s) latest)
                """,
                (import_id, cfg.CONFIG_VERSION, import_id),
            )
            if cur.rowcount == 0:
                raise ValueError("There is no config change to keep for this import.")
        conn.commit()


def _unchecked() -> list[tuple[int, int]]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT pr.import_id, pr.run_id
                FROM processing_run pr
                INNER JOIN import_batch b ON b.import_id = pr.import_id AND b.archived_at IS NULL
                LEFT JOIN config_check cc ON cc.run_id = pr.run_id AND cc.config_version = %s
                WHERE pr.run_id = (
                  SELECT MAX(run.run_id) FROM processing_run run WHERE run.import_id = pr.import_id
                )
                  AND pr.config_version <> %s
                  AND cc.run_id IS NULL
                ORDER BY pr.import_id
                """,
                (cfg.CONFIG_VERSION, cfg.CONFIG_VERSION),
            )
            return [(int(row[0]), int(row[1])) for row in cur.fetchall()]


def _state(row: dict) -> dict:
    import json

    run_config = row["config_version"]
    if run_config == cfg.CONFIG_VERSION:
        return {"state": "current", "affected": 0, "counts": {}, "run_config": run_config}
    counts = row["summary_json"]
    if isinstance(counts, str):
        counts = json.loads(counts)
    if row["affected"] is None:
        state = "unchecked"
    elif int(row["affected"]) == 0:
        state = "unaffected"
    elif row["dismissed_at"] is not None:
        state = "kept"
    else:
        state = "affected"
    return {
        "state": state,
        "affected": int(row["affected"] or 0),
        "counts": counts or {},
        "run_config": run_config,
    }
