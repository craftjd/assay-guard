"""Readable names for imports.

An import is named by its file. Versions of one file (a replacement chain) are
numbered from 1 in load order, so "despatch.csv v2" reads as the second upload.
"""

from __future__ import annotations

from pymysql.cursors import DictCursor

from app.db import get_conn


def import_names(cur=None) -> dict[int, dict]:
    if cur is None:
        with get_conn() as conn:
            with conn.cursor(DictCursor) as own:
                return import_names(own)
    cur.execute(
        """
        SELECT b.import_id, b.replaces_import_id, f.file_name
        FROM import_batch b
        LEFT JOIN import_file f ON f.import_id = b.import_id
        ORDER BY b.import_id
        """
    )
    rows = cur.fetchall()
    parent = {int(row["import_id"]): row["replaces_import_id"] for row in rows}
    files = {int(row["import_id"]): row["file_name"] or "" for row in rows}

    def root(import_id: int) -> int:
        seen = {import_id}
        while parent.get(import_id) is not None and int(parent[import_id]) not in seen:
            import_id = int(parent[import_id])
            seen.add(import_id)
        return import_id

    chains: dict[int, list[int]] = {}
    for import_id in files:
        chains.setdefault(root(import_id), []).append(import_id)
    names: dict[int, dict] = {}
    for members in chains.values():
        members.sort()
        for index, import_id in enumerate(members, start=1):
            file_name = files[import_id] or f"Import {import_id}"
            versioned = len(members) > 1
            names[import_id] = {
                "import_id": import_id,
                "file": file_name,
                "version": index,
                "versions": len(members),
                "label": f"{file_name} v{index}" if versioned else file_name,
                "short": f"v{index}" if versioned else file_name,
                "title": f"Import {import_id}" + (f", version {index} of {len(members)}" if versioned else ""),
                "chain": members,
            }
    return names


class Names(dict):
    """Lookup that never fails in a template: unknown ids get a plain name."""

    def __missing__(self, import_id):
        return {
            "import_id": import_id,
            "file": "",
            "version": 1,
            "versions": 1,
            "label": f"Import {import_id}",
            "short": f"Import {import_id}",
            "title": f"Import {import_id}",
            "chain": [import_id],
        }


def name_lookup() -> Names:
    try:
        return Names(import_names())
    except Exception:  # noqa: BLE001
        return Names()
