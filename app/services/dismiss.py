"""Dismiss proposed changes so they stop asking to be saved. Main is not touched.

A dismissal holds while the proposal stays the same. If the file value or the
main value changes, the row is proposed again.
"""

from __future__ import annotations

from app.db import get_conn
from app.services.plan import build_plan

_LISTS = {"new": "adds", "update": "replacements", "remove": "set_asides", "absent": "absent"}


def dismiss_changes(import_id: int, kind: str, keys: list[str] | None) -> int:
    if kind not in _LISTS:
        raise ValueError("Unknown change.")
    with get_conn() as conn:
        try:
            plan = build_plan(conn, import_id)
            if plan["archived"]:
                raise ValueError("This import was replaced or voided. Nothing was dismissed.")
            chosen = [item for item in plan[_LISTS[kind]] if keys is None or item["row_key"] in keys]
            if not chosen:
                raise ValueError("Those changes are no longer proposed. Nothing was dismissed.")
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO dismissed_change (import_id, row_key, kind, payload_sha, run_id)
                    VALUES (%s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE payload_sha = VALUES(payload_sha), run_id = VALUES(run_id),
                      dismissed_at = CURRENT_TIMESTAMP
                    """,
                    [(import_id, item["row_key"], kind, item["sha"], plan["run_id"]) for item in chosen],
                )
            conn.commit()
            return len(chosen)
        except Exception:
            conn.rollback()
            raise


def restore_changes(import_id: int, key: str | None) -> int:
    with get_conn() as conn:
        with conn.cursor() as cur:
            if key:
                cur.execute("DELETE FROM dismissed_change WHERE import_id = %s AND row_key = %s", (import_id, key))
            else:
                cur.execute("DELETE FROM dismissed_change WHERE import_id = %s", (import_id,))
            count = cur.rowcount
        conn.commit()
    return count
