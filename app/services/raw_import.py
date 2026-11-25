"""Register an import and load CSV bytes into raw_row.

Idempotent on the file SHA-256. A successful raw load of those bytes is not stored again.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from dataclasses import dataclass
from pathlib import Path

import pymysql
from pymysql.cursors import DictCursor

import app.config as cfg
from app.db import get_conn
from app.services.schema import ensure_model
from app.settings import ROOT

FIXTURES = ROOT / "fixtures"
NOTE_MAX = 255
# A load is committed only after every batch has been prepared.
RAW_BATCH_SIZE = 1000
# The interrupt case stops after this many batches and does not commit.
INTERRUPT_AFTER_BATCHES = 3


class SameFile(ValueError):
    """The upload is byte for byte a file that is already stored."""


class _LoadInterrupted(Exception):
    """The load stopped before the commit, so nothing was stored."""

    def __init__(self, done: int, total: int) -> None:
        self.done = done
        self.total = total


def ensure_import_model() -> None:
    ensure_model()


@dataclass(frozen=True)
class ParsedCsv:
    headers: list[str]
    rows: list[tuple[int, dict[str, str]]]
    blank_rows: int


@dataclass(frozen=True)
class LoadResult:
    import_id: int
    file_id: int
    file_name: str
    fingerprint: str
    row_count: int
    column_count: int
    already_loaded: bool
    rows_inserted: int
    raw_count: int
    batches: int
    blank_rows: int = 0


def list_fixtures() -> list[str]:
    if not FIXTURES.is_dir():
        return []
    return sorted(path.name for path in FIXTURES.glob("*.csv"))


def register_and_load(
    note: str | None,
    fixture_name: str,
    group_key: str | None = None,
) -> LoadResult:
    ensure_import_model()
    file_name, data = _read_fixture(fixture_name)
    parsed = parse_csv(data)
    clean_note = _clean_note(note)
    with get_conn() as conn:
        existing = _existing_success(conn, hashlib.sha256(data).hexdigest())
        if existing is not None:
            return _already(existing)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO import_batch (status, note, group_key)
                    VALUES ('succeed', %s, %s)
                    """,
                    (clean_note, group_key or file_name),
                )
                import_id = int(cur.lastrowid)
            result = _load_parsed(conn, import_id, file_name, data, parsed)
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise


def batch_count(row_count: int, batch_size: int = RAW_BATCH_SIZE) -> int:
    if row_count <= 0:
        return 0
    return (row_count + batch_size - 1) // batch_size


def load_interrupted(fixture_name: str, group_key: str) -> dict:
    """Prepare batches, then stop before the commit. Raw rows are never stored."""
    ensure_import_model()
    file_name, data = _read_fixture(fixture_name)
    parsed = parse_csv(data)
    fingerprint = hashlib.sha256(data).hexdigest()
    with get_conn() as conn:
        existing = _existing_success(conn, fingerprint)
    if existing is not None:
        return {
            "already": True,
            "import_id": int(existing["import_id"]),
            "file_name": existing["file_name"],
            "failed": False,
        }
    headers = parsed.headers
    rows = parsed.rows
    total = batch_count(len(rows))
    done = min(INTERRUPT_AFTER_BATCHES, total)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM raw_row")
            before = int(cur.fetchone()[0])
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO import_batch (status, group_key)
                    VALUES ('succeed', %s)
                    """,
                    (group_key,),
                )
                import_id = int(cur.lastrowid)
            _load_parsed(
                conn,
                import_id,
                file_name,
                data,
                parsed,
                stop_after=INTERRUPT_AFTER_BATCHES,
            )
            raise _LoadInterrupted(done, total)
        except _LoadInterrupted as stopped:
            done = stopped.done
            conn.rollback()
        except Exception:
            conn.rollback()
            raise
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM raw_row")
            after = int(cur.fetchone()[0])
            if after != before:
                raise RuntimeError("Raw rows were stored before the load finished.")

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO import_batch (status, group_key)
                VALUES ('fail', %s)
                """,
                (group_key,),
            )
            import_id = int(cur.lastrowid)
            cur.execute(
                """
                INSERT INTO import_file (
                  import_id, file_name, fingerprint, row_count, column_count,
                  blank_rows, headers_json
                ) VALUES (%s, %s, %s, 0, %s, %s, %s)
                """,
                (
                    import_id,
                    file_name,
                    fingerprint,
                    len(headers),
                    parsed.blank_rows,
                    json.dumps(headers, ensure_ascii=False),
                ),
            )
        conn.commit()
    return {
        "import_id": import_id,
        "file_name": file_name,
        "failed": True,
        "done": done,
        "total": total,
        "batch_size": RAW_BATCH_SIZE,
    }


def delete_import(import_id: int) -> str:
    """Remove a load that main and archive do not cite."""
    ensure_import_model()
    with get_conn() as conn:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT import_id FROM import_batch WHERE import_id = %s FOR UPDATE",
                    (import_id,),
                )
                if cur.fetchone() is None:
                    raise ValueError(f"Import {import_id} was not found.")
                cur.execute(
                    "SELECT COUNT(*) FROM main_assay WHERE import_id = %s",
                    (import_id,),
                )
                in_main = int(cur.fetchone()[0])
                cur.execute(
                    """
                    SELECT COUNT(*) FROM archive_assay
                    WHERE from_import_id = %s OR replaced_by_import_id = %s
                    """,
                    (import_id, import_id),
                )
                in_archive = int(cur.fetchone()[0])
                if in_main or in_archive:
                    raise ValueError(
                        "This import is already in main or archive, so it stays."
                    )
                cur.execute(
                    """
                    SELECT file_name FROM import_file
                    WHERE import_id = %s
                    ORDER BY file_id
                    LIMIT 1
                    """,
                    (import_id,),
                )
                named = cur.fetchone()
                file_name = named[0] if named else ""
                cur.execute(
                    """
                    DELETE cc FROM config_check cc
                    INNER JOIN processing_run pr ON pr.run_id = cc.run_id
                    WHERE pr.import_id = %s
                    """,
                    (import_id,),
                )
                cur.execute("DELETE FROM dismissed_change WHERE import_id = %s", (import_id,))
                cur.execute("DELETE FROM processing_row WHERE import_id = %s", (import_id,))
                cur.execute("DELETE FROM processing_run WHERE import_id = %s", (import_id,))
                cur.execute(
                    "UPDATE import_batch SET replaces_import_id = NULL WHERE replaces_import_id = %s",
                    (import_id,),
                )
                cur.execute("DELETE FROM raw_row WHERE import_id = %s", (import_id,))
                cur.execute("DELETE FROM import_file WHERE import_id = %s", (import_id,))
                cur.execute("DELETE FROM import_batch WHERE import_id = %s", (import_id,))
            conn.commit()
            return file_name
        except Exception:
            conn.rollback()
            raise


def replace_import(old_id: int, file_name: str, data: bytes) -> LoadResult:
    """Store a new version of an import. The old one is kept, read only, as an earlier version."""
    ensure_import_model()
    name = Path(file_name or "").name.strip()
    if not name.lower().endswith(".csv"):
        raise ValueError("Choose a .csv file.")
    parsed = parse_csv(data)
    fingerprint = hashlib.sha256(data).hexdigest()
    with get_conn() as conn:
        try:
            with conn.cursor(DictCursor) as cur:
                cur.execute(
                    """
                    SELECT b.import_id, b.group_key, b.archived_at, f.fingerprint
                    FROM import_batch b
                    LEFT JOIN import_file f ON f.import_id = b.import_id
                    WHERE b.import_id = %s AND b.status = 'succeed'
                    FOR UPDATE
                    """,
                    (old_id,),
                )
                old = cur.fetchone()
            if old is None:
                raise ValueError(f"Import {old_id} was not found.")
            if old["archived_at"] is not None:
                raise ValueError(f"Import {old_id} was already replaced or voided.")
            if old["fingerprint"] == fingerprint:
                raise SameFile(f"{name} is the same file as the current version, so nothing was replaced.")
            existing = _existing_success(conn, fingerprint)
            if existing is not None:
                raise SameFile(
                    f"These exact bytes are already stored as {_label(int(existing['import_id']))}, so nothing was replaced."
                )
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO import_batch (status, group_key, replaces_import_id)
                    VALUES ('succeed', %s, %s)
                    """,
                    (old["group_key"], old_id),
                )
                import_id = int(cur.lastrowid)
            result = _load_parsed(conn, import_id, name, data, parsed)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE import_batch
                    SET archived_at = CURRENT_TIMESTAMP, archived_reason = 'replaced'
                    WHERE import_id = %s
                    """,
                    (old_id,),
                )
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise


def _label(import_id: int) -> str:
    from app.services.names import import_names

    found = import_names().get(import_id)
    return found["label"] if found else f"import {import_id}"


def _versions(cur, import_id: int) -> list[int]:
    """This import and every earlier version it replaced."""
    found = [import_id]
    while True:
        cur.execute("SELECT replaces_import_id FROM import_batch WHERE import_id = %s", (found[-1],))
        row = cur.fetchone()
        earlier = row["replaces_import_id"] if row else None
        if earlier is None or int(earlier) in found:
            return found
        found.append(int(earlier))


def void_import(import_id: int) -> dict:
    """Take this import's rows out of main, keep them in the archive, and retire the import."""
    from app.services.apply import record_manifest, payload_sha

    ensure_import_model()
    with get_conn() as conn:
        try:
            with conn.cursor(DictCursor) as cur:
                cur.execute(
                    """
                    SELECT b.import_id, b.archived_at, f.file_name
                    FROM import_batch b
                    LEFT JOIN import_file f ON f.import_id = b.import_id
                    WHERE b.import_id = %s
                    FOR UPDATE
                    """,
                    (import_id,),
                )
                batch = cur.fetchone()
                if batch is None:
                    raise ValueError(f"Import {import_id} was not found.")
                if batch["archived_at"] is not None:
                    raise ValueError(f"Import {import_id} was already replaced or voided.")
                cur.execute(
                    "SELECT MAX(run_id) AS run_id FROM processing_run WHERE import_id = %s",
                    (import_id,),
                )
                run = cur.fetchone()
                run_id = run["run_id"] if run else None
                versions = _versions(cur, import_id)
                marks = ", ".join(["%s"] * len(versions))
                cur.execute(
                    f"SELECT row_key, import_id, payload_json FROM main_assay WHERE import_id IN ({marks}) FOR UPDATE",
                    versions,
                )
                rows = cur.fetchall()
                entries = []
                for row in rows:
                    payload = _json_obj(row["payload_json"])
                    cur.execute(
                        """
                        INSERT INTO archive_assay (
                          row_key, from_import_id, payload_json, reason, run_id, payload_sha
                        ) VALUES (%s, %s, %s, 'import voided', %s, %s)
                        """,
                        (row["row_key"], row["import_id"], json.dumps(payload, ensure_ascii=False), run_id, payload_sha(payload)),
                    )
                    entries.append({"row_key": row["row_key"], "payload_sha": payload_sha(payload)})
                cur.execute(f"DELETE FROM main_assay WHERE import_id IN ({marks})", versions)
                if run_id is not None and entries:
                    record_manifest(cur, int(run_id), "voided", entries)
                cur.execute(
                    """
                    UPDATE import_batch
                    SET archived_at = CURRENT_TIMESTAMP, archived_reason = 'voided'
                    WHERE import_id = %s
                    """,
                    (import_id,),
                )
            conn.commit()
            return {"file_name": batch["file_name"] or "", "removed": len(entries)}
        except Exception:
            conn.rollback()
            raise


def roll_back_import(import_id: int) -> dict:
    """Undo a replacement: retire this version and make the one it replaced current again.

    Rows this version put in main are archived and removed. Main values it
    replaced or removed come back from the archive, unless another file has
    saved that key since.
    """
    from app.services.apply import payload_sha, record_manifest

    ensure_import_model()
    with get_conn() as conn:
        try:
            with conn.cursor(DictCursor) as cur:
                cur.execute(
                    """
                    SELECT b.import_id, b.archived_at, b.replaces_import_id, f.file_name
                    FROM import_batch b
                    LEFT JOIN import_file f ON f.import_id = b.import_id
                    WHERE b.import_id = %s
                    FOR UPDATE
                    """,
                    (import_id,),
                )
                batch = cur.fetchone()
                if batch is None:
                    raise ValueError(f"Import {import_id} was not found.")
                if batch["archived_at"] is not None:
                    raise ValueError("This version is no longer current, so it can't be rolled back.")
                earlier_id = batch["replaces_import_id"]
                if earlier_id is None:
                    raise ValueError("This file has no earlier version to roll back to.")
                cur.execute(
                    "SELECT archived_reason FROM import_batch WHERE import_id = %s FOR UPDATE",
                    (earlier_id,),
                )
                earlier = cur.fetchone()
                if earlier is None or earlier["archived_reason"] != "replaced":
                    raise ValueError("The earlier version was voided, so there is nothing to roll back to.")
                cur.execute("SELECT MAX(run_id) AS run_id FROM processing_run WHERE import_id = %s", (import_id,))
                run = cur.fetchone()
                run_id = run["run_id"] if run else None

                cur.execute("SELECT row_key, payload_json FROM main_assay WHERE import_id = %s FOR UPDATE", (import_id,))
                removed = []
                for row in cur.fetchall():
                    payload = _json_obj(row["payload_json"])
                    cur.execute(
                        """
                        INSERT INTO archive_assay (row_key, from_import_id, payload_json, reason, run_id, payload_sha)
                        VALUES (%s, %s, %s, 'rolled back', %s, %s)
                        """,
                        (row["row_key"], import_id, json.dumps(payload, ensure_ascii=False), run_id, payload_sha(payload)),
                    )
                    removed.append({"row_key": row["row_key"], "payload_sha": payload_sha(payload)})
                cur.execute("DELETE FROM main_assay WHERE import_id = %s", (import_id,))

                cur.execute(
                    """
                    SELECT a.row_key, a.from_import_id, a.payload_json
                    FROM archive_assay a
                    INNER JOIN (
                      SELECT row_key, MIN(archive_id) AS first_id
                      FROM archive_assay
                      WHERE replaced_by_import_id = %s
                      GROUP BY row_key
                    ) firsts ON firsts.first_id = a.archive_id
                    """,
                    (import_id,),
                )
                restored = []
                for row in cur.fetchall():
                    cur.execute("SELECT 1 FROM main_assay WHERE row_key = %s FOR UPDATE", (row["row_key"],))
                    if cur.fetchone() is not None:
                        continue
                    payload = _json_obj(row["payload_json"])
                    cur.execute(
                        "INSERT INTO main_assay (row_key, import_id, payload_json) VALUES (%s, %s, %s)",
                        (row["row_key"], int(row["from_import_id"]), json.dumps(payload, ensure_ascii=False)),
                    )
                    restored.append({"row_key": row["row_key"], "payload_sha": payload_sha(payload)})
                if run_id is not None:
                    if removed:
                        record_manifest(cur, int(run_id), "rolled_back", removed)
                    if restored:
                        record_manifest(cur, int(run_id), "restored", restored)
                cur.execute(
                    "UPDATE import_batch SET archived_at = CURRENT_TIMESTAMP, archived_reason = 'rolled_back' WHERE import_id = %s",
                    (import_id,),
                )
                cur.execute(
                    "UPDATE import_batch SET archived_at = NULL, archived_reason = NULL WHERE import_id = %s",
                    (earlier_id,),
                )
            conn.commit()
            return {
                "file_name": batch["file_name"] or "",
                "earlier_id": int(earlier_id),
                "removed": len(removed),
                "restored": len(restored),
            }
        except Exception:
            conn.rollback()
            raise


def fixture_bytes(fixture_name: str) -> tuple[str, bytes]:
    return _read_fixture(fixture_name)


def load_fixture(import_id: int, fixture_name: str) -> LoadResult:
    file_name, data = _read_fixture(fixture_name)
    parsed = parse_csv(data)
    with get_conn() as conn:
        try:
            result = _load_parsed(conn, import_id, file_name, data, parsed)
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise


def list_imports() -> list[dict]:
    sql = """
        SELECT
          b.import_id,
          b.status,
          b.note,
          b.created_at,
          f.file_id,
          f.file_name,
          f.fingerprint,
          f.row_count,
          f.column_count,
          (
            SELECT COUNT(*)
            FROM raw_row r
            WHERE r.file_id = f.file_id
          ) AS raw_count
        FROM import_batch b
        LEFT JOIN import_file f ON f.import_id = b.import_id
        ORDER BY b.import_id DESC, f.file_id
    """
    with get_conn() as conn:
        with conn.cursor(DictCursor) as cur:
            cur.execute(sql)
            fetched = cur.fetchall()

    imports: list[dict] = []
    index: dict[int, dict] = {}
    for row in fetched:
        import_id = int(row["import_id"])
        item = index.get(import_id)
        if item is None:
            item = {
                "import_id": import_id,
                "status": row["status"],
                "note": row["note"],
                "created_at": row["created_at"],
                "files": [],
                "raw_count": 0,
            }
            index[import_id] = item
            imports.append(item)
        if row["file_id"] is None:
            continue
        raw_count = int(row["raw_count"] or 0)
        item["files"].append(
            {
                "file_id": int(row["file_id"]),
                "file_name": row["file_name"],
                "fingerprint": row["fingerprint"],
                "row_count": int(row["row_count"] or 0),
                "column_count": int(row["column_count"] or 0),
                "raw_count": raw_count,
            }
        )
        item["raw_count"] += raw_count
    return imports


def raw_preview(import_id: int, limit: int = 20) -> list[dict]:
    with get_conn() as conn:
        with conn.cursor(DictCursor) as cur:
            cur.execute(
                """
                SELECT r.source_row_no, r.row_key, r.payload_json, f.headers_json
                FROM raw_row r
                INNER JOIN import_file f ON f.file_id = r.file_id
                WHERE r.import_id = %s
                ORDER BY r.file_id, r.source_row_no
                LIMIT %s
                """,
                (import_id, limit),
            )
            rows = cur.fetchall()
    preview = []
    for row in rows:
        payload = _json_obj(row["payload_json"])
        headers = _json_obj(row["headers_json"])
        ordered = {name: payload.get(name, "") for name in headers}
        for name, value in payload.items():
            if name not in ordered:
                ordered[name] = value
        preview.append(
            {
                "source_row_no": int(row["source_row_no"]),
                "row_key": row["row_key"] or "",
                "payload_json": json.dumps(ordered, ensure_ascii=False),
            }
        )
    return preview


def _json_obj(value: object) -> dict | list:
    if isinstance(value, str):
        loaded = json.loads(value)
        return loaded
    if isinstance(value, (dict, list)):
        return value
    return {}


def parse_csv(data: bytes) -> ParsedCsv:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("CSV must be UTF-8.") from exc

    reader = csv.reader(io.StringIO(text))
    try:
        header_row = next(reader)
    except StopIteration as exc:
        raise ValueError("CSV has no header row.") from exc

    headers: list[str] = []
    seen: set[str] = set()
    for field in header_row:
        name = (field or "").strip()
        if not name:
            raise ValueError("CSV has a blank header.")
        key = name.lower()
        if key in seen:
            raise ValueError(f"CSV has a duplicate header: {name}.")
        seen.add(key)
        headers.append(name)

    rows: list[tuple[int, dict[str, str]]] = []
    blank_rows = 0
    width = len(headers)
    source_row = 1
    for record in reader:
        source_row += 1
        cells = [(cell or "").strip() for cell in record]
        if not cells or not any(cells):
            blank_rows += 1
            continue
        if len(cells) < width:
            raise ValueError(
                f"CSV row {source_row} has {len(cells)} values; expected {width}."
            )
        if len(cells) > width and any(cells[width:]):
            raise ValueError(
                f"CSV row {source_row} has more values than headers."
            )
        rows.append((source_row, dict(zip(headers, cells[:width], strict=True))))
    return ParsedCsv(headers, rows, blank_rows)


def _existing_success(conn: pymysql.Connection, fingerprint: str) -> dict | None:
    with conn.cursor(DictCursor) as cur:
        cur.execute(
            """
            SELECT
              b.import_id,
              f.file_id,
              f.file_name,
              f.fingerprint,
              f.row_count,
              f.column_count,
              f.blank_rows
            FROM import_file f
            INNER JOIN import_batch b ON b.import_id = f.import_id
            WHERE f.fingerprint = %s
              AND b.status = 'succeed'
              AND EXISTS (
                SELECT 1 FROM raw_row r WHERE r.import_id = b.import_id
              )
            ORDER BY b.import_id
            LIMIT 1
            """,
            (fingerprint,),
        )
        return cur.fetchone()


def _already(existing: dict) -> LoadResult:
    row_count = int(existing["row_count"] or 0)
    return LoadResult(
        import_id=int(existing["import_id"]),
        file_id=int(existing["file_id"]),
        file_name=existing["file_name"],
        fingerprint=existing["fingerprint"],
        row_count=row_count,
        column_count=int(existing["column_count"] or 0),
        already_loaded=True,
        rows_inserted=0,
        raw_count=row_count,
        batches=batch_count(row_count),
        blank_rows=int(existing["blank_rows"] or 0),
    )


def _load_parsed(
    conn: pymysql.Connection,
    import_id: int,
    file_name: str,
    data: bytes,
    parsed: ParsedCsv,
    stop_after: int | None = None,
) -> LoadResult:
    fingerprint = hashlib.sha256(data).hexdigest()
    headers = parsed.headers
    rows = parsed.rows
    with conn.cursor() as cur:
        cur.execute(
            "SELECT import_id FROM import_batch WHERE import_id = %s FOR UPDATE",
            (import_id,),
        )
        if cur.fetchone() is None:
            raise ValueError(f"Import {import_id} was not found.")

        cur.execute(
            """
            SELECT file_id, row_count, column_count, blank_rows
            FROM import_file
            WHERE import_id = %s AND fingerprint = %s
            """,
            (import_id, fingerprint),
        )
        existing = cur.fetchone()
        if existing is not None:
            return LoadResult(
                import_id=import_id,
                file_id=int(existing[0]),
                file_name=file_name,
                fingerprint=fingerprint,
                row_count=int(existing[1] or 0),
                column_count=int(existing[2] or 0),
                already_loaded=True,
                rows_inserted=0,
                raw_count=_raw_count(cur, import_id),
                batches=batch_count(int(existing[1] or 0)),
                blank_rows=int(existing[3] or 0),
            )

        cur.execute(
            """
            INSERT INTO import_file (
              import_id, file_name, fingerprint, row_count, column_count,
              blank_rows, headers_json
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                import_id,
                file_name,
                fingerprint,
                len(rows),
                len(headers),
                parsed.blank_rows,
                json.dumps(headers, ensure_ascii=False),
            ),
        )
        file_id = int(cur.lastrowid)
        prepared = _insert_batches(cur, import_id, file_id, rows, stop_after)
        cur.execute(
            "UPDATE import_batch SET status = 'succeed' WHERE import_id = %s",
            (import_id,),
        )
        return LoadResult(
            import_id=import_id,
            file_id=file_id,
            file_name=file_name,
            fingerprint=fingerprint,
            row_count=len(rows),
            column_count=len(headers),
            already_loaded=False,
            rows_inserted=prepared,
            raw_count=_raw_count(cur, import_id),
            batches=batch_count(len(rows)),
            blank_rows=parsed.blank_rows,
        )


def _insert_batches(
    cur,
    import_id: int,
    file_id: int,
    rows: list[tuple[int, dict[str, str]]],
    stop_after: int | None,
) -> int:
    """Prepare raw rows in batches. The caller commits only if every batch finishes."""
    total = batch_count(len(rows))
    prepared = 0
    done = 0
    sql = """
        INSERT INTO raw_row (
          import_id, file_id, source_row_no, row_key, payload_json
        ) VALUES (%s, %s, %s, %s, %s)
    """
    for start in range(0, len(rows), RAW_BATCH_SIZE):
        chunk = rows[start : start + RAW_BATCH_SIZE]
        cur.executemany(
            sql,
            [
                (
                    import_id,
                    file_id,
                    source,
                    _row_key(payload),
                    json.dumps(payload, ensure_ascii=False),
                )
                for source, payload in chunk
            ],
        )
        prepared += len(chunk)
        done += 1
        if stop_after is not None and done >= stop_after:
            raise _LoadInterrupted(done, total)
    return prepared


def _raw_count(cur: pymysql.cursors.Cursor, import_id: int) -> int:
    cur.execute("SELECT COUNT(*) FROM raw_row WHERE import_id = %s", (import_id,))
    row = cur.fetchone()
    return int(row[0] if row else 0)


def _row_key(payload: dict[str, str]) -> str | None:
    for name, value in payload.items():
        if name.lower() == cfg.KEY_HEADER.lower() and value:
            return value
    return None


def _clean_note(note: str | None) -> str | None:
    if note is None:
        return None
    cleaned = note.strip()
    if not cleaned:
        return None
    if len(cleaned) > NOTE_MAX:
        raise ValueError("Note must be 255 characters or fewer.")
    return cleaned


def _read_fixture(fixture_name: str) -> tuple[str, bytes]:
    name = (fixture_name or "").strip()
    if not name or Path(name).name != name or not name.lower().endswith(".csv"):
        raise ValueError("Choose a fixture CSV.")
    path = (FIXTURES / name).resolve()
    root = FIXTURES.resolve()
    if root not in path.parents or not path.is_file():
        raise ValueError("Fixture not found.")
    return path.name, path.read_bytes()
