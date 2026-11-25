"""Numbers for the home page."""

from __future__ import annotations

from pymysql.cursors import DictCursor

import app.config as cfg
from app.db import get_conn
from app.services.config_watch import affected_count
from app.services.process_queue import queue
from app.services.schema import ensure_model


def home_stats() -> dict:
    ensure_model()
    with get_conn() as conn:
        with conn.cursor(DictCursor) as cur:
            cur.execute(
                """
                SELECT COUNT(DISTINCT b.import_id) AS n
                FROM import_batch b
                INNER JOIN import_file f ON f.import_id = b.import_id
                INNER JOIN raw_row r ON r.import_id = b.import_id
                WHERE b.archived_at IS NULL
                """
            )
            files = int(cur.fetchone()["n"])
            cur.execute("SELECT COUNT(*) AS n FROM main_assay")
            main_rows = int(cur.fetchone()["n"])
            cur.execute(
                """
                SELECT COUNT(*) AS n
                FROM processing_row pr
                INNER JOIN (
                  SELECT import_id, MAX(run_id) AS run_id
                  FROM processing_run
                  GROUP BY import_id
                ) latest ON latest.run_id = pr.run_id
                WHERE pr.outcome = 'invalid'
                """
            )
            rejections = int(cur.fetchone()["n"])
            cur.execute(
                """
                SELECT COUNT(DISTINCT b.import_id) AS n
                FROM import_batch b
                INNER JOIN processing_run pr ON pr.import_id = b.import_id
                INNER JOIN config_check cc ON cc.run_id = pr.run_id
                WHERE b.archived_at IS NULL AND cc.affected > 0 AND cc.dismissed_at IS NULL
                """
            )
            config_flagged = int(cur.fetchone()["n"])
    pending = 0
    for group in queue():
        for item in group["items"]:
            if item.get("state") == "review":
                pending += 1
    try:
        config_affected = affected_count()
    except Exception:  # noqa: BLE001
        config_affected = 0
    return {
        "files": files,
        "main_rows": main_rows,
        "rejections": rejections,
        "pending_review": pending,
        "config_flagged": config_flagged,
        "config_affected": config_affected,
        "config_version": cfg.CONFIG_VERSION,
    }