"""Classify raw rows into processing outcomes and store a run."""

from __future__ import annotations

import json
from dataclasses import dataclass

from pymysql.cursors import DictCursor

import app.config as cfg
from app.db import get_conn
from app.services.rules import (
    RULES_ID,
    business_values,
    classify,
    current_rules,
    duplicate_notes,
)

OUTCOMES = ("good", "already_same", "changed", "invalid")


@dataclass(frozen=True)
class ProcessResult:
    import_id: int
    run_id: int
    run_no: int
    config_version: str
    status: str
    reason: str | None
    counts: dict[str, int]
    stored: bool
    changes: str


def process_import(import_id: int) -> ProcessResult:
    """Classify every raw row. A new run is stored only if a result differs from the latest run."""
    with get_conn() as conn:
        try:
            with conn.cursor(DictCursor) as cur:
                cur.execute(
                    "SELECT import_id, archived_at FROM import_batch WHERE import_id = %s FOR UPDATE",
                    (import_id,),
                )
                batch = cur.fetchone()
                if batch is None:
                    raise ValueError(f"Import {import_id} was not found.")
                if batch["archived_at"] is not None:
                    raise ValueError(f"Import {import_id} is archived and can no longer be processed.")
            evaluated = _evaluate(conn, import_id)
            with conn.cursor(DictCursor) as cur:
                latest = latest_run(cur, import_id)
                compared = _compare(cur, import_id, latest, evaluated)
            if latest is not None and not compared["differs"]:
                if latest["config_version"] != cfg.CONFIG_VERSION:
                    record_check(conn, int(latest["run_id"]), 0, {})
                conn.commit()
                return ProcessResult(
                    import_id=import_id,
                    run_id=int(latest["run_id"]),
                    run_no=int(latest["run_no"] or 0),
                    config_version=latest["config_version"],
                    status=evaluated["status"],
                    reason=evaluated["reason"],
                    counts=evaluated["counts"],
                    stored=False,
                    changes="",
                )
            result = _store_run(
                conn,
                import_id,
                status=evaluated["status"],
                reason=evaluated["reason"],
                counts=evaluated["counts"],
                classified=evaluated["classified"],
                changes=compared["summary"],
            )
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise


def latest_run(cur, import_id: int) -> dict | None:
    cur.execute(
        """
        SELECT run_id, run_no, config_version, summary_json
        FROM processing_run
        WHERE import_id = %s
        ORDER BY run_id DESC
        LIMIT 1
        """,
        (import_id,),
    )
    return cur.fetchone()


def stored_rows(cur, run_id: int) -> dict[int, dict]:
    """Every row of a stored run, keyed by raw row, in file order."""
    cur.execute(
        """
        SELECT prow.raw_id, raw.source_row_no, prow.row_key, prow.outcome, prow.reason, prow.payload_json
        FROM processing_row prow
        INNER JOIN raw_row raw ON raw.raw_id = prow.raw_id
        WHERE prow.run_id = %s
        ORDER BY raw.file_id, raw.source_row_no
        """,
        (run_id,),
    )
    return {
        int(row["raw_id"]): {
            "raw_id": int(row["raw_id"]),
            "source_row_no": int(row["source_row_no"]),
            "row_key": row["row_key"] or "",
            "outcome": row["outcome"],
            "reason": row["reason"] or "",
            "payload": _json_dict(row["payload_json"]),
        }
        for row in cur.fetchall()
    }


def main_snapshot(cur) -> dict[str, dict]:
    cur.execute("SELECT row_key, import_id, payload_json FROM main_assay")
    return {
        row["row_key"]: {"import_id": int(row["import_id"]), "payload": _json_dict(row["payload_json"])}
        for row in cur.fetchall()
        if row["row_key"]
    }


def lineage(cur, import_id: int) -> list[int]:
    """This import, then each import it replaced, newest first."""
    chain = [import_id]
    current = import_id
    while True:
        cur.execute("SELECT replaces_import_id FROM import_batch WHERE import_id = %s", (current,))
        row = cur.fetchone()
        earlier = (row["replaces_import_id"] if isinstance(row, dict) else row[0]) if row else None
        if earlier is None or int(earlier) in chain:
            return chain
        current = int(earlier)
        chain.append(current)


def compare_with_latest(conn, import_id: int, evaluated: dict) -> dict:
    with conn.cursor(DictCursor) as cur:
        return _compare(cur, import_id, latest_run(cur, import_id), evaluated)


def _compare(cur, import_id: int, latest: dict | None, evaluated: dict) -> dict:
    """Does this evaluation differ from the latest stored run? Also the summary kept on a new run."""
    from app.services.diff import count_kinds, run_changes, version_changes

    rules = current_rules()
    new_rows = {row["raw_id"]: row for row in evaluated["rows"]}
    if latest is None:
        summary: dict = {}
        cur.execute("SELECT replaces_import_id FROM import_batch WHERE import_id = %s", (import_id,))
        earlier = cur.fetchone()
        earlier_id = earlier["replaces_import_id"] if earlier else None
        if earlier_id is not None:
            before = latest_run(cur, int(earlier_id))
            if before is not None:
                found = version_changes(
                    list(stored_rows(cur, int(before["run_id"])).values()),
                    list(new_rows.values()),
                    rules,
                )
                summary = {"replaces": int(earlier_id), "counts": count_kinds(found)}
        return {"differs": True, "changes": [], "settled": 0, "summary": summary}
    old_summary = _json_dict(latest["summary_json"])
    if old_summary.get("status") != evaluated["status"] or (
        evaluated["status"] != "completed" and (old_summary.get("reason") or "") != evaluated["reason"]
    ):
        return {
            "differs": True,
            "changes": [],
            "settled": 0,
            "summary": {"compared_with": int(latest["run_no"] or 0), "counts": {}, "status_changed": True},
        }
    if old_summary.get("rules") != RULES_ID:
        return {
            "differs": True,
            "changes": [],
            "settled": 0,
            "summary": {"compared_with": int(latest["run_no"] or 0), "counts": {}, "rules_changed": True},
        }
    changes, settled = run_changes(
        stored_rows(cur, int(latest["run_id"])),
        new_rows,
        rules,
        main_snapshot(cur),
        set(lineage(cur, import_id)),
    )
    return {
        "differs": bool(changes),
        "changes": changes,
        "settled": settled,
        "summary": {"compared_with": int(latest["run_no"] or 0), "counts": count_kinds(changes)},
    }


def record_check(conn, run_id: int, affected: int, counts: dict) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO config_check (run_id, config_version, affected, summary_json)
            VALUES (%s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
              affected = VALUES(affected),
              summary_json = VALUES(summary_json),
              checked_at = CURRENT_TIMESTAMP,
              dismissed_at = IF(VALUES(affected) = 0, NULL, dismissed_at)
            """,
            (run_id, cfg.CONFIG_VERSION, affected, json.dumps(counts)),
        )


ROWS_PAGE = 25


def run_rows(run_id: int, outcome: str | None, query: str, page: int) -> dict:
    """One page of a stored run's outcomes, in file row order."""
    where = ["prow.run_id = %s"]
    params: list = [run_id]
    if outcome in OUTCOMES:
        where.append("prow.outcome = %s")
        params.append(outcome)
    if query:
        where.append("(prow.row_key LIKE %s OR prow.reason LIKE %s OR raw.source_row_no = %s)")
        like = f"%{query}%"
        params.extend([like, like, int(query) if query.isdigit() else -1])
    clause = " AND ".join(where)
    with get_conn() as conn:
        with conn.cursor(DictCursor) as cur:
            cur.execute(
                f"""
                SELECT COUNT(*) AS n
                FROM processing_row prow
                INNER JOIN raw_row raw ON raw.raw_id = prow.raw_id
                WHERE {clause}
                """,
                params,
            )
            total = int(cur.fetchone()["n"])
            pages = max((total + ROWS_PAGE - 1) // ROWS_PAGE, 1)
            page = min(max(page, 1), pages)
            cur.execute(
                f"""
                SELECT raw.source_row_no, prow.row_key, prow.outcome, prow.reason
                FROM processing_row prow
                INNER JOIN raw_row raw ON raw.raw_id = prow.raw_id
                WHERE {clause}
                ORDER BY raw.file_id, raw.source_row_no
                LIMIT %s OFFSET %s
                """,
                [*params, ROWS_PAGE, (page - 1) * ROWS_PAGE],
            )
            fetched = cur.fetchall()
    return {
        "rows": [
            {
                "source_row_no": int(row["source_row_no"]),
                "row_key": row["row_key"] or "",
                "outcome": row["outcome"],
                "reason": row["reason"] or "",
            }
            for row in fetched
        ],
        "total": total,
        "page": page,
        "pages": pages,
        "start": 0 if total == 0 else (page - 1) * ROWS_PAGE + 1,
        "end": (page - 1) * ROWS_PAGE + len(fetched),
    }


def preview_import(conn, import_id: int) -> dict:
    """Classify again with the loaded config, in memory. Nothing is stored."""
    return _evaluate(conn, import_id)


def _evaluate(conn, import_id: int) -> dict:
    with conn.cursor(DictCursor) as cur:
        cur.execute(
            """
            SELECT file_id, file_name, headers_json
            FROM import_file
            WHERE import_id = %s
            ORDER BY file_id
            """,
            (import_id,),
        )
        files = cur.fetchall()
        if not files:
            raise ValueError(f"Import {import_id} has no file to process.")
        file_name = ", ".join(row["file_name"] for row in files)

        cur.execute(
            """
            SELECT raw_id, file_id, source_row_no, payload_json
            FROM raw_row
            WHERE import_id = %s
            ORDER BY file_id, source_row_no
            """,
            (import_id,),
        )
        raw_rows = cur.fetchall()
        if not raw_rows:
            raise ValueError(f"Import {import_id} has no raw rows to process.")

        cur.execute("SELECT row_key, payload_json FROM main_assay")
        main_rows = cur.fetchall()

    main_by_key = {}
    for row in main_rows:
        if row["row_key"]:
            main_by_key[row["row_key"]] = _json_dict(row["payload_json"])

    rules = current_rules()
    bindings, header_problems = _bind_files(files)
    if header_problems:
        reason = " ".join(header_problems)
        return _evaluation(
            import_id,
            status="failed",
            reason=reason,
            counts=_empty_counts(),
            rows=[],
            classified=[],
            file_name=file_name,
        )

    prepared = []
    occurrences: dict[str, list[int]] = {}
    for raw in raw_rows:
        binding = bindings[int(raw["file_id"])][0]
        payload, note = business_values(_json_dict(raw["payload_json"]), rules, binding)
        source = int(raw["source_row_no"])
        key = payload.get(rules.key) or ""
        if key:
            occurrences.setdefault(key, []).append(source)
        prepared.append((raw, payload, note, source))
    dupes = duplicate_notes(occurrences, rules.key)

    classified = []
    rows = []
    counts = _empty_counts()
    for raw, payload, note, source in prepared:
        outcome, reason, row_key = classify(
            payload,
            main_by_key.get(row_key_of(payload)),
            dupes.get(source),
            rules,
        )
        if note and outcome != "invalid":
            reason = f"{reason}; {note}" if reason else note
        counts[outcome] += 1
        classified.append(
            (
                int(raw["raw_id"]),
                row_key,
                outcome,
                reason,
                json.dumps(payload, ensure_ascii=False),
            )
        )
        rows.append(
            {
                "raw_id": int(raw["raw_id"]),
                "source_row_no": source,
                "row_key": row_key or "",
                "outcome": outcome,
                "reason": reason or "",
                "payload": payload,
            }
        )

    evaluation = _evaluation(
        import_id,
        status="completed",
        reason=None,
        counts=counts,
        rows=rows,
        classified=classified,
        file_name=file_name,
    )
    return evaluation


def _evaluation(
    import_id: int,
    status: str,
    reason: str | None,
    counts: dict[str, int],
    rows: list[dict],
    classified: list[tuple],
    file_name: str,
) -> dict:
    return {
        "import_id": import_id,
        "file_name": file_name,
        "config_version": cfg.CONFIG_VERSION,
        "status": status,
        "reason": reason or "",
        "counts": counts,
        "rows": [
            {
                "raw_id": row["raw_id"],
                "source_row_no": row["source_row_no"],
                "row_key": row["row_key"],
                "outcome": row["outcome"],
                "reason": row["reason"],
                "payload": row["payload"],
            }
            for row in rows
        ],
        "classified": classified,
    }


def _store_run(
    conn,
    import_id: int,
    status: str,
    reason: str | None,
    counts: dict[str, int],
    classified: list[tuple],
    changes: dict | None = None,
) -> ProcessResult:
    summary = {
        "status": status,
        "config_version": cfg.CONFIG_VERSION,
        "rules": RULES_ID,
        "counts": counts,
        "row_count": sum(counts.values()),
    }
    if changes:
        summary["changes"] = changes
    if reason:
        summary["reason"] = reason
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COALESCE(MAX(run_no), 0) + 1 FROM processing_run WHERE import_id = %s",
            (import_id,),
        )
        run_no = int(cur.fetchone()[0])
        cur.execute(
            """
            INSERT INTO processing_run (import_id, run_no, config_version, summary_json)
            VALUES (%s, %s, %s, %s)
            """,
            (import_id, run_no, cfg.CONFIG_VERSION, json.dumps(summary, ensure_ascii=False)),
        )
        run_id = int(cur.lastrowid)
        if classified:
            cur.executemany(
                """
                INSERT INTO processing_row (
                  run_id, import_id, raw_id, row_key, outcome, reason, payload_json
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                [
                    (run_id, import_id, raw_id, row_key, outcome, row_reason, payload)
                    for raw_id, row_key, outcome, row_reason, payload in classified
                ],
            )
        cur.execute(
            """
            UPDATE processing_run
            SET finished_at = CURRENT_TIMESTAMP, summary_json = %s
            WHERE run_id = %s
            """,
            (json.dumps(summary, ensure_ascii=False), run_id),
        )
    from app.services.diff import kinds_phrase

    return ProcessResult(
        import_id=import_id,
        run_id=run_id,
        run_no=run_no,
        config_version=cfg.CONFIG_VERSION,
        status=status,
        reason=reason,
        counts=counts,
        stored=True,
        changes=kinds_phrase((changes or {}).get("counts") or {}),
    )


def _bind_files(files: list[dict]) -> tuple[dict[int, tuple[dict[str, str], list[str]]], list[str]]:
    bindings: dict[int, tuple[dict[str, str], list[str]]] = {}
    problems: list[str] = []
    required_lower = {name.lower() for name in cfg.REQUIRED_HEADERS}
    for file in files:
        headers = _json_list(file["headers_json"])
        by_lower = {header.lower(): header for header in headers}
        missing = [name for name in cfg.REQUIRED_HEADERS if name.lower() not in by_lower]
        if missing:
            problems.append(
                f"{file['file_name']} is missing required headers: {', '.join(missing)}."
            )
            continue
        binding = {name: by_lower[name.lower()] for name in cfg.REQUIRED_HEADERS}
        extras = [header for header in headers if header.lower() not in required_lower]
        bindings[int(file["file_id"])] = (binding, extras)
    return bindings, problems


def row_key_of(payload: dict[str, str]) -> str | None:
    return payload.get(cfg.KEY_HEADER) or None


def _text(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _empty_counts() -> dict[str, int]:
    return {name: 0 for name in OUTCOMES}


def _json_dict(value: object) -> dict:
    loaded = _json_value(value)
    return loaded if isinstance(loaded, dict) else {}


def _json_list(value: object) -> list:
    loaded = _json_value(value)
    return loaded if isinstance(loaded, list) else []


def _json_value(value: object):
    if isinstance(value, str):
        return json.loads(value)
    return value
