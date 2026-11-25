"""What differs between two results of the same file, or two versions of a file.

A run is only worth keeping if a row's result differs from the run before it.
A row that went from New to Same because this import saved it does not count.
"""

from __future__ import annotations

from app.services.rules import field_changes, same_business

KIND_ORDER = ("now_fails", "now_passes", "values", "main", "added", "removed")


def run_changes(
    previous: dict[int, dict],
    latest: dict[int, dict],
    rules,
    main_rows: dict[str, dict],
    own_imports: set[int],
) -> tuple[list[dict], int]:
    """Rows whose result differs, keyed by raw row. Returns (changes, rows settled by own saves)."""
    changes: list[dict] = []
    settled = 0
    for raw_id in sorted(set(previous) | set(latest)):
        old = previous.get(raw_id)
        new = latest.get(raw_id)
        if old is None or new is None:
            row = new or old
            changes.append(
                {
                    "row_key": row["row_key"],
                    "previous": old["outcome"] if old else "",
                    "latest": new["outcome"] if new else "",
                    "kind": "added" if new else "removed",
                    "detail": "",
                }
            )
            continue
        values_changed = not same_business(old["payload"], new["payload"], rules)
        if old["outcome"] == new["outcome"] and old["reason"] == new["reason"] and not values_changed:
            continue
        current = main_rows.get(new["row_key"]) if new["row_key"] else None
        if (
            new["outcome"] == "already_same"
            and old["outcome"] in {"good", "changed"}
            and not values_changed
            and current is not None
            and current["import_id"] in own_imports
        ):
            settled += 1
            continue
        changes.append(
            {
                "row_key": new["row_key"] or old["row_key"],
                "previous": old["outcome"],
                "latest": new["outcome"],
                "kind": _kind(old, new, values_changed),
                "detail": _why(old, new, values_changed, rules),
            }
        )
    return changes, settled


def version_changes(old_rows: list[dict], new_rows: list[dict], rules) -> list[dict]:
    """Key by key, what the replacement file changes compared with the file it replaces."""
    before = {row["row_key"]: row for row in old_rows if row["row_key"]}
    after = {row["row_key"]: row for row in new_rows if row["row_key"]}
    found = []
    for key in sorted(set(before) | set(after)):
        old = before.get(key)
        new = after.get(key)
        if old is None:
            found.append({"row_key": key, "kind": "added", "changes": []})
        elif new is None:
            found.append({"row_key": key, "kind": "removed", "changes": []})
        else:
            diff = field_changes(old["payload"], new["payload"], rules)
            if diff:
                found.append(
                    {
                        "row_key": key,
                        "kind": "values",
                        "changes": [{"field": name, "before": was, "after": now} for name, was, now in diff],
                    }
                )
    return found


def count_kinds(changes: list[dict]) -> dict[str, int]:
    counts = {kind: 0 for kind in KIND_ORDER}
    for change in changes:
        counts[change["kind"]] = counts.get(change["kind"], 0) + 1
    return counts


def kinds_phrase(counts: dict[str, int]) -> str:
    """Short plain sentence fragment, e.g. "2 now fail the config · 1 has new values"."""
    words = {
        "now_fails": ("now fails the config", "now fail the config"),
        "now_passes": ("now passes the config", "now pass the config"),
        "values": ("has new values", "have new values"),
        "main": ("compares differently with main", "compare differently with main"),
        "added": ("added", "added"),
        "removed": ("removed", "removed"),
    }
    parts = []
    for kind in KIND_ORDER:
        count = int(counts.get(kind) or 0)
        if count:
            one, many = words[kind]
            parts.append(f"{count:,} {one if count == 1 else many}")
    return " · ".join(parts)


def _kind(old: dict, new: dict, values_changed: bool) -> str:
    if new["outcome"] == "invalid" and old["outcome"] != "invalid":
        return "now_fails"
    if old["outcome"] == "invalid" and new["outcome"] != "invalid":
        return "now_passes"
    if values_changed:
        return "values"
    return "main"


def _why(old: dict, new: dict, values_changed: bool, rules) -> str:
    if values_changed:
        return ", ".join(
            f"{name} {was or 'blank'} → {now or 'blank'}"
            for name, was, now in field_changes(old["payload"], new["payload"], rules)
        )
    if new["outcome"] == "invalid":
        if old["outcome"] == "invalid":
            return new["reason"]
        return f"Now fails: {new['reason']}"
    if old["outcome"] == "invalid":
        return "Now passes the config"
    if new["outcome"] == "changed":
        return "Main now holds a different value"
    if new["outcome"] == "already_same":
        return "Main now holds the same value"
    if new["outcome"] == "good":
        return "No longer in main"
    return ""
