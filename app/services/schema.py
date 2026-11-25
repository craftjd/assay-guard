"""Bring an existing database up to sql/schema.sql without rewriting rows."""

from __future__ import annotations

from pathlib import Path

import pymysql

from app.db import get_conn
from app.settings import ROOT

_READY = False
_SQL = ROOT / "sql"


def ensure_model() -> None:
    global _READY
    if _READY:
        return
    with get_conn() as conn:
        with conn.cursor() as cur:
            for statement in _statements((_SQL / "schema.sql").read_text(encoding="utf-8")):
                cur.execute(statement)
            _add_column(cur, "import_batch", "group_key", "VARCHAR(64) NULL")
            _add_column(cur, "import_batch", "replaces_import_id", "BIGINT UNSIGNED NULL")
            _add_column(cur, "import_batch", "archived_at", "TIMESTAMP NULL")
            _add_column(cur, "import_batch", "archived_reason", "VARCHAR(16) NULL")
            _add_column(cur, "import_file", "blank_rows", "INT UNSIGNED NOT NULL DEFAULT 0")
            cur.execute(
                "ALTER TABLE processing_row MODIFY reason VARCHAR(1024) NULL"
            )
            _add_column(cur, "archive_assay", "run_id", "BIGINT UNSIGNED NULL")
            _add_column(
                cur,
                "archive_assay",
                "replaced_by_import_id",
                "BIGINT UNSIGNED NULL",
            )
            _add_column(cur, "archive_assay", "payload_sha", "CHAR(64) NULL")
            _add_unique(cur, "processing_row", "uq_processing_raw", "(run_id, raw_id)")
            _add_column(cur, "processing_run", "run_no", "INT UNSIGNED NULL AFTER import_id")
            _backfill_run_numbers(cur)
            _add_unique(cur, "processing_run", "uq_processing_run_no", "(import_id, run_no)")
            _add_foreign_key(
                cur,
                "fk_archive_from_import",
                """
                ALTER TABLE archive_assay
                ADD CONSTRAINT fk_archive_from_import
                FOREIGN KEY (from_import_id) REFERENCES import_batch (import_id)
                """,
            )
            _add_foreign_key(
                cur,
                "fk_archive_run",
                """
                ALTER TABLE archive_assay
                ADD CONSTRAINT fk_archive_run
                FOREIGN KEY (run_id) REFERENCES processing_run (run_id)
                """,
            )
            _add_foreign_key(
                cur,
                "fk_archive_replaced_by",
                """
                ALTER TABLE archive_assay
                ADD CONSTRAINT fk_archive_replaced_by
                FOREIGN KEY (replaced_by_import_id) REFERENCES import_batch (import_id)
                """,
            )
            _backfill_groups(cur)
            _add_sort_columns(cur)
            for statement in _statements((_SQL / "views.sql").read_text(encoding="utf-8")):
                cur.execute(statement)
        conn.commit()
    _READY = True


def _statements(text: str) -> list[str]:
    lines = []
    for line in text.splitlines():
        comment = line.find("--")
        if comment >= 0:
            line = line[:comment]
        lines.append(line)
    return [chunk.strip() for chunk in "\n".join(lines).split(";") if chunk.strip()]


def _add_column(cur, table: str, column: str, definition: str) -> None:
    if _column_exists(cur, table, column):
        return
    cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _column_exists(cur, table: str, column: str) -> bool:
    cur.execute(
        """
        SELECT COUNT(*)
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = %s
          AND COLUMN_NAME = %s
        """,
        (table, column),
    )
    return int(cur.fetchone()[0]) > 0


# Main is read a page at a time in any column order. Each sortable header gets a
# stored, indexed copy so a page is an index range, not a sort of every row.
SORT_COLUMNS = {
    "sort_hole": ("HoleID", "text"),
    "sort_from": ("SampFrom", "num"),
    "sort_to": ("SampTo", "num"),
    "sort_type": ("SampleType", "text"),
    "sort_au": ("Au_ppm", "num"),
    "sort_cu": ("Cu_pct", "num"),
    "sort_ag": ("Ag_ppm", "num"),
    "sort_as": ("As_ppm", "num"),
}
_NUMBER = "'^[-+]?([0-9]+([.][0-9]*)?|[.][0-9]+)$'"


def _add_sort_columns(cur) -> None:
    for column, (header, kind) in SORT_COLUMNS.items():
        value = f"JSON_UNQUOTE(JSON_EXTRACT(payload_json, '$.\"{header}\"'))"
        if kind == "num":
            definition = (
                f"DECIMAL(18, 6) AS (CASE WHEN {value} REGEXP {_NUMBER} "
                f"THEN CAST({value} AS DECIMAL(18, 6)) END) STORED"
            )
        else:
            definition = f"VARCHAR(191) AS (LEFT({value}, 191)) STORED"
        _add_column(cur, "main_assay", column, definition)
        _add_index(cur, "main_assay", f"ix_main_{column}", f"({column})")
    _add_index(cur, "main_assay", "ix_main_loaded", "(loaded_at)")


def _add_index(cur, table: str, name: str, columns: str) -> None:
    cur.execute(
        """
        SELECT COUNT(*)
        FROM information_schema.STATISTICS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = %s
          AND INDEX_NAME = %s
        """,
        (table, name),
    )
    if int(cur.fetchone()[0]):
        return
    cur.execute(f"ALTER TABLE {table} ADD INDEX {name} {columns}")


def _add_unique(cur, table: str, name: str, columns: str) -> None:
    cur.execute(
        """
        SELECT COUNT(*)
        FROM information_schema.STATISTICS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = %s
          AND INDEX_NAME = %s
        """,
        (table, name),
    )
    if int(cur.fetchone()[0]):
        return
    cur.execute(f"ALTER TABLE {table} ADD UNIQUE KEY {name} {columns}")


def _add_foreign_key(cur, name: str, ddl: str) -> None:
    cur.execute(
        """
        SELECT COUNT(*)
        FROM information_schema.TABLE_CONSTRAINTS
        WHERE CONSTRAINT_SCHEMA = DATABASE()
          AND CONSTRAINT_NAME = %s
        """,
        (name,),
    )
    if int(cur.fetchone()[0]):
        return
    try:
        cur.execute(ddl)
    except pymysql.err.IntegrityError:
        return
    except pymysql.err.OperationalError as exc:
        if exc.args and exc.args[0] in {1452, 1215, 1826}:
            return
        raise


def _backfill_run_numbers(cur) -> None:
    cur.execute(
        """
        UPDATE processing_run pr
        INNER JOIN (
          SELECT a.run_id, COUNT(*) AS run_no
          FROM processing_run a
          INNER JOIN processing_run b ON b.import_id = a.import_id AND b.run_id <= a.run_id
          GROUP BY a.run_id
        ) numbered ON numbered.run_id = pr.run_id
        SET pr.run_no = numbered.run_no
        WHERE pr.run_no IS NULL
        """
    )


def _backfill_groups(cur) -> None:
    from app.services.demos import FILE_GROUP

    for file_name, group_key in FILE_GROUP.items():
        cur.execute(
            """
            UPDATE import_batch b
            INNER JOIN import_file f ON f.import_id = b.import_id
            SET b.group_key = %s
            WHERE b.group_key IS NULL AND f.file_name = %s
            """,
            (group_key, file_name),
        )
    cur.execute(
        """
        UPDATE import_batch b
        INNER JOIN import_file f ON f.import_id = b.import_id
        SET b.group_key = f.file_name
        WHERE b.group_key IS NULL
        """
    )


_CLEAR_TABLES = (
    "config_check",
    "dismissed_change",
    "processing_row",
    "processing_run",
    "raw_row",
    "main_assay",
    "archive_assay",
    "import_file",
    "import_batch",
)


def clear_import_data() -> None:
    """Remove every import, run, and main row. The loaded config file is unchanged."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SET FOREIGN_KEY_CHECKS=0")
            for table in _CLEAR_TABLES:
                cur.execute(f"TRUNCATE TABLE {table}")
            cur.execute("SET FOREIGN_KEY_CHECKS=1")
        conn.commit()


def reset_for_tests(path: Path | None = None) -> None:
    """Test hook. Production code leaves the ready flag alone."""
    global _READY
    _READY = False
    if path is not None:
        global _SQL
        _SQL = path
