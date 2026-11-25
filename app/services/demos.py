"""Starting-point loads. Related files share a group key."""

from __future__ import annotations

from app.db import get_conn
from app.services.raw_import import (
    INTERRUPT_AFTER_BATCHES,
    RAW_BATCH_SIZE,
    batch_count,
    fixture_bytes,
    load_interrupted,
    register_and_load,
    replace_import,
)

FILE_GROUP = {
    "gold_proj_despatch.csv": "gold-proj",
    "gold_proj_despatch_edited.csv": "gold-proj",
    "gold_proj_bad_rows.csv": "bad-rows",
    "gold_proj_reordered.csv": "gold-proj",
    "gold_proj_despatch_8000.csv": "large-despatch",
    "gold_proj_overlap.csv": "overlap",
}

LARGE_ROW_COUNT = 8000

CASES = (
    {
        "id": "load-batch",
        "title": "Load the gold despatch",
        "detail": "100 samples from DDH-26-001, DDH-26-002, and DDH-26-004, with standards, blanks, field duplicates, and a few below-detection gold values.",
        "fixture": "gold_proj_despatch.csv",
        "group": "gold-proj",
        "crash": False,
    },
    {
        "id": "reload-batch",
        "title": "Replace it with the same file",
        "detail": "Identical bytes. Nothing is replaced and nothing new is stored.",
        "fixture": "gold_proj_despatch.csv",
        "group": "gold-proj",
        "crash": False,
        "replace": True,
    },
    {
        "id": "reload-changed",
        "title": "Replace it with a corrected file",
        "detail": "Three corrected values: ACQ00104 gold is 6.40 ppm instead of 5.12, ACQ00137 gold, and ACQ00168 copper. The earlier version is kept, read only.",
        "fixture": "gold_proj_despatch_edited.csv",
        "group": "gold-proj",
        "crash": False,
        "replace": True,
    },
    {
        "id": "bad-rows",
        "title": "Load a despatch with bad rows",
        "detail": "Blank sample id, reversed interval, gold that is not a number, and a duplicate sample id.",
        "fixture": "gold_proj_bad_rows.csv",
        "group": "bad-rows",
        "crash": False,
    },
    {
        "id": "reordered",
        "title": "Replace it with the columns rearranged",
        "detail": "Same assays as the first file. Headers match by name, so order, casing, and an extra Note column make no difference.",
        "fixture": "gold_proj_reordered.csv",
        "group": "gold-proj",
        "crash": False,
        "replace": True,
    },
    {
        "id": "overlap",
        "title": "Load a resample that overlaps saved intervals",
        "detail": (
            "ACQ00301 is DDH-26-001 5.0–6.5 m and passes every row rule, so it saves. "
            "With the gold despatch in main, the overlap check on Main then flags it "
            "against ACQ00104 and ACQ00106."
        ),
        "fixture": "gold_proj_overlap.csv",
        "group": "overlap",
        "crash": False,
    },
    {
        "id": "server-failure",
        "title": "Interrupt the 8,000-row despatch",
        "detail": "",
        "fixture": "gold_proj_despatch_8000.csv",
        "group": "large-despatch",
        "crash": True,
    },
    {
        "id": "large-batch",
        "title": "Load an 8,000-row despatch",
        "detail": "",
        "fixture": "gold_proj_despatch_8000.csv",
        "group": "large-despatch",
        "crash": False,
    },
)


def cases() -> list[dict]:
    total = batch_count(LARGE_ROW_COUNT)
    prepared = [
        f"Prepared in batches of {RAW_BATCH_SIZE:,}.",
        f"The rows are saved only after batch {total} finishes.",
    ]
    interrupted = [
        f"Stops during batch {INTERRUPT_AFTER_BATCHES} of {total}.",
        "Unfinished batches are not saved.",
    ]
    items = []
    for case in CASES:
        item = dict(case)
        if case["id"] == "large-batch":
            item["detail"] = " ".join(prepared)
        elif case["id"] == "server-failure":
            item["detail"] = " ".join(interrupted)
        items.append(item)
    return items


def run_demo(demo_id: str):
    case = next((item for item in CASES if item["id"] == demo_id), None)
    if case is None:
        raise ValueError("Unknown demo.")
    if case["crash"]:
        return load_interrupted(case["fixture"], case["group"])
    if case.get("replace"):
        current = current_in_group(case["group"])
        if current is not None:
            name, data = fixture_bytes(case["fixture"])
            return {"replaced": current, "result": replace_import(current, name, data)}
    return register_and_load(None, case["fixture"], case["group"])


def current_in_group(group_key: str) -> int | None:
    """The newest version in a group that is not replaced or voided."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT MAX(b.import_id)
                FROM import_batch b
                WHERE b.group_key = %s
                  AND b.status = 'succeed'
                  AND b.archived_at IS NULL
                  AND EXISTS (SELECT 1 FROM raw_row r WHERE r.import_id = b.import_id)
                """,
                (group_key,),
            )
            row = cur.fetchone()
    return int(row[0]) if row and row[0] is not None else None
