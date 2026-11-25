"""One row per file, current version first. Its other versions stay folded under it."""

from __future__ import annotations

from pymysql.cursors import DictCursor

from app.db import get_conn
from app.services.names import import_names
from app.services.raw_import import ensure_import_model
from app.services.when import friendly


def file_groups() -> list[dict]:
    ensure_import_model()
    sql = """
        SELECT
          b.import_id,
          b.status,
          b.group_key,
          b.created_at,
          b.replaces_import_id,
          b.archived_at,
          b.archived_reason,
          f.file_name,
          f.fingerprint,
          f.blank_rows,
          (SELECT COUNT(*) FROM raw_row r WHERE r.import_id = b.import_id) AS raw_count,
          (SELECT COUNT(*) FROM main_assay m WHERE m.import_id = b.import_id) AS main_count,
          (SELECT COUNT(*) FROM archive_assay a WHERE a.from_import_id = b.import_id) AS archive_count,
          (SELECT COUNT(*) FROM processing_run pr WHERE pr.import_id = b.import_id) AS run_count
        FROM import_batch b
        LEFT JOIN import_file f ON f.import_id = b.import_id
        ORDER BY b.import_id DESC, f.file_id
    """
    with get_conn() as conn:
        with conn.cursor(DictCursor) as cur:
            cur.execute(sql)
            rows = cur.fetchall()
            names = import_names(cur)

    items: dict[int, dict] = {}
    for row in rows:
        import_id = int(row["import_id"])
        if import_id in items:
            continue
        raw_count = int(row["raw_count"] or 0)
        failed = row["status"] == "fail" or raw_count == 0
        fingerprint = row["fingerprint"] or ""
        main_count = int(row["main_count"] or 0)
        archive_count = int(row["archive_count"] or 0)
        archived = row["archived_reason"] if row["archived_at"] is not None else None
        name = names[import_id]
        items[import_id] = {
            "import_id": import_id,
            "file_name": row["file_name"] or "",
            "label": name["label"],
            "version": name["version"],
            "versions": name["versions"],
            "chain": name["chain"],
            "failed": failed,
            "archived": archived,
            "when": friendly(row["created_at"]),
            "raw_count": raw_count,
            "blank_rows": int(row["blank_rows"] or 0),
            "main_count": main_count,
            "archive_count": archive_count,
            "run_count": int(row["run_count"] or 0),
            "fingerprint": fingerprint,
            "short": fingerprint[:8],
            "replaces": int(row["replaces_import_id"]) if row["replaces_import_id"] is not None else None,
            "can_delete": main_count == 0 and archive_count == 0 and archived is None,
            "can_change": not failed and archived is None,
        }
    for item in items.values():
        replaced_by = [other for other in items.values() if other["replaces"] == item["import_id"]]
        if replaced_by:
            item["replaced_by"] = max(other["import_id"] for other in replaced_by)
            item["replaced_by_version"] = names[item["replaced_by"]]["version"]

    groups: dict[int, list[dict]] = {}
    for item in items.values():
        groups.setdefault(item["chain"][0], []).append(item)
    ordered = []
    for root in sorted(groups, key=lambda key: max(item["import_id"] for item in groups[key]), reverse=True):
        versions = sorted(groups[root], key=lambda item: item["import_id"], reverse=True)
        current = next((item for item in versions if item["archived"] is None and not item["failed"]), versions[0])
        others = [item for item in versions if item is not current]
        for item in versions:
            item["status"] = _status(item)
            item["main_note"] = ""
            item["can_roll_back"] = False
        earlier_main = sum(item["main_count"] for item in others)
        if earlier_main and current["archived"] is None:
            current["main_count"] += earlier_main
            current["main_note"] = f"{earlier_main:,} saved by other versions"
            current["can_delete"] = False
        previous = next(
            (item for item in versions if item["import_id"] == current["replaces"] and item["archived"] == "replaced"),
            None,
        )
        if current["archived"] is None and previous is not None:
            current["can_roll_back"] = True
            current["roll_back_to"] = previous["version"]
        ordered.append({"latest": current, "earlier": others})
    return ordered


def _status(item: dict) -> tuple[str, str]:
    if item["failed"]:
        return "err", "Load failed"
    if item["archived"] == "replaced":
        by = item.get("replaced_by_version")
        return "dim", f"Replaced by v{by}" if by else "Replaced"
    if item["archived"] == "rolled_back":
        return "dim", "Rolled back"
    if item["archived"] == "voided":
        return "dim", "Voided"
    if item["run_count"] == 0:
        return "idle", "Not processed"
    return "ok", "Current"
